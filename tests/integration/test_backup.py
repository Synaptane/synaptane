"""Backup then restore into a fresh database (requires pg_dump and pg_restore)."""

import io
import json
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text

from synaptane import git_commit
from synaptane.config import Settings
from synaptane.core.errors import BackupError
from synaptane.core.security import SecretBox
from synaptane.git.credentials import GitCredentials
from synaptane.services import (
    accounts,
    ai_settings,
    backup,
    content,
    git_sync,
    organizations,
    spaces,
)
from synaptane.services.secrets import get_space_secret, put_space_secret, readable
from synaptane.storage.db import Database, upgrade
from synaptane.storage.models import Job, Organization, Space, User
from tests.integration.conftest import actor_of, make_user
from tests.integration.gitutil import Remote, workspace_for


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    from synaptane.storage.db import alembic_config

    return str(ScriptDirectory.from_config(alembic_config("postgresql://x")).get_current_head())


pytestmark = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="client PostgreSQL (pg_dump, pg_restore) absent",
)


@pytest.fixture
def empty_database(new_database: Callable[[], str]) -> str:
    return new_database()


def test_backup_then_restore_on_a_new_server(
    settings: Settings, database: Database, empty_database: str, tmp_path: Path
) -> None:
    with database.session() as session:
        root = make_user(session, "root", superadmin=True)
        organizations.create_organization(
            session, actor_of(session, root), slug="acme", name="Acme"
        )
    (settings.repos_dir / "demo").mkdir(parents=True)
    (settings.repos_dir / "demo" / "README.md").write_text("# Démo\n")

    archive = io.BytesIO()
    info = backup.backup(settings, archive, with_repos=True)
    assert info.key and info.repos
    with tarfile.open(fileobj=io.BytesIO(archive.getvalue())) as tar:
        names = tar.getnames()
    assert {"manifest.json", "database.dump", "master.key", "repos/demo/README.md"} <= set(names)

    # New server: empty database, key generated at first startup, nothing else.
    target = Settings(database_url=empty_database, data_dir=tmp_path / "new")
    target.data_dir.mkdir()
    target.key_file.write_bytes(SecretBox.generate_key())
    restored = backup.restore(target, io.BytesIO(archive.getvalue()))
    assert restored.repos
    assert target.key_file.read_bytes() == settings.key_file.read_bytes()
    assert (target.repos_dir / "demo" / "README.md").read_text() == "# Démo\n"
    copy = Database(empty_database)
    try:
        with copy.session() as session:
            assert session.scalars(select(User.username)).all() == ["root"]
            assert session.scalars(select(Organization.slug)).all() == ["acme"]
            assert session.scalars(select(Job)).all() == []  # no space to resynchronize
    finally:
        copy.dispose()

    # An instance that already has accounts is not overwritten without --force.
    with pytest.raises(BackupError, match="--force"):
        backup.restore(target, io.BytesIO(archive.getvalue()))


def test_an_instance_with_a_space_comes_back_whole(
    settings: Settings, database: Database, empty_database: str, tmp_path: Path
) -> None:
    """Space linked to a repository, page not pushed yet, encrypted secrets (AI key, Git
    credentials) → backup → fresh database → restore. The secrets can be read back with the
    restored key, synchronization and indexing are queued again, and the page written before
    the outage ends up on the remote repository."""
    remote = Remote(tmp_path / "remote")
    remote.push({"content/README.md": "# Infra\n"})
    ctx = workspace_for(settings)
    with database.session() as session:
        root = actor_of(session, make_user(session, "root", superadmin=True))
        org = organizations.create_organization(session, root, slug="acme", name="Acme")
        space = spaces.create_space(
            session, root, org, slug="infra", name="Infra", repo_url=remote.url
        )
        ai_settings.configure(
            session, root, ctx.box, org, provider="anthropic", api_key="sk-ant-sauvegarde"
        )
        token = GitCredentials.https("jeton-git-sauvegarde", "bot").to_json()
        put_space_secret(session, ctx.box, space.id, git_sync.GIT_CREDENTIALS, token, None)
        space_id = space.id
    git_sync.run_sync(database, ctx, space_id)
    with database.session() as session:
        actor = actor_of(session, accounts.get_user(session, "root"))
        found = session.get(Space, space_id)
        assert found is not None
        content.write_file(session, database, actor, ctx, found, "panne.md", b"# Avant la panne\n")
        session.execute(delete(Job))  # the worker has not run: not pushed yet
    assert remote.read("content/panne.md") is None

    archive = tmp_path / "sauvegarde.tar.gz"
    with archive.open("wb") as out:
        info = backup.backup(settings, out)
    assert (info.users, info.spaces) == (1, 1)
    manifest = backup.verify(settings, archive)
    assert manifest["counts"] == {"users": 1, "organizations": 1, "spaces": 1}

    target = Settings(
        database_url=empty_database, data_dir=tmp_path / "new", allow_local_repos=True
    )
    target.data_dir.mkdir()
    target.key_file.write_bytes(SecretBox.generate_key())  # first-startup key
    with archive.open("rb") as source:
        restored = backup.restore(target, source)
    assert restored.spaces == 1 and restored.repos

    copy = Database(empty_database)
    try:
        box = SecretBox(target.key_file.read_bytes())
        with copy.session() as session:
            assert readable(session, box) is True
            found = session.get(Space, space_id)
            assert found is not None
            config = ai_settings.config_for(session, box, found)
            assert config is not None and config.api_key == "sk-ant-sauvegarde"
            git_secret = get_space_secret(session, box, space_id, git_sync.GIT_CREDENTIALS)
            assert git_secret is not None
            assert GitCredentials.from_json(git_secret).token == "jeton-git-sauvegarde"
            queued = {(job.kind, job.space_id) for job in session.scalars(select(Job))}
            assert queued == {("sync", space_id), ("index", space_id)}
        # The worker resumes: the page left in the local copy goes to the repository.
        git_sync.run_sync(copy, workspace_for(target), space_id)
    finally:
        copy.dispose()
    assert remote.read("content/panne.md") == "# Avant la panne\n"


def test_verify_catches_a_truncated_or_incomplete_archive(
    settings: Settings, database: Database, tmp_path: Path
) -> None:
    archive = tmp_path / "bonne.tar.gz"
    with archive.open("wb") as out:
        backup.backup(settings, out, with_repos=False)
    assert backup.verify(settings, archive)["format"] == backup.FORMAT
    data = archive.read_bytes()

    cut = tmp_path / "coupee.tar.gz"
    cut.write_bytes(data[: len(data) // 2])  # disk full, copy interrupted
    with pytest.raises(BackupError, match="Damaged archive"):
        backup.verify(settings, cut)

    without_dump = tmp_path / "sans-dump.tar.gz"
    without_dump.write_bytes(
        rewrite_archive(data, lambda name, raw: None if name == "database.dump" else raw)
    )
    with pytest.raises(BackupError, match="dump"):
        backup.verify(settings, without_dump)

    damaged = tmp_path / "dump-abime.tar.gz"
    damaged.write_bytes(
        rewrite_archive(
            data, lambda name, raw: raw[: len(raw) // 3] if name == "database.dump" else raw
        )
    )
    with pytest.raises(BackupError, match="pg_restore"):
        backup.verify(settings, damaged)


def test_foreign_archives_are_refused(settings: Settings, tmp_path: Path) -> None:
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:gz") as tar:
        entry = tarfile.TarInfo("../../etc/passwd")
        entry.size = 1
        tar.addfile(entry, io.BytesIO(b"x"))
    with pytest.raises(BackupError, match="Unexpected archive entry"):
        backup.restore(settings, io.BytesIO(evil.getvalue()))
    escape = io.BytesIO()
    with tarfile.open(fileobj=escape, mode="w:gz") as tar:
        entry = tarfile.TarInfo("repos/../../x")
        entry.size = 1
        tar.addfile(entry, io.BytesIO(b"x"))
    with pytest.raises(tarfile.FilterError):
        backup.restore(settings, io.BytesIO(escape.getvalue()))


def test_archive_from_an_older_version_restores_over_a_migrated_base(
    new_database: Callable[[], str], tmp_path: Path
) -> None:
    from alembic import command

    from synaptane.storage.db import alembic_config

    # Old instance (schema 0005); new instance already migrated by `migrate`.
    old_url, new_url = new_database(), new_database()
    command.upgrade(alembic_config(old_url), "0005")
    upgrade(new_url)
    old = Settings(database_url=old_url, data_dir=tmp_path / "old")
    old.data_dir.mkdir()
    old.key_file.write_bytes(SecretBox.generate_key())
    archive = io.BytesIO()
    backup.backup(old, archive, with_repos=False)

    target = Settings(database_url=new_url, data_dir=tmp_path / "new")
    target.data_dir.mkdir()
    target.key_file.write_bytes(SecretBox.generate_key())
    backup.restore(target, io.BytesIO(archive.getvalue()))
    check = Database(new_url)
    try:
        with check.engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert version == head_revision()  # restored at 0005, then upgraded
    finally:
        check.dispose()


def test_restore_refuses_a_newer_archive_or_a_running_instance(
    settings: Settings, database: Database, empty_database: str, tmp_path: Path
) -> None:
    archive = io.BytesIO()
    backup.backup(settings, archive, with_repos=False)
    data = archive.getvalue()
    target = Settings(database_url=empty_database, data_dir=tmp_path / "new")
    target.data_dir.mkdir()
    target.key_file.write_bytes(SecretBox.generate_key())

    running = Database(empty_database)  # the application is still running
    try:
        with running.engine.connect(), pytest.raises(BackupError, match="stop them"):
            backup.restore(target, io.BytesIO(data))
    finally:
        running.dispose()

    newer = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(data)) as source,
        tarfile.open(fileobj=newer, mode="w:gz") as out,
    ):
        for member in source.getmembers():
            payload = source.extractfile(member)
            if member.name == "manifest.json" and payload is not None:
                manifest = json.loads(payload.read())
                manifest["revision"] = "0099"
                raw = json.dumps(manifest).encode()
                member.size = len(raw)
                out.addfile(member, io.BytesIO(raw))
            else:
                out.addfile(member, payload)
    with pytest.raises(BackupError, match="newer version"):
        backup.restore(target, io.BytesIO(newer.getvalue()))


def rewrite_archive(data: bytes, change: Callable[[str, bytes], bytes | None]) -> bytes:
    """Copy of the archive, each member passed through `change` (None: removed)."""
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(data)) as source,
        tarfile.open(fileobj=out, mode="w:gz") as target,
    ):
        for member in source.getmembers():
            payload = source.extractfile(member)
            raw = change(member.name, payload.read() if payload else b"")
            if raw is None:
                continue
            member.size = len(raw)
            target.addfile(member, io.BytesIO(raw))
    return out.getvalue()


def test_the_manifest_names_the_commit_and_older_archives_still_restore(
    settings: Settings,
    database: Database,
    empty_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Know which commit made a backup, without refusing older ones."""
    git_commit.cache_clear()
    monkeypatch.setenv("SYNAPTANE_GIT_SHA", "0123456789abcdef0123456789abcdef01234567")
    try:
        archive = io.BytesIO()
        backup.backup(settings, archive, with_repos=False)
    finally:
        git_commit.cache_clear()
    with tarfile.open(fileobj=io.BytesIO(archive.getvalue())) as tar:
        member = tar.extractfile("manifest.json")
        assert member is not None
        manifest = json.loads(member.read())
    assert manifest["commit"] == "0123456789abcdef0123456789abcdef01234567"

    def without_commit(name: str, raw: bytes) -> bytes:
        if name != "manifest.json":
            return raw
        old = json.loads(raw)
        del old["commit"]
        return json.dumps(old).encode()

    target = Settings(database_url=empty_database, data_dir=tmp_path / "new")
    target.data_dir.mkdir()
    target.key_file.write_bytes(SecretBox.generate_key())
    older = rewrite_archive(archive.getvalue(), without_commit)
    restored = backup.restore(target, io.BytesIO(older))
    assert restored.commit == "unknown" and restored.version == manifest["version"]


def test_a_damaged_dump_leaves_the_base_untouched(
    settings: Settings, database: Database, empty_database: str, tmp_path: Path
) -> None:
    with database.session() as session:
        make_user(session, "root", superadmin=True)
    archive = io.BytesIO()
    backup.backup(settings, archive, with_repos=False)
    target = Settings(database_url=empty_database, data_dir=tmp_path / "new")
    target.data_dir.mkdir()
    target.key_file.write_bytes(SecretBox.generate_key())
    backup.restore(target, io.BytesIO(archive.getvalue()))  # running instance, with "root"

    def truncate(name: str, raw: bytes) -> bytes:
        return raw[: len(raw) // 3] if name == "database.dump" else raw

    damaged = rewrite_archive(archive.getvalue(), truncate)
    with pytest.raises(BackupError, match="pg_restore"):
        backup.restore(target, io.BytesIO(damaged), force=True)
    check = Database(empty_database)
    try:
        with check.session() as session:
            assert session.scalars(select(User.username)).all() == ["root"]  # nothing lost
    finally:
        check.dispose()


def test_an_archive_without_key_needs_the_same_key(
    settings: Settings, database: Database, empty_database: str, tmp_path: Path
) -> None:
    archive = io.BytesIO()
    backup.backup(settings, archive, with_repos=False, with_key=False)
    target = Settings(database_url=empty_database, data_dir=tmp_path / "new")
    target.data_dir.mkdir()
    target.key_file.write_bytes(SecretBox.generate_key())  # new server's fresh key
    with pytest.raises(BackupError, match="put the saved key"):
        backup.restore(target, io.BytesIO(archive.getvalue()))
    target.key_file.write_bytes(settings.key_file.read_bytes())  # the right key put in place
    backup.restore(target, io.BytesIO(archive.getvalue()))


def test_restored_copies_lose_hooks_and_dangerous_config(tmp_path: Path) -> None:
    """An archive from elsewhere must not be able to execute anything."""
    import subprocess

    from synaptane.git import repo

    copy = tmp_path / "copie"
    subprocess.run(["git", "init", "-q", "-b", "main", str(copy)], check=True)
    git = ["git", "-C", str(copy), "config"]
    subprocess.run([*git, "remote.origin.url", "https://forge.example/acme.git"], check=True)
    subprocess.run([*git, "filter.evil.smudge", "touch /tmp/pwned"], check=True)
    subprocess.run([*git, "core.sshCommand", "sh -c 'id'"], check=True)
    subprocess.run([*git, "include.path", "/etc/evil"], check=True)
    hook = copy / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\ntouch /tmp/pwned\n")
    hook.chmod(0o755)

    dropped = repo.sanitize(copy)
    assert {"filter.evil.smudge", "core.sshcommand", "include.path"} <= set(dropped)
    assert not (copy / ".git" / "hooks").exists()
    config = (copy / ".git" / "config").read_text()
    assert "forge.example/acme.git" in config and "evil" not in config
    assert "repositoryformatversion" in config
