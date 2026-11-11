"""The night of a space: routines before and after the tidy-up, in order, then one report.

The model is simulated for the whole night (`NightModel`): each conversation it opens is
recorded, a routine by the marker of its instructions, or the tidy-up."""

import json
import re
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.ai.base import AIConfig, AIError, ToolSpec
from synaptane.config import Settings
from synaptane.core.access import OrgRole
from synaptane.core.errors import SynaptaneError
from synaptane.services import (
    deposits,
    night,
    organizations,
    routine_files,
    routines,
    secrets,
    tidy,
    tidy_commit,
    webhooks,
)
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import (
    AuditEvent,
    Job,
    NightRun,
    OrgAISettings,
    Routine,
    Secret,
    Space,
    TidyRun,
)
from synaptane.worker.runner import Worker
from tests.integration.conftest import make_user, token_for
from tests.integration.test_alert_mails import received
from tests.integration.test_invitations import Inbox
from tests.integration.test_routines import Failing, RoutineScript, smtp, verified
from tests.integration.test_tidy import CloseAll, configure_ai
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki

__all__ = ["smtp"]  # fixture

# Monday 5 October 2026, 03:30 in Paris: the night of 02:00 is due.
MONDAY_NIGHT = datetime(2026, 10, 5, 1, 30, tzinfo=UTC)
TUESDAY_NIGHT = MONDAY_NIGHT + timedelta(days=1)
MONDAY = date(2026, 10, 5)
HOOK = "https://hooks.slack.example/services/T000/B000/secret-token-du-test"
_MARK = re.compile(r"ROUTINE-([a-z0-9-]+)")


class NightModel:
    """One simulated model for a whole night. `fail`: routines that fail with this error;
    `empty`: routines whose model stops without producing anything."""

    def __init__(
        self,
        fail: dict[str, AIError] | None = None,
        empty: tuple[str, ...] = (),
        summary: str = "",
    ) -> None:
        self.order: list[str] = []
        self.fail = fail or {}
        self.empty = empty
        self.summary = summary

    def factory(self, config: AIConfig, system: str, first: str, tools: list[ToolSpec]) -> Any:
        if "mark_done" in {t.name for t in tools}:
            self.order.append("tidy")
            return CloseAll().factory(config, system, first, tools)
        found = _MARK.search(system)
        assert found is not None, system
        name = found.group(1)
        self.order.append(name)
        if name in self.fail:
            return Failing(self.fail[name]).factory(config, system, first, tools)
        if name in self.empty:
            return RoutineScript([]).factory(config, system, first, tools)
        text = self.summary or f"{name} : tout va bien."
        turns = [
            [("write_report", {"content": f"# {name}\n\nRAS.\n"}), ("finish", {"summary": text})]
        ]
        return RoutineScript(turns).factory(config, system, first, tools)


def spaced(text: str) -> str:
    """French typography's no-break spaces as plain spaces."""
    return text.replace("\u00a0", " ").replace("\u202f", " ")


def add_routine(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    name: str,
    place: str = "after",
    frequency: str = "daily",
    **values: Any,
) -> Routine:
    routine = routines.create(
        db,
        database,
        wiki.root,
        ctx,
        wiki.infra,
        routines.Fields(
            title=f"Routine {name}",
            instructions=f"ROUTINE-{name}: relis le wiki.",
            frequency=frequency,
            night=place,
            output_page=f"rapports/{name}.md",
            **values,
        ),
        name=name,
    )
    db.commit()
    return routine


def old_spaces(db: Session, wiki: Wiki) -> None:
    for space in (wiki.infra, wiki.other):
        space.created_at = datetime(2026, 10, 1, tzinfo=UTC)
    db.commit()


def nights_of(db: Session, wiki: Wiki) -> list[NightRun]:
    db.expire_all()
    return list(
        db.scalars(
            select(NightRun).where(NightRun.space_id == wiki.infra.id).order_by(NightRun.day)
        )
    )


def note(db: Session, database: Database, ctx: Workspace, wiki: Wiki, title: str = "NAS") -> None:
    deposits.deposit(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        title=title,
        body="Le NAS passe à 8 To.",
        source="web",
    )
    db.commit()


def run_night(database: Database, ctx: Workspace, wiki: Wiki, model: NightModel) -> NightRun:
    found = night.run(
        database, ctx, wiki.infra.id, factory=model.factory, public_url="https://brain.example.com"
    )
    assert found is not None
    return found


def report_to_admins(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    """The report by email to the organization's admins: Ada, verified, reads French."""
    ada = make_user(db, "ada")
    organizations.set_member(db, wiki.root, wiki.infra.organization, ada, OrgRole.ADMIN)
    verified(db, ada, "ada@example.com")
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    night.update_report(db, wiki.root, ctx.box, space, email="admins")
    db.commit()


# --- Order, days, once ------------------------------------------------------------------------


def test_routines_run_before_and_after_the_tidy_up_in_order_then_one_report(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, smtp: Inbox
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "20-apres")
    add_routine(db, database, ctx, wiki, "10-apres")
    add_routine(db, database, ctx, wiki, "avant", place="before")
    fixed = routines.create(
        db,
        database,
        wiki.root,
        ctx,
        wiki.infra,
        routines.Fields(
            title="Fixe",
            instructions="ROUTINE-fixe",
            frequency="daily",
            at="02:30",
            output_page="rapports/fixe.md",
        ),
    )
    db.commit()
    note(db, database, ctx, wiki)
    report_to_admins(db, ctx, wiki)

    too_early = MONDAY_NIGHT - timedelta(hours=2)  # 01:30 in Paris
    assert night.schedule(database, too_early) == 0
    assert night.schedule(database, MONDAY_NIGHT) == 1  # Infra only: Autre has no night
    assert night.schedule(database, MONDAY_NIGHT) == 0  # never twice
    # The tidy-up is in the night: the plain schedule leaves it (Autre keeps its own).
    assert tidy.schedule(database, MONDAY_NIGHT) == 1
    db.expire_all()
    queued = db.scalars(select(Job).where(Job.status == "queued", Job.kind == tidy.TIDY)).all()
    assert [j.space_id for j in queued] == [wiki.other.id]
    # The routine at a set time keeps its own schedule.
    assert routines.schedule(database, MONDAY_NIGHT) == 1
    db.expire_all()
    assert db.get(Routine, fixed.id).requested == "schedule"  # type: ignore[union-attr]

    model = NightModel()
    done = run_night(database, ctx, wiki, model)
    assert model.order == ["avant", "tidy", "10-apres", "20-apres"]
    assert done.status == "done" and done.day == MONDAY
    assert [(s["kind"], s.get("name"), s["status"]) for s in done.steps] == [
        ("routine", "avant", "done"),
        ("tidy", None, "done"),
        ("routine", "10-apres", "done"),
        ("routine", "20-apres", "done"),
    ]
    tidied = db.get(TidyRun, done.steps[1]["run_id"])
    assert tidied is not None and tidied.trigger == "schedule" and tidied.items_done == 1
    # One report, by email to the admins (root), in their language.
    assert done.report == {"quiet": False, "emails": 1, "slack": ""}
    [message] = smtp.messages
    to, subject, body = received(message)
    body = spaced(body)
    assert to == "ada@example.com" and subject == "Nuit du 5 octobre 2026 · Infra"
    assert "Rangement : 1 élément rangé, 0 laissé dans l'inbox." in body
    assert "Routine avant (avant le rangement) : fait" in body
    assert "Routine 10-apres (après le rangement) : fait" in body
    assert "https://brain.example.com/o/acme/infra/p/rapports/avant.md" in body
    assert "Journal de la nuit : https://brain.example.com/o/acme/infra/tidy" in body
    # The night does not run again, nor send again.
    assert night.run(database, ctx, wiki.infra.id, factory=model.factory) is None
    assert len(smtp.messages) == 1
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "space.tidy_settings"))
    assert event is not None


def test_night_days_and_routines_set_after_the_time(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    monday = add_routine(db, database, ctx, wiki, "lundi", frequency="weekly", weekday=0)
    first = add_routine(db, database, ctx, wiki, "premier", frequency="monthly", monthday=6)
    assert night.schedule(database, MONDAY_NIGHT) == 1
    [planned] = nights_of(db, wiki)
    assert [s.get("name") for s in planned.steps] == [None, "lundi"]
    db.expire_all()
    assert db.get(Routine, monday.id).last_date == MONDAY  # type: ignore[union-attr]
    assert db.get(Routine, first.id).last_date != MONDAY  # type: ignore[union-attr]
    assert night.schedule(database, TUESDAY_NIGHT) == 1  # the 6th: the monthly one
    tuesday = nights_of(db, wiki)[-1]
    assert [s.get("name") for s in tuesday.steps] == [None, "premier"]
    # A night routine is never due through the routines' own schedule.
    assert routines.schedule(database, TUESDAY_NIGHT) == 0
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    upcoming = routines.next_run(db.get(Routine, monday.id), space, MONDAY_NIGHT)  # type: ignore[arg-type]
    assert upcoming is not None and upcoming.hour == 2 and upcoming.date() == date(2026, 10, 12)
    # Switched to the night after the tidy-up's time: not tonight (no surprise run at 14:00).
    late = add_routine(db, database, ctx, wiki, "tard")
    routine_files.skip_today(late, space, MONDAY_NIGHT + timedelta(days=2))
    db.commit()
    wednesday = MONDAY_NIGHT + timedelta(days=2)
    assert night.schedule(database, wednesday) == 0  # nothing else that night
    assert [n.day for n in nights_of(db, wiki)] == [MONDAY, date(2026, 10, 6)]


def test_a_failing_routine_does_not_stop_the_night(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "a-vide", place="before")
    add_routine(db, database, ctx, wiki, "b-ok", place="before")
    note(db, database, ctx, wiki)
    night.schedule(database, MONDAY_NIGHT)
    model = NightModel(empty=("a-vide",))
    done = run_night(database, ctx, wiki, model)
    assert model.order == ["a-vide", "b-ok", "tidy"]
    assert [s["status"] for s in done.steps] == ["failed", "done", "done"]


def test_a_broken_ai_connection_skips_the_rest_of_the_night(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "avant", place="before")
    add_routine(db, database, ctx, wiki, "apres")
    note(db, database, ctx, wiki)
    night.schedule(database, MONDAY_NIGHT)
    model = NightModel(fail={"avant": AIError("Clé Anthropic refusée (401).")})
    done = run_night(database, ctx, wiki, model)
    assert model.order == ["avant"]  # neither the tidy-up nor the other routine are tried
    assert [(s["status"], s.get("reason")) for s in done.steps] == [
        ("failed", None),
        ("skipped", "connection"),
        ("skipped", "connection"),
    ]
    report = night.build(db, db.get(Space, wiki.infra.id), done, "https://b")  # type: ignore[arg-type]
    text = night.write(report, "en")
    assert "Routine avant (before the tidy-up): failed: " in text
    assert "Tidy-up: skipped: the AI connection failed earlier in the night." in text


def test_a_transient_failure_is_retried_outside_the_night(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    routine = add_routine(db, database, ctx, wiki, "apres")
    night.schedule(database, MONDAY_NIGHT)
    busy = AIError("Erreur Anthropic 529 : surchargé", transient=True)
    done = run_night(database, ctx, wiki, NightModel(fail={"apres": busy}))
    assert done.steps[-1]["status"] == "failed" and done.steps[-1]["reason"] == "retry"
    assert done.steps[-1]["run_id"]
    db.expire_all()
    assert db.get(Routine, routine.id).requested == "night"  # type: ignore[union-attr]
    kinds = {j.kind for j in db.scalars(select(Job).where(Job.status == "queued"))}
    assert routines.ROUTINE in kinds


def test_tidy_up_off_or_without_ai(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "apres")
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    tidy.update_settings(db, wiki.root, space, enabled=False)
    db.commit()
    # Off: the routines still run at the tidy-up's time; without AI, nothing is tried.
    assert night.schedule(database, MONDAY_NIGHT) == 1
    done = run_night(database, ctx, wiki, NightModel())
    assert [(s["kind"], s["status"], s.get("reason")) for s in done.steps] == [
        ("tidy", "skipped", "disabled"),
        ("routine", "skipped", "no_ai"),
    ]
    assert done.report == {"quiet": True}  # nothing configured, nothing to say
    configure_ai(db, ctx, wiki)
    assert night.schedule(database, TUESDAY_NIGHT) == 1
    model = NightModel()
    done = run_night(database, ctx, wiki, model)
    assert model.order == ["apres"]
    assert [s["status"] for s in done.steps] == ["skipped", "done"]


def test_the_monthly_budget_stops_the_routines(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "apres")
    settings = db.get(OrgAISettings, wiki.infra.org_id)
    assert settings is not None
    settings.monthly_budget = Decimal(1)
    db.add(TidyRun(space_id=wiki.infra.id, trigger="manual", status="done", cost=Decimal(2)))
    db.commit()
    night.schedule(database, MONDAY_NIGHT)
    model = NightModel()
    done = run_night(database, ctx, wiki, model)
    assert model.order == []
    assert done.steps[-1]["reason"] == "budget"


def test_a_busy_space_puts_the_night_back_and_a_stale_night_is_skipped(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "apres")
    night.schedule(database, MONDAY_NIGHT)
    for taken in db.scalars(select(Job).where(Job.kind == night.NIGHT)):
        taken.status = "running"  # taken by the worker, as for real
    db.commit()
    with tidy_commit.pass_lock(database.engine, wiki.infra.id) as acquired:
        assert acquired  # a tidy-up or a routine is running
        assert night.run(database, ctx, wiki.infra.id, factory=NightModel().factory) is None
    db.expire_all()
    [job] = db.scalars(select(Job).where(Job.kind == night.NIGHT, Job.status == "queued"))
    assert job.run_after > datetime.now(UTC) + timedelta(seconds=60)
    # The worker was gone: the next night comes while Monday's still waits.
    night.schedule(database, TUESDAY_NIGHT)
    model = NightModel()
    run_night(database, ctx, wiki, model)
    assert model.order == ["apres"]  # once, for Tuesday
    monday, tuesday = nights_of(db, wiki)
    assert [s.get("reason") for s in monday.steps] == ["superseded", "superseded"]
    assert tuesday.status == "done" and tuesday.steps[-1]["status"] == "done"


def test_a_step_cut_off_by_a_stopped_worker_is_not_run_again(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "avant", place="before")
    add_routine(db, database, ctx, wiki, "apres")
    night.schedule(database, MONDAY_NIGHT)
    [planned] = nights_of(db, wiki)
    steps = list(planned.steps)
    steps[0] = {**steps[0], "status": "running"}
    planned.steps, planned.status = steps, "running"
    db.commit()
    model = NightModel()
    done = run_night(database, ctx, wiki, model)
    assert model.order == ["apres"]
    assert done.steps[0]["reason"] == "interrupted"
    # Closed for good after a long while, nothing sent.
    stuck = NightRun(space_id=wiki.infra.id, day=date(2026, 9, 1), steps=[], report={})
    stuck.created_at = datetime.now(UTC) - timedelta(days=1)
    db.add(stuck)
    db.commit()
    assert night.close_orphans(database) == 1


# --- The report: quiet nights, Slack, the secret ---------------------------------------------


def test_a_quiet_night_sends_nothing_unless_asked(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, smtp: Inbox
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    report_to_admins(db, ctx, wiki)
    # No routine, an empty inbox: the night is only the tidy-up, which has nothing to do.
    assert night.schedule(database, MONDAY_NIGHT) == 1
    done = run_night(database, ctx, wiki, NightModel())
    assert done.steps == [{"kind": "tidy", "when": "", "status": "nothing"}]
    assert done.report == {"quiet": True} and smtp.messages == []
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    night.update_report(db, wiki.root, ctx.box, space, always=True)
    db.commit()
    night.schedule(database, TUESDAY_NIGHT)
    done = run_night(database, ctx, wiki, NightModel())
    assert done.report["emails"] == 1 and len(smtp.messages) == 1
    assert "Rangement : rien à faire." in spaced(received(smtp.messages[0])[2])
    # Without a report, a night of the tidy-up alone stays the plain scheduled run.
    night.update_report(db, wiki.root, ctx.box, space, email="", always=False)
    db.commit()
    assert night.schedule(database, TUESDAY_NIGHT + timedelta(days=1)) == 0


class Receiver(BaseHTTPRequestHandler):
    bodies: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 — http.server's name
        length = int(self.headers["Content-Length"])
        Receiver.bodies.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@pytest.fixture
def receiver() -> Iterator[str]:
    Receiver.bodies = []
    server = HTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()
    server.server_close()


def test_slack_report_escapes_agent_texts_and_the_url_stays_secret(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    client: TestClient,
    receiver: str,
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "apres")
    headers = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    url = "/api/v1/organizations/acme/spaces/infra/tidy-settings"
    response = client.patch(url, json={"report_webhook_url": HOOK}, headers=headers)
    assert response.status_code == 204, response.text
    got = client.get(url, headers=headers)
    assert got.status_code == 200 and got.json()["report_webhook"] is True
    assert "secret-token" not in got.text and "hooks.slack" not in got.text
    stored = db.scalar(select(Secret).where(Secret.purpose == night.REPORT_WEBHOOK))
    assert stored is not None and b"secret-token" not in stored.ciphertext
    assert secrets.get_space_secret(db, ctx.box, wiki.infra.id, night.REPORT_WEBHOOK) == HOOK
    bad = client.patch(url, json={"report_webhook_url": "http://hooks.example/x"}, headers=headers)
    assert bad.status_code == 400
    assert client.get(url, headers={}).status_code == 401

    night.schedule(database, MONDAY_NIGHT)
    evil = "Fini <!channel> @here <@U123> & <https://evil.example|cliquez> *gras*"
    done = run_night(database, ctx, wiki, NightModel(summary=evil))
    assert done.report["slack"] == "queued"
    db.expire_all()
    [job] = db.scalars(select(Job).where(Job.kind == night.SLACK))
    text = str(job.payload["text"])
    assert HOOK not in json.dumps(job.payload)  # the URL is read at delivery time
    assert "<!channel>" not in text and "<@U123>" not in text and "<https://evil" not in text
    assert "&lt;!channel&gt;" in text and "@​here" in text and "&amp;" in text
    assert "<https://brain.example.com/o/acme/infra/p/rapports/apres.md|" in text
    # Delivered to the webhook (a local receiver here, as with allow_private_webhooks).
    secrets.put_space_secret(db, ctx.box, wiki.infra.id, night.REPORT_WEBHOOK, receiver, None)
    db.commit()
    night.deliver_slack(database, ctx.box, job.payload, allow_private=True)
    [body] = Receiver.bodies
    assert body["text"] == text and body["unfurl_links"] is False and body["mrkdwn"] is True
    db.expire_all()
    assert nights_of(db, wiki)[0].report["slack"] == "sent"
    # Removed: nothing is posted any more, and "configured" goes back to false.
    client.patch(url, json={"report_webhook_url": ""}, headers=headers)
    assert client.get(url, headers=headers).json()["report_webhook"] is False
    night.deliver_slack(database, ctx.box, job.payload, allow_private=True)
    assert len(Receiver.bodies) == 1


def test_settings_page_test_report_and_the_nights_in_the_journal(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    client: TestClient,
    smtp: Inbox,
) -> None:
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "avant", place="before")
    verified(db, wiki.root.user, "root@example.com")
    db.commit()
    login(client, "root")
    page = client.get("/o/acme/infra/-/tidy-settings").text
    assert "La nuit et son compte rendu" in page and "Routine avant" in page
    assert "Envoyer un compte rendu d'essai" not in page  # nobody receives it yet
    form = {
        "csrf": re.findall(r'name="csrf" value="([^"]+)"', page)[0],
        "tidy_time": "02:00",
        "tidy_timezone": "Europe/Paris",
        "tidy_enabled": "true",
        "night_report_email": "admins",
        "night_report_webhook": HOOK,
    }
    client.post("/o/acme/infra/-/tidy-settings", data=form)
    page = client.get("/o/acme/infra/-/tidy-settings").text
    assert "Webhook Slack configuré" in page and HOOK not in page and "secret-token" not in page
    assert '<option value="admins" selected>' in page
    # An empty field keeps the webhook.
    client.post("/o/acme/infra/-/tidy-settings", data={**form, "night_report_webhook": ""})
    assert night.has_webhook(db, wiki.infra.id)
    client.post("/o/acme/infra/-/night-report/test", data={"csrf": form["csrf"]})
    [message] = smtp.messages
    to, subject, body = received(message)
    assert to == "root@example.com" and subject.startswith("[test] ")
    assert "Compte rendu d'essai" in body
    db.expire_all()
    assert len(db.scalars(select(Job).where(Job.kind == night.SLACK)).all()) == 1

    night.schedule(database, MONDAY_NIGHT)
    run_night(database, ctx, wiki, NightModel())
    journal = client.get("/o/acme/infra/tidy").text
    assert "Les dernières nuits" in journal and "Nuit du 5 octobre 2026" in journal
    assert "Routine avant" in journal and "/-/routines/" in journal
    assert "Compte rendu envoyé" in journal
    api = client.get(
        "/api/v1/organizations/acme/spaces/infra/nights",
        headers={"Authorization": f"Bearer {token_for(database, 'root')}"},
    ).json()
    assert [s["kind"] for s in api[0]["steps"]] == ["routine", "tidy"]
    # Remove it from the form.
    client.post(
        "/o/acme/infra/-/tidy-settings",
        data={**form, "night_report_webhook": "", "night_webhook_clear": "true"},
    )
    assert not night.has_webhook(db, wiki.infra.id)


# --- The clock: one for the night and the plain schedule, changes of time ----------------------


def test_the_worker_plans_the_night_and_the_tidy_up_at_the_same_instant(
    settings: Settings,
    database: Database,
    ctx: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clocks would let the tidy-up's time pass between the two calls: the plain schedule
    would then take the tidy-up of a night not planned yet (out of its order, or without its
    report)."""
    seen: list[tuple[str, datetime | None]] = []

    def recorder(name: str) -> Any:
        def scheduled(database: Database, now: datetime | None = None) -> int:
            seen.append((name, now))
            return 0

        return scheduled

    monkeypatch.setattr(night, "schedule", recorder("night"))
    monkeypatch.setattr(tidy, "schedule", recorder("tidy"))
    Worker(settings, database, ctx).maintenance()
    assert [name for name, _ in seen] == ["night", "tidy"]
    assert seen[0][1] is not None and seen[0][1] == seen[1][1]


def test_changes_of_time_plan_one_night(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Paris, tidy-up at 02:00: on 25 October 2026 that hour comes twice (one night), on
    29 March 2026 it never comes (the night starts at 03:00)."""
    configure_ai(db, ctx, wiki)
    for space in (wiki.infra, wiki.other):
        space.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    db.commit()
    add_routine(db, database, ctx, wiki, "apres")
    assert night.schedule(database, datetime(2026, 10, 25, 0, 30, tzinfo=UTC)) == 1  # 02:30 CEST
    assert night.schedule(database, datetime(2026, 10, 25, 1, 30, tzinfo=UTC)) == 0  # 02:30 CET
    assert night.schedule(database, datetime(2026, 3, 29, 0, 59, tzinfo=UTC)) == 0  # 01:59 CET
    assert night.schedule(database, datetime(2026, 3, 29, 1, 0, tzinfo=UTC)) == 1  # 03:00 CEST
    assert [n.day for n in nights_of(db, wiki)] == [date(2026, 3, 29), date(2026, 10, 25)]


def test_the_test_report_cannot_flood_a_channel(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    night.update_report(db, wiki.root, ctx.box, space, webhook_url=HOOK)
    db.commit()
    for _ in range(night.TESTS_PER_HOUR):
        assert night.send_test(db, wiki.root, ctx, space, "https://b") == (False, True)
        db.commit()
    with pytest.raises(SynaptaneError, match="test reports per hour"):
        night.send_test(db, wiki.root, ctx, space, "https://b")
    db.rollback()
    queued = db.scalars(select(Job).where(Job.kind == night.SLACK)).all()
    assert len(queued) == night.TESTS_PER_HOUR
    # Another space keeps its own tries.
    other = db.get(Space, wiki.other.id)
    assert other is not None
    night.update_report(db, wiki.root, ctx.box, other, webhook_url=HOOK)
    assert night.send_test(db, wiki.root, ctx, other, "https://b") == (False, True)


def test_a_refused_webhook_address_stays_out_of_the_nights_log(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Readers of the space see the night's log: the address refused is only in the task's
    error, for the administrators."""
    configure_ai(db, ctx, wiki)
    old_spaces(db, wiki)
    add_routine(db, database, ctx, wiki, "apres")
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    night.update_report(db, wiki.root, ctx.box, space, webhook_url="https://127.0.0.1:9/hook")
    db.commit()
    night.schedule(database, MONDAY_NIGHT)
    run_night(database, ctx, wiki, NightModel())
    db.expire_all()
    [job] = db.scalars(select(Job).where(Job.kind == night.SLACK))
    with pytest.raises(webhooks.DeliveryError, match=r"127\.0\.0\.1"):
        night.deliver_slack(database, ctx.box, job.payload)
    db.expire_all()
    slack = str(nights_of(db, wiki)[0].report["slack"])
    assert slack == "Internal address refused." and "127.0.0.1" not in slack
