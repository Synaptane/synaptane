"""Undoing a tidy run for some pages only: the pages chosen go back, a note comes back to the
inbox once all its pages are undone, a page changed since is refused."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.services import content, deposits, tidy, tidy_undo
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import TidyRun
from tests.integration.conftest import token_for
from tests.integration.test_tidy import Scripted, configure_ai
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki


def _note(db: Session, database: Database, ctx: Workspace, wiki: Wiki, title: str) -> str:
    path = deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title=title, body=title, source="web"
    )
    db.commit()
    return path.rsplit("/", 1)[-1]


def _run(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> TidyRun:
    """A run: note "A" into a.md and README.md, note "B" into b.md."""
    configure_ai(db, ctx, wiki)
    a, b = _note(db, database, ctx, wiki, "A"), _note(db, database, ctx, wiki, "B")
    script = Scripted(
        [
            [("read_inbox_item", {"item": a}), ("read_inbox_item", {"item": b})],
            [
                ("write_page", {"path": "a.md", "content": "# A\n"}),
                ("write_page", {"path": "b.md", "content": "# B\n"}),
                (
                    "edit_page",
                    {"path": "README.md", "old": "Voir [", "new": "Voir [A](a.md), [B](b.md), ["},
                ),
                ("mark_done", {"item": a, "summary": "A", "pages": ["a.md", "README.md"]}),
                ("mark_done", {"item": b, "summary": "B", "pages": ["b.md"]}),
            ],
            [("finish", {"summary": "2 rangés"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit, record
    assert not [r.content for r in script.results if r.is_error]
    db.expire_all()
    return record


def _titles(ctx: Workspace, wiki: Wiki) -> set[str]:
    return {i.title for i in deposits.list_items(wiki.alice, ctx, wiki.infra)}


def test_some_pages_of_a_run_are_undone(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    record = _run(db, database, ctx, wiki)
    assert _titles(ctx, wiki).isdisjoint({"A", "B"})
    _, pages = tidy_undo.run_pages(db, wiki.bob, ctx, wiki.infra, record.id)
    assert [(p.path, p.status, p.state) for p in pages] == [
        ("README.md", "edited", "undoable"),
        ("a.md", "created", "undoable"),
        ("b.md", "created", "undoable"),
    ]

    with pytest.raises(SynaptaneError) as reader:
        tidy_undo.undo_pages(db, database, wiki.bob, ctx, wiki.infra, record.id, ["b.md"])
    assert reader.value.code == "forbidden"
    with pytest.raises(SynaptaneError, match="Not changed by this run"):
        tidy_undo.undo_pages(db, database, wiki.alice, ctx, wiki.infra, record.id, ["zz.md"])

    # b.md is note B's only page: the note comes back.
    done = tidy_undo.undo_pages(db, database, wiki.alice, ctx, wiki.infra, record.id, ["b.md"])
    db.commit()
    assert done.pages == ["b.md"] and done.notes_back == ["B"] and done.notes_kept == []
    paths = {b.path for b in content.list_files(wiki.alice, ctx, wiki.infra)}
    assert "b.md" not in paths and "a.md" in paths and "B" in _titles(ctx, wiki)
    undo = content.history(wiki.alice, ctx, wiki.infra, "b.md")[0]
    assert undo.subject.startswith("Undo the tidy-up of") and undo.subject.endswith(" for b.md")
    assert (undo.author.name, undo.source) == ("Alice", "web")
    last = content.history(wiki.alice, ctx, wiki.infra, "README.md")[0]
    assert last.subject.startswith("Tidy-up:")  # left as the run wrote it

    # a.md only: note A still lives in README.md, it stays filed.
    done = tidy_undo.undo_pages(db, database, wiki.alice, ctx, wiki.infra, record.id, ["a.md"])
    db.commit()
    assert done.notes_back == [] and done.notes_kept == [("A", ("README.md",))]
    assert "A" not in _titles(ctx, wiki)
    with pytest.raises(SynaptaneError, match="already undone"):
        tidy_undo.undo_pages(db, database, wiki.alice, ctx, wiki.infra, record.id, ["a.md"])

    # README.md changed by hand since the run: refused, the history repairs it.
    readme = content.read_file(wiki.alice, ctx, wiki.infra, "README.md")
    content.write_file(
        db, database, wiki.alice, ctx, wiki.infra, "README.md", b"# Infra\n", base_sha=readme.sha
    )
    db.commit()
    _, pages = tidy_undo.run_pages(db, wiki.bob, ctx, wiki.infra, record.id)
    assert {p.path: p.state for p in pages} == {
        "README.md": "changed",
        "a.md": "undone",
        "b.md": "undone",
    }
    with pytest.raises(SynaptaneError) as changed:
        tidy_undo.undo_pages(db, database, wiki.alice, ctx, wiki.infra, record.id, ["README.md"])
    assert changed.value.code == "conflict"


def test_run_pages_in_the_web(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    record = _run(db, database, ctx, wiki)
    url = f"/o/acme/infra/-/tidy/{record.id}/pages"
    login(client, "bob")  # a reader sees the pages, without undoing
    journal = client.get("/o/acme/infra/tidy").text
    assert f'href="{url}"' in journal and "Pages modifiées" in journal
    seen = client.get(url).text
    assert seen.count('data-test="run-page"') == 3 and 'name="path"' not in seen
    assert f'href="/o/acme/infra/diff/{record.commit}/a.md"' in seen

    login(client, "alice")
    assert "Annuler certaines pages" in client.get("/o/acme/infra/tidy").text
    token = csrf(client, url)
    undone = client.post(url, data={"csrf": token, "path": ["a.md", "b.md"]})
    assert "Annulé pour a.md, b.md" in undone.text and "De retour dans l&#39;inbox" in undone.text
    assert "Toujours rangées" in undone.text  # note A: README.md keeps it
    assert 'value="a.md"' not in undone.text  # undone: no box left to tick
    empty = client.post(url, data={"csrf": token})
    assert "Cochez les pages à annuler" in empty.text


def test_run_pages_over_rest(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    record = _run(db, database, ctx, wiki)
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    bob = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    base = f"/api/v1/organizations/acme/spaces/infra/tidy/runs/{record.id}"
    listed = client.get(f"{base}/pages", headers=bob).json()
    assert {(p["path"], p["state"]) for p in listed} >= {("b.md", "undoable")}
    body = {"paths": ["b.md"]}
    assert client.post(f"{base}/undo-pages", json=body, headers=bob).status_code == 403
    done = client.post(f"{base}/undo-pages", json=body, headers=alice).json()
    assert done["pages"] == ["b.md"] and done["notes_back"] == ["B"] and done["commit"]
