"""Automatic backups, made by the worker (docs/admin/backup.md).

Every day at `backup_time` (server time, `TZ`), the worker queues a `backup` job, which runs
in its own lane (a copy to a slow destination can last hours without delaying the sync):

1. a verified archive in `backup_dir` (`/backups` in the image), written to a hidden `.part`
   file and renamed only once read back: an incomplete archive never looks like a good one;
2. the encrypted off-site copy, when destinations are configured (`services.offsite`);
3. local retention: archives older than `backup_local_retention_days` are deleted, never the
   newest `KEEP_AT_LEAST` ones (a failing backup must not end with an empty folder).

Nothing to set up: the default settings make a backup every night. The settings of an
existing installation keep working:
- **one backup at a time**: the worker's, the host's cron job (`deploy/host/backup.sh`), a
  deployment or a manual run share the lock of `backup.exclusive`;
- **the host's cron job has precedence**: when `deploy/host/backup.sh` (run without a label,
  as in cron) has made a complete backup in the last `HOST_FRESH`, the automatic backup of
  the day is skipped, and it starts again by itself if the cron job stops. Same when any
  complete backup is less than `RECENT` old (for example the one of a deployment).

A failure is an alert to the super admins (`alerts.notify_instance`), by email and on
`/admin/health`: no cron email anymore to report it.
"""

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.config import Settings, parse_time
from synaptane.core.access import Action, require
from synaptane.core.errors import BackupError, invalid
from synaptane.core.security import SecretBox
from synaptane.services import Actor, alerts, audit, backup, instance, jobs, offsite
from synaptane.storage.db import Database
from synaptane.storage.models import Job

log = logging.getLogger("synaptane.backup")

BACKUP = "backup"  # job kind
SCHEDULE, MANUAL = "schedule", "manual"  # triggers
SCHEDULED = ".backup-scheduled"  # mark: day of the last automatic run queued
HOST_DONE = ".backup-host"  # mark: last complete backup of deploy/host/backup.sh (cron)
HOST_ENV = "SYNAPTANE_HOST_BACKUP"  # set to 1 by deploy/host/backup.sh without a label
HOST_FRESH = timedelta(hours=26)  # a daily cron job, give or take
RECENT = timedelta(hours=6)
KEEP_AT_LEAST = 3
ARCHIVES = "synaptane-*.tar.gz"
PART = ".synaptane-*.part"
MIN_FREE = 200 * 1024**2  # bytes, on top of twice the last archive


def enabled(settings: Settings) -> bool:
    return settings.backup_dir is not None and parse_time(settings.backup_time) is not None


def due(settings: Settings, now: datetime | None = None) -> bool:
    """True if the automatic backup of the day is to be queued: its time has passed, and it
    was not queued today. A worker started after the time (new installation, restart)
    queues it right away."""
    at = parse_time(settings.backup_time)
    if settings.backup_dir is None or at is None:
        return False
    local = (now or datetime.now(UTC)).astimezone()  # server time zone (TZ)
    if local.time() < at:
        return False
    last = instance.marked(settings, SCHEDULED)
    return last is None or last.astimezone().date() != local.date()


def pending(session: Session) -> bool:
    """A backup job waits or runs."""
    found = session.scalar(
        select(Job.id).where(Job.kind == BACKUP, Job.status.in_(("queued", "running"))).limit(1)
    )
    return found is not None


def schedule(database: Database, settings: Settings, now: datetime | None = None) -> bool:
    """Queues the automatic backup when it is due (worker maintenance). True if queued."""
    if not due(settings, now):
        return False
    instance.mark(settings, SCHEDULED)
    with database.session() as session:
        if pending(session):
            return False
        jobs.enqueue(session, BACKUP, None, {"trigger": SCHEDULE})
    return True


def request(session: Session, actor: Actor, settings: Settings) -> bool:
    """The "Back up now" button of `/admin/health` (super admins). False if a backup already
    waits or runs."""
    require(actor.access, Action.MANAGE_INSTANCE)
    if settings.backup_dir is None:
        raise invalid("No backup folder: set SYNAPTANE_BACKUP_DIR.")
    if pending(session):
        return False
    jobs.enqueue(session, BACKUP, None, {"trigger": MANUAL})
    audit(session, actor, "instance.backup_requested")
    return True


def host_in_charge(settings: Settings, now: datetime | None = None) -> bool:
    """The host's cron job (deploy/host/backup.sh) made a complete backup recently."""
    return _complete_since(settings, HOST_DONE, (now or datetime.now(UTC)) - HOST_FRESH)


def _complete_since(settings: Settings, mark: str, since: datetime) -> bool:
    """A backup (`mark`) since `since`, and its off-site copy if destinations exist."""
    done = instance.marked(settings, mark)
    if done is None or done < since:
        return False
    if not offsite.remotes(settings):
        return True
    copied = instance.marked(settings, instance.OFFSITE_DONE)
    return copied is not None and copied >= since


@dataclass
class Result:
    archive: Path | None = None
    skipped: str = ""  # "busy", "host" or "recent": nothing was done
    outcomes: list[offsite.Outcome] = field(default_factory=list)
    offsite_error: str = ""  # copy refused (setting missing) or encryption failed
    pruned: list[Path] = field(default_factory=list)

    @property
    def offsite_ok(self) -> bool:
        return not self.offsite_error and all(outcome.ok for outcome in self.outcomes)


def run(
    settings: Settings,
    database: Database,
    box: SecretBox,
    *,
    trigger: str = SCHEDULE,
    public_url: str = "",
) -> Result:
    """Archive, off-site copy, local retention. Raises BackupError if no archive could be made
    (an alert has gone out); an off-site failure only alerts: the archive is kept."""
    folder = settings.backup_dir
    if folder is None:
        raise BackupError("No backup folder: set SYNAPTANE_BACKUP_DIR (or backup_dir).")
    with backup.exclusive(settings) as acquired:
        if not acquired:
            log.info("Backup skipped: another one is running.")
            return Result(skipped="busy")
        now = datetime.now(UTC)
        if trigger == SCHEDULE:
            if host_in_charge(settings, now):
                log.info("Automatic backup skipped: the host's cron job made one.")
                return Result(skipped="host")
            if _complete_since(settings, instance.BACKUP_DONE, now - RECENT):
                log.info("Automatic backup skipped: a complete backup is less than 6 hours old.")
                return Result(skipped="recent")
        try:
            archive = write_archive(settings, folder)
        except (BackupError, OSError) as error:  # OSError: folder not writable, disk full
            _alert(database, box, public_url, f"Archive: {error}")
            raise
        result = Result(archive=archive)
        if offsite.remotes(settings):
            _offsite(settings, result)
            failed = [o for o in result.outcomes if not o.ok]
            if result.offsite_error or failed:
                problems = [result.offsite_error] if result.offsite_error else []
                problems += [f"{o.remote}: {o.error}" for o in failed]
                _alert(database, box, public_url, "Off-site copy: " + "; ".join(problems))
            else:
                instance.mark(settings, instance.OFFSITE_DONE)
        result.pruned = prune_local(folder, settings.backup_local_retention_days, keep=archive)
    copied = ""
    if offsite.remotes(settings):
        copied = ", off-site copy done" if result.offsite_ok else ", off-site copy FAILED"
    log.info(
        "Backup verified: %s%s, %d old one(s) deleted.", archive.name, copied, len(result.pruned)
    )
    return result


def write_archive(settings: Settings, folder: Path) -> Path:
    """A verified archive in `folder`, under its final name only once read back."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for stale in folder.glob(PART):  # left by an interrupted backup (the lock is ours)
        stale.unlink(missing_ok=True)
    check_space(folder)
    name = backup.default_name()
    part = folder / f".{name}.part"
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            backup.backup(settings, out)
        backup.verify(settings, part)
        archive = folder / name
        part.rename(archive)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    instance.mark(settings, instance.BACKUP_DONE)
    return archive


def latest(folder: Path) -> Path | None:
    try:
        found = sorted(p for p in folder.glob(ARCHIVES) if p.is_file())
    except OSError:
        return None
    return found[-1] if found else None


def space_needed(folder: Path) -> int:
    """Free space to have before a backup: twice the last archive, and a margin."""
    last = latest(folder)
    try:
        size = last.stat().st_size if last else 0
    except OSError:
        size = 0
    return 2 * size + MIN_FREE


def check_space(folder: Path) -> None:
    free = shutil.disk_usage(folder).free
    needed = space_needed(folder)
    if free < needed:
        raise BackupError(
            f"Not enough free space in {folder}: {free // 1024**2} MB free, "
            f"{needed // 1024**2} MB needed (twice the last archive). Free some space, or "
            "lower SYNAPTANE_BACKUP_LOCAL_RETENTION_DAYS."
        )


def _offsite(settings: Settings, result: Result) -> None:
    assert result.archive is not None
    try:
        offsite.require_ready(settings)
    except BackupError as error:
        result.offsite_error = str(error)
        return
    with tempfile.TemporaryDirectory(prefix="offsite-", dir=settings.data_dir) as scratch:
        try:
            result.outcomes = offsite.copy(settings, result.archive, Path(scratch))
        except BackupError as error:
            result.offsite_error = str(error)


def prune_local(folder: Path, days: int, *, keep: Path | None = None) -> list[Path]:
    """Deletes the archives of `folder` older than `days` days (0: none), except `keep` and
    the newest KEEP_AT_LEAST. Only `synaptane-*.tar.gz` files at its root are concerned."""
    if not days:
        return []
    archives = sorted(p for p in folder.glob(ARCHIVES) if p.is_file() and not p.is_symlink())
    limit = datetime.now(UTC).timestamp() - days * 86400
    removed = []
    for path in archives[:-KEEP_AT_LEAST]:
        if path == keep:
            continue
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def _alert(database: Database, box: SecretBox, public_url: str, error: str) -> None:
    log.warning("Backup: %s", error)
    try:
        with database.session() as session:
            alerts.notify_instance(
                session, box, kind="backup_failed", public_url=public_url, error=error[:500]
            )
    except Exception:  # noqa: BLE001 — the alert must not hide the backup's own error
        log.warning("Backup alert not recorded", exc_info=True)
