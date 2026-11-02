"""What the assistants wrote, as runs: one assistant for one person in one go, each page
undoable as for a tidy run."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.services import Actor, assistant_runs, content
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.wikidata import Wiki


def test_runs_group_one_assistant_for_one_person_and_undo_page_by_page(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    claude = replace(wiki.alice, via="MCP · Claude")
    chatgpt = replace(wiki.alice, via="MCP · ChatGPT")
    readme = content.read_file(wiki.alice, ctx, wiki.infra, "README.md")

    def write(actor: Actor, path: str, text: bytes, source: str = "mcp") -> None:
        sha = None
        if path == "README.md":
            sha = content.read_file(actor, ctx, wiki.infra, path).sha
        content.write_file(
            db, database, actor, ctx, wiki.infra, path, text, base_sha=sha, source=source
        )

    write(claude, "a.md", b"# A\n")
    write(claude, "b.md", b"# B\n")
    write(claude, "b.md", b"# B, again\n")
    write(claude, "README.md", (readme.text or "").encode() + b"\nAdded by Claude.\n")
    write(wiki.alice, "c.md", b"# By hand\n", source="web")  # someone else's change: a new run
    write(claude, "d.md", b"# D\n")
    write(chatgpt, "e.md", b"# E\n")  # another assistant: another run

    found = assistant_runs.runs(wiki.bob, ctx, wiki.infra)  # readers see them too
    assert [(r.client, len(r.commits)) for r in found] == [
        ("ChatGPT", 1),
        ("Claude", 1),
        ("Claude", 4),
    ]
    first = found[-1]
    assert first.author == "Alice" and first.space == "acme/infra"
    assert assistant_runs.find(wiki.bob, ctx, wiki.infra, first.id) == first
    pages = {p.path: (p.status, p.state) for p in assistant_runs.run_pages(ctx, wiki.infra, first)}
    assert pages == {
        "a.md": ("created", "undoable"),
        "b.md": ("created", "undoable"),  # created then edited: created, net
        "README.md": ("edited", "undoable"),
    }

    with pytest.raises(SynaptaneError) as refused:  # a reader does not undo
        assistant_runs.undo_pages(db, database, wiki.bob, ctx, wiki.infra, first.id, ["a.md"])
    assert refused.value.code is ErrorCode.FORBIDDEN

    write(wiki.alice, "b.md", b"# B, by hand\n", source="web")
    with pytest.raises(SynaptaneError) as changed:  # changed since: its history restores it
        assistant_runs.undo_pages(db, database, wiki.alice, ctx, wiki.infra, first.id, ["b.md"])
    assert changed.value.code is ErrorCode.CONFLICT

    # From the view: through the assistant's connection, yet not part of its run.
    done = assistant_runs.undo_pages(
        db, database, claude, ctx, wiki.infra, first.id, ["a.md", "README.md"], source="mcp"
    )
    assert len(assistant_runs.runs(wiki.alice, ctx, wiki.infra)) == 3
    assert sorted(done.pages) == ["README.md", "a.md"] and done.commit
    assert content.read_file(wiki.alice, ctx, wiki.infra, "README.md").text == readme.text
    with pytest.raises(SynaptaneError):
        content.read_file(wiki.alice, ctx, wiki.infra, "a.md")
    again = assistant_runs.find(wiki.alice, ctx, wiki.infra, first.id)  # same id after the undo
    states = {p.path: p.state for p in assistant_runs.run_pages(ctx, wiki.infra, again)}
    assert states == {"a.md": "undone", "b.md": "changed", "README.md": "undone"}

    with pytest.raises(SynaptaneError) as unknown:
        assistant_runs.find(wiki.alice, ctx, wiki.infra, "0123456789ab..0123456789ab")
    assert unknown.value.code is ErrorCode.NOT_FOUND


def test_a_pause_starts_a_new_run(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = replace(wiki.alice, via="MCP · Claude")
    for path in ("a.md", "b.md"):
        content.write_file(db, database, claude, ctx, wiki.infra, path, b"# X\n", source="mcp")
    assert [len(r.commits) for r in assistant_runs.runs(wiki.alice, ctx, wiki.infra)] == [2]
    monkeypatch.setattr(assistant_runs, "GAP", timedelta(seconds=-1))  # any pause is too long
    assert [len(r.commits) for r in assistant_runs.runs(wiki.alice, ctx, wiki.infra)] == [1, 1]
