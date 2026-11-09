"""HTML to Markdown, for the pages of an imported export (Confluence).

The HTML is not trusted: it is only parsed (standard library, nothing is run or loaded),
scripts, styles, frames and forms are dropped, and links keep only safe schemes. Where each
link and image leads is decided by the caller (`link`, `image`): the export's files are
renamed, and an address the caller does not know is kept as written.

What Markdown can say is kept: headings, paragraphs, emphasis, lists (tasks too), tables,
code, quotes, links and images. Confluence macros become their Markdown equivalent:
info, tip, note and warning panels become callouts (`> [!info]`), an expand becomes a folded
callout, a code block a fenced block with its language. Colours, layouts and the rest of the
look are lost.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
# Dropped with what they hold: nothing to read in them, or nothing safe.
DROPPED = {
    "script",
    "style",
    "head",
    "noscript",
    "template",
    "iframe",
    "frame",
    "frameset",
    "object",
    "applet",
    "svg",
    "math",
    "canvas",
    "form",
    "button",
    "select",
    "textarea",
    "video",
    "audio",
    "map",
}
INLINE = {
    "a",
    "abbr",
    "b",
    "bdi",
    "bdo",
    "big",
    "br",
    "cite",
    "code",
    "data",
    "del",
    "dfn",
    "em",
    "font",
    "i",
    "img",
    "input",
    "ins",
    "kbd",
    "label",
    "mark",
    "q",
    "s",
    "samp",
    "small",
    "span",
    "strike",
    "strong",
    "sub",
    "sup",
    "time",
    "tt",
    "u",
    "var",
    "wbr",
}
# A new block closes a paragraph left open (`<p>a<div>b</div>`).
_CLOSES_P = {
    "address",
    "article",
    "aside",
    "blockquote",
    "details",
    "div",
    "dl",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "ul",
}
MAX_DEPTH = 64  # deeper elements are attached to the last open one: no unbounded recursion
# Empty cells added to a table (spans, short rows): beyond, rows are left short, which Markdown
# reads the same way. A table of a few bytes per cell would otherwise grow to its rows times
# its widest row.
MAX_PADDING = 100_000
SAFE_SCHEMES = {"", "http", "https", "mailto", "tel", "ftp"}
# Confluence panels: their class (or panel type) → callout type understood by Synaptane.
PANELS = {
    "information": "info",
    "info": "info",
    "tip": "tip",
    "success": "tip",
    "note": "warning",
    "warning": "danger",
    "error": "danger",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = None

    def get(self, name: str) -> str:
        return self.attrs.get(name, "")

    @property
    def classes(self) -> set[str]:
        return set(self.get("class").split())

    def walk(self) -> Iterator["Node"]:
        """This node and its descendants, in document order (without recursion)."""
        stack: list[Node] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(child for child in reversed(node.children) if isinstance(child, Node))

    def find(self, test: Callable[["Node"], bool]) -> "Node | None":
        return next((node for node in self.walk() if test(node)), None)

    def find_all(self, test: Callable[["Node"], bool]) -> list["Node"]:
        return [node for node in self.walk() if test(node)]

    def text(self) -> str:
        """Its text, spaces collapsed."""
        parts: list[str] = []
        stack: list[Node | str] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in DROPPED:
                stack.extend(reversed(item.children))
        return " ".join("".join(parts).split())


def by_id(name: str) -> Callable[[Node], bool]:
    return lambda node: node.get("id") == name


def by_class(*names: str) -> Callable[[Node], bool]:
    return lambda node: all(name in node.classes for name in names)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.current = self.root
        self.depth = 0

    def _close(self, tag: str, stop: set[str]) -> None:
        """Closes the open `tag` if no element of `stop` is open after it."""
        node: Node | None = self.current
        while node is not None and node is not self.root:
            if node.tag == tag:
                self._pop_to(node)
                return
            if node.tag in stop:
                return
            node = node.parent

    def _pop_to(self, node: Node) -> None:
        current: Node | None = self.current
        while current is not None and current is not node:
            current = current.parent
            self.depth -= 1
        self.current = node.parent or self.root
        self.depth -= 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _CLOSES_P:
            self._close("p", {"td", "th", "li", "div", "blockquote", "table"})
        if tag == "li":
            self._close("li", {"ul", "ol"})
        elif tag in ("td", "th"):
            self._close("td", {"tr", "table"})
            self._close("th", {"tr", "table"})
        elif tag == "tr":
            self._close("tr", {"table"})
        elif tag in ("dt", "dd"):
            self._close("dt", {"dl"})
            self._close("dd", {"dl"})
        node = Node(tag, {key: value or "" for key, value in attrs}, [], self.current)
        self.current.children.append(node)
        if tag not in VOID and self.depth < MAX_DEPTH:
            self.current = node
            self.depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs}, [], self.current)
        self.current.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        node: Node | None = self.current
        while node is not None and node is not self.root:
            if node.tag == tag:
                self._pop_to(node)
                return
            node = node.parent

    def handle_data(self, data: str) -> None:
        children = self.current.children
        if children and isinstance(children[-1], str):
            children[-1] += data
        else:
            children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


# --- Writing Markdown ------------------------------------------------------------------------

_SPACES = re.compile(r"\s+")
_ESCAPED = re.compile(r"([\\`*\[\]<])")  # `<`: raw HTML, or an autolink
_UNDERSCORE = re.compile(r"(?<![^\W_])_|_(?![^\W_])")  # at a word's edge: snake_case stays
_ENTITY = re.compile(r"&(?=#?\w+;)")
_LINE_START = re.compile(r"^(\s*)([#>+=-])(?=\s|$)")
_NUMBERED = re.compile(r"^(\s*\d+)([.)])(?=\s|$)")
_FENCE_RUN = re.compile(r"`+")
_BREAK = "\\\n"


def escape(text: str) -> str:
    """Text written as is in Markdown: nothing in it becomes formatting."""
    text = _ESCAPED.sub(r"\\\1", text)
    text = _UNDERSCORE.sub(r"\\_", text)
    text = _ENTITY.sub(r"\\&", text)
    # Obsidian syntax read by Synaptane: highlighting, comments, strikethrough.
    return text.replace("==", "=\\=").replace("%%", "%\\%").replace("~~", "~\\~")


def destination(url: str) -> str:
    """Address of a Markdown link, written so that spaces and parentheses do not cut it."""
    url = url.replace("<", "%3C").replace(">", "%3E").replace("\n", "").replace("\r", "")
    return f"<{url}>" if re.search(r"[\s()]", url) else url


def safe_url(url: str) -> bool:
    try:
        scheme = urlsplit(url.strip()).scheme.lower()
    except ValueError:
        return False
    return scheme in SAFE_SCHEMES


def callout(kind: str, title: str, body: str, folded: bool = False) -> str:
    """A callout read by Synaptane (and Obsidian): `> [!kind] Title`, then its content."""
    head = f"[!{kind}]{'-' if folded else ''}" + (f" {title}" if title else "")
    lines = [head, *([""] + body.splitlines() if body.strip() else [])]
    return "\n".join(f"> {line}" if line.strip() else ">" for line in lines)


def _row(cells: list[str]) -> str:
    return "|" + "|".join(f" {cell} " if cell else " " for cell in cells) + "|"


def _line_start(line: str) -> str:
    """A line of text that Markdown would take for a heading, a quote or a list item."""
    if _NUMBERED.match(line):
        return _NUMBERED.sub(r"\1\\\2", line)
    return _LINE_START.sub(r"\1\\\2", line)


def _code_span(text: str) -> str:
    text = _SPACES.sub(" ", text)
    if not text.strip():
        return ""
    longest = max((len(run) for run in _FENCE_RUN.findall(text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _raw_text(node: Node) -> str:
    """Text of a code block, line breaks included."""
    parts: list[str] = []

    def collect(item: Node | str) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif item.tag == "br":
            parts.append("\n")
        elif item.tag not in DROPPED:
            if item.tag in ("div", "p") and parts and not parts[-1].endswith("\n"):
                parts.append("\n")
            for child in item.children:
                collect(child)
            if item.tag in ("div", "p") and parts and not parts[-1].endswith("\n"):
                parts.append("\n")

    collect(node)
    return "".join(parts)


class Writer:
    """One page: `link` and `image` give the new address of a link or image (None: the
    link is dropped and its text kept, the image is dropped); `shift` lowers every heading."""

    def __init__(
        self,
        link: Callable[[str], str | None] = lambda href: href,
        image: Callable[[str], str | None] = lambda src: src,
        shift: int = 0,
    ) -> None:
        self.link = link
        self.image = image
        self.shift = shift
        self.in_table = 0

    # Blocks

    def markdown(self, node: Node) -> str:
        text = self.container(node.children)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return "\n".join(line.rstrip() for line in text.splitlines()).strip() + "\n"

    def container(self, children: list[Node | str], tight: bool = False) -> str:
        """Blocks separated by a blank line; `tight` (a list item): a list right after the
        text, so that the list stays tight."""
        blocks: list[str] = []
        lists: set[int] = set()  # positions of blocks that are lists
        run: list[Node | str] = []

        def flush() -> None:
            if run:
                paragraph = self.paragraph(run)
                if paragraph:
                    blocks.append(paragraph)
                run.clear()

        for child in children:
            if isinstance(child, str) or child.tag in INLINE:
                run.append(child)
                continue
            if child.tag in DROPPED:
                continue
            flush()
            block = self.block(child).strip("\n")
            if block.strip():
                if child.tag in ("ul", "ol"):
                    lists.add(len(blocks))
                blocks.append(block)
        flush()
        if not tight:
            return "\n\n".join(blocks)
        text = blocks[0] if blocks else ""
        for index in range(1, len(blocks)):
            text += ("\n" if index in lists else "\n\n") + blocks[index]
        return text

    def paragraph(self, nodes: list[Node | str]) -> str:
        text = self.inline(nodes)
        lines = [line.strip() for line in text.split("\n")]
        while lines and lines[-1] in ("", "\\"):
            lines.pop()
        while lines and lines[0] in ("", "\\"):
            lines.pop(0)
        if lines and lines[-1].endswith("\\"):
            lines[-1] = lines[-1][:-1].rstrip()
        return "\n".join(_line_start(line) for line in lines if line)

    def block(self, node: Node) -> str:
        tag, classes = node.tag, node.classes
        special = self.macro(node, classes)
        if special is not None:
            return special
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self.inline(node.children).replace("\\\n", " ").strip().rstrip(":").rstrip()
            if not text:
                return ""
            if self.in_table:
                return f"**{text}**"
            return "#" * min(6, int(tag[1]) + self.shift) + " " + text
        if tag in ("ul", "ol"):
            return self.listing(node, ordered=tag == "ol")
        if tag == "pre":
            return self.code(node, node)
        if tag == "blockquote":
            body = self.container(node.children)
            return "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
        if tag == "table":
            return self.table(node)
        if tag == "hr":
            return "---"
        if tag == "dl":
            return self.definitions(node)
        if tag == "details":
            summary = next(
                (c for c in node.children if isinstance(c, Node) and c.tag == "summary"), None
            )
            title = self.title(summary) if summary else ""
            rest = [c for c in node.children if c is not summary]
            return callout("note", title, self.container(rest), folded=True)
        if tag == "aside":
            return callout("note", "", self.container(node.children))
        if tag == "figcaption":
            text = self.inline(node.children).strip()
            return f"*{text}*" if text else ""
        return self.container(node.children)

    def title(self, node: Node) -> str:
        """Title of a callout or a code block: its text, without formatting."""
        return escape(node.text())

    def macro(self, node: Node, classes: set[str]) -> str | None:
        """Confluence macros, recognized by their classes. None: not a macro."""
        if not classes:
            return None
        if classes & {"toc-macro", "client-side-toc-macro", "toc-indentation"}:
            return ""  # a table of contents: Synaptane shows its own outline
        if "confluence-information-macro" in classes:
            kind = next(
                (
                    PANELS[c.rsplit("-", 1)[1]]
                    for c in classes
                    if c.startswith("confluence-information-macro-")
                    and c.rsplit("-", 1)[1] in PANELS
                ),
                "info",
            )
            title_node = node.find(by_class("title"))
            body = node.find(by_class("confluence-information-macro-body"))
            title = self.title(title_node) if title_node else ""
            return callout(kind, title, self.container(body.children) if body else "")
        if "expand-container" in classes:
            title_node = node.find(by_class("expand-control-text"))
            body = node.find(by_class("expand-content"))
            title = self.title(title_node) if title_node else ""
            content = self.container(body.children) if body else ""
            return callout("note", title, content, folded=True)
        if "panel" in classes:
            pre = node.find(lambda n: n.tag == "pre")
            if ("code" in classes or "preformatted" in classes) and pre is not None:
                header = node.find(by_class("codeHeader"))
                title = self.title(header) if header else ""
                fenced = self.code(pre, pre)
                return f"**{title}**\n\n{fenced}" if title else fenced
            header = node.find(by_class("panelHeader"))
            body = node.find(by_class("panelContent"))
            kind = PANELS.get(node.get("data-panel-type"), "note")
            title = self.title(header) if header else ""
            return callout(kind, title, self.container(body.children if body else node.children))
        return None

    def code(self, node: Node, pre: Node) -> str:
        text = _raw_text(node).strip("\n")
        if self.in_table:
            return _code_span(text)
        language = ""
        params = pre.get("data-syntaxhighlighter-params")
        brush = re.search(r"brush:\s*([\w+#-]+)", params)
        if brush:
            language = brush.group(1)
        else:
            for item in [pre, *(c for c in pre.children if isinstance(c, Node))]:
                for name in item.classes:
                    if name.startswith(("language-", "lang-")):
                        language = name.split("-", 1)[1]
        if language.lower() in ("none", "text", "plain"):
            language = ""
        longest = max((len(run) for run in _FENCE_RUN.findall(text)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}{language}\n{text}\n{fence}"

    def listing(self, node: Node, ordered: bool) -> str:
        try:
            number = int(node.get("start") or 1)
        except ValueError:
            number = 1
        tasks = "inline-task-list" in node.classes
        items: list[str] = []
        for child in node.children:
            if isinstance(child, str):
                if not child.strip():
                    continue
                child = Node("li", {}, [child])
            elif child.tag in DROPPED:
                continue
            marker = f"{number}. " if ordered else "- "
            number += 1
            children: list[Node | str] = child.children if child.tag == "li" else [child]
            box = ""
            if tasks:
                box = "[x] " if "checked" in child.classes else "[ ] "
            first = next((c for c in children if not (isinstance(c, str) and not c.strip())), None)
            if isinstance(first, Node) and first.tag == "input" and first.get("type") == "checkbox":
                box = "[x] " if "checked" in first.attrs else "[ ] "
                children = [c for c in children if c is not first]
            body = self.container(children, tight=True) if child.tag == "li" else self.block(child)
            lines = body.splitlines() or [""]
            indent = " " * len(marker)
            text = marker + box + lines[0]
            for line in lines[1:]:
                text += "\n" + (indent + line if line.strip() else "")
            items.append(text.rstrip())
        return "\n".join(items)

    def definitions(self, node: Node) -> str:
        parts: list[str] = []
        for child in node.children:
            if isinstance(child, Node) and child.tag == "dt":
                term = self.title(child)
                if term:
                    parts.append(f"**{term}**")
            elif isinstance(child, Node) and child.tag == "dd":
                parts.append(self.container(child.children))
        return "\n\n".join(part for part in parts if part.strip())

    def table(self, table: Node) -> str:
        rows: list[list[str]] = []
        header_row = False

        def rows_of(node: Node) -> Iterator[Node]:
            for child in node.children:
                if not isinstance(child, Node):
                    continue
                if child.tag == "tr":
                    yield child
                elif child.tag in ("thead", "tbody", "tfoot"):
                    yield from rows_of(child)

        padding = MAX_PADDING
        self.in_table += 1
        try:
            for index, row in enumerate(rows_of(table)):
                cells: list[str] = []
                for cell in row.children:
                    if not isinstance(cell, Node) or cell.tag not in ("td", "th"):
                        continue
                    if index == 0 and cell.tag == "th":
                        header_row = True
                    text = " ".join(self.container(cell.children).split())
                    cells.append(text.replace("|", "\\|"))
                    try:
                        span = min(int(cell.get("colspan") or 1), 50)
                    except ValueError:
                        span = 1
                    span = min(span, padding + 1)
                    padding -= span - 1
                    cells.extend([""] * (span - 1))
                if cells:
                    rows.append(cells)
        finally:
            self.in_table -= 1
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        if sum(width - len(row) for row in rows) <= padding:
            rows = [row + [""] * (width - len(row)) for row in rows]
        if not header_row:
            rows.insert(0, [""] * width)
        rows[0] += [""] * (width - len(rows[0]))  # the header gives the number of columns
        lines = [_row(rows[0]), "|" + " --- |" * width, *(_row(row) for row in rows[1:])]
        return "\n".join(lines)

    # Inline

    def inline(self, nodes: list[Node | str]) -> str:
        return "".join(self.inline_one(node) for node in nodes)

    def inline_one(self, node: Node | str) -> str:
        if isinstance(node, str):
            return escape(_SPACES.sub(" ", node))
        tag = node.tag
        if tag in DROPPED:
            return ""
        if tag == "br":
            return " " if self.in_table else _BREAK
        if tag == "img":
            return self.picture(node)
        if tag == "a":
            return self.anchor(node)
        if tag in ("code", "kbd", "samp", "tt"):
            return _code_span(node.text() if node.children else "")
        if tag in ("strong", "b"):
            return self.wrap("**", node)
        if tag in ("em", "i", "cite", "dfn", "var"):
            return self.wrap("*", node)
        if tag in ("s", "del", "strike"):
            return self.wrap("~~", node)
        if tag == "mark":
            return self.wrap("==", node)
        if tag == "input":
            if node.get("type") == "checkbox":
                return "☑ " if "checked" in node.attrs else "☐ "
            return ""
        if tag == "span" and node.classes & {"status-macro", "aui-lozenge"}:
            return self.wrap("**", node)
        if tag not in INLINE:  # a block inside a link or a span: its text
            text = self.inline(node.children)
            return f" {text} " if text.strip() else ""
        return self.inline(node.children)

    def wrap(self, mark: str, node: Node) -> str:
        inner = self.inline(node.children)
        stripped = inner.strip()
        if not stripped or stripped == "\\":
            return inner
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()) :]
        if stripped.startswith(mark) and stripped.endswith(mark):
            return inner  # already formatted that way (<b><strong>x</strong></b>)
        return f"{lead}{mark}{stripped}{mark}{trail}"

    def anchor(self, node: Node) -> str:
        text = self.inline(node.children).strip()
        if node.classes & {"confluence-userlink", "user-mention"}:
            return text
        href = node.get("href").strip()
        if not href or not safe_url(href):
            return text
        target = self.link(href)
        if not target:
            return text
        if not text:
            text = escape(target)
        return f"[{text}]({destination(target)})"

    def picture(self, node: Node) -> str:
        if "emoticon" in node.classes:
            fallback = node.get("data-emoji-fallback")
            if fallback and not fallback.startswith(":"):
                return fallback
            return escape(node.get("alt"))
        src = node.get("src").strip()
        alt = " ".join(node.get("alt").split())
        if not src or not safe_url(src) or src.lower().startswith("data:"):
            return ""
        target = self.image(src)
        if not target:
            return ""
        return f"![{escape(alt)}]({destination(target)})"


def to_markdown(
    html: str | Node,
    link: Callable[[str], str | None] = lambda href: href,
    image: Callable[[str], str | None] = lambda src: src,
    shift: int = 0,
) -> str:
    node = parse(html) if isinstance(html, str) else html
    return Writer(link, image, shift).markdown(node)
