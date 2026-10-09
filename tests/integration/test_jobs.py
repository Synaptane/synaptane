"""Job queue: uniqueness of pending requests, recovery of interrupted jobs."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update

from synaptane.services import jobs
from synaptane.storage.db import Database
from synaptane.storage.models import Job
from tests.integration.wikidata import Wiki


def statuses(database: Database) -> list[tuple[str, str]]:
    with database.session() as session:
        return sorted((j.kind, j.status) for j in session.scalars(select(Job)))


def test_enqueue_keeps_one_pending_request_even_once_prepared(
    database: Database, wiki: Wiki
) -> None:
    # psycopg prepares the query after a few executions: the partial constraint must still
    # be recognized (regression: one enqueue in ten used to fail).
    with database.session() as session:
        for _ in range(30):
            jobs.enqueue(session, "sync", wiki.infra.id)
            jobs.enqueue(session, "index", wiki.infra.id)
    assert statuses(database) == [("index", "queued"), ("sync", "queued")]


def expire(database: Database) -> None:
    with database.session() as session:
        session.execute(
            update(Job)
            .where(Job.status == "running")
            .values(locked_until=jobs._now() - timedelta(seconds=1))
        )


def test_interrupted_job_with_a_waiting_twin_is_closed(database: Database, wiki: Wiki) -> None:
    with database.session() as session:
        jobs.enqueue(session, "sync", wiki.infra.id)
        first = jobs.claim(session)
        assert first is not None
        jobs.enqueue(session, "sync", wiki.infra.id)  # new request while the job runs
    expire(database)
    with database.session() as session:
        again = jobs.claim(session)  # regression: IntegrityError, worker stopped
        assert again is not None and again.id != first.id
    with database.session() as session:
        closed = session.get(Job, first.id)
        assert closed is not None and closed.status == "failed"
        assert "identical" in (closed.error or "")


def test_retry_with_a_waiting_twin_is_closed(database: Database, wiki: Wiki) -> None:
    with database.session() as session:
        jobs.enqueue(session, "sync", wiki.infra.id)
        job = jobs.claim(session)
        assert job is not None
        jobs.enqueue(session, "sync", wiki.infra.id)
        jobs.fail(session, job.id, "réseau", retry=True)
    assert statuses(database) == [("sync", "failed"), ("sync", "queued")]


def test_job_interrupted_too_often_is_abandoned(database: Database, wiki: Wiki) -> None:
    with database.session() as session:
        jobs.enqueue(session, "tidy", wiki.infra.id)
    for _ in range(jobs.MAX_ATTEMPTS):
        with database.session() as session:
            assert jobs.claim(session) is not None
        expire(database)
    with database.session() as session:
        assert jobs.claim(session) is None
    with database.session() as session:
        job = session.scalars(select(Job)).one()
        assert job.status == "failed" and "interrupted 5 times" in (job.error or "")


def test_heartbeat_extends_the_lease(database: Database, wiki: Wiki) -> None:
    with database.session() as session:
        jobs.enqueue(session, "sync", wiki.infra.id)
        job = jobs.claim(session)
        assert job is not None
        job_id = job.id
    expire(database)
    with database.session() as session:
        jobs.renew(session, job_id)
    with database.session() as session:
        assert jobs.claim(session) is None  # lease renewed: not taken over
        jobs.renew(session, uuid4())  # unknown job: no effect


def test_each_lane_only_takes_its_own_jobs(database: Database, wiki: Wiki) -> None:
    """Tidying, indexing and webhooks each have their own lane; the fast lane only takes the
    rest (syncs…)."""
    from synaptane.worker.runner import DEDICATED, LANES

    with database.session() as session:
        jobs.enqueue(session, "tidy", wiki.infra.id)
        jobs.enqueue(session, "index", wiki.infra.id)
        jobs.enqueue(session, "sync", wiki.infra.id)
        jobs.enqueue(session, "webhook", None, {})
    with database.session() as session:
        fast = [jobs.claim(session, exclude=DEDICATED) for _ in range(4)]
        taken = {
            lane: [jobs.claim(session, kinds=k) for _ in range(2)] for lane, k in LANES.items()
        }
    assert [j.kind for j in fast if j] == ["sync"]
    assert {lane: [j.kind for j in found if j] for lane, found in taken.items()} == {
        "index": ["index"],
        "tidy": ["tidy"],
        "webhook": ["webhook"],
    }


def test_a_worker_that_lost_its_lease_cannot_close_the_job(database: Database, wiki: Wiki) -> None:
    with database.session() as session:
        jobs.enqueue(session, "sync", wiki.infra.id)
        first = jobs.claim(session)
        assert first is not None
        job_id, first_attempt = first.id, first.attempts
    expire(database)
    with database.session() as session:
        second = jobs.claim(session)  # another worker takes over the job
        assert second is not None and second.id == job_id
    with database.session() as session:
        jobs.complete(session, job_id, attempt=first_attempt)  # the old one finishes: ignored
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.status == "running"
