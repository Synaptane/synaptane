"""Notion export ("Markdown & CSV"), laid out as a Synaptane space.

Notion names every page and folder after its title followed by its id (`Notes 0123…abcd.md`,
folder `Notes 0123…abcd/` for its subpages and files), and links to them with these names,
URL-encoded. Here:

- names lose their id (a second "Notes" in the same folder becomes "Notes (2)"); a page with
  subpages or files becomes the home page of its folder (`Notes/README.md`);
- links to a page, a file or a database of the export (relative, or `https://www.notion.so/…`
  with the id of a page of the export) are rewritten to the new paths; the others stay;
- a database (`Tasks 0123….csv`, with `_all.csv` when Notion gives it) becomes an index page
  with a table of its rows, each linked to its page, and each row's properties go to the front
  matter of its page (Notion writes them under the title: they are moved);
- callouts (`<aside>`) and toggles (`<details>`) become callouts, `<br>` a line break.
"""

import csv
import io
import json
import posixpath
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import unquote, urlsplit

from synaptane.core.html_markdown import escape
from synaptane.core.markdown import link_refs
from synaptane.services.freshness import FIELDS
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

# `Title 0123…abcd.md`, `Title 0123…abcd.csv`, `Title 0123…abcd_all.csv`, folder `Title 0123…abcd`
NAMED = re.compile(
    r"^(?:(?P<title>.*?)\s+)?(?P<id>[0-9a-f]{32})(?P<all>_all)?(?P<ext>\.md|\.csv)?$"
)
_UUID = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})(?![0-9a-f])"
)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_CODE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)")
_BR = re.compile(r"<br\s*/?>", re.I)
_DROPPED_TAGS = re.compile(r"</?(?:span|u|font|sup|sub|div|p)\b[^>]*>", re.I)
_MARK = re.compile(r"<mark\b[^>]*>(.*?)</mark>", re.I)
_TAG = re.compile(r"<[^>]+>")
_PROPERTY = re.compile(r"^([^:\n]{1,200}?):[ \t]*(.*)$")
# A relation in a CSV cell: `Title (Title%200123…abcd.md)`, several separated by commas.
_RELATION = re.compile(r"\s*([^,()]+?) \(([^()]*?([0-9a-f]{32})\.md)\)")
MAX_ROWS = 20_000  # rows of a database shown in its index page


def is_notion(names: list[str]) -> bool:
    """At least one page or table named the Notion way."""
    for name in names:
        match = NAMED.match(posixpath.basename(name))
        if match and match["ext"]:
            return True
    return False


def notion_id(url: str) -> str | None:
    """Id of the page an address of notion.so (or a site published with Notion) leads to."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (
        host in ("notion.so", "notion.site") or host.endswith((".notion.so", ".notion.site"))
    ):
        return None
    peek = re.search(r"(?:^|&)p=([0-9a-f-]{32,36})", parts.query)
    found = _UUID.findall(peek.group(1) if peek else parts.path)
    return found[-1].replace("-", "") if found else None


@dataclass
class _Unit:
    """A page, its folder and its database, which share an id."""

    id: str
    title: str = ""
    page: str | None = None  # entries, relative to the export
    table: str | None = None
    duplicates: list[str] = field(default_factory=list)  # the other CSV of the same table
    folder: str | None = None


@dataclass
class _Table:
    title: str
    source: str  # CSV in the export
    path: str  # index page
    header: list[str]
    rows: list[list[str]]
    pages: list[str | None] = field(default_factory=list)  # entry of each row's page


class Notion:
    def __init__(self, entries: list[tuple[zipfile.ZipInfo, str]], read: Read) -> None:
        """`entries`: each entry of the archive with its path in the export."""
        self.read = read
        self.entry = {path: info for info, path in entries}
        self.paths: dict[str, str] = {}  # export path → new path (files)
        self.folders: dict[str, str] = {}
        self.by_id: dict[str, str] = {}  # page id → new path
        self.tables: dict[str, _Table] = {}  # index page → table
        self.table_sources: dict[str, str] = {}  # CSV of the export → index page
        self.properties: dict[str, list[tuple[str, str]]] = {}  # export path of a row page
        self.links = Links()
        self.skipped: list[str] = []
        self.files: dict[str, set[str]] = {}  # export folder → its files

    # Layout

    def layout(self) -> Layout:
        files = self.files
        folders: dict[str, set[str]] = {"": set()}
        for path in self.entry:
            parent = posixpath.dirname(path)
            files.setdefault(parent, set()).add(path)
            while parent:
                above = posixpath.dirname(parent)
                folders.setdefault(parent, set())
                folders.setdefault(above, set()).add(parent)
                parent = above
        stack: list[tuple[str, str, Names]] = [("", "", Names())]
        while stack:
            source, target, names = stack.pop()
            stack += self._place(source, target, names, files.get(source, set()), folders[source])
        items: list[Item] = []
        for path, new in self.paths.items():
            info = self.entry[path]
            convert = _PageConverter(self, path, new) if path.lower().endswith(".md") else None
            items.append(Item(info.filename, new, info, convert))
        for new, table in self.tables.items():
            source = self.entry[table.source].filename
            items.append(Item(source, new, made=partial(self._index, table)))
        return Layout(items, sorted(self.entry[path].filename for path in self.skipped), self.links)

    def _place(
        self, source: str, target: str, names: Names, files: set[str], folders: set[str]
    ) -> Iterator[tuple[str, str, Names]]:
        """Names in the folder `source` of the export, written as `target`. Yields the
        subfolders to lay out."""
        units: dict[str, _Unit] = {}
        plain_files, plain_folders = [], []
        for path in sorted(files):
            match = NAMED.match(posixpath.basename(path))
            if match is None or not match["ext"]:
                plain_files.append(path)
                continue
            unit = units.setdefault(match["id"], _Unit(match["id"]))
            unit.title = unit.title or (match["title"] or "")
            if match["ext"] == ".md" and unit.page is None:
                unit.page = path
            elif match["ext"] == ".md":
                plain_files.append(path)
            elif unit.table is None or (match["all"] and not unit.table.endswith("_all.csv")):
                if unit.table:
                    unit.duplicates.append(unit.table)
                unit.table = path
            else:
                unit.duplicates.append(path)
        for path in sorted(folders):
            match = NAMED.match(posixpath.basename(path))
            if match is None or match["ext"] or match["all"]:
                plain_folders.append(path)
                continue
            unit = units.setdefault(match["id"], _Unit(match["id"]))
            unit.title = unit.title or (match["title"] or "")
            if unit.folder is None:
                unit.folder = path
            else:
                plain_folders.append(path)
        for unit in sorted(units.values(), key=lambda u: (u.title.lower(), u.id)):
            yield from self._unit(unit, target, names)
        for path in plain_folders:
            new = posixpath.join(target, names.take(clean_name(posixpath.basename(path))))
            self.folders[path] = new
            yield path, new, Names()
        for path in plain_files:
            stem, ext = split_name(posixpath.basename(path))
            self.paths[path] = posixpath.join(target, names.take(clean_name(stem), ext))

    def _unit(self, unit: _Unit, target: str, names: Names) -> Iterator[tuple[str, str, Names]]:
        base = clean_name(unit.title)
        self.skipped += unit.duplicates
        page = index = None
        if unit.folder is not None:
            folder = posixpath.join(target, names.take(base))
            self.folders[unit.folder] = folder
            inside = Names()
            home = posixpath.join(folder, inside.take("README", ".md"))
            if unit.page:
                page = home
                if unit.table:
                    index = posixpath.join(folder, inside.take(base, ".md"))
            elif unit.table:
                index = home
            yield unit.folder, folder, inside
        else:
            if unit.page:
                page = posixpath.join(target, names.take(base, ".md"))
            if unit.table:
                index = posixpath.join(target, names.take(base, ".md"))
        if unit.page and page:
            self.paths[unit.page] = page
            self.by_id[unit.id] = page
        if unit.table and index:
            self.by_id.setdefault(unit.id, index)
            self.table_sources[unit.table] = index
            for duplicate in unit.duplicates:
                self.table_sources[duplicate] = index
            self._read_table(unit, index)

    def _read_table(self, unit: _Unit, index: str) -> None:
        assert unit.table is not None
        data = self.read(self.entry[unit.table])
        text = data.decode("utf-8", "replace").lstrip("﻿")
        try:
            rows = [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
        except csv.Error:
            rows = []
        header = [" ".join(cell.split()) for cell in rows[0]] if rows else []
        table = _Table(
            clean_name(unit.title, "Database"), unit.table, index, header, rows[1 : MAX_ROWS + 1]
        )
        self.tables[index] = table
        if unit.folder is None or not header:
            table.pages = [None] * len(table.rows)
            return
        pages = [
            path
            for path in self.files.get(unit.folder, set())
            if (match := NAMED.match(posixpath.basename(path))) is not None
            and match["ext"] == ".md"
        ]
        table.pages = _match_rows([row[0] if row else "" for row in table.rows], pages)
        for row, page in zip(table.rows, table.pages, strict=True):
            if page is None:
                continue
            fields = []
            for key, value in zip(header[1:], row[1:], strict=False):
                value = _without_relations(value)
                if key and value:
                    fields.append((key, value))
            self.properties[page] = fields

    # Pages

    def target(self, href: str, path: str, new: str) -> str | None:
        """New address of a link of the page `path` (now `new`); None: unchanged."""
        if not href or href.startswith("#"):
            return None
        page_id = notion_id(href)
        if page_id is not None:
            found = self.by_id.get(page_id)
            if found is None:  # a page outside the export: the link still leads to Notion
                return None
            self.links.rewritten += 1
            return relative(new, found)
        try:
            parts = urlsplit(href)
        except ValueError:
            return None
        if parts.scheme or parts.netloc or not parts.path:
            return None
        written = unquote(parts.path)
        full = posixpath.normpath(posixpath.join(posixpath.dirname(path), written))
        found = (
            self.paths.get(full)
            or self.table_sources.get(full)
            or self.folders.get(full.rstrip("/"))
        )
        if found is None:
            match = NAMED.match(posixpath.basename(full))
            found = self.by_id.get(match["id"]) if match else None
        if found is None:
            self.links.broke(new, href)
            return None
        self.links.rewritten += 1
        return relative(new, found)

    def _index(self, table: _Table) -> bytes:
        lines = [f"# {escape(table.title)}", ""]
        if table.header:
            lines.append("| " + " | ".join(_cell(h) or " " for h in table.header) + " |")
            lines.append("|" + " --- |" * len(table.header))
            for row, page in zip(table.rows, table.pages, strict=True):
                cells = [_cell(value) for value in row[: len(table.header)]]
                cells += [""] * (len(table.header) - len(cells))
                if page is not None and page in self.paths:
                    title = cells[0] or "Untitled"
                    link = _destination(relative(table.path, self.paths[page]))
                    cells[0] = f"[{title}]({link})"
                for i, value in enumerate(row[1 : len(table.header)], start=1):
                    cells[i] = self._relations(value, table.path) or cells[i]
                lines.append("| " + " | ".join(cells) + " |")
        return ("\n".join(lines).rstrip("\n") + "\n").encode()

    def _relations(self, value: str, page: str) -> str:
        """A relation cell as links to the pages of the export, "" if it is not one."""
        found = list(_RELATION.finditer(value))
        if not found or _RELATION.sub("", value).strip(" ,"):
            return ""
        links = []
        for match in found:
            title, target = _cell(match[1]), self.by_id.get(match[3])
            links.append(f"[{title}]({_destination(relative(page, target))})" if target else title)
        return ", ".join(links)


class _PageConverter:
    def __init__(self, notion: Notion, path: str, new: str) -> None:
        self.notion, self.path, self.new = notion, path, new

    def __call__(self, data: bytes) -> bytes:
        text = data.decode("utf-8", "replace").lstrip("﻿").replace("\r\n", "\n")
        fields = self.notion.properties.get(self.path)
        if fields:
            text = _without_properties(text, {key for key, _ in fields})
        text = _cleaned(text)
        text = self._links(text)
        if fields:
            text = _front_matter(fields) + text
        return text.encode()

    def _links(self, text: str) -> str:
        out: list[str] = []
        position = 0
        for ref in link_refs(text, wiki=False):
            new = self.notion.target(ref.target, self.path, self.new)
            if new is None:
                continue
            angled = ref.start > 0 and text[ref.start - 1] == "<"
            out += [text[position : ref.start], new if angled else _destination(new)]
            position = ref.end
        out.append(text[position:])
        return "".join(out)


def _destination(path: str) -> str:
    if re.search(r"[<>\n]", path):
        path = path.replace("<", "%3C").replace(">", "%3E").replace("\n", "")
    return f"<{path}>" if re.search(r"[\s()]", path) else path


def _cell(value: str) -> str:
    return escape(" ".join(value.split())).replace("|", "\\|")


def _without_relations(value: str) -> str:
    """`Title (Title%200123….md), Other (…)` → `Title, Other`."""
    if not _RELATION.search(value):
        return value.strip()
    return ", ".join(match[1].strip() for match in _RELATION.finditer(value))


def _match_rows(titles: list[str], pages: list[str]) -> list[str | None]:
    """Page of each row of a database. The CSV does not give it: rows are matched with the
    pages of the table's folder by title, exact first, then with case and punctuation
    ignored, then by its start (Notion shortens long file names)."""

    def key(text: str) -> str:
        return "".join(char for char in text.lower() if char.isalnum())

    stems = {}
    for page in sorted(pages):
        match = NAMED.match(posixpath.basename(page))
        stems[page] = (match["title"] or "") if match else ""
    exact: dict[str, list[str]] = {}
    loose: dict[str, list[str]] = {}
    for page, stem in stems.items():
        exact.setdefault(clean_name(stem), []).append(page)
        loose.setdefault(key(stem), []).append(page)
    used: set[str] = set()

    def first(candidates: list[str]) -> str | None:
        return next((page for page in candidates if page not in used), None)

    found: list[str | None] = []
    shortened: list[int] = []  # rows left for the slower match
    for title in titles:
        chosen = first(exact.get(clean_name(title), [])) or first(loose.get(key(title), []))
        if chosen is not None:
            used.add(chosen)
        elif len(key(title)) >= 10:
            shortened.append(len(found))
        found.append(chosen)
    for row in shortened:
        wanted = key(titles[row])
        chosen = next(
            (
                page
                for page, stem in stems.items()
                if page not in used and len(key(stem)) >= 10 and wanted.startswith(key(stem))
            ),
            None,
        )
        if chosen is not None:
            used.add(chosen)
            found[row] = chosen
    return found


def _without_properties(text: str, keys: set[str]) -> str:
    """The `Property: value` lines Notion writes under the title of a row: they go to the front
    matter."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines[:5]) if line.startswith("# ")), None)
    if start is None:
        return text
    end = start + 1
    while end < len(lines) and not lines[end].strip():
        end += 1
    first = end
    while end < len(lines):
        match = _PROPERTY.match(lines[end])
        if match is None or " ".join(match[1].split()) not in keys:
            break
        end += 1
    if end == first:
        return text
    return "\n".join([*lines[: start + 1], "", *lines[end:]]).replace("\n\n\n", "\n\n")


def _front_matter(fields: list[tuple[str, str]]) -> str:
    lines, seen = ["---"], set()
    for key, value in fields:
        if key in FIELDS:  # verification fields of Synaptane: not to be taken for them
            key += " (Notion)"
        if key in seen:
            continue
        seen.add(key)
        plain = re.fullmatch(r"[^\W\d][\w -]*", key) and not key.endswith(" ")
        written = key if plain else json.dumps(key, ensure_ascii=False)
        lines.append(f"{written}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join([*lines, "---", ""])


def _inline_html(line: str, table: bool) -> list[str]:
    """Line breaks and formatting tags of a line, outside code spans."""
    pieces: list[tuple[str, bool]] = []
    position = 0
    for match in _CODE.finditer(line) if "`" in line else ():
        pieces += [(line[position : match.start()], False), (match.group(), True)]
        position = match.end()
    pieces.append((line[position:], False))
    text = ""
    for piece, code in pieces:
        if not code:
            piece = _MARK.sub(r"==\1==", piece)
            piece = _DROPPED_TAGS.sub("", piece)
            piece = _BR.sub(" " if table else "\\\n", piece)
        text += piece
    return text.split("\n")


def _cleaned(text: str) -> str:
    """Notion's HTML in Markdown: `<aside>` (callout) and `<details>` (toggle) become callouts,
    `<br>` a line break; code blocks stay as they are."""
    if "<" not in text:
        return text
    out: list[str] = []
    stack: list[str] = []
    fence: str | None = None

    def prefix() -> str:
        return "> " * len(stack)

    def add(line: str) -> None:
        out.append((prefix() + line) if line.strip() else prefix().rstrip())

    for line in text.split("\n"):
        stripped = line.strip()
        if fence is not None:
            add(line)
            if stripped.startswith(fence):
                fence = None
            continue
        opening = _FENCE.match(line)
        if opening:
            fence = opening.group(1)
            add(line)
            continue
        if re.fullmatch(r"<aside\b[^>]*>", stripped, re.I):
            stack.append("aside")
            add("[!note]")
            continue
        if re.fullmatch(r"<details\b[^>]*>", stripped, re.I):
            stack.append("details")
            add("[!note]-")
            continue
        summary = re.fullmatch(r"<summary\b[^>]*>(.*?)</summary>", stripped, re.I | re.S)
        if summary and stack and stack[-1] == "details":
            title = " ".join(_TAG.sub("", summary.group(1)).split())
            if out and out[-1].endswith("[!note]-"):
                out[-1] += f" {title}" if title else ""
            continue
        closing = re.fullmatch(r"</(aside|details)>", stripped, re.I)
        if closing and stack and stack[-1] == closing.group(1).lower():
            stack.pop()
            add("")
            continue
        for index, piece in enumerate(_inline_html(line, stripped.startswith("|"))):
            add(piece.lstrip() if index else piece)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))
