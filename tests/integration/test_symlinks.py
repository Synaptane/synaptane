"""A symbolic link in the repository must never cause a write outside the local copy."""

import os

import pytest
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.services import content, git_sync
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.gitutil import git
from tests.integration.wikidata import Wiki


def test_symlink_from_the_remote_is_a_plain_file(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    git(wiki.remote.work, "pull", "-q", "--ff-only", "origin", "main")
    os.symlink("../.git", wiki.remote.work / "content" / "g")
    git(wiki.remote.work, "add", "-A")
    git(wiki.remote.work, "commit", "-qm", "Lien piégé")
    git(wiki.remote.work, "push", "-q", "origin", "HEAD:main")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    repo = ctx.local_repository(wiki.infra)
    link = repo.path / "content" / "g"
    assert not link.is_symlink() and link.read_text() == "../.git"
    config = (repo.path / ".git" / "config").read_bytes()
    with pytest.raises(SynaptaneError, match="is a file"):
        content.write_file(db, database, wiki.alice, ctx, wiki.infra, "g/config", b"[core]\n")
    assert (repo.path / ".git" / "config").read_bytes() == config


def test_symlink_left_by_an_old_copy_is_removed_before_writing(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    repo = ctx.local_repository(wiki.infra)
    os.symlink("../.git", repo.path / "content" / "g")  # copy made before the fix
    config = (repo.path / ".git" / "config").read_bytes()
    # Link not followed: the copy is repaired (link removed) before the write, which stays
    # in content/; .git/config is not touched.
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "g/config", b"[core]\n")
    assert (repo.path / ".git" / "config").read_bytes() == config
    assert not (repo.path / "content" / "g").is_symlink()
    assert (repo.path / "content" / "g" / "config").read_bytes() == b"[core]\n"
    git_sync.run_sync(database, ctx, wiki.infra.id)


def test_nested_git_directory_is_refused(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    with pytest.raises(SynaptaneError, match="Invalid path"):
        content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a/.git/config", b"x")
