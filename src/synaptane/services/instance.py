"""The state of the instance, for its super admins (`/admin/health`): what works, what needs
attention, each space's sync and tidy-up, and the system alerts of the last days.

The worker and `synaptane backup` run apart from the web server: each leaves a mark in the data
folder, which both share, and that is how the web server sees them (`/metrics` too). Nothing
here reads page content.
"""

import os
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, joinedload

from synaptane.config import Settings
from synaptane.core.errors import Label, SynaptaneError
from synaptane.git.repo import GitError
from synaptane.services import alerts, jobs, mail, secrets, tidy
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database, pending_migrations
from synaptane.storage.models import AuditEvent, Job, Organization, Space, TidyRun

OK, WARN, ERROR = "ok", "warn", "error"

# Marks left in the data folder.
WORKER_ALIVE = ".worker-alive"
BACKUP_DONE = ".backup-done"
OFFSITE_DONE = ".offsite-done"

WORKER_SILENT = timedelta(minutes=10)  # the worker leaves its mark every 30 seconds
QUEUE_LATE = timedelta(minutes=15)
BACKUP_LATE = timedelta(hours=36)  # one every night, and a missed night is only a warning
BACKUP_LOST = timedelta(days=7)
DISK_LOW, DISK_FULL = 0.15, 0.05  # share of the disk still free
ALERT_DAYS = 30
ALERT_LIMIT = 50

ALERT_TITLES = {
    "deletion_cap": Label("A tidy-up stopped at the deletion limit"),
    "tidy_failed": Label("A tidy-up failed"),
    "sync_error": Label("Git sync failed"),
    "inbox_backlog": Label("The inbox is falling behind"),
    "quota_high": Label("Subscription quota nearly used up"),
    "big_changes": Label("A tidy-up made large changes"),
    "tidy_interrupted": Label("A tidy-up was interrupted"),
    "job_abandoned": Label("A background task was given up"),
    "routine_failed": Label("A routine failed"),
    "webhook_disabled": Label("A webhook was turned off"),
}


@dataclass(frozen=True)
class Check:
    title: Label
    state: str  # OK, WARN or ERROR
    message: Label
    at: datetime | None = None  # moment the message is about


@dataclass(frozen=True)
class SpaceState:
    space: Space
    state: str
    waiting: int | None  # None: unknown (clone missing or unreadable)
    last_tidy: TidyRun | None


@dataclass(frozen=True)
class Alert:
    at: datetime
    title: Label
    space: Space | None
    organization: Organization | None
    error: str


@dataclass(frozen=True)
class Report:
    checks: list[Check]
    spaces: list[SpaceState]
    alerts: list[Alert]

    @property
    def state(self) -> str:
        states = {check.state for check in self.checks} | {s.state for s in self.spaces}
        return ERROR if ERROR in states else WARN if WARN in states else OK


def mark(settings: Settings, name: str) -> None:
    """Leaves a mark; a failure here (disk full) must not stop what was being done."""
    with suppress(OSError):
        (settings.data_dir / name).touch()


def marked(settings: Settings, name: str) -> datetime | None:
    try:
        return datetime.fromtimestamp((settings.data_dir / name).stat().st_mtime, UTC)
    except OSError:
        return None


def last_backup(settings: Settings, folder: str | None = None) -> datetime | None:
    """Last archive: the mark of `synaptane backup`, or a file in the backup folder
    (SYNAPTANE_BACKUP_DIR, /backups in the image), for those written by an older version."""
    folder = folder if folder is not None else os.environ.get("SYNAPTANE_BACKUP_DIR", "")
    times = [found] if (found := marked(settings, BACKUP_DONE)) else []
    if folder:
        with suppress(OSError):
            times += [
                datetime.fromtimestamp(path.stat().st_mtime, UTC)
                for path in Path(folder).glob("synaptane-*.tar.gz*")
                if path.is_file()
            ]
    return max(times, default=None)


def last_tidies(session: Session) -> dict[object, TidyRun]:
    """Last finished tidy-up of each space."""
    return {
        run.space_id: run
        for run in session.scalars(
            select(TidyRun)
            .where(TidyRun.finished_at.is_not(None))
            .order_by(TidyRun.space_id, TidyRun.finished_at.desc())
            .ext(distinct_on(TidyRun.space_id))
        )
    }


def oldest_due_job(session: Session, now: datetime) -> datetime | None:
    return session.scalar(
        select(func.min(Job.run_after)).where(Job.status == "queued", Job.run_after <= now)
    )


def _database(database: Database) -> Check:
    if pending_migrations(database):
        return Check(
            Label("Database"), ERROR, Label("Migrations are waiting: run `synaptane db upgrade`.")
        )
    return Check(Label("Database"), OK, Label("Reachable, migrations up to date."))


def _key(session: Session, ws: Workspace) -> Check:
    if secrets.readable(session, ws.box) is False:
        return Check(
            Label("Master key"),
            ERROR,
            Label("It does not open the stored secrets: put the original key back from a backup."),
        )
    return Check(Label("Master key"), OK, Label("The stored secrets can be read."))


def _worker(settings: Settings, now: datetime) -> Check:
    seen = marked(settings, WORKER_ALIVE)
    if seen is None:
        return Check(
            Label("Worker"), ERROR, Label("No sign of life: sync and tidy-ups are not running.")
        )
    if now - seen > WORKER_SILENT:
        return Check(
            Label("Worker"),
            ERROR,
            Label("Silent: sync and tidy-ups are not running. Last sign of life:"),
            seen,
        )
    return Check(Label("Worker"), OK, Label("Running. Last sign of life:"), seen)


def _queue(session: Session, now: datetime) -> Check:
    title = Label("Background tasks")
    oldest = oldest_due_job(session, now)
    late = oldest is not None and now - oldest > QUEUE_LATE
    given_up = [
        job
        for job in jobs.abandoned(session, now - timedelta(hours=24))
        if job.kind not in alerts.ABANDON_IGNORED
    ]
    if late:
        return Check(title, WARN, Label("Falling behind. Oldest task waiting since:"), oldest)
    if given_up:
        return Check(
            title,
            WARN,
            Label("Tasks given up in the last 24 hours: {count}.", count=len(given_up)),
        )
    return Check(title, OK, Label("Nothing waiting for long."))


def _backup(settings: Settings, now: datetime) -> Check:
    title = Label("Backup")
    last = last_backup(settings)
    if last is None:
        return Check(title, ERROR, Label("None seen yet. See docs/admin/backup.md."))
    if now - last > BACKUP_LOST:
        return Check(title, ERROR, Label("Too old. Last verified archive:"), last)
    if now - last > BACKUP_LATE:
        return Check(title, WARN, Label("Late. Last verified archive:"), last)
    return Check(title, OK, Label("Last verified archive:"), last)


def _offsite(settings: Settings, now: datetime) -> Check:
    title = Label("Off-site copy")
    if not settings.backup_remotes:
        return Check(
            title, WARN, Label("Not set up: a backup on this server does not survive losing it.")
        )
    last = marked(settings, OFFSITE_DONE)
    if last is None:
        return Check(title, WARN, Label("None complete yet."))
    if now - last > BACKUP_LATE:
        return Check(title, WARN, Label("Late. Last complete copy:"), last)
    return Check(title, OK, Label("Last complete copy:"), last)


def _disk(settings: Settings) -> Check:
    title = Label("Disk")
    try:
        usage = shutil.disk_usage(settings.data_dir)
    except OSError:
        return Check(title, ERROR, Label("The data folder cannot be read."))
    free = usage.free / usage.total
    state = ERROR if free < DISK_FULL else WARN if free < DISK_LOW else OK
    return Check(
        title,
        state,
        Label(
            "{free} GB free of {total} GB.",
            free=usage.free // 1024**3,
            total=usage.total // 1024**3,
        ),
    )


def _mail(session: Session) -> Check:
    title = Label("Email sending")
    if mail.configured(session):
        return Check(title, OK, Label("Configured."))
    return Check(title, WARN, Label("Not configured: alerts and invitations cannot go out."))


def _https(settings: Settings) -> Check:
    title = Label("HTTPS")
    if settings.secure_cookies:
        return Check(title, OK, Label("The public address uses HTTPS."))
    return Check(title, WARN, Label("The public address does not use HTTPS."))


def _space_state(session: Session, ws: Workspace, space: Space, run: TidyRun | None) -> SpaceState:
    waiting = None
    if space.repo_url and space.sync_status != "never":
        with suppress(SynaptaneError, GitError):
            waiting = tidy.fresh_count(session, ws, space)
    failed = space.sync_status in ("error", "conflict") or (run and run.status == "failed")
    return SpaceState(space, ERROR if failed else OK, waiting, run)


def _alerts(session: Session, now: datetime) -> list[Alert]:
    events = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.action.startswith("alert."),
            AuditEvent.at > now - timedelta(days=ALERT_DAYS),
        )
        .order_by(AuditEvent.at.desc())
        .limit(ALERT_LIMIT)
    ).all()
    found = []
    for event in events:
        kind = event.action.removeprefix("alert.")
        space = session.get(Space, event.space_id) if event.space_id else None
        org = session.get(Organization, event.org_id) if event.org_id else None
        found.append(
            Alert(
                event.at,
                ALERT_TITLES.get(kind, Label(kind)),
                space,
                org,
                str(event.details.get("error") or event.details.get("reason") or "")[:300],
            )
        )
    return found


def report(session: Session, database: Database, ws: Workspace, settings: Settings) -> Report:
    now = datetime.now(UTC)
    tidied = last_tidies(session)
    spaces = session.scalars(
        select(Space)
        .join(Organization)
        .options(joinedload(Space.organization))
        .order_by(Organization.slug, Space.slug)
    ).all()
    states = [_space_state(session, ws, space, tidied.get(space.id)) for space in spaces]
    return Report(
        checks=[
            _database(database),
            _key(session, ws),
            _worker(settings, now),
            _queue(session, now),
            _backup(settings, now),
            _offsite(settings, now),
            _disk(settings),
            _mail(session),
            _https(settings),
        ],
        spaces=sorted(states, key=lambda s: s.state != ERROR),  # stable: names stay in order
        alerts=_alerts(session, now),
    )
