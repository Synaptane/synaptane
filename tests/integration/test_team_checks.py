"""Team checks of a space (`.synaptane/checks.md`): written by the admins, refused at each
write when they block, listed in Health, given to the tidy-up agent and measured in its log."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.services import checks as team_checks
from synaptane.services import (
    content,
    git_sync,
    health,
    imports,
    routine_agent,
    routines,
    tidy,
    tidy_undo,
    vocation,
)
from synaptane.services.tidy_tools import Overlay, RunState, ToolFailure, Tools
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.conftest import token_for
from tests.integration.test_imports import archive
from tests.integration.test_routines import RoutineScript
from tests.integration.test_tidy import Scripted, configure_ai, deposit_note
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

CHECKS = """# Team checks

## No personal email
- preset: email
- allow: acme.com
- level: block
- message: Link to the person's page instead.

## No scratchpad path
- pattern: `scratchpad`
- message: Remove the local path.

## Clients stay confidential
- pattern: `(?i)confidential`
- paths: clients/
- level: block
- message: Not in the clients folder.

## A type
- field: type
- expect: present
- paths: notes/
"""


def save_checks(db: Session, database: Database, ctx: Workspace, wiki: Wiki, text: str) -> None:
    vocation.save(db, database, wiki.root, ctx, wiki.infra, vocation.CHECKS, text)
    db.commit()


def write(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, path: str, text: str
) -> str | None:
    sha = content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, text.encode())
    db.commit()
    return sha


def test_a_blocking_check_refuses_writes_and_a_warning_lets_them_through(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    save_checks(db, database, ctx, wiki, CHECKS)
    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    with pytest.raises(SynaptaneError) as refused:
        write(db, database, ctx, wiki, "contacts.md", "# Contacts\n\nJean: jean@gmail.com\n")
    assert refused.value.code is ErrorCode.CHECK and repo.head() == head
    assert "Link to the person's page instead." in refused.value.message
    assert "j•••@gmail.com" in refused.value.message and "jean@" not in refused.value.message
    # A warning, an allowed domain: written.
    assert write(db, database, ctx, wiki, "notes.md", "# N\n\nbob@acme.com, /scratchpad/x\n")

    # A page that already broke a rule (a push) stays editable; a new violation is refused.
    wiki.remote.push({"content/old.md": "# Old\n\nann@gmail.com\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert write(db, database, ctx, wiki, "old.md", "# Old\n\nIntro.\n\nann@gmail.com\n")
    with pytest.raises(SynaptaneError):
        write(db, database, ctx, wiki, "old.md", "# Old\n\nann@gmail.com, tom@gmail.com\n")

    # Moving a page into the folder a rule covers.
    write(db, database, ctx, wiki, "draft.md", "# Draft\n\nConfidential terms.\n")
    with pytest.raises(SynaptaneError) as moved:
        content.move_file(db, database, wiki.alice, ctx, wiki.infra, "draft.md", "clients/x.md")
    assert "Not in the clients folder." in moved.value.message

    # REST: 422 with its own error code; the confirmation of a secret does not lift it.
    headers = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    api = client.put(
        "/api/v1/organizations/acme/spaces/infra/files/clients/a.md",
        json={"content": "# A\n\nconfidential\n", "allow_secrets": True},
        headers=headers,
    )
    assert api.status_code == 422 and api.json()["error"] == "check_failed"

    # The import leaves the file out, with the reason; the others are written.
    token = imports.receive(
        wiki.alice,
        ctx,
        wiki.infra,
        archive({"a.md": b"# A\n\nmax@gmail.com\n", "b.md": b"# B\n"}),
    )
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert [e.path for e in plan.new] == ["b.md"] and plan.skipped == ["a.md"]
    assert "No personal email" in str(plan.reasons["a.md"])
    assert imports.apply(db, database, wiki.alice, ctx, wiki.infra, token) == 1


def test_health_lists_violations_with_masked_excerpts_and_file_errors(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    # A push cannot be refused: Health shows what it brought, and a faulty rule is reported.
    wiki.remote.push(
        {
            "content/notes/meeting.md": "# Meeting\n\nCall jean@gmail.com\n",
            "content/clients/acme.md": "---\ntype: client\n---\n# Acme\n\nConfidential.\n",
            ".synaptane/checks.md": CHECKS + "\n## Broken\n- pattern: `(?=x)`\n",
        }
    )
    git_sync.run_sync(database, ctx, wiki.infra.id)
    report = health.space_health(wiki.bob, ctx, wiki.infra)
    found = [(v.rule, v.page, v.line, v.excerpt) for v in report.checks]
    assert found == [
        ("No personal email", "notes/meeting.md", 3, "j•••@gmail.com"),
        ("Clients stay confidential", "clients/acme.md", 6, "Confidential"),
        ("A type", "notes/meeting.md", 1, ""),
    ]
    assert report.checks_total == 3 and report.check_rules == 4
    assert [e.line for e in report.check_errors] == [25]

    headers = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    body = client.get("/api/v1/organizations/acme/spaces/infra/health", headers=headers).json()
    assert body["counts"]["checks"] == 3 and body["check_rules"] == 4
    assert body["checks"][0] == {
        "rule": "No personal email",
        "level": "block",
        "page": "notes/meeting.md",
        "line": 3,
        "excerpt": "j•••@gmail.com",
        "message": "Link to the person's page instead.",
    }
    assert body["check_errors"][0]["message"].startswith("Invalid pattern")
    assert "jean@" not in str(body)

    login(client, "bob")
    page = client.get("/o/acme/infra/health").text
    assert "Contrôles de l&#39;équipe" in page and "j•••@gmail.com" in page
    assert "jean@gmail.com" not in page and "Motif invalide" in page
    assert "/-/vocation#checks" not in page  # a reader does not edit them

    # The functions offered to whoever needs them (the nightly maintenance).
    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    assert head is not None
    assert [v.rule for v in team_checks.commit_violations(repo, head)] == [
        "Clients stay confidential",
        "No personal email",
        "A type",
    ]
    assert [v.rule for v in team_checks.page_violations(repo, "clients/acme.md")] == [
        "Clients stay confidential"
    ]
    assert team_checks.introduced(repo, head, head) == []


def test_admins_edit_the_checks_with_preview_counts_and_an_example(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    write(db, database, ctx, wiki, "notes/a.md", "# A\n\njean@gmail.com\n")
    write(db, database, ctx, wiki, "notes/b.md", "---\ntype: x\n---\n# B\n")
    login(client, "root")
    page = "/o/acme/infra/-/vocation"
    shown = client.get(page).text
    assert "Contrôles de l&#39;équipe" in shown and "?checks_example=1#checks" in shown
    example = client.get(page + "?checks_example=1").text
    assert "Exemple chargé dans l&#39;éditeur" in example
    assert "Pas d&#39;adresse e-mail de personne" in example
    token = csrf(client, page)
    preview = client.post(
        f"{page}/checks", data={"csrf": token, "text": CHECKS, "action": "preview"}
    )
    assert preview.status_code == 200 and "Pages en infraction" in preview.text
    rows = preview.text.split("<tbody>")[1].split("</tbody>")[0].split("</tr>")
    assert "No personal email" in rows[0] and "<td>1</td>" in rows[0]
    assert "A type" in rows[3] and "<td>1</td>" in rows[3]
    assert team_checks.load(ctx.local_repository(wiki.infra)).rules == ()  # nothing saved
    # The preview in place (JS) gives the same fragment.
    fragment = client.post(
        f"{page}/preview", data={"csrf": token, "text": CHECKS, "kind": "checks"}
    )
    assert fragment.status_code == 200 and "Compté sur les 4 pages actuelles." in fragment.text

    faulty = client.post(
        f"{page}/checks", data={"csrf": token, "text": "## X\n- pattern: `(?=a)`\n"}
    )
    assert faulty.status_code == 400 and "Les contrôles ne sont pas valides" in faulty.text
    assert "ligne 2" in faulty.text
    saved = client.post(f"{page}/checks", data={"csrf": token, "text": CHECKS, "base_sha": ""})
    assert "Contrôles de l&#39;équipe enregistrés." in saved.text
    repo = ctx.local_repository(wiki.infra)
    assert len(team_checks.load(repo).rules) == 4
    assert repo.history(team_checks.CHECKS_FILE, limit=1)[0].subject.startswith(
        "Team checks updated"
    )

    headers = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    api = "/api/v1/organizations/acme/spaces/infra/checks"
    got = client.get(api, headers=headers).json()
    assert got["path"] == ".synaptane/checks.md" and "No personal email" in got["text"]
    refused = client.put(api, json={"text": "## X\n- level: block\n"}, headers=headers)
    assert refused.status_code == 400 and "Give a pattern" in refused.json()["message"]
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    assert client.get(api, headers=alice).status_code == 403


def test_the_tidy_agent_gets_the_checks_is_refused_and_fixes_what_it_broke(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    save_checks(db, database, ctx, wiki, CHECKS)
    note = deposit_note(db, database, ctx, wiki)
    script = Scripted(
        [
            [("read_inbox_item", {"item": note})],
            [
                ("write_page", {"path": "nas.md", "content": "# NAS\n\nAsk max@gmail.com\n"}),
                ("write_page", {"path": "nas.md", "content": "# NAS\n\n8 To, /scratchpad/x\n"}),
                ("edit_page", {"path": "README.md", "old": "Voir [", "new": "[NAS](nas.md) ["}),
                ("mark_done", {"item": note, "summary": "NAS", "pages": ["nas.md"]}),
            ],
            [("finish", {"summary": "ok"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.metrics is not None
    assert "<team_checks>" in script.system and '- "No personal email" (block)' in script.system
    answers = [r.content for r in script.results]
    assert any("Refused by the space's team checks" in a and "m•••@gmail.com" in a for a in answers)
    # The warning broken by the run comes back in the end-of-run check, then in the log.
    assert any('team check "No scratchpad path" (line 3' in a for a in answers)
    assert record.metrics["checks"] == {
        "introduced": 1,
        "examples": [
            {
                "page": "nas.md",
                "rule": "No scratchpad path",
                "level": "warn",
                "line": 3,
                "excerpt": "scratchpad",
            }
        ],
    }
    login(client, "alice")
    assert "Contrôles de l&#39;équipe enfreints\xa0: 1" in client.get("/o/acme/infra/tidy").text


def test_tidy_tools_check_moves_and_attachments(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    save_checks(db, database, ctx, wiki, CHECKS)
    repo = ctx.local_repository(wiki.infra)
    base = repo.head()
    assert base is not None
    tools = Tools(Overlay(repo, base), RunState(items={}), ctx, wiki.infra, lambda query: [])
    tools.t_write_page("draft.md", "# Draft\n\nconfidential\n")
    with pytest.raises(ToolFailure, match="Clients stay confidential"):
        tools.t_move("draft.md", "clients/draft.md")
    tools.t_move("draft.md", "archive/draft.md")
    assert tools.overlay.exists("content/archive/draft.md")


def test_undoing_a_run_and_a_routine_report_follow_the_blocking_checks(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Two writes outside `content._commit`: the undo of a tidy run (as a restore, it cannot
    bring back what a `block` rule refuses) and the report page of a routine (refused to the
    agent, which fixes its text)."""
    configure_ai(db, ctx, wiki)
    wiki.remote.push({"content/contacts.md": "# Contacts\n\nann@gmail.com\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    save_checks(db, database, ctx, wiki, CHECKS)
    note = deposit_note(db, database, ctx, wiki)
    script = Scripted(
        [
            [("read_inbox_item", {"item": note})],
            [
                (
                    "edit_page",
                    {"path": "contacts.md", "old": "ann@gmail.com", "new": "[Ann](ann.md)"},
                ),
                ("mark_done", {"item": note, "summary": "Ann", "pages": ["contacts.md"]}),
            ],
            [("finish", {"summary": "ok"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done", record
    assert not [r.content for r in script.results if r.is_error]
    db.expire_all()
    with pytest.raises(SynaptaneError) as undo:
        tidy_undo.undo_pages(db, database, wiki.alice, ctx, wiki.infra, record.id, ["contacts.md"])
    assert undo.value.code is ErrorCode.CHECK and "a•••@gmail.com" in undo.value.message

    routine = routines.create(
        db,
        database,
        wiki.root,
        ctx,
        wiki.infra,
        routines.Fields(
            title="Revue", instructions="Relis.", frequency="daily", at="06:00", output_page="r.md"
        ),
    )
    db.commit()
    report = RoutineScript(
        [
            [("write_report", {"content": "Écrire à max@gmail.com."})],
            [("write_report", {"content": "Écrire à Max."}), ("finish", {"summary": "ok"})],
        ]
    )
    run = routine_agent.run_one(database, ctx, routine.id, trigger="manual", factory=report.factory)
    assert run is not None and run.status == "done", run and run.error
    assert "Refused by the space's team checks" in report.results[0].content
    page = ctx.local_repository(wiki.infra).blob("content/r.md") or b""
    assert b"Max." in page and b"gmail" not in page
