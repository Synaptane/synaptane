"""What the assistants wrote, as an MCP App: their runs in a space (or all), page by page,
each page undoable from the conversation.

`assistant_runs` answers "what did Claude write yesterday?": the runs of the assistants
connected over MCP (one assistant for one person, in one go: see `services.assistant_runs`),
with the pages each one created, edited, moved or deleted. A host that renders MCP Apps shows
them with, for each page, "See the change" and "Undo" (`undo_assistant_pages`, a tool meant for
the view: the page goes back as it was before the run, in a commit under the person's name).
Any other client gets the runs as text, with the version to restore each page from.
"""

from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.apps import Apps
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from synaptane.mcp import views
from synaptane.mcp.kit import CHANGES, READ_ONLY, Call, Kit, SpaceArg, quoted
from synaptane.services import assistant_runs, spaces
from synaptane.services.assistant_runs import Run
from synaptane.services.tidy_undo import PREFIX, RunPage
from synaptane.storage.models import Space

URI = "ui://synaptane/runs"
LIMIT = 20  # runs listed, newest first


def register(apps: Apps, kit: Kit) -> None:
    @apps.tool(
        resource_uri=URI,
        name="assistant_runs",
        title="What the assistants wrote",
        annotations=READ_ONLY,
        structured_output=False,
    )
    def assistant_runs_tool(
        space: Annotated[str, Field(description="Space org/space; empty: all")] = "",
        days: Annotated[int, Field(ge=1, le=assistant_runs.DAYS, description="Days back")] = 7,
    ) -> CallToolResult:
        """What AI assistants wrote lately ("what did Claude do yesterday?"), as runs, page
        by page; the user can undo pages in the view."""
        with kit.call() as c:
            targets = (
                [kit.space_of(c, space)]
                if space.strip()
                else spaces.list_all_visible_spaces(c.session, c.actor)
            )
            found = [
                (target, run)
                for target in targets
                for run in assistant_runs.runs(c.actor, c.ws, target, days)
            ]
            found.sort(key=lambda pair: pair[1].ended, reverse=True)
            shown = [_run(c, kit, target, run) for target, run in found[:LIMIT]]
            data: dict[str, Any] = {
                "space": targets[0].address if space.strip() else "",
                "days": days,
                "runs": shown,
                "more": max(len(found) - LIMIT, 0),
            }
            return CallToolResult(
                content=[TextContent(type="text", text=_text(data))], structured_content=data
            )

    @apps.tool(
        resource_uri=URI,
        visibility=["app"],
        name="undo_assistant_pages",
        title="Undo an assistant's changes to some pages",
        annotations=CHANGES,
        structured_output=False,
    )
    def undo_assistant_pages(
        space: SpaceArg,
        run: Annotated[str, Field(description="Run id, from assistant_runs")],
        paths: Annotated[list[str], Field(description="Pages to put back as before the run")],
    ) -> CallToolResult:
        """Puts pages back as they were before an assistant's run, when the user asks
        (contributor)."""
        with kit.call() as c:
            target = kit.space_of(c, space)
            done = assistant_runs.undo_pages(
                c.session, c.database, c.actor, c.ws, target, run, paths, source="mcp"
            )
            return CallToolResult(
                content=[
                    TextContent(
                        type="text", text=f"Undone in {target.address}: {', '.join(done.pages)}."
                    )
                ],
                structured_content={"pages": done.pages},
            )

    apps.add_html_resource(
        URI, views.page("runs"), name="runs", title="What the assistants wrote", prefers_border=True
    )


def _run(c: Call, kit: Kit, space: Space, run: Run) -> dict[str, Any]:
    base = kit.space_url(space)
    pages = assistant_runs.run_pages(c.ws, space, run)
    return {
        "id": run.id,
        "space": space.address,
        "author": run.author,
        "client": run.client,
        "started": run.started,
        "ended": run.ended,
        "start": run.start,
        "commits": len(run.commits),
        "subjects": [commit.subject for commit in run.commits[:5]],
        "pages": [_page(base, run, page) for page in pages],
        "can_undo": kit.can_write(c, space),
    }


def _page(base: str, run: Run, page: RunPage) -> dict[str, Any]:
    """`diff`: the change itself when one commit of the run made it, else the page's history
    (each commit's change from there)."""
    names = {PREFIX + p for p in (page.path, page.old) if p}
    touched = [commit.sha for commit in run.commits if names & set(commit.paths)]
    return {
        "path": page.path,
        "status": page.status,
        "old": page.old,
        "state": page.state,
        "url": "" if page.status == "deleted" else f"{base}/p/{quote(page.path)}",
        "diff": f"{base}/diff/{touched[0]}/{quote(page.path)}"
        if len(touched) == 1
        else f"{base}/history/{quote(page.path)}",
    }


def _text(data: dict[str, Any]) -> str:
    """The runs for the model (and any client that does not render apps): paths and commit
    messages are written by the assistants, from what they read: untrusted."""
    where = data["space"] or "the spaces you can access"
    if not data["runs"]:
        return f"No assistant wrote pages in {where} over the last {data['days']} day(s)."
    lines = []
    for run in data["runs"]:
        who = f"{run['author']} via {run['client'] or 'an MCP assistant'}"
        lines.append(
            f"Run {run['id']} in {run['space']}, {who}, {run['started'][:16]} to "
            f"{run['ended'][11:16]} ({run['commits']} commit(s)), before it: {run['start']}"
        )
        for subject in run["subjects"]:
            lines.append(f"  commit: {subject}")
        for page in run["pages"]:
            moved = f" from {page['old']}" if page["old"] else ""
            lines.append(f"  - {page['status']}{moved}: {page['path']} ({page['state']})")
    more = f" (the {len(data['runs'])} latest; {data['more']} more)" if data["more"] else ""
    head = f"{len(data['runs'])} assistant run(s) in {where}{more}, newest first:\n"
    undo = (
        "\nThe user can undo pages from the view; without it, restore a page at the commit "
        "before its run (restore), once they confirm."
    )
    return head + quoted("assistant_runs", "\n".join(lines)) + undo
