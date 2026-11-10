"""Nightly maintenance and the team's checks: what people break of a rule that blocks, or of
personal data, is fixed at night like a broken link; the data itself never leaves the page."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.services import git_sync, health, maintenance, tidy
from synaptane.services.tidy_tools import Op, Overlay, RunState
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import MaintenanceMark, TidyRun
from tests.integration.test_maintenance import leave_all, pending, problems, reviewed, space
from tests.integration.test_tidy import Scripted, configure_ai
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki

NAME = maintenance.NAME
ADDRESS = "jean.dupont@gmail.com"
MASKED = "j•••@gmail.com"
EMAIL = f"No personal email\n{MASKED}"

CHECKS = """# Team checks

## No personal email
- preset: email
- allow: acme.com
- level: block
- message: Write the person's name, not their address.

## No scratchpad path
- pattern: `scratchpad`
- message: Remove the local path.

## A type
- field: type
- expect: present
- paths: notes/
- level: block
- message: Add `type:` to the front matter.
"""


def push(database: Database, ctx: Workspace, wiki: Wiki, files: dict[str, str | None]) -> None:
    """A push: it cannot be refused, whatever it brings."""
    wiki.remote.push(files)
    git_sync.run_sync(database, ctx, wiki.infra.id)


def contacts(*lines: str) -> str:
    return "# Contacts\n\n" + "".join(f"{line}\n" for line in lines)


def team(found: maintenance.Found | None) -> list[tuple[str, str, str]]:
    return [p for p in problems(found) if p[0] == maintenance.TEAM_KIND]


def test_a_violation_pushed_by_git_is_fixed_the_next_night(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, client: TestClient
) -> None:
    configure_ai(db, ctx, wiki)
    push(database, ctx, wiki, {".synaptane/checks.md": CHECKS})
    page = contacts(f"Jean Dupont : {ADDRESS}.", "Notes kept in scratchpad.")
    push(database, ctx, wiki, {"content/contacts.md": page})
    reviewed(db, ctx, wiki)
    script = Scripted(
        [
            [("read_inbox_item", {"item": NAME})],
            [
                ("edit_page", {"path": "contacts.md", "old": f" : {ADDRESS}", "new": ""}),
                (
                    "mark_done",
                    {
                        "item": NAME,
                        "summary": "Adresse retirée. P2: index à revoir par l'équipe",
                        "pages": ["contacts.md"],
                    },
                ),
            ],
            [("finish", {"summary": "Entretien fait"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit, record
    read = script.results[0].content
    assert (
        'P1 — contacts.md: team check "No personal email" (block, personal data, masked), '
        f"line 3 `{MASKED}`: Write the person's name, not their address."
    ) in read
    assert "P2 — contacts.md: missing from the folder index README.md" in read
    assert ADDRESS not in read and ADDRESS not in script.first and ADDRESS not in script.system
    assert "No scratchpad path" not in read  # a warning: Health shows it, nobody fixes it
    assert "never copy it" in script.system  # the cautious rules come with the checks
    logged = [
        (op["op"], op["path"], op["detail"])
        for op in record.operations
        if op["op"] in (Op.FIXED, Op.PROBLEM_LEFT)
    ]
    assert logged == [
        (Op.FIXED, "contacts.md", f'team check "No personal email" `{MASKED}`'),
        (
            Op.PROBLEM_LEFT,
            "contacts.md",
            "missing from the folder index README.md — index à revoir par l'équipe",
        ),
    ]
    data = ctx.local_repository(wiki.infra).blob("content/contacts.md") or b""
    assert ADDRESS.encode() not in data and b"Jean Dupont." in data
    login(client, "root")
    log = client.get("/o/acme/infra/tidy").text
    assert "Problème du wiki corrigé" in log and "contrôle de l&#39;équipe" in log
    assert MASKED in log and ADDRESS not in log
    # The next night: nothing new, no run.
    assert (
        tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=Scripted([]).factory)
        is None
    )


def test_a_masked_excerpt_never_carries_the_data(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, client: TestClient
) -> None:
    """Not in the item, the log, the marks or the summary, even when the agent, told never to
    copy it, writes it in its reason."""
    configure_ai(db, ctx, wiki)
    push(database, ctx, wiki, {".synaptane/checks.md": CHECKS})
    push(database, ctx, wiki, {"content/contacts.md": contacts(f"Jean : {ADDRESS}")})
    reviewed(db, ctx, wiki)
    reason = f"P1: {ADDRESS} est le contact du client, à garder; P2: à voir"
    script = Scripted(
        [
            [("read_inbox_item", {"item": NAME})],
            [
                ("leave_in_inbox", {"item": NAME, "reason": reason}),
                ("finish", {"summary": f"Rien corrigé ({ADDRESS} laissé)"}),
            ],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=script.factory)
    assert record is not None and record.status == "done", record
    assert ADDRESS not in script.results[0].content and MASKED in script.results[0].content
    stored = db.get(TidyRun, record.id)
    assert stored is not None
    assert ADDRESS not in repr(stored.operations) and ADDRESS not in (stored.summary or "")
    assert f"Rien corrigé ({MASKED} laissé)" == stored.summary
    left = [op for op in stored.operations if op["op"] == Op.PROBLEM_LEFT]
    assert left[0]["detail"] == (
        f'team check "No personal email" `{MASKED}` — {MASKED} est le contact du client, à garder'
    )
    marks = db.scalars(select(MaintenanceMark).order_by(MaintenanceMark.kind)).all()
    assert [(m.kind, m.detail) for m in marks] == [
        (maintenance.TEAM_KIND, EMAIL),
        ("not_in_index", "README.md"),
    ]
    assert all(ADDRESS not in m.detail + m.reason for m in marks)
    login(client, "root")
    assert ADDRESS not in client.get("/o/acme/infra/tidy").text


def test_a_left_violation_comes_back_only_if_introduced_again(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    push(database, ctx, wiki, {".synaptane/checks.md": CHECKS})
    push(database, ctx, wiki, {"content/contacts.md": contacts(f"Jean : {ADDRESS}")})
    assert team(pending(db, ctx, wiki)) == [(maintenance.TEAM_KIND, "contacts.md", EMAIL)]
    leave_all(db, ctx, wiki, "P1: contact du client; P2: à voir")
    assert pending(db, ctx, wiki) is None

    # Edited elsewhere: not introduced again.
    push(database, ctx, wiki, {"content/contacts.md": contacts(f"Jean : {ADDRESS}", "Suite.")})
    assert pending(db, ctx, wiki) is None
    # Written once more: introduced again.
    twice = contacts(f"Jean : {ADDRESS}", f"Encore : {ADDRESS}")
    push(database, ctx, wiki, {"content/contacts.md": twice})
    assert team(pending(db, ctx, wiki)) == [(maintenance.TEAM_KIND, "contacts.md", EMAIL)]
    leave_all(db, ctx, wiki, "P1: toujours le contact")
    # Removed by hand: gone, and so is its mark; written again later: new.
    push(database, ctx, wiki, {"content/contacts.md": contacts("Jean : voir sa fiche")})
    assert pending(db, ctx, wiki) is None
    assert [m.kind for m in db.scalars(select(MaintenanceMark))] == ["not_in_index"]
    push(database, ctx, wiki, {"content/contacts.md": contacts(f"Jean : {ADDRESS}")})
    assert team(pending(db, ctx, wiki)) == [(maintenance.TEAM_KIND, "contacts.md", EMAIL)]


def test_the_cap_is_shared_with_the_wiki_checks_most_urgent_first(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Personal data first, then the `block` rules, then the wiki's own problems: ten a night,
    the others the next nights."""
    push(database, ctx, wiki, {".synaptane/checks.md": CHECKS})
    batch: dict[str, str | None] = {
        f"content/notes/n{n}.md": f"# N{n}\n\n[Ailleurs](x{n}.md)\n" for n in range(9)
    }
    batch["content/people/a.md"] = contacts(f"A : {ADDRESS}")
    batch["content/people/b.md"] = contacts("B : marie@gmail.com")
    batch["content/misc.md"] = "# Misc\n\nIn scratchpad.\n"
    push(database, ctx, wiki, batch)
    found = pending(db, ctx, wiki)
    assert found is not None and len(found.problems) == maintenance.MAX_PROBLEMS
    given = [(p.kind, p.page, p.detail.split("\n")[0]) for p in found.problems]
    assert given[:2] == [
        (maintenance.TEAM_KIND, "people/a.md", "No personal email"),
        (maintenance.TEAM_KIND, "people/b.md", "No personal email"),
    ]
    assert given[2:] == [(maintenance.TEAM_KIND, f"notes/n{n}.md", "A type") for n in range(8)]
    assert not found.complete and found.waiting > 20
    assert "scratchpad" not in found.describe()
    # The night after: the last `block` one, then the broken links.
    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    assert head is not None
    state = RunState(items={}, left={NAME: "à voir"})
    closure = maintenance.close(found, state, Overlay(repo, head))
    maintenance.settle(db, ctx, space(db, wiki), found, closure, head, None)
    db.commit()
    following = pending(db, ctx, wiki)
    assert following is not None
    assert [p.kind for p in following.problems[:2]] == [maintenance.TEAM_KIND, "broken_link"]
    assert following.problems[0].page == "notes/n8.md"


def test_a_warning_is_only_reported_unless_the_team_says_fix(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    rules = """## No scratchpad path
- pattern: `scratchpad`

## No draft marker
- pattern: `TODO-DRAFT`
- fix: yes

## No personal email
- preset: email
- level: block
- fix: no
"""
    push(database, ctx, wiki, {".synaptane/checks.md": rules})
    push(
        database,
        ctx,
        wiki,
        {"content/README.md": f"# Infra\n\nscratchpad, TODO-DRAFT, {ADDRESS}\n"},
    )
    assert team(pending(db, ctx, wiki)) == [
        (maintenance.TEAM_KIND, "README.md", "No draft marker\nTODO-DRAFT")
    ]
    # Health still shows every one.
    report = health.analyze(ctx.local_repository(wiki.infra))
    assert {v.rule for v in report.checks} == {
        "No scratchpad path",
        "No draft marker",
        "No personal email",
    }


def test_imports_and_a_space_without_maintenance_are_left_alone(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    push(database, ctx, wiki, {".synaptane/checks.md": CHECKS})
    batch: dict[str, str | None] = {
        f"content/import/p{n}.md": f"# P{n}\n\nContact : p{n}@gmail.com\n" for n in range(25)
    }
    push(database, ctx, wiki, batch)  # an import: reviewed as a whole by its own item
    assert team(pending(db, ctx, wiki)) == []
    push(
        database,
        ctx,
        wiki,
        {"content/import/p3.md": "# P3\n\nContact : p3@gmail.com, autre@gmail.com\n"},
    )
    assert team(pending(db, ctx, wiki)) == [
        (maintenance.TEAM_KIND, "import/p3.md", "No personal email\na•••@gmail.com")
    ]
    # Maintenance turned off: nothing is computed, nothing runs.
    configure_ai(db, ctx, wiki)
    tidy.update_settings(db, wiki.root, space(db, wiki), maintain=False)
    db.commit()
    reviewed(db, ctx, wiki)
    push(database, ctx, wiki, {"content/contacts.md": contacts(f"Jean : {ADDRESS}")})
    reviewed(db, ctx, wiki)
    assert tidy.run(database, ctx, wiki.infra.id, trigger="schedule") is None


def test_a_faulty_checks_file_does_not_break_the_maintenance(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A faulty rule is left out (Health names its line), the others apply; a source that
    fails is left out for the night, the wiki's problems are still given."""
    faulty = "## Broken\n- pattern: `(`\n\n" + CHECKS.split("\n", 2)[2]
    push(database, ctx, wiki, {".synaptane/checks.md": faulty})
    page = contacts(f"Jean : {ADDRESS}", "[Ailleurs](nulle-part.md)")
    push(database, ctx, wiki, {"content/contacts.md": page})
    report = health.analyze(ctx.local_repository(wiki.infra))
    assert [e.line for e in report.check_errors] == [2]
    found = pending(db, ctx, wiki)
    assert [p[0] for p in problems(found)] == [maintenance.TEAM_KIND, "broken_link", "not_in_index"]

    def broken(self: maintenance.TeamChecks, window: maintenance.Window) -> list[object]:
        raise RuntimeError("panne")

    monkeypatch.setattr(maintenance.TeamChecks, "introduced", broken)
    assert [p[0] for p in problems(pending(db, ctx, wiki))] == ["broken_link", "not_in_index"]
    # Unreadable as a whole: no rule, the night goes on without them.
    monkeypatch.undo()
    push(database, ctx, wiki, {".synaptane/checks.md": "## Only\n- level: loud\n"})
    assert [p[0] for p in problems(pending(db, ctx, wiki))] == ["broken_link", "not_in_index"]
