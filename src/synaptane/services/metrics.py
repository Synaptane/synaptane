"""What `/metrics` says about the instance, read at each scrape: accounts, spaces (sync, items
waiting in the inbox, last tidy-up), the worker's job queue, AI spending of the month, last
local backup, capture by email (if configured).

Spaces are named `organization/space`: a few series per space, and an instance has tens of
spaces, not thousands. Nothing here reads page content or names a person.
"""

from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane import __version__, git_commit
from synaptane.config import Settings
from synaptane.core.errors import SynaptaneError
from synaptane.core.metrics import Exposition, Labels
from synaptane.git.repo import GitError
from synaptane.services import budget, email_capture, instance, jobs, tidy
from synaptane.services.workspace import Workspace
from synaptane.storage.models import CaptureSettings, Job, Organization, Space, User

SYNC_STATUSES = ("never", "ok", "error", "conflict")


def _seconds(moment: datetime) -> float:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def write_info(out: Exposition) -> None:
    out.one(
        "synaptane_info",
        "gauge",
        "Version and commit of the running code.",
        1,
        version=__version__,
        commit=git_commit(),
    )


def write_instance(out: Exposition, session: Session, ws: Workspace) -> None:
    """Everything that comes from the database (and the spaces' clones)."""
    _accounts(out, session)
    _spaces(out, session, ws)
    _jobs(out, session)
    _spending(out, session)
    _capture(out, session)


def _accounts(out: Exposition, session: Session) -> None:
    rows = dict(
        session.execute(select(User.is_active, func.count()).group_by(User.is_active)).all()
    )
    out.add(
        "synaptane_users",
        "gauge",
        "Accounts, active or disabled.",
        [({"state": "active"}, rows.get(True, 0)), ({"state": "disabled"}, rows.get(False, 0))],
    )


def _spaces(out: Exposition, session: Session, ws: Workspace) -> None:
    spaces = list(
        session.scalars(select(Space).join(Organization).order_by(Organization.slug, Space.slug))
    )
    last_tidy = instance.last_tidies(session)
    status: list[tuple[Labels, float]] = []
    synced: list[tuple[Labels, float]] = []
    waiting: list[tuple[Labels, float]] = []
    tidied: list[tuple[Labels, float]] = []
    failed: list[tuple[Labels, float]] = []
    for space in spaces:
        name = {"space": space.address}
        status += [({**name, "status": s}, int(space.sync_status == s)) for s in SYNC_STATUSES]
        if space.synced_at is not None:
            synced.append((name, _seconds(space.synced_at)))
        if space.repo_url and space.sync_status != "never":
            # Clone missing or unreadable: no value rather than a wrong one.
            with suppress(SynaptaneError, GitError):
                waiting.append((name, tidy.fresh_count(session, ws, space)))
        run = last_tidy.get(space.id)
        if run is not None and run.finished_at is not None:
            tidied.append((name, _seconds(run.finished_at)))
            failed.append((name, int(run.status == "failed")))
    out.add(
        "synaptane_space_sync_status",
        "gauge",
        "Sync state of each space with its Git repository: 1 for the current one.",
        status,
    )
    out.add(
        "synaptane_space_last_sync_timestamp_seconds",
        "gauge",
        "Last successful sync of each space, in seconds since the Unix epoch.",
        synced,
    )
    out.add(
        "synaptane_space_inbox_items",
        "gauge",
        "Items waiting in each space's inbox for the tidy-up (notes and changes).",
        waiting,
    )
    out.add(
        "synaptane_space_last_tidy_timestamp_seconds",
        "gauge",
        "End of the last tidy-up pass of each space, in seconds since the Unix epoch.",
        tidied,
    )
    out.add(
        "synaptane_space_last_tidy_failed",
        "gauge",
        "1 if the last tidy-up pass of the space failed.",
        failed,
    )


def _jobs(out: Exposition, session: Session) -> None:
    now = datetime.now(UTC)
    rows = session.execute(
        select(Job.kind, Job.status, func.count())
        .where(Job.status.in_(("queued", "running")))
        .group_by(Job.kind, Job.status)
        .order_by(Job.kind, Job.status)
    ).all()
    out.add(
        "synaptane_jobs",
        "gauge",
        "Worker jobs queued or running, by kind.",
        [({"kind": kind, "status": status}, count) for kind, status, count in rows],
    )
    oldest = instance.oldest_due_job(session, now)
    out.one(
        "synaptane_jobs_waiting_seconds",
        "gauge",
        "Wait of the oldest job due to run: it grows when the worker is stopped or overwhelmed.",
        max((now - oldest).total_seconds(), 0) if oldest is not None else 0,
    )
    given_up: dict[str, int] = {}
    for job in jobs.abandoned(session, now - timedelta(hours=24)):
        given_up[job.kind] = given_up.get(job.kind, 0) + 1
    out.add(
        "synaptane_jobs_abandoned_24h",
        "gauge",
        "Jobs abandoned in the last 24 hours after using up their attempts, by kind.",
        [({"kind": kind}, count) for kind, count in sorted(given_up.items())],
    )


def _spending(out: Exposition, session: Session) -> None:
    organizations = list(session.scalars(select(Organization).order_by(Organization.slug)))
    out.add(
        "synaptane_ai_spend_month_euros",
        "gauge",
        "Estimated AI spending of each organization since the start of the month (UTC), in euros.",
        [
            ({"organization": org.slug}, float(budget.spent(session, org.id)))
            for org in organizations
        ],
    )


def _capture(out: Exposition, session: Session) -> None:
    """Capture by email: emails of the last 24 hours, and whether the mailbox is read."""
    row = session.get(CaptureSettings, 1)
    if row is None:
        return
    counts = email_capture.counts_since(session, datetime.now(UTC) - timedelta(hours=24))
    out.add(
        "synaptane_email_capture_messages_24h",
        "gauge",
        "Emails handled by the capture in the last 24 hours, by status and reason of refusal.",
        [
            ({"status": status, "reason": reason or "none"}, count)
            for status, reasons in sorted(counts.items())
            for reason, count in sorted(reasons.items())
        ],
    )
    out.one(
        "synaptane_email_capture_poll_failing",
        "gauge",
        "1 if the last poll of the capture mailbox failed.",
        int(bool(row.poll_error)),
    )
    if row.poll_ok_at is not None:
        out.one(
            "synaptane_email_capture_last_poll_timestamp_seconds",
            "gauge",
            "Last successful poll of the capture mailbox, in seconds since the Unix epoch.",
            _seconds(row.poll_ok_at),
        )


def write_backups(out: Exposition, settings: Settings) -> None:
    """Last verified archive of `synaptane backup`, wherever it was written."""
    last = instance.last_backup(settings)
    if last is not None:
        out.one(
            "synaptane_backup_last_timestamp_seconds",
            "gauge",
            "Last verified backup archive, in seconds since the Unix epoch.",
            last.timestamp(),
        )
