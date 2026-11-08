"""Automatic backup without a database: settings, time of the day, local retention, lock,
precedence of the host's cron job, keys and destinations saved from the command line."""

import os
import shutil
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from synaptane.cli import main
from synaptane.config import ConfigError, Settings, load_settings
from synaptane.core.errors import BackupError
from synaptane.services import backup, backup_schedule, instance, offsite

PUBLIC = "age1t7t44ul2630hhamyl7k5r6thy9exnjx3p8282727ntnpkjrc6s4q0an2jm"
OTHER = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"


def settings_for(tmp_path: Path, **values: object) -> Settings:
    base = {"database_url": "postgresql+psycopg://x@127.0.0.1:1/x", "data_dir": tmp_path / "data"}
    settings = Settings.model_validate(base | values)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def touch(path: Path, moment: datetime | float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    stamp = moment.timestamp() if isinstance(moment, datetime) else moment
    os.utime(path, (stamp, stamp))


def local(hour: int, minute: int = 0, day: int = 8) -> datetime:
    return datetime(2026, 11, day, hour, minute).astimezone()  # server time zone


# --- Settings ---------------------------------------------------------------------------------


def test_defaults_and_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_for(tmp_path)
    assert (settings.backup_time, settings.backup_local_retention_days) == ("04:00", 7)
    assert settings.backup_dir is None and not backup_schedule.enabled(settings)
    monkeypatch.setenv("SYNAPTANE_DATABASE_URL", "postgresql+psycopg://x@h/x")
    monkeypatch.setenv("SYNAPTANE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SYNAPTANE_BACKUP_DIR", "/backups")  # set by the Docker image
    monkeypatch.setenv("SYNAPTANE_BACKUP_TIME", " 23:30 ")
    monkeypatch.setenv("SYNAPTANE_BACKUP_LOCAL_RETENTION_DAYS", "0")
    loaded = load_settings(tmp_path / "absent.toml")
    assert loaded.backup_dir == Path("/backups") and loaded.backup_time == "23:30"
    assert loaded.backup_local_retention_days == 0 and backup_schedule.enabled(loaded)
    for off in ("off", "OFF", ""):
        monkeypatch.setenv("SYNAPTANE_BACKUP_TIME", off)
        assert load_settings(tmp_path / "absent.toml").backup_time == "off"
    monkeypatch.setenv("SYNAPTANE_BACKUP_DIR", "")  # empty variable: no folder
    assert load_settings(tmp_path / "absent.toml").backup_dir is None


@pytest.mark.parametrize(
    ("written", "read"),
    [("4:00", "04:00"), ("04h00", "04:00"), ("4h", "04:00"), ("04:00:00", "04:00"), ("23:5", "")],
)
def test_the_time_is_read_as_people_write_it(tmp_path: Path, written: str, read: str) -> None:
    settings = settings_for(tmp_path, backup_time=written, backup_dir=tmp_path / "b")
    if read:
        assert settings.backup_time == read and not backup_schedule.time_invalid(settings)
    else:  # not a time: the application still starts, the backup runs at 04:00, warned about
        assert backup_schedule.time_invalid(settings)
        assert backup_schedule.at(settings) == backup_schedule.DEFAULT_TIME
        assert backup_schedule.time_text(settings) == "04:00" and backup_schedule.enabled(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SYNAPTANE_BACKUP_LOCAL_RETENTION_DAYS", "-1"),
        ("SYNAPTANE_BACKUP_TIMEOUT_MINUTES", "0"),
    ],
)
def test_invalid_settings_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    monkeypatch.setenv("SYNAPTANE_DATABASE_URL", "postgresql+psycopg://x@h/x")
    monkeypatch.setenv("SYNAPTANE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(field, value)
    with pytest.raises(ConfigError, match=field.removeprefix("SYNAPTANE_").lower()):
        load_settings(tmp_path / "absent.toml")


# --- Time of the day --------------------------------------------------------------------------


def test_due_once_a_day_after_its_time(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, backup_dir=tmp_path / "backups", backup_time="04:00")
    assert not backup_schedule.due(settings, local(3, 59))
    assert backup_schedule.due(settings, local(4, 0))  # never queued: right away
    assert backup_schedule.due(settings, local(15, 0))  # worker started late: still today
    touch(settings.data_dir / backup_schedule.SCHEDULED, local(4, 1))
    assert not backup_schedule.due(settings, local(23, 59))  # already queued today
    assert not backup_schedule.due(settings, local(3, 0, day=9))
    assert backup_schedule.due(settings, local(4, 0, day=9))
    assert backup_schedule.at(settings.model_copy(update={"backup_time": "off"})) is None
    off = settings.model_copy(update={"backup_time": "off"})
    no_folder = settings.model_copy(update={"backup_dir": None})
    assert not backup_schedule.due(off, local(5, 0, day=9))
    assert not backup_schedule.due(no_folder, local(5, 0, day=9))


def test_the_host_cron_job_has_precedence_while_it_runs(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, backup_dir=tmp_path / "backups")
    now = datetime.now().astimezone()
    host = settings.data_dir / backup_schedule.HOST_DONE
    assert not backup_schedule.host_in_charge(settings, now)
    touch(host, now - timedelta(hours=23))  # yesterday's run of the cron job
    assert backup_schedule.host_in_charge(settings, now)
    touch(host, now - backup_schedule.HOST_FRESH - timedelta(minutes=1))  # cron job stopped
    assert not backup_schedule.host_in_charge(settings, now)
    # With destinations, the cron job must also have made the off-site copy.
    touch(host, now - timedelta(hours=1))
    remote = settings.model_copy(update={"backup_remotes": ("nas:x",)})
    assert not backup_schedule.host_in_charge(remote, now)
    touch(settings.data_dir / instance.OFFSITE_DONE, now - timedelta(hours=1))
    assert backup_schedule.host_in_charge(remote, now)


# --- Local retention, free space --------------------------------------------------------------


def test_local_retention_only_deletes_the_workers_own_archives(tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    old = time.time() - 30 * 86400
    names = [f"synaptane-2026-10-0{day}-040000-auto.tar.gz" for day in range(1, 7)]
    for name in names:
        touch(folder / name, old)
    others = [
        "synaptane-2026-01-01-030000.tar.gz",  # `synaptane backup`, kept by hand
        "synaptane-manuel.tar.gz",  # a name of one's own
        "synaptane-2026-01-01-030000-auto.tar.gz.age",
        "notes.tar.gz",
    ]
    for name in others:
        touch(folder / name, old)
    (folder / "sub").mkdir()
    touch(folder / "sub" / "synaptane-2026-01-01-040000-auto.tar.gz", old)
    assert backup_schedule.prune_local(folder, 0) == []
    keep = folder / names[0]  # the archive just made, whatever its date
    removed = backup_schedule.prune_local(folder, 7, keep=keep)
    assert sorted(p.name for p in removed) == names[1:3]
    left = sorted(p.name for p in folder.glob("synaptane-*-auto.tar.gz"))
    assert left == [names[0], *names[3:]]  # the 3 newest always stay
    assert all((folder / name).exists() for name in others) and (folder / "sub").is_dir()
    # While the host's cron job makes the backups, none is protected.
    removed = backup_schedule.prune_local(folder, 7, keep_at_least=0)
    assert sorted(p.name for p in removed) == [names[0], *names[3:]]
    assert all((folder / name).exists() for name in others)


def test_space_left_for_the_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "backups"
    folder.mkdir()
    gb = 1024**3
    assert backup_schedule.reserve(100 * gb) == 2 * gb  # 2 GB on a large disk
    assert backup_schedule.reserve(10 * gb) == gb  # a tenth of a small one
    (folder / "synaptane-manuel.tar.gz").write_bytes(b"x" * 1000)  # no automatic one yet
    assert backup_schedule.space_needed(folder, 100 * gb) == 1200 + 2 * gb
    (folder / "synaptane-2026-11-01-040000-auto.tar.gz").write_bytes(b"x" * 500)
    assert backup_schedule.space_needed(folder, 100 * gb) == 600 + 2 * gb

    class Usage:
        total = 100 * gb
        free = 600 + 2 * gb - 1

    monkeypatch.setattr(shutil, "disk_usage", lambda _: Usage)
    with pytest.raises(BackupError, match="left for the database"):
        backup_schedule.check_space(folder)
    Usage.free += 1
    backup_schedule.check_space(folder)


def test_a_full_disk_recovers_once_old_archives_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retention runs before the space check: lowering it is enough to back up again."""
    folder = tmp_path / "backups"
    settings = settings_for(tmp_path, backup_dir=folder, backup_local_retention_days=1)
    old = time.time() - 3 * 86400
    for day in range(1, 6):
        touch(folder / f"synaptane-2026-10-0{day}-040000-auto.tar.gz", old)
    checked: list[int] = []
    monkeypatch.setattr(
        backup_schedule, "check_space", lambda f: checked.append(len(list(f.iterdir())))
    )

    def failing(*_: object, **__: object) -> None:
        raise BackupError("pg_dump failed")

    monkeypatch.setattr(backup, "backup", failing)
    with pytest.raises(BackupError):
        backup_schedule.write_archive(settings, folder)
    assert checked == [3]  # only the 3 newest left when the space was checked
    assert not list(folder.glob(".*.part"))


# --- One backup at a time ---------------------------------------------------------------------


def test_one_backup_at_a_time(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    waited: list[bool] = []
    with backup.exclusive(settings) as first:
        assert first
        with backup.exclusive(settings) as second:  # another process or thread: refused
            assert not second
        with backup.exclusive(settings, 0.05, lambda: waited.append(True)) as third:
            assert not third and waited == [True]
    with backup.exclusive(settings) as again:
        assert again
    assert stat.S_IMODE((settings.data_dir / backup.LOCK).stat().st_mode) == 0o600

    released = threading.Event()

    def hold() -> None:
        with backup.exclusive(settings):
            released.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    time.sleep(0.1)
    threading.Timer(0.2, released.set).start()
    with backup.exclusive(settings, 10) as waiting:  # waits for the other one to finish
        assert waiting
    holder.join()


def test_a_scheduled_run_steps_aside_without_touching_anything(tmp_path: Path) -> None:
    """Busy lock, the host's cron job in charge, or a full backup just made: nothing is
    written, no database used. The outcome is kept for the health page."""
    folder = tmp_path / "backups"
    settings = settings_for(tmp_path, backup_dir=folder)
    database, box = object(), object()
    with backup.exclusive(settings):
        result = backup_schedule.run(settings, database, box)  # type: ignore[arg-type]
    assert result.skipped == "busy" and not folder.exists()
    assert (backup_schedule.last_status(settings) or {})["outcome"] == "busy"
    touch(settings.data_dir / backup_schedule.HOST_DONE, time.time())
    assert backup_schedule.run(settings, database, box).skipped == "host"  # type: ignore[arg-type]
    (settings.data_dir / backup_schedule.HOST_DONE).unlink()
    touch(settings.data_dir / backup_schedule.COMPLETE_DONE, time.time() - 3600)  # deployment
    assert backup_schedule.run(settings, database, box).skipped == "recent"  # type: ignore[arg-type]
    assert not folder.exists()
    assert (backup_schedule.last_status(settings) or {})["outcome"] == "skipped"
    without_folder = settings.model_copy(update={"backup_dir": None})
    with pytest.raises(BackupError, match="SYNAPTANE_BACKUP_DIR"):
        backup_schedule.run(without_folder, database, box)  # type: ignore[arg-type]


# --- Keys and destinations saved from the command line ----------------------------------------


def test_saved_keys_and_destinations_add_to_the_settings(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, backup_age_recipients=(PUBLIC,), backup_remotes=("b2:x",))
    assert offsite.save_recipient(settings, f"  {OTHER}\n")
    assert not offsite.save_recipient(settings, OTHER)  # already there
    assert not offsite.save_recipient(settings, PUBLIC)  # in the settings
    for refused in ("AGE-SECRET-KEY-1ABCDEF", f"{PUBLIC} laptop of Alice"):
        with pytest.raises(BackupError, match="Not an age public key"):
            offsite.save_recipient(settings, refused)  # never a private key; age wants no comment
    assert offsite.save_recipient(settings, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB alice@pc")
    assert offsite.recipients(settings) == [
        PUBLIC,
        OTHER,
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB alice@pc",
    ]
    # Lines edited by hand that age would refuse are left out, not passed to it.
    with (offsite.setup_dir(settings) / offsite.RECIPIENTS_FILE).open("a") as out:
        out.write(f"  # indented comment\n{OTHER} comment\n")
    assert len(offsite.recipients(settings)) == 3
    assert offsite.save_remote(settings, "nas:sauvegardes/synaptane")
    assert not offsite.save_remote(settings, "b2:x")
    for refused in ("--config=/etc/shadow", ":s3,secret_access_key=abc:seau", ""):
        with pytest.raises(BackupError, match="Destination refused"):
            offsite.save_remote(settings, refused)
    assert offsite.remotes(settings) == ("b2:x", "nas:sauvegardes/synaptane")
    folder = offsite.setup_dir(settings)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for name in (offsite.RECIPIENTS_FILE, offsite.REMOTES_FILE):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600
    # A line edited by hand that would be dangerous is never passed to rclone.
    with (folder / offsite.REMOTES_FILE).open("a") as out:
        out.write("--delete-everything\n# comment\n")
    assert offsite.remotes(settings) == ("b2:x", "nas:sauvegardes/synaptane")
    assert not offsite.remove_remote(settings, "b2:x")  # from the settings: not here
    assert offsite.remove_remote(settings, "nas:sauvegardes/synaptane")
    assert offsite.remotes(settings) == ("b2:x",)
    bare = settings_for(tmp_path / "other")
    assert not offsite.has_recipients(bare)
    offsite.save_recipient(bare, PUBLIC)
    assert offsite.has_recipients(bare)
    with pytest.raises(BackupError, match="No off-server destination"):
        offsite.require_ready(bare)


FAKE_KEYGEN = """#!/bin/sh
echo "# created: 2026-11-08T04:00:00+01:00"
echo "# public key: {public}"
echo "AGE-SECRET-KEY-1FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"
"""


@pytest.fixture
def keygen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake age-keygen (age is not on every machine): same output as the real one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "age-keygen"
    script.write_text(FAKE_KEYGEN.format(public=PUBLIC))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return script


def test_a_generated_key_pair_keeps_only_the_public_key(tmp_path: Path, keygen: Path) -> None:
    private, public = offsite.generate_identity()
    assert public == PUBLIC and "AGE-SECRET-KEY-1" in private and PUBLIC in private
    settings = settings_for(tmp_path)
    offsite.save_recipient(settings, public)
    saved = (offsite.setup_dir(settings) / offsite.RECIPIENTS_FILE).read_text()
    assert PUBLIC in saved and "AGE-SECRET-KEY" not in saved
    keygen.write_text("#!/bin/sh\necho broken >&2\nexit 1\n")
    with pytest.raises(BackupError, match="age-keygen failed: broken"):
        offsite.generate_identity()


def config_file(tmp_path: Path, **values: str) -> Path:
    lines = [
        'database_url = "postgresql+psycopg://x@127.0.0.1:1/x"',
        f'data_dir = "{tmp_path / "data"}"',
        *(f'{key} = "{value}"' for key, value in values.items()),
    ]
    path = tmp_path / "synaptane.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_offsite_key_shows_the_private_key_once_and_saves_the_public_one(
    tmp_path: Path, keygen: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_file(tmp_path)
    assert main(["--config", str(config), "offsite", "key"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("# created:") and "AGE-SECRET-KEY-1" in out  # stdout: the key only
    assert "AGE-SECRET-KEY" not in err and "shown only once" in err and PUBLIC in err
    settings = load_settings(config)
    assert offsite.recipients(settings) == [PUBLIC]
    assert "AGE-SECRET-KEY" not in "".join(
        p.read_text() for p in settings.data_dir.rglob("*") if p.is_file()
    )
    assert main(["--config", str(config), "offsite", "key", OTHER]) == 0
    assert "Public key saved." in capsys.readouterr().out
    assert main(["--config", str(config), "offsite", "key", OTHER]) == 0
    assert "already there" in capsys.readouterr().out
    assert main(["--config", str(config), "offsite", "key", "not-a-key"]) == 1
    assert "Not an age public key" in capsys.readouterr().err


def test_offsite_add_tests_before_saving_and_remove_undoes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_file(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")  # rclone "installed"
    monkeypatch.setattr(offsite, "rclone_remotes", lambda _: ["nas"])
    probes: list[str] = []

    def check(_: Settings, remote: str) -> offsite.Outcome:
        probes.append(remote)
        return offsite.Outcome(remote, "" if remote.startswith("nas:") else "write: refused")

    monkeypatch.setattr(offsite, "check", check)
    assert main(["--config", str(config), "offsite", "add", "absent:x"]) == 1
    assert "not declared in rclone" in capsys.readouterr().err and probes == []
    assert main(["--config", str(config), "offsite", "add", ":local:/nowhere"]) == 1
    assert "write: refused. Not saved." in capsys.readouterr().err
    assert main(["--config", str(config), "offsite", "add", "--", "--config=/x"]) == 1
    assert "invalid rclone destination" in capsys.readouterr().err
    assert main(["--config", str(config), "offsite", "add", "nas:synaptane"]) == 0
    assert "[ok] nas:synaptane — saved." in capsys.readouterr().out
    assert offsite.remotes(load_settings(config)) == ("nas:synaptane",)
    assert main(["--config", str(config), "offsite", "remove", "nas:synaptane"]) == 0
    assert main(["--config", str(config), "offsite", "remove", "nas:synaptane"]) == 1
    assert "is not a destination" in capsys.readouterr().err
    with_env = config_file(tmp_path, backup_remotes="b2:x")
    assert main(["--config", str(with_env), "offsite", "remove", "b2:x"]) == 1
    assert "SYNAPTANE_BACKUP_REMOTES" in capsys.readouterr().err


def test_a_stuck_dump_is_abandoned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It would otherwise hold the backup lock, and the job, forever."""
    seen: list[object] = []

    def stuck(command: list[str], **kwargs: object) -> object:
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(subprocess, "run", stuck)
    with pytest.raises(BackupError, match="pg_dump took more than 2 minutes: abandoned"):
        backup._run(["pg_dump", "x"], {}, 2)
    assert seen == [120]
    settings = settings_for(tmp_path)
    assert settings.backup_timeout_minutes == 120
