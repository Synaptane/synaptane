"""Confluence space export in HTML, laid out as a Synaptane space.

The export holds `index.html` (the page tree), one file per page (`Title_123456.html`), the
attachments (`attachments/<page id>/…`) and what the pages need to display (`images/`,
`styles/`). Here:

- the tree comes from `index.html`, and from each page's breadcrumb for a page it does not
  list: the pages under a page go in its folder, a page with subpages or attachments becomes
  the home page of its folder (`Title/README.md`), names lose the page id;
- each page becomes Markdown (`core.html_markdown`): headings, lists, tables, code, links,
  images; info, tip, note and warning panels become callouts, an expand a folded callout;
- links to a page or an attachment of the export are rewritten to the new paths, as are
  addresses of the Confluence site with the id of a page of the export (`/pages/123456`,
  `pageId=123456`); the others stay;
- attachments go next to their page, under their real name when the page gives it.

The HTML is not trusted: it is only parsed, nothing in it is run or loaded, and the paths
come from the layout made here, not from the archive.
"""

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from synaptane.core.html_markdown import Node, Writer, by_class, by_id, escape, parse
from synaptane.services.imports_common import (
    Item,
    Layout,
    Links,
    Names,
    Read,
    clean_name,
    relative,
    split_name,
)

PAGE = re.compile(r"(?:^|_)(\d+)\.html?$")
_SITE_PAGE = re.compile(r"/pages/(\d+)(?:/|$)")
_PAGE_ID = re.compile(r"(?:^|&)pageId=(\d+)")


def is_confluence(paths: list[str]) -> bool:
    """`index.html` at the top, with the pages, attachments or styles of an export."""
    if "index.html" not in paths:
        return False
    return any(PAGE.search(path) and "/" not in path for path in paths) or any(
        path.startswith(("attachments/", "styles/site.css")) for path in paths
    )


def is_confluence_xml(paths: list[str]) -> bool:
    return "entities.xml" in paths and any(
        path in ("exportDescriptor.properties", "attachments") or path.startswith("attachments/")
        for path in paths
    )


@dataclass
class _Page:
    path: str  # in the export
    id: str
    title: str
    parent: str | None = None  # in the export
    aliases: dict[str, str] = field(default_factory=dict)  # attachment → its real name
    order: int = 1 << 30


def _read_html(data: bytes) -> Node:
    return parse(data.decode("utf-8", "replace"))


class Confluence:
    def __init__(self, entries: list[tuple[zipfile.ZipInfo, str]], read: Read) -> None:
        """`entries`: each entry of the archive with its path in the export."""
        self.read = read
        self.entry = {path: info for info, path in entries}
        self.pages: dict[str, _Page] = {}
        self.by_id: dict[str, str] = {}  # page id → new path
        self.paths: dict[str, str] = {}  # export path → new path (pages and attachments)
        self.links = Links()

    def layout(self) -> Layout:
        skipped = []
        attachments = []
        for path in self.entry:
            if (
                "/" not in path
                and path.lower().endswith((".html", ".htm"))
                and path != "index.html"
            ):
                self.pages[path] = self._summary(path)
            elif path.startswith("attachments/") and not path.startswith("attachments/thumbnails/"):
                attachments.append(path)
            else:
                skipped.append(self.entry[path].filename)
        self._tree()
        children: dict[str | None, list[_Page]] = {}
        for page in self.pages.values():
            children.setdefault(page.parent, []).append(page)
        owned: dict[str, list[str]] = {}  # page → its attachments
        orphans: list[str] = []
        ids = {page.id: page.path for page in self.pages.values() if page.id}
        for path in attachments:
            parts = path.split("/")
            owner = ids.get(parts[1]) if len(parts) > 2 else None
            (owned.setdefault(owner, []) if owner else orphans).append(path)
        stack: list[tuple[str | None, str, Names]] = [(None, "", Names())]
        while stack:
            parent, folder, names = stack.pop()
            for page in sorted(children.get(parent, []), key=lambda p: (p.order, p.title.lower())):
                base = clean_name(page.title)
                if children.get(page.path) or owned.get(page.path):
                    inside = Names()
                    sub = posixpath.join(folder, names.take(base))
                    new = posixpath.join(sub, inside.take("README", ".md"))
                    for attachment in sorted(owned.get(page.path, [])):
                        stem, ext = split_name(page.aliases.get(attachment) or attachment)
                        name = posixpath.basename(stem)
                        if not ext:
                            ext = split_name(posixpath.basename(attachment))[1]
                        self.paths[attachment] = posixpath.join(
                            sub, inside.take(clean_name(name, "file"), ext)
                        )
                    stack.append((page.path, sub, inside))
                else:
                    new = posixpath.join(folder, names.take(base, ".md"))
                self.paths[page.path] = new
                if page.id:
                    self.by_id[page.id] = new
        for path in orphans:
            self.paths[path] = path
        items = [
            Item(
                self.entry[path].filename,
                new,
                self.entry[path],
                _PageConverter(self, path, new) if path in self.pages else None,
            )
            for path, new in self.paths.items()
        ]
        return Layout(items, sorted(skipped), self.links)

    def _summary(self, path: str) -> _Page:
        """Title, parent and attachment names of a page, from its HTML."""
        doc = _read_html(self.read(self.entry[path]))
        match = PAGE.search(path)
        page = _Page(path, match.group(1) if match else "", "")
        crumbs = doc.find(by_id("breadcrumbs"))
        space = ""
        if crumbs is not None:
            links = crumbs.find_all(lambda n: n.tag == "a")
            if links:
                space = links[0].text()
            for link in links:
                target = _local(link.get("href"))
                if target and target != "index.html" and target != path:
                    page.parent = target
        heading = doc.find(by_id("title-text")) or doc.find(lambda n: n.tag == "title")
        title = heading.text() if heading is not None else ""
        if space and title.startswith(f"{space} : "):
            title = title[len(space) + 3 :]
        page.title = title.strip() or posixpath.splitext(path)[0]
        # The real name of an attachment: given by Confluence, or the text of a link to it
        # that looks like its name (the list of attachments at the bottom of the page).
        for node in doc.walk():
            target = _local(node.get("href") or node.get("src"))
            if not target or not target.startswith("attachments/"):
                continue
            given = node.get("data-linked-resource-default-alias")
            text = node.text() if node.tag == "a" else ""
            ext = split_name(posixpath.basename(target))[1].lower()
            if given:
                page.aliases[target] = given
            elif text and ext and text.lower().endswith(ext) and target not in page.aliases:
                page.aliases[target] = text
        return page

    def _tree(self) -> None:
        """Parents and order from `index.html`, which lists the tree; the breadcrumb of each
        page otherwise. A parent outside the export, or a loop, puts the page at the top."""
        info = self.entry.get("index.html")
        if info is not None:
            doc = _read_html(self.read(info))
            order = 0
            for link in doc.find_all(lambda n: n.tag == "a"):
                target = _local(link.get("href"))
                page = self.pages.get(target or "")
                if page is None or page.order != 1 << 30:
                    continue
                page.order = order
                order += 1
                title = link.text()
                if title:
                    page.title = title
                item = _ancestor(link, "li")
                above = _ancestor(item, "li") if item is not None else None
                parent = _first_page_link(above) if above is not None else None
                if parent in self.pages:
                    page.parent = parent
        for page in self.pages.values():
            if page.parent not in self.pages:
                page.parent = None
        for page in self.pages.values():
            seen = {page.path}
            parent = page.parent
            while parent is not None:
                if parent in seen:
                    page.parent = None
                    break
                seen.add(parent)
                parent = self.pages[parent].parent

    def target(self, href: str, path: str, new: str, image: bool = False) -> str | None:
        """New address of a link (or image) of the page `path`, now `new`. None: dropped (an
        anchor of Confluence, an icon of the export); an unknown address stays as written."""
        if href.startswith("#"):
            return None
        try:
            parts = urlsplit(href)
        except ValueError:
            return None
        # An address of the Confluence site (`…/pages/123456/Title`, `viewpage.action?pageId=…`)
        found = _SITE_PAGE.search(parts.path) or _PAGE_ID.search(parts.query)
        if found and found.group(1) in self.by_id:
            self.links.rewritten += 1
            return relative(new, self.by_id[found.group(1)])
        if parts.scheme or parts.netloc:
            return href
        local = _local(href)
        if local is None:  # leaves the export: only the text is kept
            self.links.broke(new, href)
            return None
        if local.startswith("attachments/thumbnails/"):
            local = "attachments/" + local.removeprefix("attachments/thumbnails/")
        if local in self.paths:
            self.links.rewritten += 1
            return relative(new, self.paths[local])
        if local in ("index.html", "") or local.startswith(("images/", "styles/")):
            return None
        match = PAGE.search(local)
        if match and match.group(1) in self.by_id:
            self.links.rewritten += 1
            return relative(new, self.by_id[match.group(1)])
        self.links.broke(new, href)
        return href if not image else None


def _local(href: str) -> str | None:
    """Path in the export of a relative address, None if it is not one (or leaves it)."""
    try:
        parts = urlsplit(href.strip())
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        return None
    path = posixpath.normpath(unquote(parts.path)) if parts.path else ""
    if path.startswith(("..", "/")):
        return None
    return "" if path == "." else path


def _ancestor(node: Node | None, tag: str) -> Node | None:
    node = node.parent if node is not None else None
    while node is not None and node.tag != tag:
        node = node.parent
    return node


def _first_page_link(item: Node) -> str | None:
    for node in item.walk():
        if node.tag == "a":
            target = _local(node.get("href"))
            if target and target.lower().endswith((".html", ".htm")) and target != "index.html":
                return target
    return None


class _PageConverter:
    def __init__(self, confluence: Confluence, path: str, new: str) -> None:
        self.confluence, self.path, self.new = confluence, path, new

    def __call__(self, data: bytes) -> bytes:
        doc = _read_html(data)
        page = self.confluence.pages[self.path]
        main = (
            doc.find(by_id("main-content"))
            or doc.find(by_class("wiki-content"))
            or doc.find(lambda n: n.tag == "body")
            or doc
        )
        shift = 1 if main.find(lambda n: n.tag == "h1") is not None else 0
        writer = Writer(
            lambda href: self.confluence.target(href, self.path, self.new),
            lambda src: self.confluence.target(src, self.path, self.new, image=True),
            shift,
        )
        parts = [f"# {escape(page.title)}"]
        metadata = doc.find(by_class("page-metadata"))
        if metadata is not None and metadata.text():
            parts.append(f"*{escape(metadata.text())}*")
        parts.append(writer.markdown(main))
        for section in doc.find_all(by_class("pageSection")):
            above = _ancestor(section, "div")
            while above is not None and "pageSection" not in above.classes:
                above = _ancestor(above, "div")
            if above is None and not _inside(section, main) and not _inside(main, section):
                parts.append(writer.markdown(section))
        return ("\n\n".join(part.strip() for part in parts if part.strip()) + "\n").encode()


def _inside(node: Node, container: Node) -> bool:
    current: Node | None = node
    while current is not None:
        if current is container:
            return True
        current = current.parent
    return False
