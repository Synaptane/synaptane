"""Off-site copy: settings, refusal without an age recipient, rclone to a local folder.

The tests that run `age` or `rclone` are skipped if they are not installed (the Docker image
contains them, and `scripts/e2e-compose.sh` exercises them in CI)."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from synaptane.cli import main
from synaptane.config import ConfigError, Settings, load_settings
from synaptane.core.errors import BackupError
from synaptane.services import offsite

needs_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone absent")
needs_age = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None, reason="age absent"
)
PUBLIC = "age1t7t44ul2630hhamyl7k5r6thy9exnjx3p8282727ntnpkjrc6s4q0an2jm"


def settings_for(tmp_path: Path, **values: object) -> Settings:
    base = {"database_url": "postgresql+psycopg://x@127.0.0.1:1/x", "data_dir": tmp_path}
    return Settings.model_validate(base | values)


def test_lists_come_from_toml_or_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "synaptane.toml"
    config.write_text(
        'database_url = "postgresql+psycopg://x@h/x"\ndata_dir = "data"\n'
        f'backup_age_recipients = ["{PUBLIC}"]\n'
        'backup_remotes = ["b2:seau/synaptane", "gdrive:Mes sauvegardes"]\n',
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.backup_age_recipients == (PUBLIC,)
    assert settings.backup_remotes == ("b2:seau/synaptane", "gdrive:Mes sauvegardes")
    monkeypatch.setenv("SYNAPTANE_BACKUP_REMOTES", " nas:sauvegardes , /mnt/usb\n:local:/x,")
    monkeypatch.setenv("SYNAPTANE_BACKUP_AGE_RECIPIENTS", "")
    monkeypatch.setenv("SYNAPTANE_BACKUP_AGE_RECIPIENTS_FILE", "")
    settings = load_settings(config)
    assert settings.backup_remotes == ("nas:sauvegardes", "/mnt/usb", ":local:/x")
    assert settings.backup_age_recipients == ()
    assert settings.backup_age_recipients_file is None
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    assert settings.rclone_config == tmp_path / "data" / "rclone" / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", "/data/rclone/rclone.conf")  # image Docker
    assert settings.rclone_config == Path("/data/rclone/rclone.conf")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SYNAPTANE_BACKUP_REMOTES", "--config=/etc/shadow"),  # disguised option
        ("SYNAPTANE_BACKUP_REMOTES", ":s3,secret_access_key=abc:seau"),  # identifiants
        ("SYNAPTANE_BACKUP_AGE_RECIPIENTS", "pas-une-cle"),
        ("SYNAPTANE_BACKUP_RETENTION_DAYS", "-1"),
    ],
)
def test_dangerous_or_invalid_settings_are_refused_without_echoing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    monkeypatch.setenv("SYNAPTANE_DATABASE_URL", "postgresql+psycopg://x@h/x")
    monkeypatch.setenv("SYNAPTANE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(field, value)
    with pytest.raises(ConfigError) as error:
        load_settings(tmp_path / "absent.toml")
    assert value not in str(error.value)


def test_target_joins_remote_and_name() -> None:
    assert offsite.target("b2:seau/synaptane", "a.age") == "b2:seau/synaptane/a.age"
    assert offsite.target("nas:", "a.age") == "nas:a.age"
    assert offsite.target("/mnt/usb/", "a.age") == "/mnt/usb/a.age"


def test_offsite_is_refused_without_age_recipient(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The archive contains the master key in clear: never copied off-site unencrypted.
    The refusal comes before any write (not even a database connection)."""
    config = tmp_path / "synaptane.toml"
    config.write_text(
        'database_url = "postgresql+psycopg://x@127.0.0.1:1/x"\n'
        f'data_dir = "{tmp_path / "data"}"\nbackup_remotes = ["{tmp_path / "loin"}"]\n',
        encoding="utf-8",
    )
    assert main(["--config", str(config), "backup", "--offsite", "--output", str(tmp_path)]) == 1
    assert "no age recipient" in capsys.readouterr().err
    assert not (tmp_path / "loin").exists() and not list(tmp_path.glob("synaptane-*"))
    with pytest.raises(BackupError, match="No off-server destination"):
        offsite.require_ready(settings_for(tmp_path, backup_age_recipients=(PUBLIC,)))


@needs_rclone
def test_retention_only_removes_old_archives_at_the_root(tmp_path: Path) -> None:
    remote = tmp_path / "loin"
    (remote / "dossier").mkdir(parents=True)
    old = time.time() - 40 * 86400
    files = {
        "synaptane-2026-01-01-030000.tar.gz.age": old,  # deleted
        "synaptane-2026-10-05-030000.tar.gz.age": time.time(),  # recent
        "notes.txt": old,  # other file of the destination
        "dossier/synaptane-2026-01-01-030000.tar.gz.age": old,  # sous-dossier
    }
    for name, mtime in files.items():
        (remote / name).write_bytes(b"x")
        os.utime(remote / name, (mtime, mtime))
    settings = settings_for(tmp_path, backup_retention_days=30)
    assert offsite.prune(settings, str(remote)) == ""
    left = sorted(str(p.relative_to(remote)) for p in remote.rglob("*") if p.is_file())
    assert left == [
        "dossier/synaptane-2026-01-01-030000.tar.gz.age",
        "notes.txt",
        "synaptane-2026-10-05-030000.tar.gz.age",
    ]
    assert [f.name for f in offsite.listing(settings, str(remote))] == [
        "synaptane-2026-10-05-030000.tar.gz.age"
    ]
    assert offsite.prune(settings_for(tmp_path, backup_retention_days=0), "inconnu:x") == ""


@needs_rclone
def test_destination_check_writes_reads_and_deletes(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "data")
    remote = f":local:{tmp_path / 'loin'}"
    assert offsite.check(settings, remote).ok
    assert list((tmp_path / "loin").iterdir()) == []  # the marker file is deleted
    assert list((tmp_path / "data").iterdir()) == []
    failed = offsite.check(settings, "inconnue:seau")
    assert not failed.ok and failed.error.startswith("write: ")
    assert offsite.undefined_remotes(
        settings_for(tmp_path, backup_remotes=("inconnue:seau", ":local:/x", "/mnt/usb"))
    ) == ["inconnue"]


@needs_age
def test_encryption_opens_only_with_the_private_key(tmp_path: Path) -> None:
    identity = tmp_path / "cle.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    public = subprocess.run(
        ["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True
    ).stdout.strip()
    other = tmp_path / "autre.txt"
    subprocess.run(["age-keygen", "-o", str(other)], check=True, capture_output=True)
    source = tmp_path / "archive.tar.gz"
    source.write_bytes(b"master.key en clair" * 100)
    encrypted = tmp_path / "archive.tar.gz.age"
    offsite.encrypt(settings_for(tmp_path, backup_age_recipients=(public,)), source, encrypted)
    assert b"master.key" not in encrypted.read_bytes()
    opened = subprocess.run(
        ["age", "-d", "-i", str(identity), str(encrypted)], check=True, capture_output=True
    ).stdout
    assert opened == source.read_bytes()
    refused = subprocess.run(["age", "-d", "-i", str(other), str(encrypted)], capture_output=True)
    assert refused.returncode != 0
