"""Backup and restore of an instance (docs/admin/backup.md).

A `.tar.gz` archive contains:
- `manifest.json`: format, version and commit of Synaptane, date, database revision, number of
  accounts, organizations and spaces (the restore drill compares them);
- `database.dump`: the database (`pg_dump --format=custom`);
- `master.key`: the master key, unless `--without-key`;
- `repos/`: the local copies of the repositories, unless `--without-repos`. The contents are
  also on the remote repositories; the local copies keep what has not been pushed yet.

Text extracted from PDFs and images is not backed up: it is rebuilt from Git. Neither are the
logins of subscription CLIs (Claude Code, Codex): they are redone.
"""

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from sqlalchemy import func, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ProgrammingError

from synaptane import UNKNOWN_COMMIT, __version__, git_commit
from synaptane.config import Settings
from synaptane.core.errors import BackupError
from synaptane.core.security import SecretBox
from synaptane.git import repo
from synaptane.services import jobs
from synaptane.storage.db import APP_NAME, Database, known_revision, upgrade
from synaptane.storage.models import Organization, Space, User

FORMAT = 1
DUMP = "database.dump"
KEY = "master.key"
REPOS = "repos"
MANIFEST = "manifest.json"


def _libpq(database_url: str) -> tuple[str, dict[str, str]]:
    """libpq address without password, and environment with PGPASSWORD (out of `ps`)."""
    url = make_url(database_url)
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    bare = URL.create(  # URL.set(password=None) would keep the password
        "postgresql",
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    )
    dsn = bare.render_as_string(hide_password=False)
    return dsn, env


def _run(command: list[str], env: dict[str, str]) -> None:
    if shutil.which(command[0]) is None:
        raise BackupError(
            f'"{command[0]}" not found: install the PostgreSQL client '
            "(same major version as the server, or newer)."
        )
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise BackupError(f"{command[0]} failed: {result.stderr.strip()[-800:]}")


def _revision(database: Database) -> str:
    with database.engine.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar())


# --- Backup -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupInfo:
    spaces: int
    repos: bool
    key: bool
    users: int = 0


def backup(
    settings: Settings, output: IO[bytes], *, with_repos: bool = True, with_key: bool = True
) -> BackupInfo:
    """Writes the archive to `output` (file or standard output)."""
    dsn, env = _libpq(settings.database_url)
    database = Database(settings.database_url)
    try:
        revision = _revision(database)
        with database.session() as session:
            counts = {
                name: session.scalar(select(func.count()).select_from(model)) or 0
                for name, model in (
                    ("users", User),
                    ("organizations", Organization),
                    ("spaces", Space),
                )
            }
    finally:
        database.dispose()
    if with_key and not settings.key_file.is_file():
        raise BackupError(f"Master key not found: {settings.key_file}")
    manifest: dict[str, object] = {
        "format": FORMAT,
        "version": __version__,
        "commit": git_commit(),  # missing from older archives: read with .get()
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "revision": revision,
        "repos": with_repos,
        "key": with_key,
        "counts": counts,
        # Fingerprint of the key that encrypts this database's secrets: an archive without a
        # key only restores with the same key (otherwise every secret would be unreadable).
        "key_fingerprint": key_fingerprint(settings.key_file.read_bytes())
        if settings.key_file.is_file()
        else None,
    }
    # In the data directory, not in /tmp: in a container, /tmp is in memory.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="backup-", dir=settings.data_dir) as scratch:
        dump = Path(scratch) / DUMP
        _run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--file={dump}",
                dsn,
            ],
            env,
        )
        with tarfile.open(fileobj=output, mode="w|gz") as archive:
            data = json.dumps(manifest, indent=2).encode()
            entry = tarfile.TarInfo(MANIFEST)
            entry.size, entry.mtime, entry.mode = len(data), int(datetime.now().timestamp()), 0o600
            archive.addfile(entry, io.BytesIO(data))
            archive.add(dump, DUMP)
            if with_key:
                archive.add(settings.key_file, KEY)
            if with_repos and settings.repos_dir.is_dir():
                # Without symbolic links: refused on restore, and useless (Git has them).
                archive.add(
                    settings.repos_dir,
                    REPOS,
                    filter=lambda entry: None if entry.issym() or entry.islnk() else entry,
                )
    return BackupInfo(
        spaces=counts["spaces"], repos=with_repos, key=with_key, users=counts["users"]
    )


def default_name() -> str:
    """`synaptane-YYYY-MM-DD-HHMMSS.tar.gz`: alphabetical order is chronological order."""
    return f"synaptane-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.tar.gz"


def verify(settings: Settings, archive: Path) -> dict[str, Any]:
    """Reads back a written archive: sound compression, complete tar, manifest, key, and dump
    readable by `pg_restore --list`. A truncated archive (full disk, interrupted copy) fails
    here, on the day of the backup, not on the day it would have to be restored."""
    try:
        with gzip.open(archive, "rb") as stream:  # CRC and length at the end of gzip (gzip -t)
            while stream.read(1 << 20):
                pass
    except (OSError, EOFError, zlib.error) as error:
        raise BackupError(f"Damaged archive ({archive.name}): {error}") from None
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="verify-", dir=settings.data_dir) as scratch:
        dump = Path(scratch) / DUMP
        manifest: dict[str, Any] | None = None
        try:
            with tarfile.open(archive, mode="r|gz") as tar:
                for member in tar:
                    if member.name.split("/", 1)[0] not in (MANIFEST, DUMP, KEY, REPOS):
                        raise BackupError(f"Unexpected archive entry: {member.name}")
                    payload = tar.extractfile(member) if member.isfile() else None
                    if payload is None:
                        continue
                    if member.name == MANIFEST:
                        manifest = json.loads(payload.read())
                    elif member.name == DUMP:
                        with dump.open("wb") as out:
                            shutil.copyfileobj(payload, out)
                    elif member.name == KEY and len(payload.read()) != SecretBox.KEY_BYTES:
                        raise BackupError("Invalid master key in the archive.")
        except (tarfile.TarError, OSError, EOFError, zlib.error, ValueError) as error:
            raise BackupError(f"Unreadable archive ({archive.name}): {error}") from None
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise BackupError("Incomplete archive: manifest missing or unknown format.")
        if not dump.is_file():
            raise BackupError("Incomplete archive: database dump missing.")
        # Without a database connection. Stronger than `pg_restore --list`, which only reads the
        # table of contents: the whole dump is converted to SQL (discarded), as a restore starts.
        _run(["pg_restore", f"--file={os.devnull}", str(dump)], dict(os.environ))
    return manifest


def key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


# --- Restore ----------------------------------------------------------------------------------


@contextmanager
def _unpacked(source: IO[bytes], into: Path) -> Iterator[dict[str, Any]]:
    """Unpacks the archive (streamed read: works from standard input)."""
    with tarfile.open(fileobj=source, mode="r|gz") as archive:
        for member in archive:
            top = member.name.split("/", 1)[0]
            if top not in (MANIFEST, DUMP, KEY, REPOS):
                raise BackupError(f"Unexpected archive entry: {member.name}")
            # "data" filter: no absolute path, no escape from the directory, no special file.
            archive.extract(member, into, filter="data")
    try:
        manifest: dict[str, Any] = json.loads((into / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupError("This file is not a Synaptane backup.") from None
    if manifest.get("format") != FORMAT or not (into / DUMP).is_file():
        raise BackupError("Unknown backup format or incomplete archive.")
    yield manifest


@dataclass(frozen=True)
class RestoreInfo:
    created_at: str
    version: str
    spaces: int
    repos: bool
    commit: str = UNKNOWN_COMMIT


def restore(settings: Settings, source: IO[bytes], *, force: bool = False) -> RestoreInfo:
    """Restores database, key and repositories. The application and the worker must be stopped.

    Without `force`, refuses to overwrite an instance that already has accounts. A new instance
    (no account) takes the archive's master key instead of the one generated at startup.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="restore-", dir=settings.data_dir) as scratch,
        _unpacked(source, Path(scratch)) as manifest,
    ):
        unpacked = Path(scratch)
        key = unpacked / KEY
        if key.is_file():
            if len(key.read_bytes()) != SecretBox.KEY_BYTES:
                raise BackupError("Invalid master key in the archive.")
        else:
            expected = manifest.get("key_fingerprint")
            present = (
                key_fingerprint(settings.key_file.read_bytes())
                if settings.key_file.is_file()
                else None
            )
            if present is None or (expected and present != expected):
                raise BackupError(
                    "The archive does not hold the master key, and the key in place is not the "
                    "backup's: put the saved key in "
                    f"{settings.key_file} (with Docker: docker compose cp master.key "
                    "app:/data/master.key), then run again."
                )
        revision = str(manifest.get("revision", ""))
        if not known_revision(settings.database_url, revision):
            raise BackupError(
                f"Backup made by a newer version of Synaptane ({revision}): "
                "install at least that version before restoring it."
            )
        database = Database(settings.database_url, f"{APP_NAME}-restore")
        try:
            with database.session() as session:
                others = session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND pid <> pg_backend_pid() AND application_name LIKE :app"
                    ),
                    {"app": f"{APP_NAME}%"},
                )
                try:
                    users = session.scalar(select(func.count()).select_from(User)) or 0
                except ProgrammingError:  # empty database, no tables
                    session.rollback()
                    users = 0
            if others:
                raise BackupError(
                    "The application or the worker is connected to the database: stop them "
                    "before restoring (docker compose stop app worker)."
                )
            if users and not force:
                raise BackupError(
                    "This instance already has accounts: run again with --force to replace "
                    "them with the backup."
                )
        finally:
            database.dispose()
        dsn, env = _libpq(settings.database_url)
        # 1. The whole dump is first converted to SQL: a damaged or too recent dump fails here,
        #    before the database is touched.
        script = unpacked / "restore.sql"
        _run(
            [
                "pg_restore",
                "--no-owner",
                "--no-privileges",
                f"--file={script}",
                str(unpacked / DUMP),
            ],
            env,
        )
        # 2. Reset and restore in a single transaction: on error, the database stays as it was.
        #    (The backup may come from an earlier version, whose schema differs from the one
        #    `migrate` has just created.)
        reset = unpacked / "reset.sql"
        reset.write_text("DROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n", encoding="utf-8")
        _run(
            [
                "psql",
                "--no-psqlrc",
                "--quiet",
                "--single-transaction",
                "--set=ON_ERROR_STOP=1",
                f"--dbname={dsn}",
                f"--file={reset}",
                f"--file={script}",
            ],
            env,
        )
        if key.is_file():
            fd = os.open(f"{settings.key_file}.new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(key.read_bytes())
            os.replace(f"{settings.key_file}.new", settings.key_file)
        repos = unpacked / REPOS
        had_repos = repos.is_dir()
        shutil.rmtree(settings.repos_dir, ignore_errors=True)
        if had_repos:
            repos.rename(settings.repos_dir)
            for copy in settings.repos_dir.iterdir():  # an archive may come from elsewhere
                if copy.is_dir() and not copy.is_symlink():
                    repo.sanitize(copy)
        else:
            settings.repos_dir.mkdir()  # the worker will clone again from the remote repositories
        shutil.rmtree(settings.data_dir / "extract", ignore_errors=True)
    upgrade(settings.database_url)  # backup from an earlier version
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            spaces = list(session.scalars(select(Space.id)))
            for space_id in spaces:
                jobs.enqueue(session, "sync", space_id)
                jobs.enqueue(session, "index", space_id)
    finally:
        database.dispose()
    return RestoreInfo(
        created_at=str(manifest.get("created_at", "?")),
        version=str(manifest.get("version", "?")),
        commit=str(manifest.get("commit") or UNKNOWN_COMMIT),
        spaces=len(spaces),
        repos=had_repos,
    )
