"""Agents container seen from the instance: connection test through it, state reported by the
worker, health page, `synaptane doctor`, sign-in commands of the AI page."""

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.ai import agents_client, agents_runner, cli_agents
from synaptane.cli import main
from synaptane.config import Settings
from synaptane.core.errors import Label
from synaptane.services import ai_settings, instance
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.worker.runner import Worker
from tests import fake_cli
from tests.fake_cli import Fake, fake  # noqa: F401 — shared fixture
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki, wiki  # noqa: F401 — shared fixture

TITLE = "Subscription agents"
CLAUDE = agents_client.CLIState("claude", "Claude Code (claude)", True, "2.1.314")
CODEX = agents_client.CLIState("codex", "Codex (codex)", True, "0.166.0")


def subscribe(db: Session, ctx: Workspace, data: Wiki, provider: str = "claude_code") -> None:
    ai_settings.configure(
        db, data.root, ctx.box, data.infra.organization, provider=provider, model=""
    )
    db.commit()


def report(settings: Settings, **fields: object) -> None:
    state = agents_client.State(
        **{"mode": "container", "checked_at": datetime.now(UTC), "url": "http://agents:8700"}
        | fields  # type: ignore[arg-type]
    )
    agents_client.write_state(settings.data_dir, state)


def agents_check(
    settings: Settings, db: Session, database: Database, ctx: Workspace
) -> instance.Check | None:
    found = {str(c.title): c for c in instance.report(db, database, ctx, settings).checks}
    return found.get(TITLE)


@pytest.fixture
def agents(
    fake: Fake,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[agents_runner.Runner]:
    key_file = tmp_path / "agents-key"
    monkeypatch.setenv("SYNAPTANE_AGENTS_KEY_FILE", str(key_file))
    runner = agents_runner.Runner(key_file, 2, check_contract=False)
    runner.contract = []
    with fake_cli.serving(agents_runner.create_app(runner)) as url:
        monkeypatch.setenv("SYNAPTANE_AGENTS_URL", url)
        yield runner


def test_the_connection_test_goes_through_the_agents_container(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,  # noqa: F811
    agents: agents_runner.Runner,
) -> None:
    """**Test the connection** (worker job): login state and real tool call, both in the
    agents container."""
    subscribe(db, ctx, wiki)
    ai_settings.run_test(database, ctx.box, wiki.infra.organization.id)
    db.expire_all()
    view = ai_settings.view(db, wiki.root, ctx.box, wiki.infra.organization)
    assert view is not None and view.test_ok, view
    assert view.test_message.template == "{status} Tool call successful ({tokens} tokens)."
    assert "Claude Code answers" in view.test_message


def test_the_worker_reports_where_the_clis_run(
    settings: Settings,
    database: Database,
    ctx: Workspace,
    agents: agents_runner.Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = Worker(settings, database, ctx)
    worker.report_agents()
    state = agents_client.read_state(settings.data_dir)
    assert state is not None and state.container and state.reachable
    assert state.clis == () and not state.contract_pending
    # Settled: the next report in 10 minutes; a call before does nothing.
    (settings.data_dir / agents_client.STATE_FILE).unlink()
    worker.report_agents()
    assert agents_client.read_state(settings.data_dir) is None

    monkeypatch.setenv("SYNAPTANE_AGENTS_URL", "http://127.0.0.1:9")
    worker._agents_due = float("-inf")
    worker.report_agents()
    state = agents_client.read_state(settings.data_dir)
    assert state is not None and not state.reachable
    assert worker._agents_due - time.monotonic() <= agents_client.RETRY.total_seconds()

    monkeypatch.delenv("SYNAPTANE_AGENTS_URL")
    worker._agents_due = float("-inf")
    worker.maintenance()
    state = agents_client.read_state(settings.data_dir)
    assert state is not None and state.mode == agents_client.LOCAL


def test_health_page_tells_where_the_clis_run(
    settings: Settings,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,  # noqa: F811
) -> None:
    # No subscription, no report: nothing to say.
    assert agents_check(settings, db, database, ctx) is None
    agents_client.write_state(settings.data_dir, agents_client.State("local", datetime.now(UTC)))
    assert agents_check(settings, db, database, ctx) is None
    report(settings, reachable=True)  # published image: no CLI
    assert agents_check(settings, db, database, ctx) is None

    subscribe(db, ctx, wiki)
    check = agents_check(settings, db, database, ctx)
    assert check is not None and check.state == instance.WARN and check.guide == "ai"
    assert "has no CLI" in check.message

    (settings.data_dir / agents_client.STATE_FILE).unlink()
    check = agents_check(settings, db, database, ctx)
    assert check is not None and check.state == instance.WARN
    agents_client.write_state(settings.data_dir, agents_client.State("local", datetime.now(UTC)))
    check = agents_check(settings, db, database, ctx)
    assert check is not None and check.state == instance.WARN
    assert "less isolated" in check.message

    report(settings, reachable=True, clis=(CLAUDE, CODEX))
    check = agents_check(settings, db, database, ctx)
    assert check is not None and check.state == instance.OK
    assert check.message.params["clis"] == "Claude Code (claude) 2.1.314, Codex (codex) 0.166.0"

    report(settings, reachable=True, contract_pending=True)
    check = agents_check(settings, db, database, ctx)
    assert check is not None and check.state == instance.OK and "being checked" in check.message

    broken = agents_client.CLIState("codex", "Codex (codex)", True, "0.167.0", ("shell",))
    report(settings, reachable=True, clis=(CLAUDE, broken))
    check = agents_check(settings, db, database, ctx)
    assert check is not None and check.state == instance.WARN
    assert check.command == instance.AGENTS_CHECK and "Codex" in check.message

    report(settings, reachable=True, clis=(CLAUDE,), exposures=("variable POSTGRES_PASSWORD",))
    check = agents_check(settings, db, database, ctx)
    assert check is not None and check.state == instance.ERROR
    assert "POSTGRES_PASSWORD" in check.message

    error = Label("The agents container did not answer in time.")
    report(settings, reachable=False, error=error)
    check = agents_check(settings, db, database, ctx)
    assert check is not None and check.state == instance.ERROR
    assert check.command == instance.AGENTS_LOGS
    assert check.message.params["error"].template == error.template

    report(settings, reachable=True, checked_at=datetime.now(UTC) - timedelta(hours=2))
    check = agents_check(settings, db, database, ctx)
    assert check is not None and check.state == instance.WARN and check.at is not None


def test_health_page_shows_the_agents_check(
    client: TestClient,
    settings: Settings,
    db: Session,
    ctx: Workspace,
    wiki: Wiki,  # noqa: F811
) -> None:
    subscribe(db, ctx, wiki)
    report(settings, reachable=True, clis=(CLAUDE,))
    login(client, "root")
    page = client.get("/admin/health").text
    assert "Agents des abonnements" in page
    assert "Dans leur propre conteneur, joignable" in page
    agents_client.write_state(settings.data_dir, agents_client.State("local", datetime.now(UTC)))
    page = client.get("/admin/health").text
    assert "moins isolés que dans le conteneur des agents" in page
    assert "Comment les agents des abonnements sont isolés" in page


def test_ai_page_gives_the_sign_in_command_of_the_agents_container(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    wiki: Wiki,  # noqa: F811
) -> None:
    monkeypatch.setattr(cli_agents, "in_docker", lambda: True)
    login(client, "root")
    page = client.get("/o/acme/-/ai").text  # the worker has said nothing yet: as before
    assert "docker compose exec worker claude auth login" in page
    report(settings, reachable=True)
    page = client.get("/o/acme/-/ai").text
    assert "docker compose exec agents claude auth login" in page
    assert "docker compose exec agents codex login --device-auth" in page
    assert "exec worker" not in page
    assert "volume propre au conteneur des agents" in page


def test_doctor_reports_the_agents_container(
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "synaptane.toml"
    config.write_text(
        f'database_url = "{settings.database_url}"\ndata_dir = "{settings.data_dir}"\n'
    )
    os.chmod(settings.key_file, 0o600)
    (settings.data_dir / "repos").mkdir(exist_ok=True)
    monkeypatch.delenv("SYNAPTANE_AGENTS_URL", raising=False)
    # In the web container: what the worker reported.
    broken = agents_client.CLIState("codex", "Codex (codex)", True, "0.167.0", ("shell",))
    report(settings, reachable=True, clis=(CLAUDE, broken))
    assert main(["--config", str(config), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "[ok] Agents container (subscription CLIs, http://agents:8700)" in out
    assert "[ok] Agents container isolated" in out
    assert "[ok] Claude subscription (claude): agent isolation (2.1.314, agents container)" in out
    assert "[warning] ChatGPT subscription (codex): agent isolation" in out
    assert "docker compose exec agents synaptane check-clis" in out

    report(
        settings,
        reachable=False,
        error=Label("The agents container did not answer in time."),
        checked_at=datetime.now(UTC) - timedelta(hours=1),
    )
    assert main(["--config", str(config), "doctor"]) == 0  # warnings only
    out = capsys.readouterr().out
    assert "[warning] Agents container (subscription CLIs" in out
    assert "did not answer in time" in out
    assert "[warning] Agents container: recent report from the worker" in out

    report(settings, reachable=True, exposures=("master key (/data/master.key)",))
    main(["--config", str(config), "doctor"])
    out = capsys.readouterr().out
    assert "[warning] Agents container isolated — it can reach: master key" in out


def test_doctor_in_the_worker_asks_the_agents_container(
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    agents: agents_runner.Runner,
) -> None:
    config = tmp_path / "synaptane.toml"
    config.write_text(
        f'database_url = "{settings.database_url}"\ndata_dir = "{settings.data_dir}"\n'
    )
    agents.contract = None  # still checking
    main(["--config", str(config), "doctor"])
    out = capsys.readouterr().out
    assert "[ok] Agents container (subscription CLIs, http://127.0.0.1:" in out
    assert "[warning] Agents container: isolation contract — not checked yet" in out


def test_doctor_without_agents_container_says_the_clis_run_in_the_worker(
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from synaptane.ai.cli_contract import Report

    monkeypatch.delenv("SYNAPTANE_AGENTS_URL", raising=False)
    monkeypatch.setattr(
        "synaptane.ai.cli_contract.check_all",
        lambda: [Report("claude", "Claude Code (claude)", version="2.1.314")],
    )
    config = tmp_path / "synaptane.toml"
    config.write_text(
        f'database_url = "{settings.database_url}"\ndata_dir = "{settings.data_dir}"\n'
    )
    main(["--config", str(config), "doctor"])
    out = capsys.readouterr().out
    assert "[ok] Claude subscription (claude): agent isolation (2.1.314)" in out
    assert "[warning] Subscription CLIs apart from the worker — they run in the worker" in out
    monkeypatch.setattr("synaptane.ai.cli_contract.check_all", lambda: [])
    main(["--config", str(config), "doctor"])
    assert "apart from the worker" not in capsys.readouterr().out  # no CLI: nothing to say
