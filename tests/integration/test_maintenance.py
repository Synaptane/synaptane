"""Nightly maintenance: the problems people introduce are fixed even with an empty inbox,
without catching up on the past, without a flood and without asking twice."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.services import budget, changes, content, git_sync, maintenance, tidy
from synaptane.services.tidy_tools import Op, Overlay, RunState
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import MaintenanceMark, Space
from tests.integration.conftest import token_for
from tests.integration.gitutil import git
from tests.integration.test_tidy import Scripted, configure_ai
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki

NAME = maintenance.NAME


def write(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, path: str, text: str
) -> None:
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, text.encode())
    db.commit()


def reviewed(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    """The page changes are already reviewed: the inbox is empty."""
    pending = changes.pending(db, ctx, wiki.infra)
    head = ctx.local_repository(wiki.infra).head()
    assert head is not None
    changes.record(db, wiki.infra, {c.path: ("done", "vu", c) for _, c in pending}, head)
    db.flush()
    changes.compact(db, ctx, wiki.infra)
    db.commit()
    assert changes.pending(db, ctx, wiki.infra) == []


def space(db: Session, wiki: Wiki) -> Space:
    db.expire_all()
    found = db.get(Space, wiki.infra.id)
    assert found is not None
    return found


def pending(db: Session, ctx: Workspace, wiki: Wiki) -> maintenance.Found | None:
    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    assert head is not None
    found = maintenance.pending(db, ctx, space(db, wiki), Overlay(repo, head))
    db.commit()
    return found


def problems(found: maintenance.Found | None) -> list[tuple[str, str, str]]:
    return [(p.kind, p.page, p.detail) for p in found.problems] if found else []


def test_an_empty_inbox_still_gets_the_new_problems_fixed(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, client: TestClient
) -> None:
    configure_ai(db, ctx, wiki)
    head = ctx.local_repository(wiki.infra).head()
    assert space(db, wiki).maintain_watermark == head  # set by the sync: nothing to catch up
    write(
        db, database, ctx, wiki, "serveurs/nas.md", "# NAS\n\nSauvé par [Proxmox](proxmox2.md).\n"
    )
    reviewed(db, ctx, wiki)
    script = Scripted(
        [
            [("read_inbox_item", {"item": NAME})],
            [
                ("edit_page", {"path": "serveurs/nas.md", "old": "proxmox2", "new": "proxmox"}),
                (
                    "mark_done",
                    {
                        "item": NAME,
                        "summary": "Lien réparé. P2: page à relier par l'équipe",
                        "pages": ["serveurs/nas.md"],
                    },
                ),
            ],
            [("finish", {"summary": "Entretien fait"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit, record
    assert f'- {NAME} [maintenance] "Wiki maintenance (2)"' in script.first
    read = script.results[0].content
    assert re.match(r"<inbox_item_\w+ ", read)  # the pages and links are data
    assert "P1 — serveurs/nas.md: broken link proxmox2.md" in read
    assert "P2 — serveurs/nas.md: no incoming link" in read
    assert 'item of type "maintenance"' in script.system
    logged = [
        (op["op"], op["path"], op["detail"])
        for op in record.operations
        if op["op"] in (Op.FIXED, Op.PROBLEM_LEFT)
    ]
    assert logged == [
        (Op.FIXED, "serveurs/nas.md", "broken link proxmox2.md"),
        (Op.PROBLEM_LEFT, "serveurs/nas.md", "no incoming link — page à relier par l'équipe"),
    ]
    assert (record.items_done, record.items_left) == (0, 0)  # not an inbox item
    counted = (record.metrics or {}).get("items")
    assert isinstance(counted, dict) and counted["done"] == 0
    repo = ctx.local_repository(wiki.infra)
    assert b"[Proxmox](proxmox.md)" in (repo.blob("content/serveurs/nas.md") or b"")
    subject = git(ctx.clone_path(wiki.infra.id), "log", "-1", "--format=%s")
    assert subject.endswith("0 item(s) filed, 1 wiki problem(s) fixed"), subject
    # The watermark moves to the run's commit; the problem left is remembered.
    assert space(db, wiki).maintain_watermark == record.commit
    mark = db.scalars(select(MaintenanceMark)).one()
    assert (mark.kind, mark.page, mark.reason) == (
        "orphan",
        "serveurs/nas.md",
        "page à relier par l'équipe",
    )
    # The log says it in the reader's language, the agent's reason as it is.
    login(client, "root")
    log = client.get("/o/acme/infra/tidy").text
    assert "Problème du wiki corrigé" in log and "lien cassé proxmox2.md" in log
    assert "aucun lien entrant — page à relier par l&#39;équipe" in log
    # The next night: nothing new, no run and no log entry.
    assert (
        tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=Scripted([]).factory)
        is None
    )


def test_a_left_problem_comes_back_only_once_its_page_changed(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    write(db, database, ctx, wiki, "brouillon.md", "# Brouillon\n")
    found = pending(db, ctx, wiki)
    assert problems(found) == [("not_in_index", "brouillon.md", "README.md")]
    assert found is not None and found.complete
    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    assert head is not None
    state = RunState(items={}, left={NAME: "P1: brouillon personnel"})
    closure = maintenance.close(found, state, Overlay(repo, head))
    assert [(p.page, why) for p, why in closure.left] == [("brouillon.md", "brouillon personnel")]
    maintenance.settle(db, ctx, space(db, wiki), found, closure, head, None)
    db.commit()
    assert space(db, wiki).maintain_watermark == head
    assert pending(db, ctx, wiki) is None  # left, page unchanged: not asked again

    write(db, database, ctx, wiki, "brouillon.md", "# Brouillon\n\nSuite.\n")
    assert problems(pending(db, ctx, wiki)) == [("not_in_index", "brouillon.md", "README.md")]

    # Fixed by hand: the problem has gone, and so has its memory.
    write(db, database, ctx, wiki, "README.md", "# Infra\n\n[Brouillon](brouillon.md)\n")
    assert pending(db, ctx, wiki) is None
    assert db.scalars(select(MaintenanceMark)).all() == []


def test_what_counts_is_what_people_broke(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """A page deleted by someone breaks the links that pointed to it, in pages nobody
    touched; what Synaptane's own commits (the tidy-up) break is left to the run's own check."""
    content.delete_file(db, database, wiki.alice, ctx, wiki.infra, "serveurs/proxmox.md")
    db.commit()
    assert problems(pending(db, ctx, wiki)) == [("broken_link", "README.md", "serveurs/proxmox.md")]
    pending(db, ctx, wiki)  # computed twice: the watermark has not moved
    assert space(db, wiki).maintain_watermark != ctx.local_repository(wiki.infra).head()

    clone = ctx.clone_path(wiki.infra.id)
    (clone / "content" / "auto.md").write_text("# Auto\n\n[Cassé](nulle-part.md)\n")
    git(clone, "add", "-A")
    git(
        clone,
        "-c",
        "user.name=Synaptane",
        "-c",
        "user.email=synaptane@localhost",
        "commit",
        "-qm",
        "Tidy-up: auto",
        author=ctx.committer,
    )
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert problems(pending(db, ctx, wiki)) == [("broken_link", "README.md", "serveurs/proxmox.md")]


def test_no_catching_up_and_at_most_ten_problems_a_night(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    tidy.update_settings(db, wiki.root, wiki.infra, maintain=False)
    db.commit()
    tidy.update_settings(db, wiki.root, wiki.infra, maintain=True)  # turned on again
    db.commit()
    assert space(db, wiki).maintain_watermark is None
    batch: dict[str, str | None] = {
        f"content/lot/p{n}.md": f"# P{n}\n\n[Ailleurs](x{n}.md)\n" for n in range(12)
    }
    wiki.remote.push(batch, "Lot 1")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    watermark = space(db, wiki).maintain_watermark
    assert watermark == ctx.local_repository(wiki.infra).head()  # the past is not caught up
    assert pending(db, ctx, wiki) is None

    wiki.remote.push({k.replace("p", "q", 1): v for k, v in batch.items()}, "Lot 2")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    found = pending(db, ctx, wiki)
    assert found is not None and len(found.problems) == maintenance.MAX_PROBLEMS == 10
    assert {p.kind for p in found.problems} == {"broken_link"}  # the most urgent first
    assert found.waiting == 24 and not found.complete
    assert "14 more will come in the next runs." in found.describe()
    assert space(db, wiki).maintain_watermark == watermark  # moves once all are given


def test_nothing_is_computed_without_budget_or_when_turned_off(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_ai(db, ctx, wiki)
    watermark = space(db, wiki).maintain_watermark
    write(db, database, ctx, wiki, "a.md", "# A\n\n[B](b.md)\n")
    reviewed(db, ctx, wiki)
    with monkeypatch.context() as patch:
        patch.setattr(budget, "refusal", lambda *_: "Monthly AI budget reached.")
        assert tidy.run(database, ctx, wiki.infra.id, trigger="schedule") is None
    tidy.update_settings(db, wiki.root, wiki.infra, maintain=False)
    db.commit()
    assert tidy.run(database, ctx, wiki.infra.id, trigger="schedule") is None
    # A run started after a drop does not maintain either: the scheduled one does.
    tidy.update_settings(db, wiki.root, wiki.infra, maintain=True)
    db.commit()
    assert tidy.run(database, ctx, wiki.infra.id, trigger="deposit") is None
    assert (
        space(db, wiki).maintain_watermark is None
        or space(db, wiki).maintain_watermark == watermark
    )


def test_an_interrupted_run_gives_the_problems_again(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    watermark = space(db, wiki).maintain_watermark
    write(db, database, ctx, wiki, "a.md", "# A\n\n[B](b.md)\n")
    reviewed(db, ctx, wiki)
    script = Scripted([[("read_inbox_item", {"item": NAME})]])  # then stops, item open
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None
    assert [op["op"] for op in record.operations] == [Op.UNTREATED]
    assert record.operations[0]["path"] == "Wiki maintenance (2)"
    assert space(db, wiki).maintain_watermark == watermark
    assert db.scalars(select(MaintenanceMark)).all() == []
    assert len(problems(pending(db, ctx, wiki))) == 2


def test_the_setting_in_the_web_and_in_rest(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, client: TestClient
) -> None:
    headers = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    url = "/api/v1/organizations/acme/spaces/infra/tidy-settings"
    assert client.patch(url, json={"maintain": False}, headers=headers).status_code == 204
    assert not space(db, wiki).tidy_maintain
    login(client, "root")
    page = client.get("/o/acme/infra/-/tidy-settings").text
    assert 'name="tidy_maintain" value="true" aria-describedby="tidy-maintain-help" >' in page
    assert "Entretenir le wiki chaque nuit" in page and "10 au plus par nuit" in page
    form = {
        "csrf": re.findall(r'name="csrf" value="([^"]+)"', page)[0],
        "tidy_time": "02:00",
        "tidy_timezone": "Europe/Paris",
        "tidy_enabled": "true",
        "tidy_maintain": "true",
    }
    client.post("/o/acme/infra/-/tidy-settings", data=form)
    found = space(db, wiki)
    assert found.tidy_maintain and found.maintain_watermark is None  # from the head, later
