"""`brief`: what the spaces say on a topic, in one call.

An assistant asked a question usually searches, then reads page after page. `brief` gives it,
within a budget of characters, the best section of each relevant page (the one the search
found the topic in), with what makes it trustworthy or not (verified, due for review, reported
outdated, last change and whether an AI made it) and the pages that section links to, to
read further. Enough to answer, citing; `read` remains for a whole page.
"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from synaptane.core.errors import SynaptaneError
from synaptane.core.markdown import WikiIndex, link_targets
from synaptane.mcp import pages
from synaptane.mcp.kit import READ_ONLY, Call, Kit, quoted
from synaptane.services import content as files
from synaptane.services import search as search_service
from synaptane.services import spaces as spaces_service
from synaptane.storage.models import Space

PAGES = 8  # pages in a brief at most
LINKS = 5  # linked pages named per page
SHARE = 600  # characters a page gets at least


def register(server: MCPServer, kit: Kit) -> None:
    @server.tool(title="Brief on a topic", annotations=READ_ONLY, structured_output=False)
    def brief(
        topic: Annotated[str, Field(description="Topic, in the pages' language")],
        spaces: Annotated[
            list[str] | None, Field(description="Only these org/space spaces; empty: all")
        ] = None,
        budget: Annotated[
            int, Field(ge=2_000, le=40_000, description="Characters of page text at most")
        ] = 12_000,
    ) -> str:
        """Best section of each page on a topic, in one call: URL, trust (verified, stale,
        last change, AI), links. To answer; read gives a whole page."""
        with kit.call() as c:
            hits = search_service.search(
                c.session, c.actor, c.ws, topic, addresses=spaces or [], limit=PAGES
            )
            if not hits:
                return (
                    f'Nothing found on "{topic}": try other words (synonyms, the pages\' '
                    "language) with search, and say that the spaces do not cover it."
                )
            visible = {
                s.address: s for s in spaces_service.list_all_visible_spaces(c.session, c.actor)
            }
            texts = _texts(c, visible, hits)
            indexes = {
                a: files.wiki_index(c.actor, c.ws, visible[a])
                for a in {address for address, _ in texts}
            }
            shown = {(h.address, h.path) for h in hits}
            share = max(budget // len(hits), SHARE)
            left = budget
            parts = []
            for hit in hits:
                text = texts.get((hit.address, hit.path))
                index = indexes.get(hit.address)
                part, used = _page(kit, hit, text, index, max(min(share, left), SHARE), shown)
                parts.append(part)
                left = max(left - used, 0)
                share = max(left // max(len(hits) - len(parts), 1), SHARE)
            head = (
                f'{len(hits)} page(s) on "{topic}", the most relevant first. Cite them by '
                "title and URL; say what they do not cover.\n\n"
            )
            return head + "\n\n".join(parts)


def _texts(
    c: Call, visible: dict[str, Space], hits: list[search_service.Hit]
) -> dict[tuple[str, str], str]:
    """Text of the Markdown pages among the results, one batched read per space."""
    found: dict[tuple[str, str], str] = {}
    for address in {h.address for h in hits}:
        wanted = [h.path for h in hits if h.address == address and h.path.endswith(".md")]
        if address not in visible or not wanted:
            continue
        try:
            read = c.ws.local_repository(visible[address]).read_many(
                [files.content_path(p) for p in wanted]
            )
        except SynaptaneError:
            continue
        for path, data in read.items():
            found[address, files.relative_path(path)] = data.decode("utf-8", "replace")
    return found


def _page(
    kit: Kit,
    hit: search_service.Hit,
    text: str | None,
    index: WikiIndex | None,
    share: int,
    shown: set[tuple[str, str]],
) -> tuple[str, int]:
    """One page of the brief, and the characters of page text it used."""
    anchor = hit.anchor
    if text is not None:
        found = pages.sections(text)
        if not anchor and hit.snippets and hit.snippets[0].line:
            anchor = pages.anchor_at(text, hit.snippets[0].line)
        section = pages.find_section(found, anchor) if anchor else None
        body = pages.section_text(text, section) if section else text
        where = f"section {section.anchor}" if section else "start of the page"
    else:  # PDF, Office document, image: the search excerpts
        body = "\n".join(hit.passage) or "\n".join(s.text for s in hit.snippets)
        section, where = None, "excerpts (extracted text)"
    if len(body) > share:
        cut = body[:share].rsplit("\n", 1)[0] or body[:share]
        rest = f'read with section="{section.anchor}"' if section else "read the page"
        body = f"{cut}\n[… {len(body) - len(cut)} more characters: {rest}]"
    trust = []
    if hit.reported:
        trust.append(f"reported outdated ×{hit.reported}: say so when citing it")
    if hit.stale:
        trust.append("due for review: may be outdated")
    elif hit.verified:
        trust.append("verified")
    if hit.edit:
        trust.append(
            f"edited {hit.edit.date[:10]} by {hit.edit.author}"
            + (f" (AI: {hit.edit.origin})" if hit.edit.ai else "")
        )
    lines = [
        f"## {hit.title} — {hit.address}/{hit.path} ({where})",
        f"url: {kit.web_url(hit.org, hit.space, hit.path, section.anchor if section else '')}",
    ]
    if trust:
        lines.append("trust: " + "; ".join(trust))
    if text is not None and index is not None:
        targets = sorted(
            {
                t
                for _, t in link_targets(body, hit.path, index)
                if t and t in index.paths and (hit.address, t) not in shown and t != hit.path
            }
        )
        if targets:
            lines.append("links: " + ", ".join(targets[:LINKS]))
    lines.append(quoted("page", body, f"{hit.address}/{hit.path}"))
    return "\n".join(lines), len(body)
