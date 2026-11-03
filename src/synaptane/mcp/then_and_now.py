"""`then_and_now`: what the spaces said on a topic at a past date, next to what they say now.

"What did we say about the backup in September?", "how has the onboarding changed since
spring?": the assistant gets, for each page on the topic, its section now and the change since
that date (a diff: the lines it said then and no longer says, those it says since). Pages that
spoke of the topic then and no longer do (deleted, or the passage removed) come too, with what
they said. Each space is read as it was at that date, on its line of commits (Git keeps it).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from synaptane.core.errors import SynaptaneError
from synaptane.core.markdown import title_of
from synaptane.git.repo import Repository
from synaptane.mcp import editing, pages
from synaptane.mcp.kit import READ_ONLY, Kit, quoted
from synaptane.services import content as files
from synaptane.services import search as search_service
from synaptane.services import spaces as spaces_service
from synaptane.services.inbox import CONTENT_DIR
from synaptane.storage.models import Space

PAGES = 8  # pages compared at most
SCANNED = 500  # pages changed since the date, read as they were, at most per space
SHARE = 600  # characters a page gets at least


@dataclass(frozen=True)
class _Start:
    """A space at the date: its commit then, and the files changed since (net)."""

    space: Space
    repo: Repository
    start: str
    changes: list[tuple[str, str, str]]  # (status, path now, path then), relative to the repo


@dataclass
class _Page:
    space: Space
    path: str  # now (or then, for a page deleted since)
    old: str  # path then, "" if the page did not exist
    now: str | None  # text now, None if deleted since
    then: str | None  # text then, None if created since
    anchor: str  # section found


def register(server: MCPServer, kit: Kit) -> None:
    @server.tool(title="Then and now", annotations=READ_ONLY, structured_output=False)
    def then_and_now(
        topic: Annotated[str, Field(description="Topic, in the pages' language")],
        date: Annotated[str, Field(description="Past date, YYYY-MM-DD")],
        spaces: Annotated[
            list[str] | None, Field(description="Only these org/space spaces; empty: all")
        ] = None,
        budget: Annotated[
            int, Field(ge=2_000, le=40_000, description="Characters at most")
        ] = 12_000,
    ) -> str:
        """What your spaces said on a topic at a past date and say now: per page, its section
        now and the diff since (pages that no longer say it too)."""
        day = _day(date)
        with kit.call() as c:
            query = search_service.Query.parse(topic)
            visible = spaces_service.list_all_visible_spaces(c.session, c.actor)
            wanted = set(spaces or [])
            in_scope = {s.address: s for s in visible if not wanted or s.address in wanted}
            hits = search_service.search(
                c.session, c.actor, c.ws, topic, addresses=spaces or [], limit=PAGES * 2
            )
            starts: dict[str, _Start] = {}
            for address, space in in_scope.items():
                try:
                    repo = c.ws.local_repository(space)
                    start = repo.commit_before(f"{day.isoformat()}T23:59:59")
                    if start:
                        changes = repo.changes_between(start, "HEAD", CONTENT_DIR)
                        starts[address] = _Start(space, repo, start, changes)
                except SynaptaneError:
                    continue
            found: list[_Page] = []
            for hit in hits:
                if hit.address in starts and hit.path.endswith(".md") and len(found) < PAGES:
                    found.append(_now(starts[hit.address], hit))
            seen = {(p.space.address, p.path) for p in found}
            for at in starts.values():
                if len(found) < PAGES:
                    found += _gone(at, query, seen)[: PAGES - len(found)]
            if not found:
                return (
                    f'Nothing on "{topic}" in the spaces as they were on {day} nor now: try '
                    "other words with search."
                )
            share = max(budget // len(found), SHARE)
            left, parts = budget, []
            for page in found:
                part, used = _render(kit, page, starts[page.space.address].start, day, share)
                parts.append(part)
                left = max(left - used, 0)
                share = max(left // max(len(found) - len(parts), 1), SHARE)
            head = (
                f'{len(found)} page(s) on "{topic}", on {day} and now. For each: its text now '
                "and the change since (diff: `-` lines said then, `+` lines said since). Say "
                "what changed, citing each page by title and URL.\n\n"
            )
            return head + "\n\n".join(parts)


def _day(value: str) -> date:
    try:
        day = date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise ToolError("`date` must be YYYY-MM-DD, for example 2026-09-01.") from None
    if day >= datetime.now(UTC).date() or day.year < 1970:
        raise ToolError("`date` must be in the past: what the spaces said that day.")
    return day


def _text(repo: Repository, path: str, rev: str = "HEAD") -> str | None:
    data = repo.blob(files.content_path(path), rev)
    return None if data is None else data.decode("utf-8", "replace")


def _now(at: _Start, hit: search_service.Hit) -> _Page:
    """A page on the topic now, with its text then (under its old path if moved since)."""
    moved = {path: old for status, path, old in at.changes if status == "R"}
    old = files.relative_path(moved.get(files.content_path(hit.path), "")) or hit.path
    now = _text(at.repo, hit.path)
    anchor = hit.anchor
    if not anchor and now is not None and hit.snippets and hit.snippets[0].line:
        anchor = pages.anchor_at(now, hit.snippets[0].line)
    then = _text(at.repo, old, at.start)
    return _Page(at.space, hit.path, old if then is not None else "", now, then, anchor)


def _gone(at: _Start, query: search_service.Query, seen: set[tuple[str, str]]) -> list[_Page]:
    """Pages that spoke of the topic at the date and no longer do: deleted since, or changed
    and no longer found by the search."""
    changed = [
        (status, files.relative_path(path), files.relative_path(old) or files.relative_path(path))
        for status, path, old in at.changes
        if status in ("D", "M", "R") and (old or path).endswith(".md")
    ][:SCANNED]
    texts = at.repo.read_many([files.content_path(old) for _, _, old in changed], at.start)
    found = []
    for status, path, old in changed:
        data = texts.get(files.content_path(old))
        if data is None or (at.space.address, path) in seen:
            continue
        then = data.decode("utf-8", "replace")
        if len(query.matched_terms(then)) < len(query.terms):
            continue
        line = next(
            (n for n, row in enumerate(then.splitlines(), 1) if query.matched_terms(row)), 0
        )
        now = None if status == "D" else _text(at.repo, path)
        anchor = pages.anchor_at(then, line) if line else ""
        found.append(_Page(at.space, path, old, now, then, anchor))
    return found


def _render(kit: Kit, page: _Page, start: str, day: date, share: int) -> tuple[str, int]:
    """One page: its section now, and the change since `day`."""
    text = page.now if page.now is not None else page.then or ""
    title = title_of(text, page.path.rsplit("/", 1)[-1])
    now_section = _section(page.now, page.anchor)
    then_section = _section(page.then, page.anchor, now_section)
    org, slug = page.space.address.split("/", 1)
    anchor = now_section[0] if now_section and page.now is not None else ""
    lines = [f"## {title} — {page.space.address}/{page.path}"]
    if page.now is not None:
        lines.append(f"url: {kit.web_url(org, slug, page.path, anchor)}")
    source = f"{page.space.address}/{page.path}"
    then_text = then_section[1] if then_section else page.then
    now_text = now_section[1] if now_section else page.now
    where = f"section {anchor}" if anchor else "whole page"
    if page.then is None:
        state = f"created since {day} ({where})"
    elif page.now is None:
        state = f"deleted since {day}: what it said then (commit {start[:12]})"
    elif page.old and page.old != page.path:
        state = f"moved since {day} from {page.old} ({where})"
    else:
        state = f"{where}"
    lines.append(state)
    body = (now_text if page.now is not None else then_text) or ""
    used = min(len(body), share)
    if len(body) > share:
        body = body[:share].rsplit("\n", 1)[0] + f"\n[… {len(body) - share} more characters]"
    lines.append(quoted("page", body, source))
    if page.now is not None and page.then is not None:
        change = editing.change(then_text or "", now_text or "")
        if not change:
            lines.append(f"Unchanged since {day}.")
        else:
            lines.append(f"Change since {day} (commit {start[:12]}):")
            lines.append(quoted("diff", editing.short(change, max(share, SHARE)), source))
            used += min(len(change), share)
    return "\n".join(lines), used


def _section(
    text: str | None, anchor: str, other: tuple[str, str] | None = None
) -> tuple[str, str] | None:
    """(anchor, text) of the section `anchor` of a page, found by its title when the anchor
    changed (`other`: the same section in the other version); None: the whole page."""
    if text is None or not anchor:
        return None
    found = pages.sections(text)
    section = pages.find_section(found, anchor)
    if section is None and other is not None:
        heading = other[1].split("\n", 1)[0].lstrip("#").strip()
        section = pages.find_section(found, heading)
    return (section.anchor, pages.section_text(text, section)) if section else None
