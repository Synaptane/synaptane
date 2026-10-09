"""`synaptane backup --offsite` end to end, without network: real database, real archive,
age encryption with a key pair generated here, local rclone destinations."""

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from synaptane.cli import main
from synaptane.config import Settings
from synaptane.storage.db import Database
from tests.integration.conftest import make_user

needs_tools = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("pg_dump", "pg_restore", "age", "rclone")),
    reason="pg_dump, pg_restore, age ou rclone absent",
)


def keypair(tmp_path: Path) -> tuple[Path, str]:
    identity = tmp_path / "cle-privee.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    public = subprocess.run(
        ["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True
    ).stdout.strip()
    return identity, public


def write_config(settings: Settings, path: Path, **backup: object) -> Path:
    lines = [f'database_url = "{settings.database_url}"', f'data_dir = "{settings.data_dir}"']
    for key, value in backup.items():
        if isinstance(value, list | tuple):
            items = ", ".join(f'"{item}"' for item in value)
            lines.append(f"{key} = [{items}]")
        else:
            lines.append(f"{key} = {value!r}" if isinstance(value, int) else f'{key} = "{value}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def decrypt(identity: Path, encrypted: Path) -> bytes:
    return subprocess.run(
        ["age", "-d", "-i", str(identity), str(encrypted)], check=True, capture_output=True
    ).stdout


@needs_tools
def test_offsite_copies_reach_every_working_destination(
    settings: Settings,
    database: Database,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with database.session() as session:
        make_user(session, "root", superadmin=True)
    identity, public = keypair(tmp_path)
    near, far = tmp_path / "nas", tmp_path / "s3"
    config = write_config(
        settings,
        tmp_path / "synaptane.toml",
        backup_age_recipients=[public],
        backup_remotes=[str(near), f":local:{far}/synaptane", "absente:seau"],
    )
    local = tmp_path / "local"
    local.mkdir()

    assert main(["--config", str(config), "offsite", "test"]) == 1  # "absente" fails
    report = capsys.readouterr().out
    assert f"[ok] {near}" in report and "[FAILED] absente:seau — " in report

    code = main(["--config", str(config), "backup", "--offsite", "--output", str(local)])
    err = capsys.readouterr().err
    assert code == 3, err  # archive fine, one destination failing
    assert "absente:seau" in err.split("Incomplete")[-1]
    archive = next(local.glob("synaptane-*.tar.gz"))
    copies = [*near.glob("synaptane-*.tar.gz.age"), *(far / "synaptane").glob("*.age")]
    assert [copy.name for copy in copies] == [f"{archive.name}.age"] * 2
    for copy in copies:
        assert decrypt(identity, copy) == archive.read_bytes()
    with tarfile.open(archive) as tar:
        assert "master.key" in tar.getnames()
    # Nothing is left lying in the data directory (plain archive, encrypted copy).
    assert sorted(p.name for p in settings.data_dir.iterdir()) == ["master.key"]

    assert main(["--config", str(config), "offsite", "list"]) == 1
    assert f"{archive.name}.age" in capsys.readouterr().out


@needs_tools
def test_cron_form_streams_the_archive_and_copies_it(
    settings: Settings,
    database: Database,
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`backup --offsite --output - > file.part`: the form of the production cron job."""
    identity, public = keypair(tmp_path)
    far = tmp_path / "loin"
    config = write_config(
        settings,
        tmp_path / "synaptane.toml",
        backup_age_recipients=[public],
        backup_remotes=[str(far)],
        backup_retention_days=14,
    )
    monkeypatch.setenv("SYNAPTANE_BACKUP_DIR", str(tmp_path))  # not used with --output -
    assert main(["--config", str(config), "backup", "--offsite", "--output", "-"]) == 0
    captured = capsysbinary.readouterr()
    assert b"1 destination(s)" in captured.err
    (copy,) = far.glob("synaptane-*.tar.gz.age")
    assert decrypt(identity, copy) == captured.out
    with tarfile.open(fileobj=io.BytesIO(captured.out)) as tar:
        assert "manifest.json" in tar.getnames()

    # --offsite alone: no local copy (the /backups volume has no rotation).
    assert main(["--config", str(config), "backup", "--offsite"]) == 0
    assert not list(tmp_path.glob("synaptane-*.tar.gz"))
    assert len(list(far.glob("synaptane-*.tar.gz.age"))) >= 1


def test_doctor_warns_but_never_fails_on_offsite_settings(
    settings: Settings, database: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.key_file.chmod(0o600)
    settings.repos_dir.mkdir()
    config = write_config(settings, tmp_path / "a.toml")
    assert main(["--config", str(config), "doctor"]) == 0
    assert "[warning] Off-site backup — not configured" in capsys.readouterr().out

    config = write_config(settings, tmp_path / "b.toml", backup_remotes=["nas:sauvegardes"])
    assert main(["--config", str(config), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "[ok] Off-site backup: 1 destination(s)" in out
    assert "[warning] age recipients — no public key" in out
    if shutil.which("rclone"):
        assert "[warning] Destinations declared in rclone" in out and "nas" in out
    assert "FAILED] Off-site" not in out and "FAILED] age" not in out
    assert "FAILED] Destinat" not in out
