"""Automatic backup by the worker, end to end: queued at its time, archive verified then
restored (round trip), off-site copy, alert on failure, and living alongside the host's cron
job (`deploy/host/backup.sh` → `synaptane backup --output -`). "Back up now" on the health
page: super admins only, with CSRF, in the audit log."""

import io
import os
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane import cli
from synaptane.cli import main
from synaptane.config import Settings
from synaptane.core.errors import BackupError, SynaptaneError
from synaptane.core.security import SecretBox
from synaptane.services import (
    backup,
    backup_schedule,
    instance,
    jobs,
    offsite,
    organizations,
)
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Job, Organization, User
from synaptane.worker.runner import LANES, Worker
from tests.integration.conftest import actor_of, make_user
from tests.integration.test_offsite import decrypt, keypair, write_config
from tests.integration.test_web import csrf, login

needs_pg = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="client PostgreSQL (pg_dump, pg_restore) absent",
)
needs_tools = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("pg_dump", "pg_restore", "age", "rclone")),
    reason="pg_dump, pg_restore, age ou rclone absent",
)


@pytest.fixture
def automatic(settings: Settings, tmp_path: Path) -> Settings:
    """Settings of the Docker image: a backup folder, the default time."""
    return settings.model_copy(update={"backup_dir": tmp_path / "backups"})


def alerts_of(database: Database) -> list[AuditEvent]:
    with database.session() as session:
        found = list(
            session.scalars(select(AuditEvent).where(AuditEvent.action == "alert.backup_failed"))
        )
        session.expunge_all()
        return found


def backup_jobs(database: Database) -> list[Job]:
    with database.session() as session:
        found = list(session.scalars(select(Job).where(Job.kind == backup_schedule.BACKUP)))
        session.expunge_all()
        return found


# --- Scheduling -------------------------------------------------------------------------------


def test_the_worker_queues_one_backup_a_day_in_its_own_lane(
    automatic: Settings, database: Database, ctx: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert LANES["backup"] == frozenset({backup_schedule.BACKUP})
    early = automatic.model_copy(update={"backup_time": "00:00"})
    worker = Worker(early, database, ctx)
    worker.maintenance()
    (job,) = backup_jobs(database)
    assert job.space_id is None and job.payload == {"trigger": backup_schedule.SCHEDULE}
    assert backup_schedule.schedule(database, early) is False  # already queued today
    ran: list[str] = []

    def run(*_: object, trigger: str, **__: object) -> backup_schedule.Result:
        ran.append(trigger)
        return backup_schedule.Result()

    monkeypatch.setattr(backup_schedule, "run", run)
    assert worker.run_once(LANES["backup"])
    assert ran == [backup_schedule.SCHEDULE] and backup_jobs(database)[0].status == "done"
    # The next day, at its time, once again.
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    (automatic.data_dir / backup_schedule.SCHEDULED).unlink()  # as if queued yesterday
    assert backup_schedule.schedule(database, early, tomorrow)
    assert (
        backup_schedule.schedule(database, automatic.model_copy(update={"backup_dir": None}))
        is False
    )


def test_a_queued_backup_is_not_queued_twice(
    automatic: Settings, database: Database, db: Session
) -> None:
    jobs.enqueue(db, backup_schedule.BACKUP, None, {"trigger": backup_schedule.MANUAL})
    db.commit()
    early = automatic.model_copy(update={"backup_time": "00:00"})
    assert backup_schedule.schedule(database, early) is False  # one is waiting already
    assert len(backup_jobs(database)) == 1


# --- The backup itself ------------------------------------------------------------------------


@needs_pg
def test_round_trip_of_an_automatic_backup(
    automatic: Settings,
    database: Database,
    ctx: Workspace,
    new_database: Callable[[], str],
    tmp_path: Path,
) -> None:
    with database.session() as session:
        root = actor_of(session, make_user(session, "root", superadmin=True))
        organizations.create_organization(session, root, slug="acme", name="Acme")
    folder = automatic.backup_dir
    assert folder is not None
    folder.mkdir()
    old = time.time() - 30 * 86400
    for day in range(1, 5):  # the archives of the last nights, too old
        path = folder / f"synaptane-2026-10-0{day}-040000.tar.gz"
        path.write_bytes(b"old")
        os.utime(path, (old, old))
    (folder / ".synaptane-2026-10-05-040000.tar.gz.part").write_bytes(b"interrupted")

    result = backup_schedule.run(automatic, database, ctx.box)
    assert result.archive is not None and result.archive.parent == folder
    assert not result.skipped and result.offsite_ok and result.outcomes == []
    assert (result.archive.stat().st_mode & 0o777) == 0o600
    assert (automatic.data_dir / instance.BACKUP_DONE).exists()
    assert not list(folder.glob(".*.part"))  # what an interrupted backup left
    # Retention: the 3 newest stay (the new one and two old ones).
    assert sorted(p.name for p in folder.glob("synaptane-*.tar.gz")) == [
        "synaptane-2026-10-03-040000.tar.gz",
        "synaptane-2026-10-04-040000.tar.gz",
        result.archive.name,
    ]
    assert len(result.pruned) == 2
    manifest = backup.verify(automatic, result.archive)
    assert manifest["counts"] == {"users": 1, "organizations": 1, "spaces": 0}

    # Round trip: the archive of the worker restores on a new server.
    target = Settings(database_url=new_database(), data_dir=tmp_path / "new")
    target.data_dir.mkdir()
    target.key_file.write_bytes(SecretBox.generate_key())
    with result.archive.open("rb") as source:
        restored = backup.restore(target, source)
    assert restored.version == manifest["version"]
    assert target.key_file.read_bytes() == automatic.key_file.read_bytes()
    copy = Database(target.database_url)
    try:
        with copy.session() as session:
            assert session.scalar(select(Organization.slug)) == "acme"
            assert session.scalar(select(User.username)) == "root"
    finally:
        copy.dispose()


def test_a_failed_backup_alerts_the_super_admins_and_leaves_nothing(
    automatic: Settings,
    database: Database,
    ctx: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    with database.session() as session:
        make_user(session, "root", superadmin=True)

    def broken(*_: object, **__: object) -> None:
        raise BackupError("pg_dump failed: connection refused")

    monkeypatch.setattr(backup, "backup", broken)
    with pytest.raises(BackupError):
        backup_schedule.run(automatic, database, ctx.box)
    folder = automatic.backup_dir
    assert folder is not None and list(folder.iterdir()) == []  # no .part, no archive
    assert not (automatic.data_dir / instance.BACKUP_DONE).exists()
    (alert,) = alerts_of(database)
    assert alert.details == {"error": "Archive: pg_dump failed: connection refused"}
    assert alert.org_id is None and alert.actor_id is None
    with pytest.raises(BackupError):  # the same failure the next night: not sent again
        backup_schedule.run(automatic, database, ctx.box, trigger=backup_schedule.MANUAL)
    assert len(alerts_of(database)) == 1
    login(client, "root")
    page = client.get("/admin/health").text
    assert "Une sauvegarde a échoué" in page and "pg_dump failed: connection refused" in page


@needs_pg
def test_without_encryption_key_the_archive_stays_and_the_copy_is_refused(
    automatic: Settings, database: Database, ctx: Workspace, tmp_path: Path
) -> None:
    configured = automatic.model_copy(update={"backup_remotes": (str(tmp_path / "far"),)})
    result = backup_schedule.run(configured, database, ctx.box)
    assert result.archive is not None and result.archive.exists()
    assert "no age recipient" in result.offsite_error and not result.offsite_ok
    assert not (tmp_path / "far").exists()
    assert not (automatic.data_dir / instance.OFFSITE_DONE).exists()
    (alert,) = alerts_of(database)
    assert str(alert.details["error"]).startswith("Off-site copy: Off-server copy refused")


@needs_pg
def test_off_site_outcomes_are_reported(
    automatic: Settings, database: Database, ctx: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_backup_schedule import PUBLIC

    configured = automatic.model_copy(
        update={"backup_remotes": ("nas:x", "b2:y"), "backup_age_recipients": (PUBLIC,)}
    )
    monkeypatch.setattr(offsite, "missing_tools", lambda: [])
    outcomes = [offsite.Outcome("nas:x"), offsite.Outcome("b2:y", "copy: quota exceeded")]
    monkeypatch.setattr(offsite, "copy", lambda *_: outcomes)
    result = backup_schedule.run(configured, database, ctx.box)
    assert result.outcomes == outcomes and not result.offsite_ok
    (alert,) = alerts_of(database)
    assert alert.details["error"] == "Off-site copy: b2:y: copy: quota exceeded"
    assert not (automatic.data_dir / instance.OFFSITE_DONE).exists()
    monkeypatch.setattr(offsite, "copy", lambda *_: [offsite.Outcome("nas:x")])
    result = backup_schedule.run(configured, database, ctx.box, trigger=backup_schedule.MANUAL)
    assert result.offsite_ok and (automatic.data_dir / instance.OFFSITE_DONE).exists()

    def unreadable(*_: object) -> list[offsite.Outcome]:
        raise BackupError("age encryption failed: bad recipient")

    monkeypatch.setattr(offsite, "copy", unreadable)
    result = backup_schedule.run(configured, database, ctx.box, trigger=backup_schedule.MANUAL)
    assert result.offsite_error == "age encryption failed: bad recipient"


@needs_tools
def test_the_encrypted_copy_of_the_worker_opens_with_the_private_key(
    automatic: Settings, database: Database, ctx: Workspace, tmp_path: Path
) -> None:
    identity, public = keypair(tmp_path)
    far = tmp_path / "far"
    configured = automatic.model_copy(update={"backup_remotes": (f":local:{far}",)})
    offsite.save_recipient(configured, public)  # as `synaptane offsite key` does
    result = backup_schedule.run(configured, database, ctx.box)
    assert result.archive is not None and result.offsite_ok, result
    (copy,) = far.glob("synaptane-*.tar.gz.age")
    assert decrypt(identity, copy) == result.archive.read_bytes()
    assert not list(automatic.data_dir.glob("offsite-*"))  # nothing left in the data folder


# --- With the host's cron job (deploy/host/backup.sh) -----------------------------------------


@needs_pg
def test_the_host_cron_job_keeps_working_and_takes_precedence(
    automatic: Settings,
    database: Database,
    ctx: Workspace,
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`docker compose exec -T -e SYNAPTANE_HOST_BACKUP=1 app synaptane backup --output -`,
    as deploy/host/backup.sh runs it from cron: the archive on standard output as before, and
    the worker's automatic backup steps aside the following night."""
    config = write_config(automatic, tmp_path / "synaptane.toml", backup_dir=automatic.backup_dir)
    monkeypatch.setenv(backup_schedule.HOST_ENV, "1")
    assert main(["--config", str(config), "backup", "--output", "-"]) == 0
    archive = capsysbinary.readouterr().out
    with backup.exclusive(automatic) as free:
        assert free  # released once the command is done
    target = tmp_path / "from-cron.tar.gz"
    target.write_bytes(archive)
    assert backup.verify(automatic, target)["format"] == backup.FORMAT
    assert (automatic.data_dir / backup_schedule.HOST_DONE).exists()
    folder = automatic.backup_dir
    assert folder is not None and not folder.exists()  # --output -: nothing in /backups
    tonight = backup_schedule.run(automatic, database, ctx.box)
    assert tonight.skipped == "host" and not folder.exists()
    # A deployment (backup.sh with a label) or a manual run: no precedence.
    (automatic.data_dir / backup_schedule.HOST_DONE).unlink()
    monkeypatch.delenv(backup_schedule.HOST_ENV)
    assert main(["--config", str(config), "backup", "--output", "-"]) == 0
    capsysbinary.readouterr()
    assert not (automatic.data_dir / backup_schedule.HOST_DONE).exists()


def test_a_cron_backup_waits_for_the_running_one(
    automatic: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_config(automatic, tmp_path / "synaptane.toml")
    monkeypatch.setattr(cli, "BACKUP_WAIT", 0.2)
    output = tmp_path / "out"
    output.mkdir()
    with backup.exclusive(automatic):  # the worker's backup is running
        assert main(["--config", str(config), "backup", "--output", str(output)]) == 1
        err = capsys.readouterr().err
        assert "Another backup is running: waiting" in err and "nothing was done" in err
        with pytest.raises(BackupError, match="A backup is running"):
            backup.restore(automatic, io.BytesIO(b"not read"))
    assert list(output.iterdir()) == []


# --- Health page ------------------------------------------------------------------------------


def check_of(settings: Settings, db: Session, database: Database, ctx: Workspace) -> instance.Check:
    found = instance.report(db, database, ctx, settings).checks
    return next(check for check in found if check.title == "Backup")


def test_the_health_page_says_how_backups_are_made(
    automatic: Settings, db: Session, database: Database, ctx: Workspace
) -> None:
    check = check_of(automatic, db, database, ctx)
    assert check.state == instance.WARN and check.message.params == {"time": "04:00"}
    assert check.note is not None and "kept {days} days" in check.note.template
    assert check.command == instance.CHECK_COMMAND and check.guide == "backup"
    off = automatic.model_copy(update={"backup_time": "off"})
    check = check_of(off, db, database, ctx)
    assert check.state == instance.ERROR and check.command == instance.SETUP_COMMAND
    assert check.note is not None and "BACKUP_TIME=off" in check.note.template
    nowhere = automatic.model_copy(update={"backup_dir": None})
    note = check_of(nowhere, db, database, ctx).note
    assert note is not None and "SYNAPTANE_BACKUP_DIR" in note.template
    all_kept = automatic.model_copy(update={"backup_local_retention_days": 0})
    note = check_of(all_kept, db, database, ctx).note
    assert note is not None and "all kept" in note.template
    instance.mark(automatic, backup_schedule.HOST_DONE)
    instance.mark(automatic, instance.BACKUP_DONE)
    check = check_of(automatic, db, database, ctx)
    assert check.state == instance.OK and check.command == ""
    assert check.note is not None and "cron job" in check.note.template


def test_back_up_now_from_the_health_page(
    automatic: Settings, database: Database, db: Session
) -> None:
    from synaptane.server import create_app

    make_user(db, "root", superadmin=True)
    make_user(db, "alice")
    db.commit()
    with TestClient(create_app(automatic, database), base_url="http://localhost") as client:
        login(client, "root")
        page = client.get("/admin/health").text
        assert "Sauvegarder maintenant" in page
        assert instance.CHECK_COMMAND in page and instance.SETUP_COMMAND in page
        assert "docs/admin/backup.fr.md" in page
        assert "Automatique chaque jour à 04:00" in page
        token = csrf(client, "/admin/health")
        refused = client.post("/admin/health/backup", data={"csrf": "x"}, follow_redirects=False)
        assert refused.status_code == 403 and backup_jobs(database) == []
        done = client.post("/admin/health/backup", data={"csrf": token})
        assert "Sauvegarde demandée\xa0: le worker la fait dans un instant." in done.text
        assert "Une sauvegarde attend ou est en cours." in done.text
        again = client.post("/admin/health/backup", data={"csrf": token})
        assert "Une sauvegarde attend déjà ou est en cours." in again.text
        journal = client.get("/admin/journal").text
        assert "Sauvegarde demandée" in journal
        client.cookies.clear()
        login(client, "alice")
        token = csrf(client, "/")
        assert client.post("/admin/health/backup", data={"csrf": token}).status_code == 403
    (job,) = backup_jobs(database)
    assert job.payload == {"trigger": backup_schedule.MANUAL}
    with database.session() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "instance.backup_requested")
        ).all()
        assert len(events) == 1 and events[0].details == {}


def test_back_up_now_needs_a_backup_folder(settings: Settings, db: Session) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    with pytest.raises(SynaptaneError, match="SYNAPTANE_BACKUP_DIR"):
        backup_schedule.request(db, root, settings)
    alice = actor_of(db, make_user(db, "alice"))
    with pytest.raises(SynaptaneError):
        backup_schedule.request(db, alice, settings.model_copy(update={"backup_dir": Path("/b")}))


def test_no_button_without_a_backup_folder(client: TestClient, db: Session) -> None:
    make_user(db, "root", superadmin=True)
    db.commit()
    login(client, "root")
    page = client.get("/admin/health").text
    assert "Sauvegarder maintenant" not in page
    assert "SYNAPTANE_BACKUP_DIR" in page
