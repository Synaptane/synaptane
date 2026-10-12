"""Page changes computed from Git, reviewed by tidying."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.services import changes, content, git_sync
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import ChangeMark, Space
from tests.integration.gitutil import git
from tests.integration.wikidata import Wiki


def titles(db: Session, ctx: Workspace, wiki: Wiki) -> list[str]:
    db.expire_all()
    return [f"{i.status}:{i.title}" for i, _ in changes.pending(db, ctx, wiki.infra)]


def test_rename_is_one_change_and_decisions_follow_new_edits(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.move_file(db, database, wiki.alice, ctx, wiki.infra, "serveurs/notes.txt", "notes.txt")
    db.commit()
    assert titles(db, ctx, wiki) == ["pending:Page moved: notes.txt"]

    [(item, change)] = changes.pending(db, ctx, wiki.infra)
    head = ctx.local_repository(wiki.infra).head()
    assert head is not None
    changes.record(db, wiki.infra, {change.path: ("left", "À relire", change)}, head)
    db.commit()
    assert titles(db, ctx, wiki) == ["left:Page moved: notes.txt"]

    # A new change to the page brings it back as new.
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "notes.txt", b"autre\n")
    db.commit()
    assert titles(db, ctx, wiki) == ["pending:Page moved: notes.txt"]


def test_watermark_advances_once_everything_is_reviewed(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# A\n")
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "b.md", b"# B\n")
    db.commit()
    pending = changes.pending(db, ctx, wiki.infra)
    head = ctx.local_repository(wiki.infra).head()
    assert head is not None and len(pending) == 2
    decisions = {
        pending[0][1].path: ("done", "vu", pending[0][1]),
        pending[1][1].path: ("left", "à trancher", pending[1][1]),
    }
    changes.record(db, wiki.infra, decisions, head)
    db.flush()
    changes.compact(db, ctx, wiki.infra)
    db.commit()
    space = db.get(Space, wiki.infra.id)
    assert space is not None and space.change_watermark == head
    marks = db.scalars(select(ChangeMark.status)).all()
    assert marks == ["left"]  # closed decisions erased, left page kept
    assert titles(db, ctx, wiki) == ["left:Page created: b.md"]


def test_own_commits_are_ignored(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    clone = ctx.clone_path(wiki.infra.id)
    (clone / "content" / "auto.md").write_text("# Auto\n")
    git(clone, "add", "-A")
    git(
        clone,
        "-c",
        "user.name=Synaptane",
        "-c",
        "user.email=synaptane@localhost",
        "commit",
        "-qm",
        "Rangement : auto",
        author=ctx.committer,
    )
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert titles(db, ctx, wiki) == []


def test_a_bulk_import_is_one_item(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    wiki.remote.push(
        {f"content/import/page-{n}.md": f"# Page {n}\n" for n in range(25)}, "Import Notion"
    )
    git_sync.run_sync(database, ctx, wiki.infra.id)
    pending = changes.pending(db, ctx, wiki.infra)
    assert [i.title for i, _ in pending] == ["Import: 25 pages (Import Notion)"]
    [(_, change)] = pending
    text = changes.describe(ctx.local_repository(wiki.infra), wiki.infra, change)
    assert "import/page-0.md" in text and "Do not reread every page" in text


def test_many_commits_and_a_chain_of_renames(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """No commit limit, chained renames followed."""
    for n in range(60):
        content.write_file(
            db, database, wiki.alice, ctx, wiki.infra, "journal.md", f"# J\n\n{n}\n".encode()
        )
    for n in range(30):
        content.write_file(
            db, database, wiki.alice, ctx, wiki.infra, f"lot/p{n}.md", f"# P{n}\n".encode()
        )
    content.move_file(db, database, wiki.alice, ctx, wiki.infra, "serveurs/notes.txt", "a.txt")
    content.move_file(db, database, wiki.alice, ctx, wiki.infra, "a.txt", "b/c.txt")
    db.commit()
    found = {change.path: change for _, change in changes.pending(db, ctx, wiki.infra)}
    assert len(found) == 32  # log, 30 pages, the file moved twice
    assert len(found["journal.md"].commits) == 60
    moved = found["b/c.txt"]
    assert (moved.status, moved.previous) == ("moved", "serveurs/notes.txt")


def test_a_page_created_then_deleted_is_not_reported(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "brouillon.md", b"# B\n")
    content.delete_file(db, database, wiki.alice, ctx, wiki.infra, "brouillon.md")
    db.commit()
    assert titles(db, ctx, wiki) == []


def test_a_left_page_comes_back_after_a_new_edit_and_survives_compaction(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# A\n")
    db.commit()
    [(_, change)] = changes.pending(db, ctx, wiki.infra)
    head = ctx.local_repository(wiki.infra).head()
    assert head is not None
    changes.record(db, wiki.infra, {change.path: ("left", "Qui décide ?", change)}, head)
    db.flush()
    changes.compact(db, ctx, wiki.infra)  # the marker moves forward, the left page stays
    db.commit()
    assert titles(db, ctx, wiki) == ["left:Page created: a.md"]
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# A\n\nsuite\n")
    db.commit()
    assert titles(db, ctx, wiki) == ["pending:Page changed: a.md"]


def test_a_force_push_resets_the_review(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Rewritten history → marker realigned, decisions forgotten, and the following changes
    show up again normally."""
    wiki.remote.push({"content/x.md": "# X\n"}, "Ajout")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    [(_, change)] = changes.pending(db, ctx, wiki.infra)
    head = ctx.local_repository(wiki.infra).head()
    assert head is not None
    changes.record(db, wiki.infra, {change.path: ("left", "À voir", change)}, head)
    db.commit()

    git(wiki.remote.work, "reset", "--hard", "HEAD~1")
    git(wiki.remote.work, "push", "--force", "-q", "origin", "HEAD:main")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    db.expire_all()
    space = db.get(Space, wiki.infra.id)
    assert space is not None and space.sync_status == "ok"
    assert ctx.local_repository(space).rev(space.change_watermark or "") is not None
    assert db.scalars(select(ChangeMark)).all() == []
    assert titles(db, ctx, wiki) == []

    wiki.remote.push({"content/y.md": "# Y\n"}, "Après la réécriture")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert titles(db, ctx, wiki) == ["pending:Page created: y.md"]
