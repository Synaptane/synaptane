"""Nothing local is lost: repository change, rewritten remote history."""

import contextlib
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.git.repo import GitError
from synaptane.services import changes, content, deposits, git_sync, organizations, spaces
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Space
from tests.integration.conftest import actor_of, make_user
from tests.integration.gitutil import Remote, git
from tests.integration.wikidata import Wiki


def pending_titles(db: Session, ctx: Workspace, space_id: object) -> list[str]:
    db.expire_all()
    found = db.get(Space, space_id)
    assert found is not None
    return sorted(f"{i.status}:{i.title}" for i, _ in changes.pending(db, ctx, found))


def test_fixing_the_url_keeps_unpushed_work(
    db: Session, database: Database, ctx: Workspace, tmp_path: Path
) -> None:
    wrong = Remote(tmp_path / "mauvais")  # push rejected (read-only key…)
    hook = wrong.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'deploy key is read-only' >&2\nexit 1\n")
    hook.chmod(0o755)
    root = actor_of(db, make_user(db, "root", superadmin=True))
    org = organizations.create_organization(db, root, slug="acme", name="Acme")
    space = spaces.create_space(db, root, org, slug="infra", name="Infra", repo_url=wrong.url)
    db.commit()
    with contextlib.suppress(GitError):  # skeleton committed, push rejected
        git_sync.run_sync(database, ctx, space.id)
    content.write_file(db, database, root, ctx, space, "procedure.md", b"# Procedure\n")
    deposits.deposit(db, database, root, ctx, space, title="Note", body="Info", source="web")
    db.commit()

    good = Remote(tmp_path / "bon")
    db.expire_all()
    found = db.get(Space, space.id)
    assert found is not None
    spaces.update_space(db, root, found, repo_url=good.url)
    db.commit()
    git_sync.run_sync(database, ctx, space.id)

    assert good.read("content/procedure.md") == "# Procedure\n"  # carried over and pushed
    db.expire_all()
    found = db.get(Space, space.id)
    assert found is not None and found.sync_status == "ok"
    assert [i.title for i in deposits.list_items(root, ctx, found)] == ["Note"]
    bundles = list(ctx.recovery_path(space.id).glob("*.bundle"))
    assert len(bundles) == 1  # the old branch is kept in full
    event = db.scalar(
        select(AuditEvent).where(AuditEvent.action == "space.local_changes_recovered")
    )
    assert event is not None and event.details["reason"] == "repository changed"
    # The carried-over pages remain to be reviewed by tidying.
    assert "pending:Page created: procedure.md" in pending_titles(db, ctx, space.id)


def test_a_new_repository_keeps_change_tracking(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, tmp_path: Path
) -> None:
    other = Remote(tmp_path / "nouvelle-forge")
    other.push({"content/README.md": "# Infra (nouvelle forge)\n"})
    found = db.get(Space, wiki.infra.id)
    assert found is not None
    spaces.update_space(db, wiki.root, found, repo_url=other.url)
    db.commit()
    git_sync.run_sync(database, ctx, wiki.infra.id)
    db.expire_all()
    found = db.get(Space, wiki.infra.id)
    assert found is not None and found.sync_status == "ok"
    content.write_file(db, database, wiki.alice, ctx, found, "nouvelle.md", b"# Nouvelle\n")
    db.commit()
    other.push({"content/externe.md": "# Externe\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    titles = pending_titles(db, ctx, wiki.infra.id)
    assert "pending:Page created: nouvelle.md" in titles
    assert "pending:Page created: externe.md" in titles


def test_a_purged_commit_is_not_pushed_back(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    remote = wiki.remote
    remote.push({"content/secret.md": "# Clé\n\nclé de prod\n"}, "Ajout par erreur")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    # Meanwhile, someone writes in Synaptane (not yet pushed at the time of the purge).
    git(remote.work, "reset", "--hard", "HEAD~1")
    git(remote.work, "push", "--force", "-q", "origin", "HEAD:main")
    clone = ctx.clone_path(wiki.infra.id)
    (clone / "content" / "locale.md").write_text("# Locale\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "Écriture locale", author=ctx.committer)

    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert remote.read("content/secret.md") is None  # the purge is honored
    assert remote.read("content/locale.md") == "# Locale\n"  # local work is carried over
    db.expire_all()
    found = db.get(Space, wiki.infra.id)
    assert found is not None and found.sync_status == "ok"
    assert ctx.local_repository(found).rev(found.change_watermark or "") is not None


def test_an_unrelated_history_is_adopted(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    remote = wiki.remote
    git(remote.work, "checkout", "-q", "--orphan", "neuve")
    git(remote.work, "rm", "-rq", "--cached", ".")
    (remote.work / "content").mkdir(exist_ok=True)
    (remote.work / "content" / "depart.md").write_text("# Départ\n")
    git(remote.work, "add", "content/depart.md")
    git(remote.work, "commit", "-qm", "Repart de zéro")
    git(remote.work, "push", "--force", "-q", "origin", "HEAD:main")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    db.expire_all()
    found = db.get(Space, wiki.infra.id)
    assert found is not None and found.sync_status == "ok"
    assert ctx.local_repository(found).blob("content/depart.md") == "# Départ\n".encode()


def test_tricky_names_and_subjects_are_tracked(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    wiki.remote.push(
        {
            'content/Réunion "kickoff".md': "# Kickoff\n",
            "content/dossier\\note.md": "# Note\n",
            "content/tab\tulé.md": "# Tab\n",
        },
        "Sujet avec \x1e séparateur",
    )
    git_sync.run_sync(database, ctx, wiki.infra.id)
    titles = pending_titles(db, ctx, wiki.infra.id)
    assert 'pending:Page created: Réunion "kickoff".md' in titles
    assert "pending:Page created: dossier\\note.md" in titles
    assert "pending:Page created: tab\tulé.md" in titles
    from synaptane.services import activity

    events, _ = activity.space_activity(wiki.alice, ctx, wiki.infra)
    assert "Sujet avec \x1e séparateur" in [e.subject for e in events]


def test_a_stale_index_lock_is_repaired(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    lock = ctx.clone_path(wiki.infra.id) / ".git" / "index.lock"
    lock.write_text("")
    old = time.time() - 600
    os.utime(lock, (old, old))  # left by a worker killed 10 minutes ago
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "apres.md", b"# Apres\n")
    db.commit()
    assert not lock.exists()
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert wiki.remote.read("content/apres.md") == "# Apres\n"


def test_old_change_files_are_removed(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    old = "---\ntype: change\ntitle: Ancien\n---\n\nmodification\n"
    wiki.remote.push({".synaptane/inbox/2026-10-01T10-00-00Z-change-proxmox-abcd.md": old})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert not [p for p in wiki.remote.files(".synaptane/inbox/") if "-change-" in p]
    assert "remove old inbox format" in wiki.remote.last_commit()


def make_conflict(database: Database, ctx: Workspace, wiki: Wiki) -> None:
    from tests.integration.gitutil import ALICE

    repo = ctx.local_repository(wiki.infra)
    repo.commit_changes({"content/README.md": b"# Local\n"}, ALICE, "local")
    wiki.remote.push({"content/README.md": "# Distant\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)


def test_a_second_resolution_is_harmless(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    make_conflict(database, ctx, wiki)
    choice = {"content/README.md": "local"}
    git_sync.run_resolution(database, ctx, wiki.infra.id, choice)
    git_sync.run_resolution(database, ctx, wiki.infra.id, choice)  # double click
    db.expire_all()
    found = db.get(Space, wiki.infra.id)
    assert found is not None and found.sync_status == "ok"


def test_an_incomplete_resolution_keeps_the_integrated_head(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    from tests.integration.gitutil import ALICE

    make_conflict(database, ctx, wiki)
    db.expire_all()
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    before = space.remote_head
    repo = ctx.local_repository(wiki.infra)
    repo.commit_changes({"content/b.md": b"# B local\n"}, ALICE, "b local")
    wiki.remote.push({"content/b.md": "# B distant\n"}, "Externe b")
    git_sync.run_resolution(database, ctx, wiki.infra.id, {"content/README.md": "local"})
    db.expire_all()
    found = db.get(Space, wiki.infra.id)
    assert found is not None and found.sync_status == "conflict"
    assert found.remote_head == before  # not integrated yet


@pytest.mark.parametrize("action", ["write", "delete"])
def test_a_directory_path_is_a_clear_error(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, action: str
) -> None:
    with pytest.raises(SynaptaneError):
        if action == "write":
            content.write_file(db, database, wiki.alice, ctx, wiki.infra, "serveurs", b"x")
        else:
            content.delete_file(db, database, wiki.alice, ctx, wiki.infra, "serveurs")
