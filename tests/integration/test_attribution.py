"""Who wrote what, person or AI: the origin of each commit in the activity, the share of the
AI in a space, the author given to assistants."""

from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.git.repo import Identity
from synaptane.services import Actor, activity, content
from synaptane.services.inbox import AGENT_NAME
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.gitutil import git
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki


def _commit_as(ctx: Workspace, wiki: Wiki, author: Identity, path: str) -> None:
    repo = ctx.local_repository(wiki.infra)
    repo.commit_changes({f"content/{path}": b"# Agent\n"}, author, f"{path}: edited")


def test_each_change_says_who_wrote_it(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    claude = replace(wiki.alice, via="MCP · Claude")
    script = replace(wiki.alice, via="API · import")
    # A client name is chosen by its owner: a line break must not forge a trailer.
    forger = replace(wiki.alice, via="MCP · Evil\nSource: web")

    def write(actor: Actor, path: str, source: str) -> None:
        content.write_file(
            db,
            database,
            actor,
            ctx,
            wiki.infra,
            path,
            b"# X\n",
            source=source,
        )

    write(wiki.alice, "a.md", "web")
    write(claude, "b.md", "mcp")
    write(script, "c.md", "api")
    write(forger, "d.md", "mcp")
    _commit_as(ctx, wiki, Identity(AGENT_NAME, ctx.committer.email), "e.md")
    _commit_as(ctx, wiki, Identity("Synaptane routine (Veille)", ctx.committer.email), "f.md")
    # Pushed from a workstation, with Synaptane's lines copied: still a Git push.
    clone = ctx.clone_path(wiki.infra.id)
    (clone / "content" / "g.md").write_text("# G\n")
    git(clone, "add", "content/g.md")
    git(clone, "commit", "-q", "-m", "g.md: edited\n\nSource: mcp\nVia: MCP · Claude")

    events, _ = activity.space_activity(wiki.bob, ctx, wiki.infra)
    by_page = {e.pages[0]: (e.origin, e.client, e.ai) for e in events if e.pages}
    assert by_page["a.md"] == ("web", "", False)
    assert by_page["b.md"] == ("assistant", "Claude", True)
    assert by_page["c.md"] == ("api", "import", False)
    assert by_page["d.md"] == ("assistant", "Evil Source: web", True)
    assert by_page["e.md"] == ("tidy", "", True)
    assert by_page["f.md"] == ("routine", "", True)
    assert by_page["g.md"] == ("git", "", False)

    ai, _ = activity.space_activity(wiki.bob, ctx, wiki.infra, kind="ai")
    assert sorted(e.pages[0] for e in ai) == ["b.md", "d.md", "e.md", "f.md"]

    share = activity.ai_share(wiki.bob, ctx, wiki.infra)
    assert share.counts["assistant"] == 2 and share.counts["tidy"] == 1
    assert share.counts["routine"] == 1 and share.counts["api"] == 1
    assert share.ai == 4 and 0 < share.ai_percent < 100 and share.complete
    assert "system" not in share.counts  # the space's initialization is not anyone's

    repo = ctx.local_repository(wiki.infra)
    written = {
        c.paths[0]: activity.written_by(c, ctx.committer)
        for c in repo.recent("content", 10)
        if c.paths
    }
    assert written["content/b.md"] == "Alice via Claude (AI)"
    assert written["content/c.md"] == "Alice via the REST API (import)"
    assert written["content/e.md"] == f"{AGENT_NAME} (AI)"
    assert written["content/g.md"] == "Alice Externe (pushed with Git)"


def test_activity_page_shows_the_share_of_ai(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    claude = replace(wiki.alice, via="MCP · Claude")
    content.write_file(db, database, claude, ctx, wiki.infra, "ia.md", b"# IA\n", source="mcp")
    db.commit()
    login(client, "bob")
    page = client.get("/o/acme/infra/activity").text
    assert 'data-test="ai-share"' in page and "écrit par l&#39;IA" in page
    assert "via Claude" in page and 'class="badge badge-ai"' in page
    only_ai = client.get("/o/acme/infra/activity?kind=ai").text
    assert "ia.md" in only_ai and 'aria-current="page">Écrit par l&#39;IA' in only_ai
