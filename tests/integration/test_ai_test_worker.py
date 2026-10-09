"""Subscription connection test run by the worker: the CLI and its login live only in the
`worker` container, never in the web process."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.ai.base import AIConfig, AIError
from synaptane.config import Settings
from synaptane.services import ai_settings
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, OrgAISettings
from synaptane.web import tidy_pages
from synaptane.worker.runner import Worker
from tests.integration.conftest import token_for
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki


def never_in_the_web(config: AIConfig) -> str:
    raise AssertionError("le test d'un abonnement ne doit pas tourner dans le processus web")


def test_subscription_test_goes_through_the_worker(
    client: TestClient,
    db: Session,
    settings: Settings,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(client, "root")
    token = csrf(client, "/o/acme/-/ai")
    client.post("/o/acme/-/ai", data={"csrf": token, "provider": "claude_code", "effort": "medium"})
    monkeypatch.setattr(tidy_pages, "check_connection", never_in_the_web)

    page = client.post("/o/acme/-/ai/test", data={"csrf": token}).text
    assert "Test lancé dans le worker" in page and "Test en cours" in page
    assert 'data-test-pending="/o/acme/-/ai/test-status"' in page
    assert client.get("/o/acme/-/ai/test-status").json() == {"pending": True}
    # A second click does not run the test twice.
    assert "déjà en cours" in client.post("/o/acme/-/ai/test", data={"csrf": token}).text
    assert len(db.scalars(select(Job).where(Job.kind == ai_settings.AI_TEST)).all()) == 1

    calls: list[str] = []

    def connected(config: AIConfig) -> str:
        calls.append(config.provider)
        return "Claude Code répond. Appel d'outil réussi (12 jetons)."

    monkeypatch.setattr(ai_settings, "check_connection", connected)
    worker = Worker(settings, database, ctx)
    assert worker.run_once(exclude={"index", "tidy"})  # fast queue
    assert calls == ["claude_code"]
    assert client.get("/o/acme/-/ai/test-status").json() == {"pending": False}
    page = client.get("/o/acme/-/ai").text
    assert "Joignable" in page and "Test en cours" not in page

    # Failure: the CLI message is shown, the task is not retried.
    def expired(config: AIConfig) -> str:
        raise AIError("La connexion de Claude Code a expiré.", transient=True)

    monkeypatch.setattr(ai_settings, "check_connection", expired)
    client.post("/o/acme/-/ai/test", data={"csrf": token})
    assert worker.run_once()
    page = client.get("/o/acme/-/ai").text
    assert "Injoignable" in page and "La connexion de Claude Code a expiré." in page
    statuses = db.scalars(select(Job.status).where(Job.kind == ai_settings.AI_TEST)).all()
    assert sorted(statuses) == ["done", "done"]

    # Read-only for others: neither test status nor launching.
    client.cookies.clear()
    login(client, "bob")
    assert client.get("/o/acme/-/ai/test-status").status_code in (403, 404)


def test_subscription_test_over_rest(
    client: TestClient, database: Database, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    client.put("/api/v1/organizations/acme/ai", json={"provider": "codex"}, headers=root)
    monkeypatch.setattr("synaptane.api.tidy.check_connection", never_in_the_web)
    started = client.post("/api/v1/organizations/acme/ai/test", headers=root)
    assert started.status_code == 202 and "worker" in started.json()["message"]
    assert client.get("/api/v1/organizations/acme/ai", headers=root).json()["test_pending"] is True


def test_old_login_in_the_data_volume_is_reported(
    client: TestClient,
    database: Database,
    settings: Settings,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Migration: the old login (`/data/home/.claude`) is reported, never read nor moved;
    the administrator logs in again in the worker, then deletes it themselves."""
    monkeypatch.setenv("SYNAPTANE_AGENT_HOME", str(tmp_path / "agents"))
    login(client, "root")
    assert "ancienne connexion" not in client.get("/o/acme/-/ai").text
    (settings.data_dir / "home" / ".claude").mkdir(parents=True)
    page = client.get("/o/acme/-/ai").text
    assert "Une ancienne connexion" in page
    assert f"rm -rf {settings.data_dir}/home/.claude " in page

    # Worker login already tested successfully: only the deletion remains.
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    client.put("/api/v1/organizations/acme/ai", json={"provider": "claude_code"}, headers=root)
    with database.session() as session:
        found = session.scalar(
            select(OrgAISettings).where(OrgAISettings.org_id == wiki.infra.org_id)
        )
        assert found is not None
        found.test_ok, found.tested_at = True, datetime.now(UTC)
    page = client.get("/o/acme/-/ai").text
    assert "Dernière étape" in page and "La connexion du worker fonctionne" in page
    assert "Une ancienne connexion" not in page
    assert f"rm -rf {settings.data_dir}/home/.claude " in page
