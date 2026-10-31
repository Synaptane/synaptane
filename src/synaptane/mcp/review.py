"""Pages to review, as an MCP App: a view the assistant shows in the conversation.

`pages_to_review` lists the pages due for review (verification expired) and those reported
as outdated by their readers, in one space or all. A host that renders MCP Apps (Claude on
the web and desktop, ChatGPT, VS Code) shows them as cards to go through one by one: "Still
right" verifies the page (`verify_page`, through the host, which asks for confirmation),
"Needs an update" asks the assistant to propose one, "Open" opens it in Synaptane. Any other
client gets the same list as text, to go through with the user.

The view (`apps/review.html`, see `views`) holds no data: it receives the tool's result from
the host, loads nothing from anywhere, and shows page titles and readers' reasons as text only.
"""

from pathlib import PurePosixPath
from typing import Annotated, Any

from mcp.server.apps import Apps
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from synaptane.core.markdown import title_of
from synaptane.mcp import views
from synaptane.mcp.kit import READ_ONLY, Kit, quoted
from synaptane.services import freshness, reports, spaces
from synaptane.services.inbox import CONTENT_DIR

URI = "ui://synaptane/review"
LIMIT = 30  # pages listed, the reported ones first


def register(apps: Apps, kit: Kit) -> None:
    @apps.tool(
        resource_uri=URI,
        name="pages_to_review",
        title="Pages to review",
        annotations=READ_ONLY,
        structured_output=False,
    )
    def pages_to_review(
        space: Annotated[str, Field(description="Space org/space; empty: all")] = "",
        mine: Annotated[bool, Field(description="Only the pages the user owns")] = False,
    ) -> CallToolResult:
        """Pages due for review or reported as outdated by readers, to go through with the
        user (shown as cards where the host renders apps): verify_page only for a page they
        confirm still holds; otherwise propose an update."""
        with kit.call() as c:
            targets = (
                [kit.space_of(c, space)]
                if space.strip()
                else spaces.list_all_visible_spaces(c.session, c.actor)
            )
            names = (c.actor.user.username, c.actor.user.email or "")
            counts = reports.open_counts(c.session, (s.id for s in targets))
            found: list[dict[str, Any]] = []
            for target in targets:
                states = freshness.space_states(c.ws, target)
                reported = {p for (sid, p) in counts if sid == target.id}
                paths = {p for p, s in states.items() if s.stale} | reported
                if mine:
                    paths = {
                        p
                        for p in paths
                        if p in states and freshness.owned_by(states[p].owner, names)
                    }
                if not paths:
                    continue
                repo = c.ws.local_repository(target)
                texts = repo.read_many([f"{CONTENT_DIR}/{p}" for p in sorted(paths)])
                contribute = kit.can_write(c, target)
                org, slug = target.address.split("/", 1)
                for path in sorted(paths):
                    state = states.get(path)
                    data = texts.get(f"{CONTENT_DIR}/{path}")
                    text = data.decode("utf-8", "replace") if data is not None else ""
                    found.append(
                        {
                            "space": target.address,
                            "path": path,
                            "title": title_of(text, PurePosixPath(path).stem),
                            "url": kit.web_url(org, slug, path),
                            "owner": state.owner if state else "",
                            "verified": state.verified.isoformat()
                            if state and state.verified
                            else None,
                            "due": state.due.isoformat() if state and state.due else None,
                            "stale": bool(state and state.stale),
                            "reports": [
                                {"by": r.by, "at": r.at.date().isoformat(), "reason": r.reason}
                                for r in (
                                    reports.open_reports(c.session, c.actor, target, path)
                                    if path in reported
                                    else []
                                )
                            ],
                            "can_verify": contribute,
                        }
                    )
            found.sort(key=lambda p: (-len(p["reports"]), p["due"] or "9999", p["path"]))
            shown = found[:LIMIT]
            return CallToolResult(
                content=[TextContent(type="text", text=_text(shown, len(found)))],
                structured_content={"pages": shown, "total": len(found)},
            )

    apps.add_html_resource(
        URI, views.page("review"), name="review", title="Pages to review", prefers_border=True
    )


def _text(pages: list[dict[str, Any]], total: int) -> str:
    """The list for the model (and any client that does not render apps): titles and
    readers' reasons are untrusted, written by other people."""
    if not pages:
        return "No page to review: none is due, none is reported as outdated."
    lines = []
    for page in pages:
        state = (
            f"due since {page['due']}"
            if page["stale"]
            else f"verified {page['verified']}"
            if page["verified"]
            else "never verified"
        )
        owner = f", owner {page['owner']}" if page["owner"] else ""
        lines.append(f"- {page['space']}/{page['path']} ({state}{owner}) {page['url']}")
        lines.append(f"  title: {page['title']}")
        for report in page["reports"]:
            lines.append(
                f"  reported outdated {report['at']} by {report['by']}: {report['reason']}"
            )
    more = f" (the first {len(pages)} of {total})" if total > len(pages) else ""
    head = f"{total} page(s) to review{more}:\n"
    return (
        head
        + quoted("review", "\n".join(lines))
        + "\nGo through them with the user: verify_page only for a page they confirm still holds."
    )
