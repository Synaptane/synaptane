"""Worker loop: execution, failures, heartbeat, maintenance and shutdown."""

import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.git.repo import GitError
from synaptane.services import git_sync, jobs
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, Space
from synaptane.worker import runner
from synaptane.worker.runner import INDEX, Worker
from tests.integration.wikidata import Wiki


def only_job(database: Database) -> Job:
    with database.session() as session:
        job = session.scalars(select(Job)).one()
        session.expunge(job)
        return job


def test_unknown_or_failing_jobs_never_stop_the_worker(
    settings: Settings, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    worker = Worker(settings, database, ctx)
    assert worker.run_once() is False  # empty queue
    with database.session() as session:
        jobs.enqueue(session, "inconnue", wiki.infra.id)
    assert worker.run_once()
    job = only_job(database)
    assert job.status == "failed" and "unknown" in (job.error or "")

    def flaky(_: Job) -> None:
        raise GitError("Forge injoignable.", transient=True)  # transient: retried later

    def broken(_: Job) -> None:
        raise ValueError("bogue")  # permanent

    worker.handlers[INDEX] = flaky
    with database.session() as session:
        session.execute(delete(Job))
        jobs.enqueue(session, INDEX, wiki.infra.id)
    assert worker.run_once()
    job = only_job(database)
    assert job.status == "queued" and job.attempts == 1 and job.run_after > datetime.now(UTC)

    worker.handlers[INDEX] = broken
    with database.session() as session:
        session.get(Job, job.id).run_after = datetime.now(UTC)  # type: ignore[union-attr]
    assert worker.run_once()
    assert only_job(database).status == "failed"


def test_a_long_job_keeps_its_lease(
    settings: Settings,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs, "HEARTBEAT", timedelta(seconds=0.05))
    worker = Worker(settings, database, ctx)
    leases: list[datetime | None] = []

    def slow(job: Job) -> None:
        time.sleep(0.3)
        with database.session() as session:
            leases.append(session.get(Job, job.id).locked_until)  # type: ignore[union-attr]

    worker.handlers[INDEX] = slow
    with database.session() as session:
        jobs.enqueue(session, INDEX, wiki.infra.id)
    claimed_at = datetime.now(UTC)
    worker.run_once()
    assert leases[0] is not None and leases[0] > claimed_at + jobs.LEASE  # renewed
    assert only_job(database).status == "done"


def test_maintenance_schedules_polls_and_removes_orphan_clones(
    settings: Settings, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    with database.session() as session:
        for space in session.scalars(select(Space)):
            space.synced_at = datetime.now(UTC) - timedelta(hours=1)
        conflicted = session.get(Space, wiki.other.id)
        assert conflicted is not None
        conflicted.sync_status = "conflict"  # in conflict: no automatic sync
    orphan = ctx.repos_dir / "00000000-0000-0000-0000-000000000000"
    orphan.mkdir(parents=True)
    worker = Worker(settings, database, ctx)
    worker.maintenance()
    with database.session() as session:
        queued = {(j.kind, j.space_id) for j in session.scalars(select(Job))}
    assert (git_sync.SYNC, wiki.infra.id) in queued
    assert (git_sync.SYNC, wiki.other.id) not in queued
    assert not orphan.exists() and ctx.clone_path(wiki.infra.id).exists()
    assert runner.HEARTBEAT_FILE.exists()

    with database.session() as session:
        session.execute(delete(Job))
    worker.maintenance()  # less than a minute later: nothing
    with database.session() as session:
        assert session.scalars(select(Job)).all() == []


def test_on_stop_a_long_job_goes_back_to_the_queue(
    settings: Settings,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown does not leave a job "running" for the whole lease."""
    monkeypatch.setattr(runner, "STOP_GRACE", 0.5)
    worker = Worker(settings, database, ctx)
    started, release = threading.Event(), threading.Event()

    def endless(_: Job) -> None:
        started.set()
        release.wait(10)

    worker.handlers[INDEX] = endless
    worker.maintenance = lambda: None  # type: ignore[method-assign]
    with database.session() as session:
        jobs.enqueue(session, INDEX, wiki.infra.id)

    def stop_when_busy() -> None:
        started.wait(10)
        os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    threading.Thread(target=stop_when_busy, daemon=True).start()
    try:
        worker.run()
    finally:
        signal.signal(signal.SIGTERM, previous[0])
        signal.signal(signal.SIGINT, previous[1])
        release.set()
    for _ in range(100):  # the long job's thread finishes (log included)
        if not worker._current:
            break
        time.sleep(0.05)
    job = only_job(database)
    assert job.status == "queued" and job.attempts == 0 and job.locked_until is None
    assert "worker shutting down" in (job.error or "")


def test_a_job_lost_to_another_worker_is_not_released(db: Session, database: Database) -> None:
    with database.session() as session:
        jobs.enqueue(session, "x", None)
    with database.session() as session:
        job = jobs.claim(session)
        assert job is not None
        job_id, attempt = job.id, job.attempts
    with database.session() as session:  # lease lost: another worker took it over
        found = session.get(Job, job_id)
        assert found is not None
        found.attempts += 1
    with database.session() as session:
        jobs.release(session, job_id, attempt)
    assert only_job(database).status == "running"


def run_until(worker: Worker, done: threading.Event) -> None:
    """Run the worker (main thread and dedicated threads) until `done`, then stop it."""

    def stop_when_done() -> None:
        done.wait(10)
        os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    threading.Thread(target=stop_when_done, daemon=True).start()
    try:
        worker.run()
    finally:
        signal.signal(signal.SIGTERM, previous[0])
        signal.signal(signal.SIGINT, previous[1])


def test_a_long_tidy_or_a_slow_webhook_blocks_nothing_else(
    settings: Settings,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long tidying run does not block indexing, an unresponsive webhook does not block
    the sync."""
    monkeypatch.setattr(runner, "STOP_GRACE", 0.5)
    worker = Worker(settings, database, ctx)
    worker.maintenance = lambda: None  # type: ignore[method-assign]
    indexed, synced, finished = threading.Event(), threading.Event(), threading.Event()
    seen: dict[str, bool] = {}

    def long_tidy(_: Job) -> None:
        seen["index pendant le rangement"] = indexed.wait(5)

    def silent_webhook(_: Job) -> None:
        seen["synchronisation pendant le webhook"] = synced.wait(5)

    def index(_: Job) -> None:
        indexed.set()
        if synced.is_set():
            finished.set()

    def sync(_: Job) -> None:
        synced.set()
        if indexed.is_set():
            finished.set()

    worker.handlers.update(
        {"tidy": long_tidy, "webhook": silent_webhook, INDEX: index, git_sync.SYNC: sync}
    )
    with database.session() as session:
        jobs.enqueue(session, "tidy", wiki.infra.id)
        jobs.enqueue(session, "webhook", None, {})
    time.sleep(0.05)  # tidying and the webhook go first in the queue
    with database.session() as session:
        jobs.enqueue(session, INDEX, wiki.infra.id)
        jobs.enqueue(session, git_sync.SYNC, wiki.infra.id)
    run_until(worker, finished)
    for _ in range(100):
        if len(seen) == 2:
            break
        time.sleep(0.05)
    assert seen == {"index pendant le rangement": True, "synchronisation pendant le webhook": True}


def test_the_healthcheck_file_is_touched_during_a_long_job(
    settings: Settings,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A first clone longer than 3 minutes no longer makes the worker "unhealthy": the job's
    heartbeat touches the file."""
    alive = tmp_path / "alive"
    monkeypatch.setattr(runner, "HEARTBEAT_FILE", alive)
    monkeypatch.setattr(runner, "TOUCH_EVERY", 0.0)
    monkeypatch.setattr(jobs, "HEARTBEAT", timedelta(seconds=0.05))
    worker = Worker(settings, database, ctx)
    touched: list[bool] = []

    def long_clone(_: Job) -> None:
        time.sleep(0.3)
        touched.append(alive.exists())

    worker.handlers[git_sync.SYNC] = long_clone
    with database.session() as session:
        jobs.enqueue(session, git_sync.SYNC, wiki.infra.id)
    assert worker.run_once()
    assert touched == [True]  # touched during the job, not only between jobs


def test_a_stuck_lane_lets_the_healthcheck_file_age(
    settings: Settings,
    database: Database,
    ctx: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    alive = tmp_path / "alive"
    monkeypatch.setattr(runner, "HEARTBEAT_FILE", alive)
    worker = Worker(settings, database, ctx)
    worker._pulses["synaptane-tidy"] = time.monotonic() - runner.ALIVE - 1  # stuck outside a job
    worker.pulse("MainThread")
    assert not alive.exists()
    worker.pulse("synaptane-tidy")
    assert alive.exists()
