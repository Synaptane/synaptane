"""PostgreSQL job queue.

- At most one queued job per (kind, space): repeated requests merge into one.
- Claimed with `FOR UPDATE SKIP LOCKED` and a short lease, renewed by the worker while the
  job runs: the job of a dead worker starts again after LEASE, 5 times at most.
- Transient failure (network): retried with growing delays, 5 attempts at most. A job that
  uses up its attempts is "abandoned": the worker's maintenance warns the admins
  (`alerts.check_abandoned_jobs`), so a lasting outage does not stay silent.
- A manual request can replace the payload of a queued request (`replace_payload`).
- A job reclaimed or retried while an identical request is already queued is closed: the
  queued request will do the work.
"""

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from synaptane.storage.models import Job

LEASE = timedelta(minutes=5)
HEARTBEAT = timedelta(minutes=1)
MAX_ATTEMPTS = 5
BACKOFF = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(hours=1))
ABANDONED = "Abandoned"  # start of the error of a job that used up its attempts
_ABANDONED_FR = "Abandonnée"  # same, written before the code was translated


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue(
    session: Session,
    kind: str,
    space_id: UUID | None,
    payload: dict[str, object] | None = None,
    *,
    delay: timedelta | None = None,
    replace: bool = False,
) -> None:
    """Adds a job; no effect if the same one is already queued (payload kept).

    `replace`: the queued request takes this payload and starts from scratch (attempts,
    delay); `delay`: the job does not start before this delay."""
    statement = insert(Job).values(
        kind=kind,
        space_id=space_id,
        payload=payload or {},
        run_after=_now() + (delay or timedelta()),
    )
    # Literal condition: a bound parameter keeps PostgreSQL from recognising the partial
    # index as soon as psycopg prepares the statement (after a few executions).
    where = text("status = 'queued'")
    if replace:
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["kind", "space_id"],
                index_where=where,
                set_={
                    "payload": statement.excluded.payload,
                    "run_after": statement.excluded.run_after,
                    "attempts": 0,
                    "error": None,
                },
            )
        )
    else:
        session.execute(
            statement.on_conflict_do_nothing(index_elements=["kind", "space_id"], index_where=where)
        )


def replace_payload(
    session: Session, kind: str, space_id: UUID, payload: dict[str, object]
) -> None:
    """Like `enqueue`, but the most recent payload wins (e.g. conflict choices, a tidy-up
    requested by hand while a scheduled run is queued)."""
    enqueue(session, kind, space_id, payload, replace=True)


def _has_queued_twin(session: Session, job: Job) -> bool:
    if job.space_id is None:  # no space (webhooks): no uniqueness
        return False
    twin = session.scalar(
        select(Job.id)
        .where(
            Job.kind == job.kind,
            Job.space_id == job.space_id,
            Job.status == "queued",
            Job.id != job.id,
        )
        .limit(1)
    )
    return twin is not None


def _close(job: Job, status: str, error: str | None) -> None:
    job.status, job.finished_at, job.locked_until = status, _now(), None
    if error:
        job.error = error


def _reclaim_expired(session: Session, now: datetime) -> None:
    """Jobs whose worker vanished: queued again, or closed."""
    expired = session.scalars(
        select(Job)
        .where(Job.status == "running", Job.locked_until < now)
        .with_for_update(skip_locked=True)
    ).all()
    for job in expired:
        if _has_queued_twin(session, job):
            _close(job, "failed", "Interrupted; an identical request is already queued.")
        elif job.attempts >= MAX_ATTEMPTS:
            _close(job, "failed", f"{ABANDONED}: interrupted {job.attempts} times.")
        else:
            job.status, job.run_after, job.locked_until = "queued", now, None
        session.flush()  # before the next one: two expired twins must not collide


def claim(
    session: Session,
    kinds: Collection[str] | None = None,
    exclude: Collection[str] = (),
) -> Job | None:
    """Claims the next due job: among `kinds` if given, outside `exclude`."""
    now = _now()
    _reclaim_expired(session, now)
    due = [Job.status == "queued", Job.run_after <= now]
    if kinds is not None:
        due.append(Job.kind.in_(list(kinds)))
    if exclude:
        due.append(Job.kind.not_in(list(exclude)))
    job = session.scalar(
        select(Job).where(*due).order_by(Job.run_after).limit(1).with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status, job.attempts, job.locked_until = "running", job.attempts + 1, now + LEASE
    session.flush()
    return job


def renew(session: Session, job_id: UUID) -> None:
    """Extends the lease of a running job (worker heartbeat)."""
    session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running")
        .values(locked_until=_now() + LEASE)
        .execution_options(synchronize_session=False)
    )


def _still_ours(job: Job | None, attempt: int | None) -> bool:
    """False if another worker took the job over in the meantime (lease lost)."""
    return job is not None and (
        attempt is None or (job.status == "running" and job.attempts == attempt)
    )


def complete(session: Session, job_id: UUID, attempt: int | None = None) -> None:
    job = session.get(Job, job_id)
    if job is not None and _still_ours(job, attempt):
        job.status, job.finished_at, job.error, job.locked_until = "done", _now(), None, None


def fail(
    session: Session, job_id: UUID, error: str, *, retry: bool, attempt: int | None = None
) -> None:
    job = session.get(Job, job_id)
    if job is None or not _still_ours(job, attempt):
        return
    job.error, job.locked_until = error[:4000], None
    if retry and _has_queued_twin(session, job):
        job.status, job.finished_at = "failed", _now()  # the queued request will do the work
    elif retry and job.attempts < MAX_ATTEMPTS:
        job.status = "queued"
        job.run_after = _now() + BACKOFF[min(job.attempts, len(BACKOFF)) - 1]
    else:
        if retry:  # lasting transient error: abandoned, reported to the admins
            job.error = f"{ABANDONED} after {job.attempts} attempts: {error}"[:4000]
        job.status, job.finished_at = "failed", _now()


def abandoned(session: Session, since: datetime) -> list[Job]:
    """Space jobs abandoned since `since`, attempts used up (see ABANDONED)."""
    return list(
        session.scalars(
            select(Job).where(
                Job.status == "failed",
                Job.space_id.is_not(None),
                Job.finished_at >= since,
                or_(Job.error.startswith(ABANDONED), Job.error.startswith(_ABANDONED_FR)),
            )
        )
    )


def release(session: Session, job_id: UUID, attempt: int | None = None) -> None:
    """Puts back in the queue a job interrupted by the worker shutting down, without counting
    the attempt: it starts again on restart rather than after the lease expires."""
    job = session.get(Job, job_id)
    if job is None or not _still_ours(job, attempt):
        return
    job.status, job.run_after, job.locked_until = "queued", _now(), None
    job.attempts = max(job.attempts - 1, 0)
    job.error = "Interrupted by the worker shutting down; resumed on restart."


def prune(session: Session, older_than: timedelta = timedelta(days=7)) -> None:
    session.execute(
        delete(Job).where(Job.status.in_(("done", "failed")), Job.finished_at < _now() - older_than)
    )
