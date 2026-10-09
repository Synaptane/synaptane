"""Without tidying, the change watermark still moves forward, the inbox is computed in a
fixed number of Git calls, and no backlog alert goes out every day."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.git.repo import Repository
from synaptane.services import ai_settings, alerts, changes, content, deposits
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Space
from synaptane.worker.runner import Worker
from tests.integration.gitutil import git
from tests.integration.wikidata import Wiki


def watermark(db: Session, wiki: Wiki) -> str | None:
    db.expire_all()
    return db.scalar(select(Space.change_watermark).where(Space.id == wiki.infra.id))


def configure_ai(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    ai_settings.configure(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        provider="anthropic",
        model="claude-opus-5-5",
        api_key="sk-ant-test",
    )
    db.commit()


def test_dismissing_every_change_advances_the_watermark(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    before = watermark(db, wiki)
    for n in range(3):
        content.write_file(db, database, wiki.alice, ctx, wiki.infra, f"p{n}.md", b"# P\n")
    db.commit()
    names = [item.name for item, _ in changes.pending(db, ctx, wiki.infra)]
    assert len(names) == 3
    for name in names[:-1]:
        deposits.dismiss_change(db, wiki.alice, ctx, wiki.infra, name)
        db.commit()
    assert watermark(db, wiki) == before  # one page is still waiting
    deposits.dismiss_change(db, wiki.alice, ctx, wiki.infra, names[-1])
    db.commit()
    head = ctx.local_repository(wiki.infra).head()
    assert watermark(db, wiki) == head != before
    assert changes.pending(db, ctx, wiki.infra) == []


def test_pending_needs_one_rev_list_whatever_the_number_of_decisions(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(12):
        content.write_file(db, database, wiki.alice, ctx, wiki.infra, f"p{n}.md", b"# P\n")
    db.commit()
    found = changes.pending(db, ctx, wiki.infra)
    # One decision per page, each on its own commit: as many distinct `until` values.
    for _, change in found[:-1]:
        changes.record(db, wiki.infra, {change.path: ("left", "À voir", change)}, change.last)
    db.commit()
    calls: list[str] = []
    original = Repository._git

    def counting(self: Repository, *args: str, **kwargs: object) -> object:
        calls.append(args[0])
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Repository, "_git", counting)
    statuses = sorted(item.status for item, _ in changes.pending(db, ctx, wiki.infra))
    assert statuses == ["left"] * 11 + ["pending"]
    assert calls.count("rev-list") == 1
    assert calls.count("rev-parse") <= 3  # local copy, HEAD, watermark: not one per decision

    # A page left then edited comes back as new (not covered by its decision).
    monkeypatch.setattr(Repository, "_git", original)
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "p0.md", b"# P\n\nsuite\n")
    db.commit()
    db.expire_all()
    fresh = [i.title for i, _ in changes.pending(db, ctx, wiki.infra) if i.status == "pending"]
    assert "Page created: p0.md" in fresh


def test_the_worker_advances_the_watermark_of_spaces_without_tidy(
    settings: Settings, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    clone = ctx.clone_path(wiki.infra.id)
    (clone / "content" / "README.md").write_text(
        "# Infra\n\nVoir  [serveurs](serveurs/proxmox.md).\n"
    )
    git(clone, "commit", "-qam", "Espaces seulement")  # nothing to review, but a new commit
    head = ctx.local_repository(wiki.infra).head()
    assert changes.pending(db, ctx, wiki.infra) == [] and watermark(db, wiki) != head

    configure_ai(db, ctx, wiki)
    worker = Worker(settings, database, ctx)
    worker.advance_idle_watermarks()
    assert watermark(db, wiki) != head  # tidying active: moving the watermark is its job

    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.tidy_enabled = False
    db.commit()
    worker.advance_idle_watermarks()
    assert watermark(db, wiki) == head


def test_no_backlog_alert_without_active_tidy(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deposits.deposit(db, database, wiki.alice, ctx, wiki.infra, title="N", body="x", source="web")
    db.commit()
    monkeypatch.setattr(alerts, "BACKLOG_DAYS", -1)
    assert alerts.check_backlogs(database, ctx) == 0  # without AI: the inbox waits for a human
    configure_ai(db, ctx, wiki)
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.tidy_enabled = False
    db.commit()
    assert alerts.check_backlogs(database, ctx) == 0  # tidying disabled
    space.tidy_enabled = True
    db.commit()
    assert alerts.check_backlogs(database, ctx) == 1
    db.expire_all()
    assert db.scalars(select(AuditEvent.action).where(AuditEvent.action.like("alert.%"))).all() == [
        "alert.inbox_backlog"
    ]
