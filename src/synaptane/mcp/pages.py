"""A page as seen by an assistant: outline, sections, anchors, tree, and the language of a
space (detected by `services.vocation.language_of`, cached here per commit).

To answer a question, an assistant often needs only one section: the outline then the
targeted section cost 5 to 10 times fewer tokens than the whole page. Anchors are those of
the web rendering (same parser): a `…#anchor` URL opens the right section.
"""

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from synaptane.core.markdown import parser
from synaptane.git.repo import Blob
from synaptane.services.content import Folder, build_tree

_PARSER = parser()


@dataclass(frozen=True)
class Section:
    level: int  # 1 to 4
    anchor: str
    title: str
    start: int  # heading line (0-based)
    end: int  # first line after the section (next heading of the same or a higher level)


def sections(text: str) -> list[Section]:
    """Sections of a Markdown page, in order; headings inside code blocks are not sections
    (the parser decides, not a regular expression)."""
    tokens = _PARSER.parse(text)
    heads: list[tuple[int, str, str, int]] = []
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open" or token.tag not in ("h1", "h2", "h3", "h4"):
            continue
        anchor = str(token.attrs.get("id", ""))
        words = tokens[index + 1].content.strip()
        if anchor and words and token.map:
            heads.append((int(token.tag[1]), anchor, words, token.map[0]))
    total = text.count("\n") + 1
    found = []
    for position, (level, anchor, words, start) in enumerate(heads):
        end = next((h[3] for h in heads[position + 1 :] if h[0] <= level), total)
        found.append(Section(level, anchor, words, start, end))
    return found


def _folded(value: str) -> str:
    from synaptane.services.search import fold

    return " ".join(fold(value).split())


def find_section(found: list[Section], wanted: str) -> Section | None:
    """Section designated by its anchor (`#` optional) or by its title (case and accents
    ignored; failing that, the first title containing the requested text)."""
    key = wanted.strip().lstrip("#").strip()
    if not key:
        return None
    for section in found:
        if section.anchor == key:
            return section
    folded = _folded(key)
    exact = [s for s in found if _folded(s.title) == folded]
    if exact:
        return exact[0]
    return next((s for s in found if folded in _folded(s.title)), None)


def section_text(text: str, section: Section) -> str:
    return "\n".join(text.split("\n")[section.start : section.end]).strip("\n") + "\n"


def anchor_at(text: str, line: int) -> str:
    """Anchor of the section containing line `line` (1-based), empty at the top of the page
    or in the main title's section (the page URL is enough)."""
    current = ""
    for section in sections(text):
        if section.start >= line:
            break
        current = "" if section.level == 1 else section.anchor
    return current


def outline_lines(found: list[Section]) -> list[str]:
    return [
        f'{"  " * (s.level - 1)}- {s.title}  (section="{s.anchor}", lines {s.start + 1}-{s.end})'
        for s in found
    ]


# --- Tree ---------------------------------------------------------------------------------


def size_of(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{size / 1024:.1f} KiB"


def tree_lines(blobs: list[Blob], depth: int) -> list[str]:
    """Folder tree down to `depth` levels: deeper folders are summarized (file count), to
    browse a large space without listing everything."""
    lines: list[str] = []

    def walk(folder: Folder, level: int) -> None:
        for sub in folder.folders.values():
            count = sum(1 for _ in sub.walk_files())
            lines.append(f"{'  ' * level}{sub.path}/  ({count} file{'s' * (count > 1)})")
            if level + 1 < depth:
                walk(sub, level + 1)
        for blob in folder.files:
            lines.append(f"{'  ' * level}{blob.path}  ({size_of(blob.size)})")

    root = build_tree(blobs)
    walk(root, 0)
    return lines


# --- Page language ------------------------------------------------------------------------

_CACHE: OrderedDict[tuple[UUID, str], str] = OrderedDict()
_LOCK = threading.Lock()


def cached_language(space_id: UUID, head: str, compute: Callable[[], str]) -> str:
    """Language of a space, computed once per commit (the space list is requested at the
    start of every conversation)."""
    key = (space_id, head)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    value = compute()
    with _LOCK:
        _CACHE[key] = value
        while len(_CACHE) > 256:
            _CACHE.popitem(last=False)
    return value
