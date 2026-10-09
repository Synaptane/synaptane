import dataclasses
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.git.repo import GitError, Identity, Repository
from synaptane.services import (
    accounts,
    changes,
    git_sync,
    jobs,
    load_actor,
    organizations,
    spaces,
)
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, Space
from synaptane.worker.runner import Worker
from tests.integration.conftest import actor_of, make_user
from tests.integration.gitutil import ALICE, COMMITTER, Remote, git


@pytest.fixture
def space(db: Session, remote: Remote) -> Space:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    org = organizations.create_organization(db, root, slug="acme", name="Acme")
    space = spaces.create_space(
        db, root, org, slug="infra", name="Infra", description="Notes d'infra", repo_url=remote.url
    )
    db.commit()
    return space


def status(database: Database, space: Space) -> Space:
    with database.session() as session:
        found = session.get(Space, space.id)
        assert found is not None
        return found


def local_repo(ctx: Workspace, space: Space, remote: Remote) -> Repository:
    return Repository(ctx.repos_dir / str(space.id), remote.url, "main", COMMITTER)


def test_empty_remote_gets_the_space_skeleton(
    database: Database, ctx: Workspace, space: Space, remote: Remote
) -> None:
    git_sync.run_sync(database, ctx, space.id)

    assert remote.read("content/README.md") == "# Infra\n\nNotes d'infra\n"
    assert remote.files(".synaptane/inbox") == [".synaptane/inbox/.gitkeep"]
    assert remote.last_commit() == (
        "Synaptane <synaptane@localhost> / Synaptane <synaptane@localhost> / "
        "Synaptane: initialize space"
    )
    synced = status(database, space)
    assert synced.sync_status == "ok" and synced.synced_at is not None
    assert synced.remote_head == git(remote.bare, "rev-parse", "main")


def test_existing_content_is_cloned_without_change_item(
    database: Database, ctx: Workspace, space: Space, remote: Remote
) -> None:
    remote.push({"content/infra/serveurs.md": "# Serveurs\n", "README.md": "dépôt\n"})
    git_sync.run_sync(database, ctx, space.id)

    assert (ctx.repos_dir / str(space.id) / "content/infra/serveurs.md").exists()
    assert remote.read("content/README.md") is None  # content/ already existed
    assert remote.files(".synaptane/inbox") == [".synaptane/inbox/.gitkeep"]


def pending_changes(database: Database, ctx: Workspace, space: Space) -> list[str]:
    with database.session() as session:
        found = session.get(Space, space.id)
        assert found is not None
        return [f"{i.title} | {i.deposited_by}" for i, _ in changes.pending(session, ctx, found)]


def test_external_push_shows_up_as_a_change(
    database: Database, ctx: Workspace, space: Space, remote: Remote
) -> None:
    git_sync.run_sync(database, ctx, space.id)
    remote.push({"content/runbook.md": "# Runbook\n"}, "Ajout du runbook")
    git_sync.run_sync(database, ctx, space.id)

    # Nothing is written to the repository: the change shows in the history.
    assert [p for p in remote.files(".synaptane/inbox") if p.endswith(".md")] == []
    assert remote.last_commit().endswith("Ajout du runbook")
    assert pending_changes(database, ctx, space) == ["Page created: runbook.md | Alice Externe"]

    # Nothing for a push outside content/, nor for a whitespace-only change.
    remote.push({"README.md": "hors contenu\n"})
    remote.push({"content/runbook.md": "# Runbook\n\n"})
    git_sync.run_sync(database, ctx, space.id)
    assert pending_changes(database, ctx, space) == ["Page created: runbook.md | Alice Externe"]


def test_diverging_histories_are_merged(
    database: Database, ctx: Workspace, space: Space, remote: Remote
) -> None:
    git_sync.run_sync(database, ctx, space.id)
    local_repo(ctx, space, remote).commit_changes(
        {"content/local.md": b"# Local\n"}, Identity("René", "rene@example.com"), "Note locale"
    )
    remote.push({"content/distant.md": "# Distant\n"})
    git_sync.run_sync(database, ctx, space.id)

    assert remote.read("content/local.md") == "# Local\n"
    assert remote.read("content/distant.md") == "# Distant\n"
    assert status(database, space).sync_status == "ok"
    log = git(remote.bare, "log", "--format=%an|%s", "main")
    assert "René|Note locale" in log


@pytest.mark.parametrize(
    ("choice", "expected"), [("local", "# Local\n"), ("remote", "# Distant\n")]
)
def test_conflict_then_resolution(
    database: Database, ctx: Workspace, space: Space, remote: Remote, choice: str, expected: str
) -> None:
    remote.push({"content/page.md": "# Origine\n"})
    git_sync.run_sync(database, ctx, space.id)
    local_repo(ctx, space, remote).commit_changes({"content/page.md": b"# Local\n"}, ALICE, "local")
    remote.push({"content/page.md": "# Distant\n"})

    git_sync.run_sync(database, ctx, space.id)
    conflicted = status(database, space)
    assert conflicted.sync_status == "conflict"
    assert conflicted.conflict_paths == ["content/page.md"]
    assert remote.read("content/page.md") == "# Distant\n"  # nothing was pushed
    with database.session() as session:  # comparison shown before choosing
        root = load_actor(session, accounts.get_user(session, "root"))
        found = session.get(Space, space.id)
        assert found is not None
        diff = git_sync.compare_with_remote(root, ctx, found, "content/page.md")
    assert "-# Local" in diff and "+# Distant" in diff

    git_sync.run_resolution(database, ctx, space.id, {"content/page.md": choice}, root.user.id)
    assert remote.read("content/page.md") == expected
    # The merge commit bears the name of who decided, and what was kept.
    author, body = git(ctx.clone_path(space.id), "log", "-1", "--format=%an%x00%B").split("\0")
    assert author == root.user.display_name
    kept = "Synaptane's version" if choice == "local" else "repository's version"
    assert f"- content/page.md: {kept}" in body and "Decided by" in body
    resolved = status(database, space)
    assert resolved.sync_status == "ok" and resolved.conflict_paths == []
    # The conflicting external commit is still reported to tidying.
    assert any("page.md | Alice Externe" in c for c in pending_changes(database, ctx, space))


def test_local_repositories_are_refused_in_production(
    database: Database, ctx: Workspace, space: Space
) -> None:
    production = dataclasses.replace(ctx, allow_local_repos=False)
    with pytest.raises(GitError):
        git_sync.run_sync(database, production, space.id)
    failed = status(database, space)
    assert failed.sync_status == "error" and "file://" in (failed.sync_error or "")


def test_unreachable_remote_is_a_transient_error(
    db: Session, database: Database, ctx: Workspace, space: Space
) -> None:
    found = db.get(Space, space.id)
    assert found is not None
    found.repo_url = "https://127.0.0.1:9/nope.git"
    db.commit()
    with pytest.raises(GitError) as error:
        git_sync.run_sync(database, ctx, space.id)
    assert error.value.transient
    assert status(database, space).sync_status == "error"


def test_changing_the_url_reclones(
    db: Session, database: Database, ctx: Workspace, space: Space, tmp_path: Path
) -> None:
    git_sync.run_sync(database, ctx, space.id)
    other = Remote(tmp_path / "other")
    other.push({"content/autre.md": "# Autre\n"})
    found = db.get(Space, space.id)
    assert found is not None
    found.repo_url = other.url
    db.commit()
    git_sync.run_sync(database, ctx, space.id)
    clone = ctx.repos_dir / str(space.id)
    assert (clone / "content/autre.md").exists() and not (clone / "content/README.md").exists()


def test_worker_runs_jobs_and_retries_transient_failures(
    db: Session, database: Database, settings: Settings, ctx: Workspace, space: Space
) -> None:
    root = actor_of(db, make_user(db, "admin2", superadmin=True))
    found = db.get(Space, space.id)
    assert found is not None
    git_sync.request_sync(db, root, found)
    git_sync.request_sync(db, root, found)  # merged with the first one
    db.commit()
    worker = Worker(settings, database, ctx)
    while worker.run_once():
        pass
    with database.session() as session:
        done = sorted((j.kind, j.status) for j in session.scalars(select(Job)))
        assert done == [("index", "done"), ("sync", "done")]  # a single sync, then the index

    with database.session() as session:
        session.get(Space, space.id).repo_url = "https://127.0.0.1:9/nope.git"  # type: ignore[union-attr]
        jobs.enqueue(session, git_sync.SYNC, space.id)
    assert worker.run_once()
    with database.session() as session:
        job = session.scalars(select(Job).where(Job.status != "done")).one()
        assert job.status == "queued" and job.attempts == 1 and job.error
        waiting_until = job.run_after

    # "Sync now" restarts the pending sync (after a DNS outage, for example) instead of
    # waiting for the end of its delay between two attempts.
    with database.session() as session:
        space_now = session.get(Space, space.id)
        assert space_now is not None
        git_sync.request_sync(session, root, space_now)
    with database.session() as session:
        job = session.scalars(select(Job).where(Job.status != "done")).one()
        assert job.attempts == 0 and job.error is None and job.run_after < waiting_until
