"""Pages to review, as an MCP App: a view the assistant shows in the conversation.

`pages_to_review` lists the pages due for review (verification expired) and those reported
as outdated by their readers, in one space or all. A host that renders MCP Apps (Claude on
the web and desktop, ChatGPT, VS Code) shows them as cards to go through one by one: "Still
right" verifies the page (`verify_page`, through the host, which asks for confirmation),
"Needs an update" asks the assistant to propose one, "Open" opens it in Synaptane. Any other
client gets the same list as text, to go through with the user.

The view (`apps/review.html`) holds no data: it receives the tool's result from the host,
loads nothing from anywhere, and shows page titles and readers' reasons as text only.
"""

import json
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Annotated, Any

from mcp.server.apps import EXTENSION_ID, Apps
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from synaptane.core.access import Action, can
from synaptane.core.markdown import title_of
from synaptane.mcp.kit import READ_ONLY, Kit, quoted
from synaptane.services import freshness, reports, spaces
from synaptane.services.inbox import CONTENT_DIR

URI = "ui://synaptane/review"
LIMIT = 30  # pages listed, the reported ones first


def build(kit: Kit) -> Apps:
    apps = Apps()

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
                contribute = can(
                    c.actor.access,
                    Action.CONTRIBUTE_SPACE,
                    org_id=target.org_id,
                    space_id=target.id,
                )
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
        URI,
        files("synaptane.mcp").joinpath("apps/review.html").read_text(encoding="utf-8"),
        name="review",
        title="Pages to review",
        prefers_border=True,
    )
    return apps


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


class AdvertiseApps:
    """Puts the MCP Apps extension in the `initialize` answer (`capabilities.extensions`).

    Hosts render a view only when both sides declare the extension. At the handshake era
    (2025-11-25, what hosts open with today), the SDK writes the answer with that era's schema,
    which has no `extensions` field yet, and drops it. The answer is a single JSON body
    (`json_response=True`): it gets the field back here, nothing else changes."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
        body = b""
        more = True
        received: list[Any] = []
        while more:
            message = await receive()
            received.append(message)
            body += message.get("body", b"")
            more = message.get("more_body", False)
        try:
            initializing = json.loads(body).get("method") == "initialize"
        except (ValueError, AttributeError):
            initializing = False

        async def replay() -> Any:
            return received.pop(0) if received else await receive()

        if not initializing:
            await self.app(scope, replay, send)
            return
        start: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def capture(message: Any) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            payload = b"".join(chunks)
            try:
                answer = json.loads(payload)
                capabilities = answer["result"]["capabilities"]
                capabilities.setdefault("extensions", {}).setdefault(EXTENSION_ID, {})
                payload = json.dumps(answer, ensure_ascii=False).encode()
            except (ValueError, KeyError, TypeError, AttributeError):
                pass  # not a JSON answer to initialize (error, stream): left as is
            headers = [
                (k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(payload)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": payload})

        await self.app(scope, replay, capture)
