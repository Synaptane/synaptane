"""Automatic backups, made by the worker (docs/admin/backup.md).

Every day at `backup_time` (server time, `TZ`), the worker queues a `backup` job, which runs
in its own lane (a copy to a slow destination can last hours without delaying the sync):

1. local retention first, so that a full disk can recover: the worker's archives older than
   `backup_local_retention_days` are deleted, never the newest `KEEP_AT_LEAST` ones. Only its
   own archives (`synaptane-YYYY-MM-DD-HHMMSS-auto.tar.gz`): a file put in the folder by hand
   is never touched;
2. a verified archive in `backup_dir` (`/backups` in the image), if the disk keeps
   `reserve()` free afterwards, written to a hidden `.part` file and renamed only once read
   back: an incomplete archive never looks like a good one;
3. the encrypted off-site copy, when destinations are configured (`services.offsite`). The
   lock is released before: a copy of several hours does not hold up other backups.

Nothing to set up: the default settings make a backup every night. The settings of an
existing installation keep working:
- **one backup at a time**: the worker's, the host's cron job (`deploy/host/backup.sh`), a
  deployment or a manual run share the lock of `backup.exclusive`;
- **the host's cron job has precedence**: when `deploy/host/backup.sh` (run without a label,
  as in cron) has made a complete backup in the last `HOST_FRESH`, the worker makes none. It
  looks again every minute: as soon as the cron job's last backup is older than `HOST_FRESH`
  (the cron job stopped, or failed one night), the automatic backup is queued. While the cron
  job is in charge, the worker's own archives older than the retention are deleted (the cron
  job keeps its own in `~/synaptane-backups`). A complete backup less than `RECENT` old (a
  deployment's, for example) counts as the backup of the day.

A failure, whatever its cause, is an alert to the super admins (`alerts.notify_instance`), by
email and on `/admin/health`, which also shows the last run: no cron email anymore to report
it.
"""

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from synaptane.config import Settings, parse_time
from synaptane.core.access import Action, require
from synaptane.core.errors import BackupError, Label, invalid
from synaptane.core.security import SecretBox
from synaptane.services import Actor, alerts, audit, backup, instance, jobs, offsite
from synaptane.storage.db import Database
from synaptane.storage.models import Job

log = logging.getLogger("synaptane.backup")

BACKUP = "backup"  # job kind
SCHEDULE, MANUAL = "schedule", "manual"  # triggers
DEFAULT_TIME = time(4, 0)  # when `backup_time` cannot be read
SCHEDULED = ".backup-scheduled"  # mark: day of the last automatic run queued
HOST_DONE = ".backup-host"  # mark, touched by deploy/host/backup.sh once its archive is good
# mark: last full backup kept (not --without-…, not /tmp); also touched by backup.sh, with or
# without a label, so that a deployment made with an older image counts.
COMPLETE_DONE = ".backup-complete"
STATUS = ".backup-last.json"  # outcome of the worker's last run, for the health page
HOST_FRESH = timedelta(hours=26)  # a daily cron job, give or take
RECENT = timedelta(hours=6)
KEEP_AT_LEAST = 3
SUFFIX = "-auto.tar.gz"
AUTO = re.compile(r"^synaptane-\d{4}-\d{2}-\d{2}-\d{6}-auto\.tar\.gz$")
PART = ".synaptane-*.part"
RESERVE = 2 * 1024**3  # bytes left free after a backup (at most 10 % of the disk)
GROWTH = 1.2  # the next archive, compared with the last one
# pg_advisory_xact_lock: several workers queue a single backup.
QUEUE_LOCK = 0x5359_4E42_4B55_5000


# --- Time of the day --------------------------------------------------------------------------


def at(settings: Settings) -> time | None:
    """Time of the automatic backup; None when off. A time that cannot be read gives 04:00
    (see `time_invalid`): a typo must not leave an instance without backup."""
    if settings.backup_time == "off":
        return None
    return parse_time(settings.backup_time) or DEFAULT_TIME


def time_invalid(settings: Settings) -> bool:
    return settings.backup_time != "off" and parse_time(settings.backup_time) is None


def time_text(settings: Settings) -> str:
    found = at(settings)
    return found.strftime("%H:%M") if found else "off"


def enabled(settings: Settings) -> bool:
    return settings.backup_dir is not None and at(settings) is not None


def due(settings: Settings, now: datetime | None = None) -> bool:
    """True if the automatic backup of the day is to be queued: its time has passed, and it
    was not queued today. A worker started after the time (new installation, restart)
    queues it right away."""
    moment = at(settings)
    if settings.backup_dir is None or moment is None:
        return False
    local = (now or datetime.now(UTC)).astimezone()  # server time zone (TZ)
    if local.time() < moment:
        return False
    last = instance.marked(settings, SCHEDULED)
    return last is None or last.astimezone().date() != local.date()


# --- Queue ------------------------------------------------------------------------------------


def latest_job(session: Session) -> Job | None:
    return session.scalar(select(Job).where(Job.kind == BACKUP).order_by(Job.created_at.desc()))


def pending(session: Session) -> bool:
    """A backup job waits or runs."""
    found = session.scalar(
        select(Job.id).where(Job.kind == BACKUP, Job.status.in_(("queued", "running"))).limit(1)
    )
    return found is not None


def _enqueue(session: Session, trigger: str) -> bool:
    """Queues a backup unless one waits or runs. Atomic: the jobs index does not deduplicate
    instance jobs (no space), so the check and the insert are made under a lock."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": QUEUE_LOCK})
    if pending(session):
        return False
    jobs.enqueue(session, BACKUP, None, {"trigger": trigger})
    return True


def schedule(database: Database, settings: Settings, now: datetime | None = None) -> bool:
    """Queues the automatic backup when it is due (worker maintenance, every minute). True if
    queued. While the host's cron job is in charge, the day is not marked: it is looked at
    again on the next run, and queued as soon as the cron job's backup is too old. A complete
    backup less than `RECENT` old (a deployment's) is the backup of the day."""
    if not due(settings, now):
        return False
    moment = now or datetime.now(UTC)
    folder = settings.backup_dir
    assert folder is not None
    if host_in_charge(settings, moment):
        # The cron job makes the backups: the worker's archives older than the retention go,
        # the newest ones too (the cron job keeps its own).
        prune_local(folder, settings.backup_local_retention_days, keep_at_least=0)
        return False
    if _complete_since(settings, COMPLETE_DONE, moment - RECENT):
        # A deployment's backup, for example: it is the backup of the day.
        instance.mark(settings, SCHEDULED)
        return False
    with database.session() as session:
        queued = _enqueue(session, SCHEDULE)
    instance.mark(settings, SCHEDULED)  # queued, or one already waits or runs
    return queued


def request(session: Session, actor: Actor, settings: Settings) -> bool:
    """The "Back up now" button of `/admin/health` (super admins). False if a backup already
    waits or runs."""
    require(actor.access, Action.MANAGE_INSTANCE)
    if settings.backup_dir is None:
        raise invalid("No backup folder: set SYNAPTANE_BACKUP_DIR.")
    if not _enqueue(session, MANUAL):
        return False
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


# --- The backup -------------------------------------------------------------------------------


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
    """Retention, archive, off-site copy. Raises if no archive could be made; an off-site
    failure keeps the archive. Every failure, whatever its cause, alerts the super admins,
    and the outcome is kept for the health page."""
    try:
        result = _run(settings, database, box, trigger, public_url)
    except Exception as error:
        message = str(error) if isinstance(error, BackupError | OSError) else repr(error)
        write_status(settings, trigger, "failed", message)
        _alert(database, box, public_url, message)
        raise
    if result.skipped:
        write_status(settings, trigger, "busy" if result.skipped == "busy" else "skipped")
    elif offsite.remotes(settings) and not result.offsite_ok:
        write_status(settings, trigger, "offsite")
    else:
        write_status(settings, trigger, "done")
    return result


def _run(
    settings: Settings, database: Database, box: SecretBox, trigger: str, public_url: str
) -> Result:
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
            if _complete_since(settings, COMPLETE_DONE, now - RECENT):
                log.info("Automatic backup skipped: a complete backup is less than 6 hours old.")
                return Result(skipped="recent")
        try:
            archive, pruned = write_archive(settings, folder)
        except (BackupError, OSError) as error:  # OSError: folder not writable, disk full
            raise BackupError(f"Archive: {error}") from error
    # Lock released: the archive is ours (only the newest ones, kept, are copied).
    result = Result(archive=archive, pruned=pruned)
    if offsite.remotes(settings):
        _offsite(settings, result)
        failed = [o for o in result.outcomes if not o.ok]
        if result.offsite_error or failed:
            problems = [result.offsite_error] if result.offsite_error else []
            problems += [f"{o.remote}: {o.error}" for o in failed]
            _alert(database, box, public_url, "Off-site copy: " + "; ".join(problems))
        else:
            instance.mark(settings, instance.OFFSITE_DONE)
    copied = ""
    if offsite.remotes(settings):
        copied = ", off-site copy done" if result.offsite_ok else ", off-site copy FAILED"
    log.info(
        "Backup verified: %s%s, %d old one(s) deleted.", archive.name, copied, len(result.pruned)
    )
    return result


def auto_name() -> str:
    return backup.default_name().removesuffix(".tar.gz") + SUFFIX


def write_archive(settings: Settings, folder: Path) -> tuple[Path, list[Path]]:
    """Retention, then a verified archive in `folder`, under its final name only once read
    back. Returns the archive and the old archives deleted."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for stale in folder.glob(PART):  # left by an interrupted backup (the lock is ours)
        stale.unlink(missing_ok=True)
    # Before the space check: a full disk recovers once the retention is lowered.
    pruned = prune_local(folder, settings.backup_local_retention_days)
    check_space(folder)
    name = auto_name()
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
    instance.mark(settings, COMPLETE_DONE)
    return archive, pruned


def worker_archives(folder: Path) -> list[Path]:
    """The worker's archives, oldest first (their names start with the date)."""
    try:
        found = [p for p in folder.iterdir() if AUTO.match(p.name) and p.is_file()]
    except OSError:
        return []
    return sorted((p for p in found if not p.is_symlink()), key=lambda p: p.name)


def _last_size(folder: Path) -> int:
    """Size of the last archive: the worker's, otherwise the most recent one of the folder."""
    candidates = worker_archives(folder)
    if not candidates:
        try:
            candidates = sorted(
                (p for p in folder.glob("synaptane-*.tar.gz") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            candidates = []
    try:
        return candidates[-1].stat().st_size if candidates else 0
    except OSError:
        return 0


def reserve(total: int) -> int:
    """Space left free after a backup, for PostgreSQL on the same disk: 2 GB, at most a tenth
    of a small disk (enough for a day of writes; refusing on a large disk with plenty of room
    would leave it without backup)."""
    return min(RESERVE, total // 10)


def space_needed(folder: Path, total: int) -> int:
    """Free space to have before a backup: the next archive (the last one, plus 20 %) and the
    reserve."""
    return int(_last_size(folder) * GROWTH) + reserve(total)


def check_space(folder: Path) -> None:
    usage = shutil.disk_usage(folder)
    needed = space_needed(folder, usage.total)
    if usage.free < needed:
        raise BackupError(
            f"Not enough free space in {folder}: {usage.free // 1024**2} MB free, "
            f"{needed // 1024**2} MB needed (the next archive, and "
            f"{reserve(usage.total) // 1024**2} MB left for the database). Free some space, "
            "or lower SYNAPTANE_BACKUP_LOCAL_RETENTION_DAYS."
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


def prune_local(
    folder: Path,
    days: int,
    *,
    keep: Path | None = None,
    keep_at_least: int = KEEP_AT_LEAST,
) -> list[Path]:
    """Deletes the worker's archives of `folder` older than `days` days (0: none), except
    `keep` and the newest `keep_at_least`. Archives put there by hand or by `synaptane backup`
    are never touched."""
    if not days:
        return []
    archives = worker_archives(folder)
    candidates = archives[:-keep_at_least] if keep_at_least else archives
    limit = datetime.now(UTC).timestamp() - days * 86400
    removed = []
    for path in candidates:
        if path == keep:
            continue
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


# --- What the health page shows ---------------------------------------------------------------


def write_status(settings: Settings, trigger: str, outcome: str, error: str = "") -> None:
    """Outcome of the worker's last run: done, offsite (archive made, copy failed), busy,
    skipped or failed."""
    record = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "trigger": trigger,
        "outcome": outcome,
        "error": error[:300],
    }
    try:
        path = settings.data_dir / STATUS
        path.with_suffix(".new").write_text(json.dumps(record), encoding="utf-8")
        path.with_suffix(".new").replace(path)
    except OSError:
        log.warning("Outcome of the backup not recorded", exc_info=True)


def last_status(settings: Settings) -> dict[str, Any] | None:
    try:
        found = json.loads((settings.data_dir / STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


OUTCOMES = {
    "done": Label("Last run of the worker: archive verified."),
    "offsite": Label("Last run of the worker: archive verified, off-site copy failed."),
    "busy": Label("Last run of the worker: skipped, another backup was running."),
    "skipped": Label("Last run of the worker: skipped, a backup had just been made."),
    "failed": Label("Last run of the worker: failed."),
}


def run_state(session: Session, settings: Settings) -> tuple[Label, datetime | None] | None:
    """The worker's backup: waiting, in progress, or the outcome of its last run."""
    job = latest_job(session)
    if job is not None and job.status == "queued":
        return Label("Backup by the worker: waiting to start. Requested:"), job.created_at
    if job is not None and job.status == "running":
        return Label("Backup by the worker: in progress. Requested:"), job.created_at
    status = last_status(settings)
    if status is None or status.get("outcome") not in OUTCOMES:
        return None
    try:
        moment: datetime | None = datetime.fromisoformat(str(status.get("at")))
    except ValueError:
        moment = None
    label = OUTCOMES[str(status["outcome"])]
    if status["outcome"] == "failed" and status.get("error"):
        label = Label("Last run of the worker: failed ({error}).", error=str(status["error"]))
    return label, moment


# --- Alerts -----------------------------------------------------------------------------------


def check_abandoned(database: Database, box: SecretBox, public_url: str = "") -> bool:
    """A backup job given up after its attempts (worker killed each time, out of memory for
    example) alerts like a failed backup: `alerts.check_abandoned_jobs` only sees the jobs of
    a space."""
    since = datetime.now(UTC) - alerts.ABANDON_WINDOW
    with database.session() as session:
        job = session.scalar(
            select(Job).where(
                Job.kind == BACKUP,
                Job.status == "failed",
                Job.finished_at >= since,
                Job.error.startswith(jobs.ABANDONED),
            )
        )
        if job is None:
            return False
        return alerts.notify_instance(
            session,
            box,
            kind="backup_failed",
            public_url=public_url,
            error=(job.error or "?")[:500],
        )


def _alert(database: Database, box: SecretBox, public_url: str, error: str) -> None:
    log.warning("Backup: %s", error)
    try:
        with database.session() as session:
            alerts.notify_instance(
                session, box, kind="backup_failed", public_url=public_url, error=error[:500]
            )
    except Exception:  # noqa: BLE001 — the alert must not hide the backup's own error
        log.warning("Backup alert not recorded", exc_info=True)
