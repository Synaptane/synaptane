"""Days of an instance replayed minute by minute, with the simulated time: the real decisions
of the worker (`schedule` every minute, then `_run` for a queued job) against the marks that
deploy/host/backup.sh and older versions leave. No database: queuing runs the job at once.

A. upgrade from 1.12 by its deploy.sh: its old backup.sh, with the old image, only leaves
   `.backup-done`, a few minutes before the new worker starts;
B. cron job of the new script: running, then marked once its archive is in place;
C. the cron job fails before reaching the container;
D. the cron job works again;
E. the archive went out of the container, but the host side failed (disk full there);
F. a cron run much longer than the day before (slow off-site copy, outside the lock);
G. the cron job fails, and another backup holds the lock when the worker takes over.
"""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from synaptane.config import Settings
from synaptane.services import backup, backup_schedule, instance

PARIS = ZoneInfo("Europe/Paris")


class NoDatabase:
    """`schedule` opens a session to queue: `_enqueue` is replaced, the session is unused."""

    @contextmanager
    def session(self) -> Iterator[None]:
        yield None


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 8, tzinfo=PARIS)


@pytest.fixture(autouse=True)
def paris_time() -> Iterator[None]:
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Paris"
    time.tzset()
    yield
    if before is None:
        os.environ.pop("TZ")
    else:
        os.environ["TZ"] = before
    time.tzset()


class Day:
    """The instance, its marks at the simulated time, and what the worker did."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = Clock()
        self.settings = Settings(
            database_url="postgresql+psycopg://x@127.0.0.1:1/x",
            data_dir=tmp_path / "data",
            backup_dir=tmp_path / "backups",
        )
        self.settings.data_dir.mkdir()
        self.log: list[str] = []
        clock = self.clock

        class Simulated(datetime):
            @classmethod
            def now(cls, tz: object = None) -> "Simulated":
                return clock.now.astimezone(tz)  # type: ignore[arg-type,return-value]

        monkeypatch.setattr(backup_schedule, "datetime", Simulated)
        monkeypatch.setattr(instance, "mark", lambda _s, name: self.mark(name))
        monkeypatch.setattr(backup_schedule, "_enqueue", self.enqueue)

        def write(settings: Settings, folder: Path) -> tuple[Path, list[Path]]:
            self.mark(instance.BACKUP_DONE)
            self.mark(backup_schedule.COMPLETE_DONE)
            return folder / backup_schedule.auto_name(), []

        monkeypatch.setattr(backup_schedule, "write_archive", write)

    def mark(self, name: str, *, remove: bool = False) -> None:
        path = self.settings.data_dir / name
        if remove:
            path.unlink(missing_ok=True)
            return
        path.touch()
        os.utime(path, (self.clock.now.timestamp(),) * 2)

    def enqueue(self, _session: object, trigger: str) -> bool:
        """The job runs at once, through the real checks of `_run`."""
        result = backup_schedule._run(self.settings, None, None, trigger, "")  # type: ignore[arg-type]
        what = f"skipped ({result.skipped})" if result.skipped else "backup"
        self.log.append(f"{self.clock.now:%d %H:%M} {what}")
        return True

    def at(self, day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 11, day, hour, minute, tzinfo=PARIS)

    def until(self, moment: datetime) -> list[str]:
        """Worker maintenance every minute until `moment`; what it did since the last call."""
        while self.clock.now < moment:
            backup_schedule.schedule(NoDatabase(), self.settings, self.clock.now)  # type: ignore[arg-type]
            self.clock.now += timedelta(minutes=1)
        done, self.log = self.log, []
        return done

    def cron(self, start: datetime, end: datetime, *, ok: bool = True) -> None:
        """deploy/host/backup.sh without a label, from `start` to `end`."""
        self.until(start)
        self.mark(backup_schedule.HOST_RUNNING)
        assert self.until(end) == []
        if ok:
            for name in (instance.BACKUP_DONE, backup_schedule.COMPLETE_DONE):
                self.mark(name)
            self.mark(backup_schedule.HOST_DONE)
        self.mark(backup_schedule.HOST_RUNNING, remove=True)


def test_a_week_with_the_cron_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    day = Day(tmp_path, monkeypatch)
    # A. 1.12: cron at 03:00 (old script, old image), then deploy.sh at 15:00 runs the old
    #    backup.sh, before its checkout: only `.backup-done`. The new worker starts at 15:02.
    day.clock.now = day.at(8, 3, 5)
    day.mark(instance.BACKUP_DONE)
    day.clock.now = day.at(8, 15, 0)
    day.mark(instance.BACKUP_DONE)
    day.clock.now = day.at(8, 15, 2)
    assert day.until(day.at(9, 0)) == []  # the deployment's backup is that of the day
    # B. the cron job, new script.
    day.cron(day.at(9, 3, 0), day.at(9, 3, 5))
    assert day.until(day.at(10, 0)) == []
    # C. it fails before reaching the container: the worker, 26 hours after its last one.
    assert day.until(day.at(11, 0)) == ["10 05:06 backup"]
    # D. it works again.
    day.cron(day.at(11, 3, 0), day.at(11, 3, 5))
    assert day.until(day.at(12, 0)) == []
    # E. the archive left the container, the host side failed: nothing marked, the worker
    #    takes over the same day (and the health page keeps the previous archive).
    last_good = instance.marked(day.settings, instance.BACKUP_DONE)
    day.cron(day.at(12, 3, 0), day.at(12, 3, 5), ok=False)
    assert instance.marked(day.settings, instance.BACKUP_DONE) == last_good
    assert day.until(day.at(13, 0)) == ["12 05:06 backup"]
    # F. a cron run of 5 hours: no double backup while it runs.
    day.cron(day.at(13, 3, 0), day.at(13, 8, 0))
    assert day.until(day.at(14, 0)) == []
    # G. it fails; at its relay time, another backup holds the lock: the worker waits for it,
    #    without giving up the day.
    day.until(day.at(14, 9, 59))
    with backup.exclusive(day.settings):
        assert day.until(day.at(14, 10, 30)) == []
    assert day.until(day.at(15, 0)) == ["14 10:30 backup"]


def test_a_new_instance_backs_up_at_once_and_then_every_night(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = Day(tmp_path, monkeypatch)
    day.clock.now = day.at(8, 15, 0)
    assert day.until(day.at(10, 0)) == ["08 15:00 backup", "09 04:00 backup"]


def test_a_run_mark_left_by_a_killed_script_does_not_hold_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = Day(tmp_path, monkeypatch)
    day.cron(day.at(8, 3, 0), day.at(8, 3, 5))
    day.until(day.at(9, 3, 0))
    day.mark(backup_schedule.HOST_RUNNING)  # killed: never removed
    found = day.until(day.at(10, 0))
    assert found == ["09 15:00 backup"]  # 12 hours after the start of the run


def test_a_job_that_finds_the_lock_taken_is_queued_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between queuing and running, another backup took the lock: the day is not done."""
    day = Day(tmp_path, monkeypatch)
    day.clock.now = day.at(8, 5, 0)
    day.mark(backup_schedule.SCHEDULED)
    with backup.exclusive(day.settings):
        result = backup_schedule._run(day.settings, None, None, "schedule", "")  # type: ignore[arg-type]
    assert result.skipped == "busy"
    assert not (day.settings.data_dir / backup_schedule.SCHEDULED).exists()
    assert day.until(day.at(8, 5, 2)) == ["08 05:00 backup"]
    manual = backup_schedule.MANUAL
    day.mark(backup_schedule.SCHEDULED)
    with backup.exclusive(day.settings):
        backup_schedule._run(day.settings, None, None, manual, "")  # type: ignore[arg-type]
    assert (day.settings.data_dir / backup_schedule.SCHEDULED).exists()  # a manual one: kept
