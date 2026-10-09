"""Shared Markdown parsing: a single parser, a single rule for resolving links."""

import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.gfm_autolink import gfm_autolink_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

# Wiki link (Obsidian, Foam…): [[name]], [[name|label]], [[name#section]], [[folder/name]].
WIKILINK = re.compile(r"\[\[([^\[\]\n|#]+?)(?:#([^\[\]\n|]*))?(?:\|([^\[\]\n]+))?\]\]")
WIKI_SCHEME = "wiki:"  # temporary address of a wiki link, resolved at render time


def _wikilink(state: StateInline, silent: bool) -> bool:
    if not state.src.startswith("[[", state.pos):
        return False
    match = WIKILINK.match(state.src, state.pos)
    if match is None:
        return False
    if not silent:
        name, fragment, label = match.group(1).strip(), match.group(2), match.group(3)
        opening = state.push("link_open", "a", 1)
        opening.attrs = {"href": WIKI_SCHEME + name + (f"#{fragment}" if fragment else "")}
        opening.meta = {"wikilink": True}
        text = state.push("text", "", 0)
        text.content = (label or name).strip()
        state.push("link_close", "a", -1)
    state.pos = match.end()
    return True


def parser(highlight: object = None) -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "highlight": highlight})
    md.enable(["table", "strikethrough"])
    md.use(front_matter_plugin).use(tasklists_plugin).use(anchors_plugin, max_level=4)
    md.use(gfm_autolink_plugin)  # bare https://…, www.… and emails become links
    md.inline.ruler.before("link", "wikilink", _wikilink)
    return md


@dataclass
class WikiIndex:
    """Files of a space (paths relative to content/), to resolve wiki links."""

    paths: set[str] = field(default_factory=set)
    by_stem: dict[str, list[str]] = field(default_factory=dict)  # pages: name without ".md"
    by_name: dict[str, list[str]] = field(default_factory=dict)  # any file: full name

    @classmethod
    def of(cls, paths: Iterable[str]) -> "WikiIndex":
        index = cls()
        for path in paths:
            index.paths.add(path)
            name = posixpath.basename(path).lower()
            index.by_name.setdefault(name, []).append(path)
            if name.endswith(".md"):
                index.by_stem.setdefault(name[:-3], []).append(path)
        return index

    def is_folder(self, path: str) -> bool:
        """True if `path` is a folder of the space (it contains at least one file).

        The set of folders is computed once: the wiki health check asks the question for
        thousands of links."""
        folders: set[str] | None = self.__dict__.get("_folders")
        if folders is None:
            folders = set()
            for file in self.paths:
                parent = posixpath.dirname(file)
                while parent and parent not in folders:
                    folders.add(parent)
                    parent = posixpath.dirname(parent)
            self.__dict__["_folders"] = folders
        return path.rstrip("/") in folders


def resolve_wiki(target: str, current: str, index: WikiIndex) -> str | None:
    """File targeted by a wiki link from `current`.

    - With a path (`[[folder/name]]`): relative to the page, then to the root; otherwise, the
      closest file whose path ends with `folder/name` (case ignored).
      A path that matches nothing never falls back to another folder: the link is missing,
      rather than silently leading to another page.
    - Without a path: page name (without `.md`) or file name; on equal names, the closest.
    """
    name = target.strip().strip("/")
    if not name:
        return None
    key = posixpath.basename(name).lower()
    found = index.by_stem.get(key.removesuffix(".md")) or index.by_name.get(key) or []
    if "/" in name:
        for candidate in (posixpath.join(posixpath.dirname(current), name), name):
            normal = posixpath.normpath(candidate)
            if normal.startswith(".."):
                continue
            for path in (normal, normal + ".md"):
                if path in index.paths:
                    return path
        normal = posixpath.normpath(name).lower()
        tail = "/" + normal.removeprefix("./")
        found = [
            p
            for p in found
            if not normal.startswith("..") and ("/" + p.lower()).endswith((tail, tail + ".md"))
        ]
    if len(found) < 2:  # the common case, without computing distances
        return found[0] if found else None
    folder = posixpath.dirname(current)

    def distance(path: str) -> tuple[int, int, str]:
        shared = posixpath.commonpath([folder, posixpath.dirname(path)]) if folder else ""
        return (-len(shared.split("/")) if shared else 0, path.count("/"), path)

    return min(found, key=distance)


_PARSER = parser()


def resolve(current: str, target: str) -> str | None:
    """Path (relative to content/) targeted by a link from the page `current`.

    `../a.md` is relative to the page; `/a.md` starts from the root of `content/`.
    None for an external link, a bare anchor or a link that leaves `content/`.
    """
    if ":" in target or "?" in target or "#" in target or target.startswith("//"):
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or not parts.path:
            return None
        path = parts.path
    else:  # plain path: the common case, without URL parsing (health check of a large space)
        path = target
    if not path:
        return None
    if "%" in path:
        path = unquote(path)
    base = "" if path.startswith("/") else posixpath.dirname(current)
    resolved = posixpath.normpath(posixpath.join(base, path.lstrip("/")))
    if resolved in (".", "") or resolved.startswith(".."):
        return None
    return resolved


def walk(tokens: list[Token]) -> list[Token]:
    flat: list[Token] = []
    for token in tokens:
        flat.append(token)
        if token.children:
            flat.extend(walk(token.children))
    return flat


def links_of(text: str, current: str, index: WikiIndex | None = None) -> set[str]:
    """Pages targeted by the relative links of a Markdown page (and by its wiki links, if the
    index of the space's files is given)."""
    return {target for _, target in link_targets(text, current, index) if target}


def link_targets(
    text: str, current: str, index: WikiIndex | None = None
) -> list[tuple[str, str | None]]:
    """Internal links of a page: (link as written, target path relative to content/).

    Images included. The path is None for a wiki link that leads to no file. External links,
    bare anchors and links that leave content/ are omitted."""
    found: list[tuple[str, str | None]] = []
    for token in walk(_PARSER.parse(text)):
        if token.type == "image":  # an image is a link to a file of the space
            if target := resolve(current, str(token.attrGet("src") or "")):
                found.append((str(token.attrGet("src")), target))
            continue
        if token.type != "link_open":
            continue
        href = str(token.attrGet("href") or "")
        if href.startswith(WIKI_SCHEME):
            name = href[len(WIKI_SCHEME) :].split("#")[0]
            target = resolve_wiki(name, current, index) if index else None
            if index:
                found.append((f"[[{name}]]", target))
            continue
        target = resolve(current, href)
        if target:
            found.append((href, target))
    return found


def broken_links(text: str, current: str, index: WikiIndex) -> list[tuple[str, str | None]]:
    """Broken internal links of a page, with a suggestion (relative link to the closest file
    with the same name) when there is one."""
    return broken_among(link_targets(text, current, index), current, index)


def broken_among(
    targets: Iterable[tuple[str, str | None]], current: str, index: WikiIndex
) -> list[tuple[str, str | None]]:
    """Among links (as written, target), those that lead to no file or folder of the space,
    with their suggestion."""
    broken: list[tuple[str, str | None]] = []
    for written, target in targets:
        if target is not None and (target in index.paths or index.is_folder(target)):
            continue
        suggestion = None
        if target is not None:
            nearest = resolve_wiki(posixpath.basename(target), current, index)
            if nearest:
                suggestion = posixpath.relpath(nearest, posixpath.dirname(current) or ".")
        if (written, suggestion) not in broken:
            broken.append((written, suggestion))
    return broken


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[^\n]*$|\Z)", re.M | re.S)
_CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.S)
_INLINE_LINK = re.compile(r"\]\(\s*(<[^>\n]*>|[^)\s]+)")
_REFERENCE = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*(<[^>\n]*>|\S+)", re.M)


@dataclass(frozen=True)
class LinkRef:
    """Link found in the source text, with its position: enough to rewrite its target alone.

    `kind`: `link` (inline link or image), `reference` (definition `[x]: target`) or `wiki`.
    `start` and `end` surround the target as written (`target`): the address without its
    angle brackets, or the name of a wiki link without section or label."""

    kind: str
    start: int
    end: int
    target: str


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group())


def without_code(text: str) -> str:
    """Text whose code blocks and code spans are replaced with spaces: positions stay those
    of the original text."""
    if "`" not in text and "~~~" not in text:
        return text
    return _CODE_SPAN.sub(_blank, _FENCE.sub(_blank, text))


def link_refs(text: str, *, wiki: bool = True) -> list[LinkRef]:
    """Links of a page, outside code blocks and code spans, in text order.

    Regular expressions rather than full parsing: fast over thousands of pages, and the
    returned positions are those of the original text (code is masked with spaces, not
    removed). Exotic Markdown cases may differ from `link_targets`."""
    masked = without_code(text)
    found: list[LinkRef] = []
    for kind, pattern in (("link", _INLINE_LINK), ("reference", _REFERENCE)):
        for match in pattern.finditer(masked):
            start, end = match.span(1)
            if masked[start] == "<":
                start, end = start + 1, end - 1
            found.append(LinkRef(kind, start, end, text[start:end]))
    if wiki and "[[" in masked:
        for match in WIKILINK.finditer(masked):
            raw = match.group(1)
            start = match.start(1) + len(raw) - len(raw.lstrip())
            end = match.end(1) - (len(raw) - len(raw.rstrip()))
            if start < end:
                found.append(LinkRef("wiki", start, end, text[start:end]))
    return sorted(found, key=lambda ref: ref.start)


def links_fast(text: str, current: str, index: WikiIndex | None = None) -> set[str]:
    """Like `links_of`, much faster (regular expressions, without full parsing).

    For indexing thousands of pages: inline links and reference definitions, outside code
    blocks and code spans. Exotic Markdown cases may differ from `links_of`.
    """
    wiki = index is not None and "[[" in text
    if "](" not in text and "]:" not in text and not wiki:
        return set()
    found = set()
    for ref in link_refs(text, wiki=wiki):
        if ref.kind != "wiki":
            target = resolve(current, ref.target)
        else:
            target = resolve_wiki(ref.target, current, index) if index is not None else None
        if target:
            found.add(target)
    return found


def broken_fast(text: str, current: str, index: WikiIndex) -> list[tuple[str, str | None]]:
    """Like `broken_links`, much faster (see `link_refs`): for a whole space."""
    if "](" not in text and "]:" not in text and "[[" not in text:
        return []
    targets: list[tuple[str, str | None]] = []
    for ref in link_refs(text):
        if ref.kind == "wiki":
            targets.append((f"[[{ref.target}]]", resolve_wiki(ref.target, current, index)))
        elif target := resolve(current, ref.target):
            targets.append((ref.target, target))
    return broken_among(targets, current, index)


# --- Rewriting links after a move ------------------------------------------------------------

# Characters a link address cannot carry as is (outside angle brackets).
_UNSAFE_HREF = re.compile(r"[\s()<>]")
_UNSAFE_ANGLED = re.compile(r"[\n<>]")
_WIKI_FORBIDDEN = re.compile(r"[\[\]|#\n]")


def moved_path(path: str, moves: Mapping[str, str]) -> str:
    """New path of `path` after the moves `moves` (old → new; a moved folder takes everything
    it contains with it)."""
    if path in moves:
        return moves[path]
    for old, new in moves.items():
        if path.startswith(old + "/"):
            return new + path[len(old) :]
    return path


def rewrite_links(
    text: str,
    before: str,
    after: str,
    moves: Mapping[str, str],
    old_index: WikiIndex,
    new_index: WikiIndex,
) -> str:
    """Text of the page `before` (now `after`, or left in place) where every internal link
    still targets the same file once the moves `moves` are done.

    - Relative link (`../a.md#section`, images included): recomputed from the page's new
      location to its target's new location; section and query kept. A link from the root
      (`/a.md`) stays written from the root.
    - Wiki link: kept as long as it still resolves to the right file (`[[name]]` follows the
      page wherever it goes); otherwise, `[[name]]` becomes the shortest form that leads
      there, and `[[folder/name]]` the full path. Section and label are kept.
    - An external link, a link to a file that does not move (from a page that does not move)
      and a wiki link without a target stay as they are.

    Only link targets change: the rest of the text is kept identical. Only the links the full
    parser also sees are rewritten (nothing inside a code block)."""
    refs = link_refs(text)
    if not refs:
        return text
    seen = link_targets(text, before, old_index)
    real_targets = {t for written, t in seen if t is not None and not written.startswith("[[")}
    real_names = {written[2:-2] for written, _ in seen if written.startswith("[[")}
    out: list[str] = []
    position = 0
    for ref in refs:
        new: str | None
        if ref.kind == "wiki":
            new = None
            if ref.target in real_names:
                new = _rewrite_wiki(ref.target, before, after, moves, old_index, new_index)
        else:
            angled = ref.start > 0 and text[ref.start - 1] == "<"
            new = _rewrite_href(ref.target, before, after, moves, real_targets, angled)
        if new is None or new == ref.target:
            continue
        out += [text[position : ref.start], new]
        position = ref.end
    out.append(text[position:])
    return "".join(out)


def _rewrite_href(
    href: str,
    before: str,
    after: str,
    moves: Mapping[str, str],
    real: set[str],
    angled: bool = False,
) -> str | None:
    """New address of a relative link, or None if it does not need to change. `angled`:
    address between angle brackets, where spaces are allowed."""
    target = resolve(before, href)
    if target is None or target not in real:
        return None
    new_target = moved_path(target, moves)
    cut = min((i for i in (href.find("#"), href.find("?")) if i >= 0), default=len(href))
    path, suffix = href[:cut], href[cut:]
    if resolve(after, path) == new_target:
        return None  # already right from the new location (link from the root…)
    if path.startswith("/"):
        new = "/" + new_target
    else:
        new = posixpath.relpath(new_target, posixpath.dirname(after) or ".")
        if path.startswith("./") and not new.startswith("."):
            new = "./" + new
    if path.endswith("/") and not new.endswith("/"):
        new += "/"
    if "%" in path or (_UNSAFE_ANGLED if angled else _UNSAFE_HREF).search(new):
        new = quote(new, safe="/!$&'*+,;=:@~")
    return new + suffix


def _rewrite_wiki(
    name: str,
    before: str,
    after: str,
    moves: Mapping[str, str],
    old_index: WikiIndex,
    new_index: WikiIndex,
) -> str | None:
    target = resolve_wiki(name, before, old_index)
    if target is None:
        return None
    new_target = moved_path(target, moves)
    if resolve_wiki(name, after, new_index) == new_target:
        return None
    parts = new_target.split("/")
    if new_target.endswith(".md") and not name.lower().endswith(".md"):
        parts[-1] = parts[-1][:-3]
    forms = ["/".join(parts[k:]) for k in range(len(parts) - 1, -1, -1)]  # shortest first
    if "/" in name:  # `[[folder/name]]` stays a path: the full one, unambiguous
        forms.reverse()
    for form in forms:
        if not _WIKI_FORBIDDEN.search(form) and resolve_wiki(form, after, new_index) == new_target:
            return form
    return None


def title_of(text: str, fallback: str) -> str:
    """First level-1 heading, else `fallback`."""
    tokens = _PARSER.parse(text)
    for index, token in enumerate(tokens[:-1]):
        if token.type == "heading_open" and token.tag == "h1":
            return tokens[index + 1].content.strip() or fallback
    return fallback


def outline(text: str) -> list[tuple[int, str, str]]:
    """Outline of a page: (level 1 to 4, anchor, text) of each heading, in order.
    The anchors are those of the rendering (same parser, `anchors` plugin)."""
    tokens = _PARSER.parse(text)
    found = []
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open" or token.tag not in ("h1", "h2", "h3", "h4"):
            continue
        anchor = str(token.attrs.get("id", ""))
        inline = tokens[index + 1]
        words = "".join(
            child.content
            for child in inline.children or []
            if child.type in ("text", "code_inline", "html_inline")
        ).strip()
        if anchor and words:
            found.append((int(token.tag[1]), anchor, words))
    return found
