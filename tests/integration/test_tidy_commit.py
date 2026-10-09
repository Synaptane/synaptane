"""Robust tidying, never paid for twice for nothing."""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from synaptane.git.repo import GitError
from synaptane.services import alerts, content, deposits, git_sync, jobs, mail, tidy, tidy_commit
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database, space_lock
from synaptane.storage.models import AuditEvent, ChangeMark, Job, Space, TidyRun
from tests.integration.test_tidy import CloseAll, Scripted, configure_ai, deposit_note
from tests.integration.wikidata import Wiki

PROXMOX = "content/serveurs/proxmox.md"


def alert_actions(db: Session) -> list[str]:
    db.expire_all()
    return list(db.scalars(select(AuditEvent.action).where(AuditEvent.action.like("alert.%"))))


def human_writes(database: Database, ctx: Workspace, wiki: Wiki, path: str, data: bytes) -> None:
    with database.session() as session:
        content.write_file(session, database, wiki.alice, ctx, wiki.infra, path, data)


# --- Per-file merge, never a new model pass ----------------------------------------------------


def test_a_human_edit_elsewhere_in_the_page_is_merged(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    note = deposit_note(db, database, ctx, wiki)
    before = (ctx.local_repository(wiki.infra).blob(PROXMOX) or b"").decode()

    def human_edit(step: int) -> None:
        if step == 1:  # the title changes while the agent adds a link at the bottom of the page
            human_writes(
                database,
                ctx,
                wiki,
                "serveurs/proxmox.md",
                before.replace("# Proxmox", "# PVE").encode(),
            )

    script = Scripted(
        [
            [("read_page", {"path": "serveurs/proxmox.md"}), ("read_inbox_item", {"item": note})],
            [
                ("write_page", {"path": "stockage/nas.md", "content": "# NAS\n\n8 To.\n"}),
                (
                    "write_page",
                    {
                        "path": "serveurs/proxmox.md",
                        "content": before + "\nVoir aussi le [NAS](../stockage/nas.md).\n",
                    },
                ),
                (
                    "mark_done",
                    {
                        "item": note,
                        "summary": "NAS",
                        "pages": ["stockage/nas.md", "serveurs/proxmox.md"],
                    },
                ),
            ],
        ],
        on_step=human_edit,
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit
    assert script.index == 2 and record.items_done == 1
    page = (ctx.clone_path(wiki.infra.id) / PROXMOX).read_text()
    assert page.startswith("# PVE") and "Voir aussi le [NAS]" in page  # both kept
    assert ("fusionné", "serveurs/proxmox.md") in {(o["op"], o["path"]) for o in record.operations}
    assert deposits.list_items(wiki.alice, ctx, wiki.infra) == []


def test_a_note_edited_during_the_pass_keeps_the_human_version(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    note = deposit_note(db, database, ctx, wiki)

    def human_edit(step: int) -> None:
        if step == 1:
            with database.session() as session:
                deposits.update_item(
                    session,
                    database,
                    wiki.alice,
                    ctx,
                    wiki.infra,
                    note,
                    title="Nouveau NAS",
                    body="Le NAS passe à 16 To, finalement.",
                )

    script = Scripted(
        [
            [("read_inbox_item", {"item": note})],
            [
                ("write_page", {"path": "nas.md", "content": "# NAS\n\n8 To.\n"}),
                ("mark_done", {"item": note, "summary": "NAS", "pages": ["nas.md"]}),
            ],
        ],
        on_step=human_edit,
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit
    assert record.items_done == 0  # the note is not removed: it changed in the meantime
    [item] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    assert item.status == "pending" and "16 To" in item.body
    assert any(op["op"] == "non traité" for op in record.operations)
    assert (ctx.clone_path(wiki.infra.id) / "content/nas.md").exists()  # the rest is committed


def test_a_busy_space_delays_the_commit_without_a_new_pass(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    deposit_note(db, database, ctx, wiki)
    held, release = threading.Event(), threading.Event()

    def sync_in_progress() -> None:
        with space_lock(database.engine, wiki.infra.id):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=sync_in_progress)
    holder.start()
    held.wait(5)
    threading.Timer(1.5, release.set).start()  # the "sync" ends during the wait
    model = CloseAll()
    started = time.monotonic()
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=model.factory)
    holder.join()
    assert record is not None and record.status == "done" and record.commit
    assert time.monotonic() - started >= 1.0 and len(model.seen) == 1


def test_a_space_busy_for_too_long_fails_without_retrying_the_model(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tidy_commit, "COMMIT_WAIT", timedelta(seconds=0.5))
    configure_ai(db, ctx, wiki)
    deposit_note(db, database, ctx, wiki)
    with space_lock(database.engine, wiki.infra.id):
        record = tidy.run(
            database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory
        )
    # No RetryLater: retrying the job would pay for the whole pass again.
    assert record is not None and record.status == "failed" and "busy" in (record.error or "")
    assert "alert.tidy_failed" in alert_actions(db)


# --- One pass at a time per space --------------------------------------------------------------


def test_a_second_pass_waits_for_the_running_one(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    deposit_note(db, database, ctx, wiki)
    idle = Scripted([])
    with tidy_commit.pass_lock(database.engine, wiki.infra.id) as first:  # another worker
        assert first
        with tidy_commit.pass_lock(database.engine, wiki.infra.id) as second:
            assert not second
        with tidy_commit.pass_lock(database.engine, wiki.other.id) as elsewhere:
            assert elsewhere  # another space does not wait
        with space_lock(database.engine, wiki.infra.id):
            pass  # the local copy lock stays free: different key space
        by = wiki.alice.user.id
        result = tidy.run(
            database, ctx, wiki.infra.id, trigger="manual", requested_by=by, factory=idle.factory
        )
    assert result is None and idle.first == ""  # model never called
    db.expire_all()
    assert db.scalars(select(TidyRun)).all() == []  # no journal…
    [job] = db.scalars(select(Job).where(Job.kind == tidy.TIDY)).all()
    assert job.status == "queued" and job.attempts == 0  # … nor attempt used up
    assert job.payload == {"trigger": "manual", "by": str(by)}
    assert job.run_after > datetime.now(UTC) + timedelta(seconds=60)


# --- "Tidy now" is not absorbed by a scheduled pass -------------------------------------------


def test_a_manual_request_replaces_a_waiting_scheduled_pass(
    db: Session, database: Database, wiki: Wiki
) -> None:
    jobs.enqueue(db, tidy.TIDY, wiki.infra.id, {"trigger": "schedule"})
    db.flush()
    db.execute(update(Job).values(attempts=2, error="réseau"))
    tidy.request_run(db, wiki.alice, wiki.infra)
    db.commit()
    db.expire_all()
    [job] = db.scalars(select(Job).where(Job.kind == tidy.TIDY)).all()
    assert job.payload == {"trigger": "manual", "by": str(wiki.alice.user.id)}
    assert (job.attempts, job.error) == (0, None)
    jobs.enqueue(db, tidy.TIDY, wiki.infra.id, {"trigger": "schedule"})  # does not overwrite it
    db.commit()
    db.expire_all()
    assert db.scalars(select(Job.payload)).one()["trigger"] == "manual"


# --- "Left" (decision) and "not processed" (interruption) -------------------------------------


def test_an_interrupted_page_change_stays_new_with_a_counter(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "notes.md", b"# Notes\n")
    db.commit()
    [change] = deposits.inbox(db, wiki.alice, ctx, wiki.infra)
    assert change.type == "change"
    for attempt in range(1, tidy.MAX_ITEM_ATTEMPTS):
        record = tidy.run(
            database, ctx, wiki.infra.id, trigger="schedule", factory=Scripted([]).factory
        )
        assert record is not None and record.items_left == 0
        db.expire_all()
        mark = db.get(ChangeMark, (wiki.infra.id, "notes.md"))
        assert mark is not None and mark.status == "pending" and mark.info["attempts"] == attempt
        [item] = deposits.inbox(db, wiki.alice, ctx, wiki.infra)
        assert item.status == "pending"  # still new: picked up the next night
    record = tidy.run(
        database, ctx, wiki.infra.id, trigger="schedule", factory=Scripted([]).factory
    )
    assert record is not None and record.items_left == 1
    db.expire_all()
    [item] = deposits.inbox(db, wiki.alice, ctx, wiki.infra)
    assert item.status == "left" and "3 interrupted runs" in item.reason
    # An agent decision clears the counter; the watermark can then move forward.
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory)
    assert record is not None and record.items_done == 1
    db.expire_all()
    assert deposits.inbox(db, wiki.alice, ctx, wiki.infra) == []
    assert db.scalars(select(ChangeMark)).all() == []


def test_an_agent_decision_is_not_an_interruption(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    note = deposit_note(db, database, ctx, wiki)
    leave = Scripted([[("leave_in_inbox", {"item": note, "reason": "Quel NAS ?"})]])
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=leave.factory)
    assert record is not None and record.items_left == 1
    [item] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    assert item.status == "left" and item.reason == "Quel NAS ?"
    assert alert_actions(db) == []  # a decision notifies no one


def test_an_interrupted_pass_is_not_chained_right_away(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tidy, "MAX_ITEMS", 1)
    configure_ai(db, ctx, wiki)
    first = deposit_note(db, database, ctx, wiki)
    deposit_note(db, database, ctx, wiki)
    script = Scripted(
        [
            [("read_inbox_item", {"item": first})],
            [("mark_done", {"item": first, "summary": "vu"})],
        ]
    )
    monkeypatch.setattr(tidy, "MAX_STEPS", 1)  # cap reached before closing
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.items_done == 0
    db.expire_all()
    assert db.scalars(select(Job).where(Job.kind == tidy.TIDY)).all() == []
    assert "alert.tidy_interrupted" in alert_actions(db)


# --- Lasting transient errors ------------------------------------------------------------------


def test_an_abandoned_job_alerts_once(database: Database, ctx: Workspace, wiki: Wiki) -> None:
    with database.session() as session:
        for kind in ("index", "sync"):
            jobs.enqueue(session, kind, wiki.infra.id)
    for _ in range(jobs.MAX_ATTEMPTS):
        with database.session() as session:
            for _kind in ("index", "sync"):
                job = jobs.claim(session)
                assert job is not None
                jobs.fail(session, job.id, "Forge injoignable", retry=True)
            session.execute(update(Job).values(run_after=jobs._now()))
    with database.session() as session:
        failed = session.scalars(select(Job)).all()
        assert all(j.status == "failed" and (j.error or "").startswith("Abandoned") for j in failed)
    assert alerts.check_abandoned_jobs(database, ctx) == 1  # the sync has its alert
    assert alerts.check_abandoned_jobs(database, ctx) == 0
    with database.session() as session:
        details = session.scalars(
            select(AuditEvent.details).where(AuditEvent.action == "alert.job_abandoned")
        ).one()
        assert details["task"] == "index" and "Forge injoignable" in str(details["error"])


def test_a_lasting_transient_sync_error_alerts(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.repo_url = "https://127.0.0.1:9/nope.git"
    db.commit()
    with pytest.raises(GitError) as error:
        git_sync.run_sync(database, ctx, wiki.infra.id)
    assert error.value.transient and alert_actions(db) == []  # transient: not yet
    db.expire_all()
    space = db.get(Space, wiki.infra.id)
    assert space is not None and space.sync_error_since is not None
    space.sync_error_since = datetime.now(UTC) - timedelta(hours=7)
    db.commit()
    with pytest.raises(GitError):
        git_sync.run_sync(database, ctx, wiki.infra.id)
    assert alert_actions(db) == ["alert.sync_error"]
    details = db.scalars(
        select(AuditEvent.details).where(AuditEvent.action == "alert.sync_error")
    ).one()
    assert "transient error for 7 h" in str(details["error"])
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.repo_url = wiki.remote.url
    db.commit()
    git_sync.run_sync(database, ctx, wiki.infra.id)
    db.expire_all()
    space = db.get(Space, wiki.infra.id)
    assert space is not None and space.sync_error_since is None  # streak closed


def test_every_alert_has_its_mail_in_both_languages() -> None:
    for kind in alerts.KINDS:
        for locale in ("fr", "en"):
            assert locale in mail._TEXTS[f"alert_{kind}"], kind
    subject, body = mail.compose(
        "alert_tidy_interrupted",
        "en",
        space="Infra",
        org="Acme",
        link="https://x/o/acme/infra",
        reason="Erreur 529",
        count=2,
        attempts=3,
        given_up="Nouveau NAS",
    )
    assert "interrupted" in subject and "Nouveau NAS" in body and "/tidy" in body
    subject, body = mail.compose(
        "alert_job_abandoned",
        "fr",
        space="Infra",
        org="Acme",
        link="https://x",
        task="index",
        attempts=5,
        error="Abandonnée après 5 essais : réseau",
    )
    assert "abandonnée" in subject and "5 fois" in body
