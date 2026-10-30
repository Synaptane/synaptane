"""The last tidy-up, as an MCP App: what the nightly filing changed in a space, page by page,
each one undoable from the conversation.

`last_tidy` lists the pages a run created, edited, moved or deleted, and the notes it filed.
A host that renders MCP Apps shows them with, for each page, "See the change" (its diff in
Synaptane) and "Undo" (`undo_tidy_pages`, a tool meant for the view: the page goes back as it
was before the run, in a commit under the person's name; see `services.tidy_undo`). Any other
client gets the list as text, with the address of the same choice in Synaptane.
"""

from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from mcp.server.apps import Apps
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from synaptane.mcp import views
from synaptane.mcp.kit import CHANGES, READ_ONLY, Call, Kit, SpaceArg, quoted
from synaptane.services import tidy, tidy_undo
from synaptane.storage.models import Space, TidyRun

URI = "ui://synaptane/tidy"
RECENT = 30  # runs looked through for the last one that changed pages

RunArg = Annotated[str, Field(description="Run id, from last_tidy")]


def _run_id(value: str) -> UUID:
    try:
        return UUID(value.strip())
    except ValueError:
        raise ToolError("Unknown run: give the id last_tidy returns.") from None


def _last(c: Call, space: Space) -> TidyRun | None:
    """The most recent run that changed pages (a run with nothing to do has no commit)."""
    return next((r for r in tidy.runs(c.session, c.actor, space, RECENT) if r.commit), None)


def register(apps: Apps, kit: Kit) -> None:
    @apps.tool(
        resource_uri=URI,
        name="last_tidy",
        title="Last tidy-up",
        annotations=READ_ONLY,
        structured_output=False,
    )
    def last_tidy(
        space: SpaceArg,
        run: Annotated[str, Field(description="Run id; empty: the last that changed pages")] = "",
    ) -> CallToolResult:
        """What the nightly filing (tidy-up) changed in a space, page by page, and the notes
        it filed; the user can undo pages (shown as a view where the host renders apps)."""
        with kit.call() as c:
            target = kit.space_of(c, space)
            record = (
                tidy_undo.find_run(c.session, target, _run_id(run))
                if run.strip()
                else _last(c, target)
            )
            if record is None:
                text = f"No tidy-up has changed pages in {target.address} yet."
                return CallToolResult(
                    content=[TextContent(type="text", text=text)],
                    structured_content={"space": target.address, "run": None, "pages": []},
                )
            _, pages = tidy_undo.run_pages(c.session, c.actor, c.ws, target, record.id)
            notes = tidy_undo.filed_notes(c.ws.local_repository(target), record)
            base = kit.space_url(target)
            data: dict[str, Any] = {
                "space": target.address,
                "run": {
                    "id": str(record.id),
                    "started": record.started_at.isoformat(),
                    "summary": record.summary,
                    "reverted": record.reverted_at is not None,
                },
                "pages": [
                    {
                        "path": p.path,
                        "status": p.status,
                        "old": p.old,
                        "state": p.state,
                        "url": "" if p.status == "deleted" else f"{base}/p/{quote(p.path)}",
                        "diff": f"{base}/diff/{record.commit}/{quote(p.path)}",
                    }
                    for p in pages
                ],
                "notes": [{"title": n.title, "pages": list(n.pages)} for n in notes],
                "can_undo": kit.can_write(c, target) and record.reverted_at is None,
                "url": f"{base}/-/tidy/{record.id}/pages",
            }
            return CallToolResult(
                content=[TextContent(type="text", text=_text(data))], structured_content=data
            )

    @apps.tool(
        resource_uri=URI,
        visibility=["app"],
        name="undo_tidy_pages",
        title="Undo a tidy-up for some pages",
        annotations=CHANGES,
        structured_output=False,
    )
    def undo_tidy_pages(
        space: SpaceArg,
        run: RunArg,
        paths: Annotated[list[str], Field(description="Pages to put back as before the run")],
    ) -> CallToolResult:
        """Puts pages back as they were before a tidy-up run, when the user asks (contributor);
        a note returns to the inbox once all its pages are undone."""
        with kit.call() as c:
            target = kit.space_of(c, space)
            done = tidy_undo.undo_pages(
                c.session,
                c.database,
                c.actor,
                c.ws,
                target,
                _run_id(run),
                paths,
                source="mcp",
            )
            text = f"Undone in {target.address}: {', '.join(done.pages)}."
            if done.notes_back:
                text += f" Back in the inbox: {', '.join(done.notes_back)}."
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structured_content={
                    "pages": done.pages,
                    "notes_back": done.notes_back,
                    "notes_kept": [title for title, _ in done.notes_kept],
                },
            )

    apps.add_html_resource(
        URI, views.page("tidy"), name="tidy", title="Last tidy-up", prefers_border=True
    )


def _text(data: dict[str, Any]) -> str:
    """The run for the model (and any client that does not render apps): paths, titles and
    the summary come from the notes filed, written by other people: untrusted."""
    run = data["run"]
    lines = [f"Summary: {run['summary']}"] if run["summary"] else []
    for page in data["pages"]:
        moved = f" from {page['old']}" if page["old"] else ""
        lines.append(f"- {page['status']}{moved}: {page['path']} ({page['state']})")
    for note in data["notes"]:
        lines.append(f"- note filed: {note['title']} → {', '.join(note['pages'])}")
    head = (
        f"Tidy-up of {run['started'][:16].replace('T', ' ')} UTC in {data['space']} "
        f"(run {run['id']}): {len(data['pages'])} page(s) changed"
        + (", since undone as a whole" if run["reverted"] else "")
        + ".\n"
    )
    undo = (
        f"\nThe user can undo pages (undoable ones) from the view or at {data['url']}."
        if data["can_undo"]
        else ""
    )
    return head + quoted("tidy", "\n".join(lines)) + undo
