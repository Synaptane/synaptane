"""Nightly maintenance: the problems people introduce are fixed even with an empty inbox,
without catching up on the past, without a flood and without asking twice."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.git.repo import Repository
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


def leave_all(db: Session, ctx: Workspace, wiki: Wiki, reason: str) -> None:
    """What the agent would do: leave every problem given, with a reason."""
    found = pending(db, ctx, wiki)
    assert found is not None
    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    assert head is not None
    state = RunState(items={}, left={NAME: reason})
    closure = maintenance.close(found, state, Overlay(repo, head))
    assert len(closure.left) == len(found.problems)
    maintenance.settle(db, ctx, space(db, wiki), found, closure, head, None)
    db.commit()
    assert space(db, wiki).maintain_watermark == head


def test_a_left_problem_comes_back_only_if_introduced_again(
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
    assert pending(db, ctx, wiki) is None  # left: not asked again

    # Edited: not enough, the problem was not introduced again.
    write(db, database, ctx, wiki, "brouillon.md", "# Brouillon\n\nSuite.\n")
    assert pending(db, ctx, wiki) is None
    # Deleted then created anew by someone: introduced again.
    content.delete_file(db, database, wiki.alice, ctx, wiki.infra, "brouillon.md")
    db.commit()
    write(db, database, ctx, wiki, "brouillon.md", "# Brouillon\n\nDe retour.\n")
    assert problems(pending(db, ctx, wiki)) == [("not_in_index", "brouillon.md", "README.md")]

    # Fixed by hand: the problem has gone, and so has its memory.
    write(db, database, ctx, wiki, "README.md", "# Infra\n\n[Brouillon](brouillon.md)\n")
    assert pending(db, ctx, wiki) is None
    assert db.scalars(select(MaintenanceMark)).all() == []


def test_a_log_edited_every_day_with_a_link_broken_on_purpose_costs_nothing(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    journal = "# Journal\n\nVoir [la suite](suite.md), à écrire.\n"
    write(db, database, ctx, wiki, "README.md", "# Infra\n\n[Journal](journal.md)\n")
    write(db, database, ctx, wiki, "journal.md", journal)
    assert problems(pending(db, ctx, wiki)) == [("broken_link", "journal.md", "suite.md")]
    leave_all(db, ctx, wiki, "P1: page à venir")
    for day in range(3):  # written around the link, the link's own line reworded
        journal = journal.replace("à écrire", f"à écrire (jour {day})") + f"\n- jour {day}\n"
        write(db, database, ctx, wiki, "journal.md", journal)
        assert pending(db, ctx, wiki) is None
    # The same broken link written once more: introduced again.
    write(db, database, ctx, wiki, "journal.md", journal + "\nEncore [la suite](suite.md).\n")
    assert problems(pending(db, ctx, wiki)) == [("broken_link", "journal.md", "suite.md")]
    leave_all(db, ctx, wiki, "P1: toujours à venir")
    # Removed, then written again in a later commit, between two runs: introduced again.
    write(db, database, ctx, wiki, "journal.md", "# Journal\n\nRien.\n")
    write(db, database, ctx, wiki, "journal.md", "# Journal\n\n[La suite](suite.md)\n")
    assert problems(pending(db, ctx, wiki)) == [("broken_link", "journal.md", "suite.md")]


def test_imports_are_left_to_their_own_item(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """A commit of more than 20 pages is reviewed as one "Import: N pages" item: its problems
    are not the maintenance's; only what people change afterwards counts."""
    batch: dict[str, str | None] = {
        f"content/import/p{n}.md": f"# P{n}\n\n[Ailleurs](x{n}.md)\n" for n in range(25)
    }
    wiki.remote.push(batch, "Import Notion")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert [i.title for i, _ in changes.pending(db, ctx, wiki.infra)] == [
        "Import: 25 pages (Import Notion)"
    ]
    assert pending(db, ctx, wiki) is None
    page = "import/p3.md"
    write(db, database, ctx, wiki, page, "# P3\n\n[Ailleurs](x3.md) et [là](y.md)\n")
    assert problems(pending(db, ctx, wiki)) == [("broken_link", page, "y.md")]


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
    batch: dict[str, str | None] = {
        f"content/lot/p{n}.md": f"# P{n}\n\n[Ailleurs](x{n}.md)\n" for n in range(12)
    }
    wiki.remote.push(batch, "Lot 1")  # while off
    git_sync.run_sync(database, ctx, wiki.infra.id)
    tidy.update_settings(db, wiki.root, space(db, wiki), maintain=True)  # turned on again
    db.commit()
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
    assert str(record.operations[0]["detail"]).endswith("(attempt 1)")
    assert space(db, wiki).maintain_watermark == watermark
    assert db.scalars(select(MaintenanceMark)).all() == []
    assert len(problems(pending(db, ctx, wiki))) == 2
    # A model that never closes the item is not paid for every night: left on the third
    # interrupted run in a row, with the reason, like an inbox item.
    for _ in range(maintenance.MAX_ATTEMPTS - 1):
        nightly = tidy.run(
            database, ctx, wiki.infra.id, trigger="schedule", factory=Scripted([]).factory
        )
        assert nightly is not None
    assert nightly is not None
    assert {op["op"] for op in nightly.operations} == {Op.PROBLEM_LEFT}
    assert "Not processed after 3 interrupted runs" in str(nightly.operations[0]["detail"])
    assert len(db.scalars(select(MaintenanceMark)).all()) == 2
    assert space(db, wiki).maintain_attempts == 0
    assert space(db, wiki).maintain_watermark != watermark
    assert tidy.run(database, ctx, wiki.infra.id, trigger="schedule") is None


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
    # From the last synchronized head: the time it was off is not caught up.
    assert found.tidy_maintain and found.maintain_watermark == found.remote_head


def test_a_very_long_link_left_does_not_make_every_run_fail(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """The link as written has no length limit: its mark must still be recorded, otherwise
    the run fails after its commit and the same problem comes back every night."""
    configure_ai(db, ctx, wiki)
    target = "x" * 40 + "".join(f"{n:04x}" for n in range(800)) + ".md"
    write(db, database, ctx, wiki, "README.md", f"# Infra\n\n[Plus tard]({target})\n")
    reviewed(db, ctx, wiki)
    script = Scripted(
        [
            [("read_inbox_item", {"item": NAME})],
            [("leave_in_inbox", {"item": NAME, "reason": "P1: page à écrire plus tard"})],
            [("finish", {"summary": "Rien à corriger"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=script.factory)
    assert record is not None and record.status == "done", record and record.error
    mark = db.scalars(select(MaintenanceMark)).one()
    assert (mark.detail, mark.reason) == (target, "page à écrire plus tard")
    assert pending(db, ctx, wiki) is None  # left, page unchanged: not asked again


def test_a_fix_lost_in_a_conflict_is_not_taken_for_a_problem_left(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """The fix is in another page (the index), which someone changes during the run: the
    problem is given again, not remembered as left by the agent."""
    configure_ai(db, ctx, wiki)
    watermark = space(db, wiki).maintain_watermark
    write(db, database, ctx, wiki, "nouveau.md", "# Nouveau\n\nTexte.\n")
    reviewed(db, ctx, wiki)

    def human_edit(step: int) -> None:
        if step == 1:
            with database.session() as session:
                content.write_file(
                    session, database, wiki.alice, ctx, wiki.infra, "README.md", b"# Humain\n"
                )

    index = {"path": "README.md", "old": "# Infra", "new": "# Infra\n\n[Nouveau](nouveau.md)"}
    script = Scripted(
        [
            [("read_page", {"path": "README.md"}), ("read_inbox_item", {"item": NAME})],
            [
                ("edit_page", index),
                ("mark_done", {"item": NAME, "summary": "Index complété"}),  # no pages given
            ],
            [("finish", {"summary": "Entretien fait"})],
        ],
        on_step=human_edit,
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=script.factory)
    assert record is not None and any(op["op"] == Op.CONFLICT for op in record.operations)
    assert not any(op["op"] == Op.PROBLEM_LEFT for op in record.operations)
    assert db.scalars(select(MaintenanceMark)).all() == []
    assert space(db, wiki).maintain_watermark == watermark
    assert problems(pending(db, ctx, wiki)) == [("not_in_index", "nouveau.md", "README.md")]


def test_the_first_edit_after_turning_it_on_counts(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Turned on again, maintenance starts from the last synchronized head: the edit that
    triggers the next sync is not taken for the past."""
    tidy.update_settings(db, wiki.root, wiki.infra, maintain=False)
    db.commit()
    content.delete_file(db, database, wiki.alice, ctx, wiki.infra, "serveurs/proxmox.md")
    db.commit()
    git_sync.run_sync(database, ctx, wiki.infra.id)  # while off: the past
    tidy.update_settings(db, wiki.root, space(db, wiki), maintain=True)
    db.commit()
    write(db, database, ctx, wiki, "a.md", "# A\n\n[B](b.md)\n")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert problems(pending(db, ctx, wiki)) == [
        ("broken_link", "a.md", "b.md"),
        ("not_in_index", "a.md", "README.md"),
    ]


def test_a_big_window_is_read_in_a_few_git_calls(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window is computed again every night until all its problems are given: one Git
    process per page (thousands of pages) took most of a minute each night."""
    batch: dict[str, str | None] = {
        f"content/lot/p{n}.md": f"# P{n}\n\n[Ailleurs](x{n}.md)\n" for n in range(40)
    }
    pages = sorted(batch)
    # Twenty files a commit: not an import (`changes.BULK`), counted page by page.
    wiki.remote.push({"content/lot/README.md": "# Lot\n", **{p: batch[p] for p in pages[:19]}})
    wiki.remote.push({p: batch[p] for p in pages[19:39]})
    wiki.remote.push({pages[39]: batch[pages[39]]})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    blob = Repository.blob
    calls: list[str] = []

    def counted(self: Repository, path: str, rev: str = "HEAD") -> bytes | None:
        calls.append(path)
        return blob(self, path, rev)

    monkeypatch.setattr(Repository, "blob", counted)
    found = pending(db, ctx, wiki)
    assert found is not None and found.waiting == 81  # 40 broken links, 40 + 1 not indexed
    assert len(calls) < 5, calls
