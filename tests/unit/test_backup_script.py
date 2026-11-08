"""deploy/host/backup.sh, run against a fake `docker`: the cron job of existing installations
keeps working as before, and tells Synaptane it made the backup of the day (the worker's
automatic backup then steps aside); a deployment's backup (with a label) does not."""

import io
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "deploy" / "host" / "backup.sh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "tar", "gzip")),
    reason="bash, tar and gzip required",
)

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker: the call in calls.log, the archive of $FAKE_ARCHIVE on standard output.
echo "docker $*" >> "$FAKE_DIR/calls.log"
cat "$FAKE_ARCHIVE"
exit "${FAKE_STATUS:-0}"
"""


def archive_bytes() -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name in ("manifest.json", "database.dump"):
            entry = tarfile.TarInfo(name)
            entry.size = 2
            tar.addfile(entry, io.BytesIO(b"{}"))
    return data.getvalue()


def run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    bin_dir, fake = tmp_path / "bin", tmp_path / "fake"
    if not bin_dir.exists():
        bin_dir.mkdir()
        fake.mkdir()
        (bin_dir / "docker").write_text(FAKE_DOCKER)
        (bin_dir / "docker").chmod(0o755)
        (fake / "archive.tar.gz").write_bytes(archive_bytes())
    full_env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "SYNAPTANE_DIR": str(tmp_path),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "FAKE_DIR": str(fake),
        "FAKE_ARCHIVE": str(fake / "archive.tar.gz"),
        **env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=full_env, capture_output=True, text=True, timeout=30
    )


def calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "fake" / "calls.log").read_text().splitlines()


def test_the_cron_job_says_it_made_the_backup_of_the_day(tmp_path: Path) -> None:
    done = run(tmp_path)
    assert done.returncode == 0, done.stderr
    archive = Path(done.stdout.strip())
    assert archive.parent == tmp_path / "backups" and archive.name.startswith("synaptane-")
    assert archive.read_bytes() == archive_bytes()
    assert (archive.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "backups").stat().st_mode & 0o777) == 0o700
    assert calls(tmp_path) == [
        "docker compose exec -T -e SYNAPTANE_HOST_BACKUP=1 app synaptane backup --output -"
    ]
    assert not list((tmp_path / "backups").glob("*.part"))
    assert str(archive) in (tmp_path / "backups" / "journal.log").read_text()


def test_a_deployment_backup_is_not_the_backup_of_the_day(tmp_path: Path) -> None:
    done = run(tmp_path, "avant-abc1234")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("-avant-abc1234.tar.gz")
    assert calls(tmp_path) == ["docker compose exec -T app synaptane backup --output -"]


def test_offsite_failure_keeps_the_archive_with_code_3(tmp_path: Path) -> None:
    done = run(tmp_path, OFFSITE="1", FAKE_STATUS="3")
    assert done.returncode == 3
    assert Path(done.stdout.strip()).exists()
    assert calls(tmp_path)[-1].endswith("app synaptane backup --offsite --output -")
    assert "FAILED off-site copy" in (tmp_path / "backups" / "journal.log").read_text()
    done = run(tmp_path, OFFSITE="1")
    assert done.returncode == 0
    assert "off-site copy done" in (tmp_path / "backups" / "journal.log").read_text()


def test_a_failed_or_truncated_backup_replaces_nothing(tmp_path: Path) -> None:
    done = run(tmp_path, FAKE_STATUS="1")
    assert done.returncode == 1 and "Backup impossible" in done.stderr
    assert not list((tmp_path / "backups").glob("synaptane-*"))
    (tmp_path / "fake" / "archive.tar.gz").write_bytes(archive_bytes()[:40])  # cut short
    done = run(tmp_path)
    assert done.returncode == 1 and "Unreadable or incomplete archive" in done.stderr
    assert not list((tmp_path / "backups").glob("synaptane-*"))


def test_old_archives_are_deleted_after_a_good_one(tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    folder.mkdir()
    old, kept = folder / "synaptane-2026-01-01.tar.gz", folder / "notes.txt"
    for path in (old, kept):
        path.write_bytes(b"x")
        stamp = time.time() - 20 * 86400
        os.utime(path, (stamp, stamp))
    done = run(tmp_path, RETENTION_DAYS="14")
    assert done.returncode == 0
    assert not old.exists() and kept.exists()
    assert run(tmp_path, RETENTION_DAYS="two weeks").returncode == 2
    assert run(tmp_path, "bad label!").returncode == 2
