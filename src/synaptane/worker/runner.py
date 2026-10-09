"""`synaptane worker` process: runs the job queue and schedules the sync.

Four lanes, each with its share of the queue (LANES), so that no job waits behind another of
a different kind:
- fast (main thread, with maintenance): Git sync, resolution, subscription connection test,
  and any new kind of job;
- indexing: text extraction, OCR, embeddings;
- tidy-up: a pass with a subscription can last 45 minutes; it no longer delays the indexing
  of other spaces. Agent routines run there too (same cost, same slowness, and never at the
  same time as a tidy-up of the same space);
- webhooks: a slow or silent recipient no longer delays syncs.
Several workers can run: job claiming and the per-space lock prevent duplicates.
"""

import logging
import shutil
import signal
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from uuid import UUID

from sqlalchemy import or_, select

from synaptane.config import Settings
from synaptane.core.errors import SynaptaneError
from synaptane.git.repo import GitError
from synaptane.mcp import oauth
from synaptane.services import (
    ai_settings,
    alerts,
    changes,
    freshness,
    git_sync,
    jobs,
    routine_agent,
    routines,
    semantic,
    tidy,
    webhooks,
)
from synaptane.services.extraction import update_index
from synaptane.services.vocation import PENDING_SUFFIX
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, Space

log = logging.getLogger("synaptane.worker")
INDEX = "index"


HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "synaptane-worker.alive"
# The healthcheck file (`compose.yaml`: stale after 3 minutes) is only touched if every lane
# gave a sign of life less than ALIVE seconds ago, by looping over the queue or through the
# heartbeat of the job it runs (a first clone of more than 3 minutes no longer makes the
# worker "unhealthy"). A lane stuck outside a job does.
ALIVE = 150.0
TOUCH_EVERY = 30.0

# Dedicated lanes, each for one kind of job; all the others go through the fast lane.
LANES: dict[str, frozenset[str]] = {
    "index": frozenset({INDEX}),
    "tidy": frozenset({tidy.TIDY, routines.ROUTINE}),
    "webhook": frozenset({webhooks.WEBHOOK}),
}
DEDICATED: frozenset[str] = frozenset().union(*LANES.values())
# Wait for the dedicated lanes on shutdown, below Compose's `stop_grace_period` (60 s): beyond
# that, their jobs are returned to the queue and will resume on restart.
STOP_GRACE = 45.0


class Worker:
    def __init__(self, settings: Settings, database: Database, ctx: Workspace) -> None:
        self.settings = settings
        self.database = database
        self.ctx = ctx
        self.stopping = False
        self._last_maintenance = 0.0
        self._last_backlog_check = 0.0
        self._current: dict[str, tuple[UUID, int]] = {}  # lane → (current job, attempt)
        self._pulses: dict[str, float] = {}  # lane → last sign of life (monotonic clock)
        self._touched = 0.0
        self.handlers: dict[str, Callable[[Job], None]] = {
            git_sync.SYNC: self._sync,
            git_sync.RESOLVE: self._resolve,
            INDEX: self._index,
            tidy.TIDY: self._tidy,
            routines.ROUTINE: self._routine,
            webhooks.WEBHOOK: self._webhook,
            ai_settings.AI_TEST: self._ai_test,
        }

    # --- Jobs ---------------------------------------------------------------------------------

    def _sync(self, job: Job) -> None:
        assert job.space_id is not None
        git_sync.run_sync(self.database, self.ctx, job.space_id)
        with self.database.session() as session:
            jobs.enqueue(session, INDEX, job.space_id)

    def _webhook(self, job: Job) -> None:
        webhooks.deliver(
            self.database,
            self.ctx.box,
            job.payload,
            allow_private=self.settings.allow_private_webhooks,
            public_url=self.settings.public_url,
        )

    def _ai_test(self, job: Job) -> None:
        """Connection test of a subscription, requested from the AI page: the CLI and its
        login only exist in the worker. Bounded (`SUBSCRIPTION_TEST_TIMEOUT`), it goes
        through the fast lane."""
        ai_settings.run_test(self.database, self.ctx.box, UUID(str(job.payload["org_id"])))

    def _tidy(self, job: Job) -> None:
        """Inbox tidy-up by the AI agent (scheduled or requested)."""
        assert job.space_id is not None
        by = job.payload.get("by")
        tidy.run(
            self.database,
            self.ctx,
            job.space_id,
            trigger=str(job.payload.get("trigger", "manual")),
            requested_by=UUID(str(by)) if by else None,
            chain=int(str(job.payload.get("chain", 0))),
            public_url=self.settings.public_url,
        )

    def _routine(self, job: Job) -> None:
        """Agent routines requested in the space (scheduled or "Run now")."""
        assert job.space_id is not None
        routine_agent.run_pending(
            self.database, self.ctx, job.space_id, public_url=self.settings.public_url
        )

    def _index(self, job: Job) -> None:
        """Extracts the text of the space's new PDFs, Office documents and images."""
        assert job.space_id is not None
        with self.database.session() as session:
            space = session.get(Space, job.space_id)
            if space is None:
                return
            try:
                repo = self.ctx.local_repository(space)
            except SynaptaneError:
                return  # not synced yet: the sync will restart the indexing
        extracted, removed = update_index(self.ctx, job.space_id, repo)
        if extracted or removed:
            log.info("Index %s: %d extracted, %d removed.", job.space_id, extracted, removed)
        with self.database.session() as session:
            space = session.get(Space, job.space_id)
            config = semantic.config_for(session, self.ctx.box, space) if space else None
        if config is not None:
            embedded, dropped = semantic.index_space(
                self.database, self.ctx, job.space_id, repo, config
            )
            if embedded or dropped:
                log.info(
                    "Embeddings %s: %d file(s) embedded, %d removed.",
                    job.space_id,
                    embedded,
                    dropped,
                )

    def _resolve(self, job: Job) -> None:
        assert job.space_id is not None
        choices = job.payload.get("choices", {})
        assert isinstance(choices, dict)
        by = job.payload.get("by")
        git_sync.run_resolution(
            self.database, self.ctx, job.space_id, choices, UUID(str(by)) if by else None
        )
        with self.database.session() as session:
            jobs.enqueue(session, INDEX, job.space_id)

    def pulse(self, lane: str) -> None:
        """Sign of life of the lane `lane`. Touches the healthcheck file (at most every
        TOUCH_EVERY seconds) if every lane gave one recently."""
        now = time.monotonic()
        self._pulses[lane] = now
        if now - self._touched < TOUCH_EVERY:
            return
        if all(now - seen < ALIVE for seen in list(self._pulses.values())):
            self._touched = now
            HEARTBEAT_FILE.touch()

    @contextmanager
    def _heartbeat(self, job_id: UUID) -> Iterator[None]:
        """Renews the job lease while it runs, and gives a sign of life for the lane that
        runs it: a long job is not a stuck worker."""
        done = threading.Event()
        lane = threading.current_thread().name

        def beat() -> None:
            while not done.wait(jobs.HEARTBEAT.total_seconds()):
                self.pulse(lane)
                try:
                    with self.database.session() as session:
                        jobs.renew(session, job_id)
                except Exception as error:  # noqa: BLE001 — database unavailable: retry
                    log.warning("Lease of job %s not renewed: %s", job_id, error)

        thread = threading.Thread(target=beat, daemon=True, name="synaptane-heartbeat")
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join(timeout=5)

    def run_once(self, kinds: Collection[str] | None = None, exclude: Collection[str] = ()) -> bool:
        """Runs at most one job (among `kinds`, outside `exclude`). True if a job was
        processed."""
        with self.database.session() as session:
            job = jobs.claim(session, kinds, exclude)
            if job is None:
                return False
            session.expunge(job)
        handler = self.handlers.get(job.kind)
        started = time.monotonic()
        thread = threading.current_thread().name
        self._current[thread] = (job.id, job.attempts)
        try:
            if handler is None:
                raise RuntimeError(f"unknown job kind: {job.kind}")
            with self._heartbeat(job.id):
                handler(job)
        except Exception as error:  # noqa: BLE001 — a job must never kill the worker
            transient = bool(getattr(error, "transient", False))
            log.warning("Job %s (%s) failed: %s", job.kind, job.space_id, error)
            with self.database.session() as session:
                jobs.fail(session, job.id, str(error), retry=transient, attempt=job.attempts)
        else:
            with self.database.session() as session:
                jobs.complete(session, job.id, attempt=job.attempts)
            log.info(
                "Job %s (%s) done in %.1f s",
                job.kind,
                job.space_id or "instance",
                time.monotonic() - started,
            )
        finally:
            self._current.pop(thread, None)
        return True

    # --- Scheduling and maintenance -----------------------------------------------------------

    def schedule_polls(self) -> None:
        """Fallback sync of the spaces not synced for `poll_minutes`."""
        due = datetime.now(UTC) - timedelta(minutes=self.settings.poll_minutes)
        with self.database.session() as session:
            space_ids = session.scalars(
                select(Space.id).where(
                    Space.repo_url.is_not(None),
                    Space.sync_status != "conflict",
                    or_(Space.synced_at.is_(None), Space.synced_at < due),
                )
            ).all()
            for space_id in space_ids:
                jobs.enqueue(session, git_sync.SYNC, space_id)

    def remove_orphan_clones(self) -> None:
        """Deletes the local clones of deleted spaces, and their pending texts (purpose
        chosen at creation, see `vocation.pending_file`) after one hour: a space just
        created may not be committed to the database yet."""
        if not self.ctx.repos_dir.is_dir():
            return
        with self.database.session() as session:
            known = {str(i) for i in session.scalars(select(Space.id))}
        for path in self.ctx.repos_dir.iterdir():
            name = path.name.removesuffix(PENDING_SUFFIX)
            try:
                UUID(name)
            except ValueError:
                continue
            if name in known:
                continue
            if path.name.endswith(PENDING_SUFFIX):
                if time.time() - path.stat().st_mtime > 3600:
                    path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path, ignore_errors=True)

    def advance_idle_watermarks(self) -> None:
        """Without an active tidy-up (disabled, or organization without AI), nobody moves the
        change watermark: the history read again at each inbox computation would grow
        endlessly. It moves here as soon as nothing is pending: pages seen, Synaptane
        commits, whitespace-only or blank-line-only changes."""
        with self.database.session() as session:
            spaces = list(session.scalars(select(Space).where(Space.change_watermark.is_not(None))))
            for space in spaces:
                if tidy.active(session, self.ctx.box, space):
                    continue  # the tidy-up handles it at each pass
                try:
                    changes.compact(session, self.ctx, space)
                except (SynaptaneError, GitError) as error:
                    log.warning("Watermark of %s not moved: %s", space.id, error)

    def maintenance(self) -> None:
        now = time.monotonic()
        if now - self._last_maintenance < 60:
            return
        self._last_maintenance = now
        self.pulse(threading.current_thread().name)
        self.schedule_polls()
        tidy.schedule(self.database)
        tidy.close_orphans(self.database)
        routines.schedule(self.database)
        routine_agent.close_orphans(self.database)
        alerts.check_abandoned_jobs(self.database, self.ctx)  # attempts exhausted: notify
        if now - self._last_backlog_check > 3600:  # inbox backlog: once an hour
            self._last_backlog_check = now
            self.advance_idle_watermarks()
            alerts.check_backlogs(self.database, self.ctx)
            try:  # pages to review: one grouped email per week to their owner
                freshness.send_reminders(self.database, self.ctx)
            except Exception:  # noqa: BLE001 — a missed reminder must not stop the worker
                log.warning("Verification reminders not sent", exc_info=True)
        self.remove_orphan_clones()
        with self.database.session() as session:
            jobs.prune(session)
            oauth.purge_unused_clients(session)

    def run(self) -> None:
        def stop(signum: int, _frame: FrameType | None) -> None:
            log.info("Stop requested (signal %s): finishing the current job.", signum)
            self.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        log.info("Worker started.")
        # One thread per dedicated lane; all the other jobs (and maintenance) in the main
        # thread: a new kind of job is fast by default.
        lanes = [
            threading.Thread(
                target=self._loop, kwargs={"kinds": kinds}, name=f"synaptane-{lane}", daemon=True
            )
            for lane, kinds in LANES.items()
        ]
        for thread in lanes:
            thread.start()
        self._loop(exclude=DEDICATED, maintenance=True)
        deadline = time.monotonic() + STOP_GRACE
        for thread in lanes:
            thread.join(timeout=max(deadline - time.monotonic(), 0))
        if any(thread.is_alive() for thread in lanes):
            self._release_current()
        log.info("Worker stopped.")

    def _release_current(self) -> None:
        """Returns the jobs still running to the queue (the process is about to stop)."""
        for job_id, attempt in list(self._current.values()):
            log.info("Job %s returned to the queue: resumes on restart.", job_id)
            with self.database.session() as session:
                jobs.release(session, job_id, attempt)

    def _loop(
        self,
        kinds: Collection[str] | None = None,
        exclude: Collection[str] = (),
        maintenance: bool = False,
    ) -> None:
        lane = threading.current_thread().name
        while not self.stopping:
            self.pulse(lane)
            try:
                if maintenance:
                    self.maintenance()
                busy = self.run_once(kinds, exclude)
            except Exception:  # noqa: BLE001 — database unavailable, bug: the worker goes on
                log.exception("Error in the worker loop; retrying in 10 s.")

                time.sleep(10)
                continue
            if not busy:
                time.sleep(1)
