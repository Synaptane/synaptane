"""A broken AI connection is a failure, seen by the admins on every page.

The incident: a Claude Code login expired, and every nightly run ended in a second with "done"
and the error tucked away in the log. No banner, no badge, no alert for six nights; and each of
them counted one more interrupted run against the notes waiting in the inbox."""

import json
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.ai import cli_agents
from synaptane.ai.base import AIConfig, AIError, ToolSpec, Turn, Usage
from synaptane.core.access import OrgRole
from synaptane.core.errors import Label
from synaptane.services import ai_settings, deposits, organizations, tidy, tidy_journal
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, ChangeMark, Job, TidyRun
from tests.fake_cli import Fake, fake  # noqa: F401 — shared fixture
from tests.integration.test_maintenance import reviewed, space, write
from tests.integration.test_tidy import Scripted, configure_ai
from tests.integration.test_tidy_paths import item_named, subscription
from tests.integration.test_tidy_robustness import note
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki

EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"


def expired_login(transient: bool = False) -> AIError:
    """What the CLI raises when its login expired: one turn counted, no work done."""
    return AIError(
        "Claude Code: {detail}", transient=transient, usage=Usage(turns=1), detail=EXPIRED
    )


def alerts(db: Session) -> list[str]:
    db.expire_all()
    return list(db.scalars(select(AuditEvent.action).where(AuditEvent.action.like("alert.%"))))


def raw_note(ctx: Workspace, wiki: Wiki, name: str) -> bytes:
    item = item_named(ctx, wiki, name)
    assert item is not None and item.status == "pending"
    return ctx.local_repository(wiki.infra).blob(item.path) or b""


def failing_cli(
    monkeypatch: pytest.MonkeyPatch, error: AIError, before: list[tuple[str, dict[str, Any]]]
) -> None:
    """The subscription's CLI runs the tool calls `before`, then fails with `error`."""

    def fake_run(
        config: AIConfig, system: str, prompt: str, specs: list[ToolSpec], handler: Any
    ) -> cli_agents.CLIRun:
        for name, arguments in before:
            handler(name, arguments)
        raise error

    monkeypatch.setattr(cli_agents, "run_agent", fake_run)


# --- The run ------------------------------------------------------------------------------------


def test_an_expired_login_fails_the_run_and_spares_the_notes_and_the_maintenance(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscription(db, ctx, wiki)
    watermark = space(db, wiki).maintain_watermark
    write(db, database, ctx, wiki, "a.md", "# A\n\n[B](b.md)\n")  # a problem for the maintenance
    reviewed(db, ctx, wiki)
    write(db, database, ctx, wiki, "notes.md", "# Notes\n")  # a page change to review
    name = note(db, database, ctx, wiki, "Une note")
    head = ctx.local_repository(wiki.infra).head()
    # Reading is not work: the agent read the note, then the provider failed.
    failing_cli(monkeypatch, expired_login(), [("read_inbox_item", {"item": name})])
    for _night in range(tidy.MAX_ITEM_ATTEMPTS + 1):
        record = tidy.run(database, ctx, wiki.infra.id, trigger="schedule")
        assert record is not None and record.status == "failed" and record.commit is None
        assert EXPIRED in (record.error or "")
    assert ctx.local_repository(wiki.infra).head() == head  # nothing committed
    # The note is still new, without a counter: it is not given up after a few nights.
    assert b"tidy_attempts" not in raw_note(ctx, wiki, name)
    # Nor does the page change: no interrupted run counted, still new.
    db.expire_all()
    assert db.get(ChangeMark, (wiki.infra.id, "notes.md")) is None
    [change] = [i for i in deposits.inbox(db, wiki.alice, ctx, wiki.infra) if i.type == "change"]
    assert change.status == "pending"
    # The maintenance neither moves its watermark nor uses up its attempts.
    found = space(db, wiki)
    assert (found.maintain_watermark, found.maintain_attempts) == (watermark, 0)
    # Admins notified (once: the alert stays quiet afterwards); the tab's badge is on.
    assert alerts(db) == ["alert.tidy_failed"]
    assert tidy_journal.failing(db, wiki.infra.id)
    streak = tidy_journal.streak(db, wiki.infra.id)
    assert streak is not None and streak.explanation.code == "cli_login"


def cli_result(result: str) -> str:
    """What Claude Code really writes when the API stops it at once: an `is_error` result,
    one turn counted, a few tokens."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": result,
            "num_turns": 1,
            "usage": {"input_tokens": 3, "output_tokens": 0},
        }
    )


def test_an_overloaded_api_behind_the_cli_is_retried_without_alert(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    fake: Fake,  # noqa: F811
) -> None:
    """Same `is_error` result, but a passing cause: the queue retries it, nobody is alerted
    for a provider overloaded for a few minutes."""
    subscription(db, ctx, wiki)
    name = note(db, database, ctx, wiki, "Une note")
    overloaded = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'
    fake.configure(behavior="script", stdout=cli_result(overloaded), exit=1)
    with pytest.raises(tidy.RetryLater):
        tidy.run(database, ctx, wiki.infra.id, trigger="schedule")
    db.expire_all()
    record = db.scalars(select(TidyRun)).one()
    assert record.status == "failed" and "overloaded" in (record.error or "")
    assert alerts(db) == []
    assert b"tidy_attempts" not in raw_note(ctx, wiki, name)


@pytest.mark.parametrize("provider", ["api", "cli"])
def test_a_passing_provider_error_fails_the_run_and_retries_it_without_alert(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    class Overloaded(Scripted):
        def step(self) -> Turn:
            raise AIError("Anthropic error 529", transient=True)

    if provider == "cli":
        subscription(db, ctx, wiki)
        failing_cli(monkeypatch, expired_login(transient=True), [])
    else:
        configure_ai(db, ctx, wiki)
    name = note(db, database, ctx, wiki, "Une note")
    with pytest.raises(tidy.RetryLater):
        tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=Overloaded([]).factory)
    db.expire_all()
    record = db.scalars(select(TidyRun)).one()
    assert record.status == "failed" and record.commit is None
    assert alerts(db) == []
    assert b"tidy_attempts" not in raw_note(ctx, wiki, name)


def test_work_done_before_the_login_expires_is_kept_and_reported(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscription(db, ctx, wiki)
    first = note(db, database, ctx, wiki, "Première")
    second = note(db, database, ctx, wiki, "Seconde")
    failing_cli(
        monkeypatch,
        expired_login(),
        [
            ("read_inbox_item", {"item": first}),
            ("write_page", {"path": "premiere.md", "content": "# Première\n"}),
            ("mark_done", {"item": first, "summary": "page créée"}),
        ],
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="schedule")
    assert record is not None and record.status == "done" and record.commit
    assert EXPIRED in (record.error or "") and record.items_done == 1
    assert ctx.local_repository(wiki.infra).blob("content/premiere.md") is not None
    assert item_named(ctx, wiki, first) is None
    assert b"tidy_attempts: 1" in raw_note(ctx, wiki, second)  # taken up by the next run
    # Not a failure, but not silent either: the admins learn the run was interrupted.
    assert alerts(db) == ["alert.tidy_interrupted"]
    db.expire_all()
    assert db.scalars(select(Job).where(Job.kind == tidy.TIDY)).all() == []  # no chain


# --- The banner ---------------------------------------------------------------------------------


def failed_run(
    db: Session, wiki: Wiki, at: datetime, error: str = f"Claude Code: {EXPIRED}"
) -> None:
    db.add(
        TidyRun(
            space_id=wiki.infra.id,
            trigger="schedule",
            status="failed",
            started_at=at,
            finished_at=at,
            error=error,
        )
    )
    db.commit()


def org_admin(db: Session, wiki: Wiki) -> None:
    """Alice administers Acme (Bob stays a reader of the space)."""
    org = wiki.infra.organization
    organizations.set_member(db, wiki.root, org, wiki.alice.user, OrgRole.ADMIN)
    db.commit()


def banner(client: TestClient, path: str = "/") -> str:
    page = unescape(client.get(path).text)
    start = page.find('class="notice notice-error ai-alert"')
    return page[start : page.find("</div>", start)] if start >= 0 else ""


def test_admins_see_the_broken_connection_on_every_page_until_a_run_works(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    subscription(db, ctx, wiki)
    org_admin(db, wiki)
    since = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    for night in range(6):
        failed_run(db, wiki, since + timedelta(days=night))
    login(client, "alice")
    for path in ("/", "/o/acme/infra", "/account"):
        text = banner(client, path)
        assert 'role="alert"' in text, path
        assert "Rangement de nuit en panne dans Acme depuis le" in text
        assert 'datetime="2026-11-05' in text
        assert "la connexion à Claude Code a expiré" in text
        assert "L'abonnement n'est plus connecté sur le serveur" in text
        assert 'href="/o/acme/-/ai"' in text
    assert 'href="/o/acme/-/ai"' not in banner(client, "/o/acme/-/ai")  # already there
    # A member can do nothing about it: no banner (the Tidy-up tab still says it).
    client.cookies.clear()
    login(client, "bob")
    assert banner(client) == ""
    # A run that reaches the AI again: the banner goes away.
    db.add(
        TidyRun(
            space_id=wiki.infra.id,
            trigger="manual",
            status="done",
            started_at=since + timedelta(days=6, hours=8),
        )
    )
    db.commit()
    client.cookies.clear()
    login(client, "alice")
    assert banner(client) == ""


def test_a_failed_connection_test_shows_until_one_succeeds(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    subscription(db, ctx, wiki)
    org_admin(db, wiki)
    org = wiki.infra.organization
    message = Label(
        "The Claude Code login has expired: run “{command}” again (steps on this page).",
        command="claude auth login",
    )
    ai_settings.record_test(db, org, False, message)
    db.commit()
    login(client, "alice")
    text = banner(client)
    assert "Le dernier test de connexion à l'IA a échoué dans Acme le" in text
    assert "la connexion à Claude Code a expiré" in text
    # A test that works clears it, and so do the failures before it.
    failed_run(db, wiki, datetime.now(UTC) - timedelta(days=1))
    ai_settings.record_test(db, org, True, Label("Claude Code answers."))
    db.commit()
    assert banner(client) == ""


def test_a_passing_cause_waits_before_alarming(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    configure_ai(db, ctx, wiki)
    org_id = wiki.infra.org_id
    start = datetime.now(UTC) - timedelta(days=1)
    failed_run(db, wiki, start, "Anthropic rate limit reached.")
    assert tidy_journal.broken_connections(db, [org_id]) == {}
    for n in range(1, tidy_journal.FAILURES_BEFORE_WARNING):
        failed_run(db, wiki, start + timedelta(minutes=n), "Anthropic rate limit reached.")
    broken = tidy_journal.broken_connections(db, [org_id])[org_id]
    assert (broken.explanation.code, broken.runs, broken.since) == ("rate_limit", 3, start)
    # Causes on Synaptane's side are not about the AI connection.
    db.query(TidyRun).delete()
    failed_run(db, wiki, start, "The space stayed busy (sync?): the run's work was not saved.")
    assert tidy_journal.broken_connections(db, [org_id]) == {}
    # A manual run with an empty inbox proves nothing about the connection.
    failed_run(db, wiki, start, "Anthropic key refused: invalid x-api-key")
    db.add(TidyRun(space_id=wiki.infra.id, trigger="manual", status="nothing"))
    db.commit()
    assert tidy_journal.broken_connections(db, [org_id])[org_id].explanation.code == "auth"


def test_the_banner_needs_one_query_set_per_request_and_none_for_members(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscription(db, ctx, wiki)
    org_admin(db, wiki)
    calls: list[list[Any]] = []
    real = tidy_journal.broken_connections

    def counted(session: Session, org_ids: list[Any]) -> Any:
        calls.append(org_ids)
        return real(session, org_ids)

    monkeypatch.setattr(tidy_journal, "broken_connections", counted)
    login(client, "bob")
    client.get("/o/acme/infra")
    assert calls == []
    client.cookies.clear()
    login(client, "alice")
    client.get("/o/acme/infra")
    assert calls == [[wiki.infra.org_id]]
