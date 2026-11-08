"""`backup check` and `backup setup`: what is missing, and the steps that do it.

age, rclone and the destinations are simulated (the real ones are exercised in
tests/integration/test_backup_schedule.py when installed)."""

import builtins
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from synaptane import backup_cli
from synaptane.cli import main
from synaptane.config import Settings, load_settings
from synaptane.services import backup_schedule, instance, offsite
from tests.unit.test_backup_schedule import FAKE_KEYGEN, PUBLIC


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every tool "installed", rclone simulated: declared storages and probe results."""
    state: dict[str, Any] = {"declared": [], "failing": set(), "probes": [], "wizard": 0}
    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(offsite, "rclone_remotes", lambda _: list(state["declared"]))

    def check(_: Settings, remote: str) -> offsite.Outcome:
        state["probes"].append(remote)
        return offsite.Outcome(remote, "write: denied" if remote in state["failing"] else "")

    monkeypatch.setattr(offsite, "check", check)
    return state


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "synaptane.toml"
    path.write_text(
        'database_url = "postgresql+psycopg://x@127.0.0.1:1/x"\n'
        f'data_dir = "{tmp_path / "data"}"\nbackup_dir = "{tmp_path / "backups"}"\n',
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    return path


def answers(monkeypatch: pytest.MonkeyPatch, *replies: str) -> list[str]:
    """Answers typed at the prompts, in order; the questions asked are returned. Past the
    last answer: end of input (Ctrl+D)."""
    asked: list[str] = []
    pending: Iterator[str] = iter(replies)

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        try:
            return next(pending)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(backup_cli, "interactive", lambda: True)
    return asked


@pytest.fixture
def keygen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake age-keygen first in PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "age-keygen"
    script.write_text(FAKE_KEYGEN.format(public=PUBLIC))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


# --- backup check -----------------------------------------------------------------------------


def run_check(config: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str]:
    code = main(["--config", str(config), "backup", "check", *extra])
    return code, capsys.readouterr().out


def test_check_says_what_is_missing_and_what_to_run(
    config: Path, tools: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run_check(config, capsys)
    assert code == 0
    assert "[ok] Automatic backup: every day at 04:00, in " in out
    assert "[ok] Free space: " in out and "[ok] Local retention: 7 days" in out
    assert "[warning] Last verified backup — none yet: `synaptane backup` makes one now" in out
    assert "[warning] Encryption — no public key: `synaptane offsite key`" in out
    assert "[warning] Off-site destination — none: `synaptane backup setup`" in out
    assert out.rstrip().endswith("To finish, step by step: synaptane backup setup")
    assert tools["probes"] == []


def test_check_is_all_green_once_set_up(
    config: Path, tools: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    settings = load_settings(config)
    tools["declared"] = ["nas"]
    offsite.save_recipient(settings, PUBLIC)
    offsite.save_remote(settings, "nas:synaptane")
    instance.mark(settings, instance.BACKUP_DONE)
    instance.mark(settings, instance.OFFSITE_DONE)
    code, out = run_check(config, capsys)
    assert code == 0, out
    assert "[ok] Encryption: 1 public key(s)" in out
    assert "[ok] Destination nas:synaptane: writable" in out
    assert "[ok] Off-site retention: 30 days" in out and "[ok] Last off-site copy: " in out
    assert "warning" not in out and "FAILED" not in out
    assert "Backups are set up" in out and tools["probes"] == ["nas:synaptane"]

    tools["failing"].add("nas:synaptane")
    code, out = run_check(config, capsys)
    assert code == 1 and "[FAILED] Destination nas:synaptane — write: denied" in out
    code, out = run_check(config, capsys, "--no-test")  # no test file written
    assert code == 0 and "[ok] Destination nas:synaptane\n" in out
    tools["declared"] = []
    code, out = run_check(config, capsys, "--no-test")
    assert code == 1 and "not declared in rclone: `rclone config`" in out


def test_check_reports_the_other_ways_of_backing_up(
    config: Path, tools: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    settings = load_settings(config)
    instance.mark(settings, backup_schedule.HOST_DONE)
    code, out = run_check(config, capsys)
    assert "[ok] Daily backup: by the server's cron job (deploy/host/backup.sh)" in out
    (settings.data_dir / backup_schedule.HOST_DONE).unlink()
    off = config.read_text() + 'backup_time = "off"\nbackup_local_retention_days = 0\n'
    config.write_text(off)
    code, out = run_check(config, capsys)
    assert "[warning] Automatic backup — off (SYNAPTANE_BACKUP_TIME=off)" in out
    assert "[warning] Local retention — none" in out
    config.write_text("\n".join(line for line in off.splitlines() if "backup_dir" not in line))
    code, out = run_check(config, capsys)
    assert "[warning] Automatic backup — no folder: set SYNAPTANE_BACKUP_DIR" in out
    late = time.time() - instance.BACKUP_LATE.total_seconds() - 60
    instance.mark(settings, instance.BACKUP_DONE)
    os.utime(settings.data_dir / instance.BACKUP_DONE, (late, late))
    code, out = run_check(config, capsys)
    assert "[warning] Last verified backup — " in out and ", late" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes everywhere")
def test_check_fails_on_a_folder_it_cannot_write(
    config: Path, tools: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "backups").mkdir(mode=0o500)
    try:
        code, out = run_check(config, capsys)
    finally:
        (tmp_path / "backups").chmod(0o700)
    assert code == 1 and f"[FAILED] Backup folder {tmp_path / 'backups'} — not writable" in out


# --- backup setup -----------------------------------------------------------------------------


def test_setup_refuses_to_run_without_a_terminal(
    config: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_cli, "interactive", lambda: False)
    assert main(["--config", str(config), "backup", "setup"]) == 2
    err = capsys.readouterr().err
    assert "docker compose exec -it app synaptane backup setup" in err
    assert "synaptane offsite key" in err


def test_setup_makes_the_key_declares_the_storage_and_saves_the_destination(
    config: Path,
    tools: dict[str, Any],
    keygen: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_run = subprocess.run

    def wizard(command: list[str], **kwargs: Any) -> Any:
        if command[0] != "rclone":
            return real_run(command, **kwargs)  # age-keygen
        assert command == ["rclone", "config"]
        assert kwargs["env"]["RCLONE_CONFIG"].endswith("rclone/rclone.conf")
        tools["declared"] = ["nas"]  # the storage declared in the wizard
        tools["wizard"] += 1
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", wizard)
    asked = answers(
        monkeypatch,
        "",  # no public key of mine: make one
        "done",  # not the expected word
        "saved",
        "",  # start the rclone wizard: yes
        "",  # destination: the suggested nas:synaptane-backups
        "n",  # no first backup now
    )
    assert main(["--config", str(config), "backup", "setup"]) == 0
    out = capsys.readouterr().out
    assert out.count("AGE-SECRET-KEY-1") == 1 and "shown only once" in out
    assert asked[2] == asked[1] and 'Type "saved"' in asked[1]
    assert "[nas:synaptane-backups]" in asked[4]
    assert "[ok] nas:synaptane-backups: test file written, read back and deleted. Saved." in out
    settings = load_settings(config)
    assert offsite.recipients(settings) == [PUBLIC]
    assert offsite.remotes(settings) == ("nas:synaptane-backups",)
    assert tools["probes"] == ["nas:synaptane-backups"] and tools["wizard"] == 1


def test_setup_takes_my_public_key_and_keeps_what_is_done(
    config: Path,
    tools: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tools["declared"] = ["b2"]
    tools["failing"].add("b2:wrong")
    answers(monkeypatch, "not-a-key", PUBLIC, "n", "b2:wrong", "b2:synaptane", "n")
    assert main(["--config", str(config), "backup", "setup"]) == 0
    out = capsys.readouterr().out
    assert "Not an age public key" in out and "Public key saved." in out
    assert "[FAILED] b2:wrong — write: denied" in out
    settings = load_settings(config)
    assert offsite.remotes(settings) == ("b2:synaptane",)
    # Run again: what is set up is said, not asked again.
    asked = answers(monkeypatch, "", "n")
    assert main(["--config", str(config), "backup", "setup"]) == 0
    out = capsys.readouterr().out
    assert "Already set (1 public key(s)): nothing to do." in out
    assert "[ok] b2:synaptane" in out and "Add another destination?" in asked[0]


def test_setup_stopped_midway_keeps_what_was_saved(
    config: Path,
    tools: dict[str, Any],
    keygen: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers(monkeypatch, "")  # key made, then Ctrl+D instead of "saved"
    assert main(["--config", str(config), "backup", "setup"]) == 1
    assert "Stopped" in capsys.readouterr().err
    assert offsite.recipients(load_settings(config)) == []  # key never confirmed: not used


def test_setup_makes_the_first_backup(
    config: Path,
    tools: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    settings = load_settings(config)
    offsite.save_recipient(settings, PUBLIC)
    tools["declared"] = ["nas"]
    offsite.save_remote(settings, "nas:x")
    archive = tmp_path / "backups" / "synaptane-2026-11-08-150000.tar.gz"
    results = [
        backup_schedule.Result(
            archive=archive, outcomes=[offsite.Outcome("nas:x"), offsite.Outcome("b2:y", "nope")]
        ),
        backup_schedule.Result(skipped="busy"),
        backup_schedule.Result(archive=archive, offsite_error="age encryption failed: x"),
    ]
    calls: list[str] = []

    def run(*args: object, **kwargs: object) -> backup_schedule.Result:
        calls.append(str(kwargs["trigger"]))
        return results.pop(0)

    monkeypatch.setattr(backup_schedule, "run", run)
    monkeypatch.setattr("synaptane.server.load_secret_box", lambda _: None)
    for _ in range(3):
        answers(monkeypatch, "n", "")
        assert main(["--config", str(config), "backup", "setup"]) == 0
    out = capsys.readouterr().out
    assert calls == [backup_schedule.MANUAL] * 3
    assert f"[ok] Backup verified: {archive}" in out
    assert f"[ok] nas:x/{archive.name}.age" in out and "[FAILED] b2:y/" in out
    assert "Another backup is running" in out
    assert "[FAILED] Off-site copy — age encryption failed: x" in out
