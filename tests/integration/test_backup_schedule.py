"""Automatic backup by the worker, end to end: queued at its time, archive verified then
restored (round trip), off-site copy, alert on failure, and living alongside the host's cron
job (`deploy/host/backup.sh` → `synaptane backup --output -`). "Back up now" on the health
page: super admins only, with CSRF, in the audit log."""

import io
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
        path = folder / f"synaptane-2026-10-0{day}-040000-auto.tar.gz"
        path.write_bytes(b"old")
        os.utime(path, (old, old))
    by_hand = folder / "synaptane-2026-09-01-120000.tar.gz"  # put there by hand: kept
    by_hand.write_bytes(b"mine")
    os.utime(by_hand, (old, old))
    (folder / ".synaptane-2026-10-05-040000-auto.tar.gz.part").write_bytes(b"interrupted")

    result = backup_schedule.run(automatic, database, ctx.box)
    assert result.archive is not None and result.archive.parent == folder
    assert not result.skipped and result.offsite_ok and result.outcomes == []
    assert (result.archive.stat().st_mode & 0o777) == 0o600
    assert result.archive.name.endswith("-auto.tar.gz")
    assert (automatic.data_dir / instance.BACKUP_DONE).exists()
    assert (automatic.data_dir / backup_schedule.COMPLETE_DONE).exists()
    assert not list(folder.glob(".*.part"))  # what an interrupted backup left
    # Retention, before writing: the 3 newest of the worker's stay, and the one made by hand.
    assert sorted(p.name for p in folder.glob("synaptane-*.tar.gz")) == [
        "synaptane-2026-09-01-120000.tar.gz",
        "synaptane-2026-10-02-040000-auto.tar.gz",
        "synaptane-2026-10-03-040000-auto.tar.gz",
        "synaptane-2026-10-04-040000-auto.tar.gz",
        result.archive.name,
    ]
    assert len(result.pruned) == 1
    assert (backup_schedule.last_status(automatic) or {})["outcome"] == "done"
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
    """`docker compose exec -T -e SYNAPTANE_BACKUP_HOST_MARKS=1 app synaptane backup --output -`,
    as deploy/host/backup.sh runs it: the archive on standard output as before, and no mark:
    the script sets them once the archive is checked and in place on the host. The worker then
    waits, and takes over when the cron job's mark is too old."""
    config = write_config(automatic, tmp_path / "synaptane.toml", backup_dir=automatic.backup_dir)
    monkeypatch.setenv(backup_schedule.HOST_MARKS, "1")
    assert main(["--config", str(config), "backup", "--output", "-"]) == 0
    archive = capsysbinary.readouterr().out
    with backup.exclusive(automatic) as free:
        assert free  # released once the command is done
    target = tmp_path / "from-cron.tar.gz"
    target.write_bytes(archive)
    assert backup.verify(automatic, target)["format"] == backup.FORMAT
    marks = (instance.BACKUP_DONE, backup_schedule.COMPLETE_DONE, backup_schedule.HOST_DONE)
    assert not any((automatic.data_dir / mark).exists() for mark in marks)  # the host's to say
    folder = automatic.backup_dir
    assert folder is not None and not folder.exists()  # --output -: nothing in /backups
    # By hand, without the script (or with an older backup.sh): verified, not "complete".
    monkeypatch.delenv(backup_schedule.HOST_MARKS)
    assert main(["--config", str(config), "backup", "--output", "-"]) == 0
    capsysbinary.readouterr()
    assert (automatic.data_dir / instance.BACKUP_DONE).exists()
    assert not (automatic.data_dir / backup_schedule.COMPLETE_DONE).exists()
    early = automatic.model_copy(update={"backup_time": "00:00"})
    # No complete backup ever marked (upgraded by an older deploy.sh): a verified backup less
    # than 6 hours old is the backup of the day.
    assert not backup_schedule.schedule(database, early)
    scheduled = automatic.data_dir / backup_schedule.SCHEDULED
    assert scheduled.exists() and backup_jobs(database) == []
    scheduled.unlink()  # the next day
    for mark in marks:  # what backup.sh does after its `mv`
        instance.mark(automatic, mark)
    assert not backup_schedule.schedule(database, early)  # the cron job is in charge
    assert not (automatic.data_dir / backup_schedule.SCHEDULED).exists()  # looked at again
    assert backup_jobs(database) == []
    tonight = backup_schedule.run(automatic, database, ctx.box)
    assert tonight.skipped == "host" and not folder.exists()
    # The cron job failed last night: as soon as its backup is too old, the worker takes over,
    # the same day, without waiting for the next one.
    late = time.time() - backup_schedule.HOST_FRESH.total_seconds() - 60
    for mark in marks:
        os.utime(automatic.data_dir / mark, (late, late))
    assert backup_schedule.schedule(database, early)
    (job,) = backup_jobs(database)
    assert job.payload == {"trigger": backup_schedule.SCHEDULE}


@needs_pg
def test_a_broken_stream_is_not_a_backup(
    automatic: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive must reach the host: a closed pipe (disk full there) marks nothing."""
    config = write_config(automatic, tmp_path / "synaptane.toml")

    class Broken:
        def write(self, _: bytes) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            raise BrokenPipeError(32, "Broken pipe")

    class Stdout:
        buffer = Broken()

        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(sys, "stdout", Stdout())
    assert main(["--config", str(config), "backup", "--output", "-"]) == 1
    for mark in (instance.BACKUP_DONE, backup_schedule.COMPLETE_DONE):
        assert not (automatic.data_dir / mark).exists()


@needs_pg
def test_a_kept_archive_appears_under_its_name_once_verified(
    automatic: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`synaptane backup` into /backups: a `.part` until read back, like the worker's; a
    partial backup (`--without-key`) is not a full one for the worker."""
    config = write_config(automatic, tmp_path / "synaptane.toml", backup_dir=automatic.backup_dir)
    folder = automatic.backup_dir
    assert folder is not None
    folder.mkdir()
    seen: list[list[str]] = []
    verify = backup.verify

    def spying(settings: Settings, path: Path) -> dict[str, object]:
        seen.append(sorted(p.name for p in folder.iterdir()))
        return verify(settings, path)

    monkeypatch.setattr(backup, "verify", spying)
    assert main(["--config", str(config), "backup", "--without-key"]) == 0
    (name,) = seen[0]
    assert name.startswith(".synaptane-") and name.endswith(".tar.gz.part")
    (kept,) = folder.iterdir()
    assert kept.name == name[1:].removesuffix(".part")
    assert (automatic.data_dir / instance.BACKUP_DONE).exists()
    assert not (automatic.data_dir / backup_schedule.COMPLETE_DONE).exists()
    assert main(["--config", str(config), "backup", "--output", str(kept)]) == 1  # no overwrite
    assert main(["--config", str(config), "backup"]) == 0
    assert (automatic.data_dir / backup_schedule.COMPLETE_DONE).exists()


@needs_pg
def test_two_backups_in_the_same_second_both_kept(
    automatic: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archive names are to the second: a second backup in the same second into a folder
    gets a suffix instead of failing; an explicit file is still never overwritten."""
    config = write_config(automatic, tmp_path / "synaptane.toml", backup_dir=automatic.backup_dir)
    folder = automatic.backup_dir
    assert folder is not None
    folder.mkdir()
    monkeypatch.setattr(backup, "default_name", lambda: "synaptane-2026-11-08-120000.tar.gz")
    for _ in range(3):
        assert main(["--config", str(config), "backup"]) == 0
    assert sorted(p.name for p in folder.iterdir()) == [
        "synaptane-2026-11-08-120000-2.tar.gz",
        "synaptane-2026-11-08-120000-3.tar.gz",
        "synaptane-2026-11-08-120000.tar.gz",
    ]
    explicit = folder / "synaptane-2026-11-08-120000.tar.gz"
    assert main(["--config", str(config), "backup", "--output", str(explicit)]) == 1


def test_a_cron_backup_waits_for_the_running_one(
    automatic: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_config(automatic, tmp_path / "synaptane.toml")
    assert cli.backup_wait(automatic) == 4 * 3600  # twice the dump timeout (120 minutes)
    monkeypatch.setattr(cli, "backup_wait", lambda _: 0.2)
    output = tmp_path / "out"
    output.mkdir()
    with backup.exclusive(automatic):  # the worker's backup is running
        assert main(["--config", str(config), "backup", "--output", str(output)]) == 1
        err = capsys.readouterr().err
        assert "Another backup is running (the worker's nightly one" in err
        assert "still running after 0 minutes: nothing was done" in err
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


# --- Every failure alerts; one job only; what the health page says ----------------------------


@pytest.mark.parametrize("where", ["lock", "offsite"])
def test_any_failure_alerts_and_is_shown(
    automatic: Settings,
    database: Database,
    ctx: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    if where == "lock":

        @contextmanager
        def denied(*_: object, **__: object) -> Iterator[bool]:
            if where == "lock":  # always: a generator that raises when entered
                raise PermissionError(13, "Permission denied", "/data/.backup.lock")
            yield True

        monkeypatch.setattr(backup, "exclusive", denied)
    else:
        from tests.unit.test_backup_schedule import PUBLIC

        automatic = automatic.model_copy(
            update={"backup_remotes": ("nas:x",), "backup_age_recipients": (PUBLIC,)}
        )
        monkeypatch.setattr(offsite, "missing_tools", lambda: [])
        monkeypatch.setattr(
            backup_schedule, "write_archive", lambda s, f: (f / "synaptane-x-auto.tar.gz", [])
        )

        def bug(*_: object) -> list[offsite.Outcome]:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(offsite, "copy", bug)
    with pytest.raises((PermissionError, RuntimeError)):
        backup_schedule.run(automatic, database, ctx.box, trigger=backup_schedule.MANUAL)
    (alert,) = alerts_of(database)
    assert ("Permission denied" if where == "lock" else "unexpected") in str(alert.details["error"])
    status = backup_schedule.last_status(automatic) or {}
    assert status["outcome"] == "failed" and status["trigger"] == backup_schedule.MANUAL


def test_an_abandoned_backup_job_alerts(
    automatic: Settings, database: Database, db: Session, ctx: Workspace
) -> None:
    """Killed five times (out of memory): `check_abandoned_jobs` only sees space jobs."""
    assert not backup_schedule.check_abandoned(database, ctx.box)
    jobs.enqueue(db, backup_schedule.BACKUP, None, {"trigger": backup_schedule.SCHEDULE})
    db.commit()
    job = db.scalars(select(Job)).one()
    job.status, job.finished_at = "failed", datetime.now(UTC)
    job.error = f"{jobs.ABANDONED}: interrupted 5 times."
    db.commit()
    Worker(automatic, database, ctx).maintenance()
    (alert,) = alerts_of(database)
    assert alert.details["error"] == f"{jobs.ABANDONED}: interrupted 5 times."
    assert not backup_schedule.check_abandoned(database, ctx.box)  # not sent again


def test_several_workers_queue_a_single_backup(automatic: Settings, database: Database) -> None:
    early = automatic.model_copy(update={"backup_time": "00:00"})
    first = database.session()
    session = first.__enter__()
    assert backup_schedule._enqueue(session, backup_schedule.SCHEDULE)  # not committed yet
    other: list[bool] = []
    thread = threading.Thread(
        target=lambda: other.append(backup_schedule.schedule(database, early))
    )
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive()  # waits for the first one's transaction
    first.__exit__(None, None, None)  # commit
    thread.join(10)
    assert other == [False] and len(backup_jobs(database)) == 1


def test_while_the_cron_job_is_in_charge_the_workers_old_archives_go(
    automatic: Settings, database: Database
) -> None:
    """For example the one made at the upgrade, before the cron job's first new run."""
    folder = automatic.backup_dir
    assert folder is not None
    folder.mkdir()
    old = time.time() - 8 * 86400
    upgrade = folder / "synaptane-2026-10-30-150000-auto.tar.gz"
    by_hand = folder / "synaptane-2026-10-30-150000.tar.gz"
    for path in (upgrade, by_hand):
        path.write_bytes(b"x")
        os.utime(path, (old, old))
    instance.mark(automatic, backup_schedule.HOST_DONE)
    early = automatic.model_copy(update={"backup_time": "00:00"})
    assert not backup_schedule.schedule(database, early)
    assert not upgrade.exists() and by_hand.exists()


def test_the_health_page_shows_the_workers_last_run(
    automatic: Settings, database: Database, db: Session, ctx: Workspace
) -> None:
    assert check_of(automatic, db, database, ctx).run is None
    jobs.enqueue(db, backup_schedule.BACKUP, None, {"trigger": backup_schedule.MANUAL})
    db.commit()
    check = check_of(automatic, db, database, ctx)
    assert check.run is not None and "waiting" in check.run.template and check.run_at
    job = db.scalars(select(Job)).one()
    job.status = "running"
    db.commit()
    check = check_of(automatic, db, database, ctx)
    assert check.run is not None and "in progress" in check.run.template
    job.status = "done"
    db.commit()
    backup_schedule.write_status(automatic, backup_schedule.MANUAL, "busy")
    status = automatic.data_dir / backup_schedule.STATUS
    assert (status.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in automatic.data_dir.glob(".backup-last-*")] == []  # no leftover
    check = check_of(automatic, db, database, ctx)
    assert check.run == backup_schedule.OUTCOMES["busy"] and check.run_at is not None
    backup_schedule.write_status(automatic, backup_schedule.MANUAL, "failed", "disk full")
    check = check_of(automatic, db, database, ctx)
    assert check.run is not None and check.run.params == {"error": "disk full"}
    (automatic.data_dir / backup_schedule.STATUS).write_text("not json")
    assert check_of(automatic, db, database, ctx).run is None


def test_a_time_that_is_not_one_still_backs_up_and_says_so(
    automatic: Settings,
    db: Session,
    database: Database,
    ctx: Workspace,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wrong = automatic.model_copy(update={"backup_time": "4h70"})
    instance.mark(wrong, instance.BACKUP_DONE)
    check = check_of(wrong, db, database, ctx)
    assert check.state == instance.WARN
    assert check.note is not None and check.note.params == {"value": "4h70"}
    config = write_config(
        automatic, tmp_path / "synaptane.toml", backup_dir=automatic.backup_dir, backup_time="4h70"
    )
    automatic.key_file.chmod(0o600)
    automatic.repos_dir.mkdir(exist_ok=True)
    main(["--config", str(config), "doctor"])
    out = capsys.readouterr().out
    assert '[warning] Automatic backup time — "4h70" is not a time: 04:00 is used' in out
    assert "[ok] Automatic backup: every day at 04:00" in out
