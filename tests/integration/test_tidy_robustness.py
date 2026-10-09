"""Tidying: fair inbox, unexpected errors, orphan passes, a copy never left dirty."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from synaptane.ai.base import AIConfig, ToolSpec
from synaptane.git.repo import GitError, Repository
from synaptane.services import deposits, tidy
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import TidyRun
from tests.integration.gitutil import COMMITTER
from tests.integration.test_tidy import Scripted, configure_ai
from tests.integration.wikidata import Wiki


def note(db: Session, database: Database, ctx: Workspace, wiki: Wiki, title: str) -> str:
    path = deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title=title, body=title, source="web"
    )
    db.commit()
    return path.rsplit("/", 1)[-1]


def test_left_items_never_block_new_ones(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    old = note(db, database, ctx, wiki, "Ancienne question")
    leave = Scripted([[("leave_in_inbox", {"item": old, "reason": "À confirmer"})]])
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=leave.factory)

    # Scheduled, with nothing new: no model call, no journal entry.
    idle = Scripted([])
    assert tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=idle.factory) is None
    assert idle.first == ""

    note(db, database, ctx, wiki, "Nouvelle info")
    fresh = Scripted([])
    tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=fresh.factory)
    assert fresh.first.index("Nouvelle info") < fresh.first.index("Ancienne question")


def test_unexpected_error_closes_the_run(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    note(db, database, ctx, wiki, "Une note")

    def broken(config: AIConfig, system: str, first: str, tools: list[ToolSpec]) -> Any:
        raise RuntimeError("bogue")

    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=broken)
    assert record is not None and record.status == "failed"
    assert "Internal error" in (record.error or "") and record.finished_at is not None


def test_orphan_runs_are_closed(database: Database, wiki: Wiki) -> None:
    with database.session() as session:
        session.add(
            TidyRun(
                space_id=wiki.infra.id,
                trigger="schedule",
                started_at=datetime.now(UTC) - timedelta(hours=3),
            )
        )
        session.add(TidyRun(space_id=wiki.infra.id, trigger="manual"))  # recent: untouched
    assert tidy.close_orphans(database) == 1


def test_failed_commit_leaves_a_clean_copy(
    ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ctx.local_repository(wiki.infra)
    original = Repository._git

    def failing(self: Repository, *args: str, **kwargs: Any) -> Any:
        if args and args[0] == "commit":
            raise GitError("git commit : disque plein")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Repository, "_git", failing)
    with pytest.raises(GitError):
        repo.commit_changes(
            {"content/nouveau.md": b"# N\n", "content/README.md": "# Écrasé\n".encode()},
            COMMITTER,
            "essai",
        )
    monkeypatch.undo()
    status = repo._out("status", "--porcelain")
    assert status == ""
    assert not (repo.path / "content/nouveau.md").exists()


def test_long_pages_are_read_in_parts_and_never_rewritten_whole(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    from synaptane.services import content

    configure_ai(db, ctx, wiki)
    long_page = "# Long\n\n" + "ligne de contenu\n" * 6000 + "FIN IMPORTANTE\n"
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "long.md", long_page.encode())
    note(db, database, ctx, wiki, "Une note")
    script = Scripted(
        [
            [("read_page", {"path": "long.md"})],
            [("write_page", {"path": "long.md", "content": "# Long\n\nRésumé.\n"})],
            [("read_page", {"path": "long.md", "offset": 60_000})],
        ]
    )
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    first, refused, rest = (r.content for r in script.results[:3])
    assert "truncated" in first and "offset=60000" in first
    assert "edit_page" in refused  # rewrite refused: the end would have been lost
    assert "FIN IMPORTANTE" in rest
    page = content.read_file(wiki.alice, ctx, wiki.infra, "long.md")
    assert page.text is not None and "FIN IMPORTANTE" in page.text


def test_backlinks_follow_the_agent_changes(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    note(db, database, ctx, wiki, "Une note")
    script = Scripted(
        [
            [("backlinks", {"path": "serveurs/proxmox.md"})],
            [
                (
                    "write_page",
                    {"path": "guides/pve.md", "content": "[pve](../serveurs/proxmox.md)\n"},
                )
            ],
            [("read_page", {"path": "README.md"})],
            [("write_page", {"path": "README.md", "content": "# Infra\n\nPlus de lien.\n"})],
            [("backlinks", {"path": "serveurs/proxmox.md"})],
        ]
    )
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    before, *_, after = (r.content for r in script.results[:5])
    assert before == "README.md"
    assert after == "guides/pve.md"
