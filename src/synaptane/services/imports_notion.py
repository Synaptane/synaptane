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

from synaptane.core.errors import Label
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

# `Title 0123…abcd.md`, `Title 0123…abcd.csv`, `Title 0123…abcd_all.csv`, folder `Title 0123…abcd`:
# the id, after a space or alone, at the end of the name (the title is what comes before).
_NAMED = re.compile(r"(?:^|(?<=\s))(?P<id>[0-9a-f]{32})(?P<all>_all)?(?P<ext>\.md|\.csv)?\Z")
_UUID = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})(?![0-9a-f])"
)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_CODE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)")
_BR = re.compile(r"<br\s*/?>", re.I)
# `[^<>]`, and no `<mark` inside a highlight: each tag is read once, never to the end of the line
# again (a line of `<mark` or `<span` would otherwise take a time quadratic in its length).
_DROPPED_TAGS = re.compile(r"</?(?:span|u|font|sup|sub|div|p)\b[^<>]*>", re.I)
_MARK = re.compile(r"<mark\b[^<>]*>((?:(?!</?mark\b).)*?)</mark>", re.I)
_TAG = re.compile(r"<[^<>]+>")
_PROPERTY = re.compile(r"^([^:\n]{1,200}?):[ \t]*(.*)$")
# A relation in a CSV cell: `Title (Title%200123…abcd.md)`, several separated by commas.
# Each one starts the cell or follows a comma: tried from every character, the search would
# take a time quadratic in the length of the cell.
_RELATION = re.compile(r"(?:^|,)\s*([^,()]+?) \(([^()]*?([0-9a-f]{32})\.md)\)")
MAX_ROWS = 20_000  # rows of a database shown in its index page
MAX_CELL = 128 * 1024  # characters of a CSV cell, as the csv module's limit
MAX_SHORTENED = 200  # characters of a shortened file name matched with the start of a title


def named(name: str) -> dict[str, str] | None:
    """Title, id, `all` (`_all` or "") and `ext` (`.md`, `.csv` or "") of a name given by
    Notion; None if it is not one."""
    match = _NAMED.search(name)
    if match is None:
        return None
    return {
        "title": name[: match.start()].rstrip(),
        "id": match["id"],
        "all": match["all"] or "",
        "ext": match["ext"] or "",
    }


def is_notion(names: list[str]) -> bool:
    """At least one page or table named the Notion way."""
    for name in names:
        match = named(posixpath.basename(name))
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
    def __init__(
        self, entries: list[tuple[zipfile.ZipInfo, str]], read: Read, head: Read | None = None
    ) -> None:
        """`entries`: each entry of the archive with its path in the export; `head` reads the
        start of an entry (to find a page's title), `read` all of it."""
        self.read = read
        self.head = head or read
        self.entry = {path: info for info, path in entries}
        self.paths: dict[str, str] = {}  # export path → new path (files)
        self.folders: dict[str, str] = {}
        self.by_id: dict[str, str] = {}  # page id → new path
        self.tables: dict[str, _Table] = {}  # index page → table
        self.table_sources: dict[str, str] = {}  # CSV of the export → index page
        self.properties: dict[str, list[tuple[str, str]]] = {}  # export path of a row page
        self.columns: dict[str, set[str]] = {}  # property names of a row page
        self.titles: dict[str, str] = {}  # export path of a page → its title
        self.reasons: dict[str, Label] = {}
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
                if above == parent:  # `/`: never given by the check, which refuses such paths
                    break
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
        skipped = sorted(self.entry[path].filename for path in self.skipped)
        return Layout(items, skipped, self.links, self.reasons)

    def _place(
        self, source: str, target: str, names: Names, files: set[str], folders: set[str]
    ) -> Iterator[tuple[str, str, Names]]:
        """Names in the folder `source` of the export, written as `target`. Yields the
        subfolders to lay out."""
        units: dict[str, _Unit] = {}
        plain_files, plain_folders = [], []
        for path in sorted(files):
            match = named(posixpath.basename(path))
            if match is None or not match["ext"]:
                plain_files.append(path)
                continue
            unit = units.setdefault(match["id"], _Unit(match["id"]))
            unit.title = unit.title or (match["title"] or "")
            if match["ext"] == ".md" and unit.page is None:
                unit.page = path
                unit.title = self._title(path, match["title"])
            elif match["ext"] == ".md":
                plain_files.append(path)
            elif unit.table is None or (match["all"] and not unit.table.endswith("_all.csv")):
                if unit.table:
                    unit.duplicates.append(unit.table)
                unit.table = path
            else:
                unit.duplicates.append(path)
        for path in sorted(folders):
            match = named(posixpath.basename(path))
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
        rows, cut = _csv_rows(text)
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if cut:  # imported all the same, the long cells shortened: said at the check
            self.skipped.append(unit.table)
            self.reasons[self.entry[unit.table].filename] = Label(
                "cells longer than {size} KiB shortened in the index page and the properties",
                size=MAX_CELL // 1024,
            )
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
            if (match := named(posixpath.basename(path))) is not None and match["ext"] == ".md"
        ]
        stems = {
            page: self._title(page, (named(posixpath.basename(page)) or {})["title"])
            for page in pages
        }
        table.pages = _match_rows([row[0] if row else "" for row in table.rows], stems)
        for row, page in zip(table.rows, table.pages, strict=True):
            if page is None:
                continue
            self.columns[page] = set(header[1:])
            fields = []
            for key, value in zip(header[1:], row[1:], strict=False):
                value = _without_relations(value)
                if key and value:
                    fields.append((key, value))
            self.properties[page] = fields

    def _title(self, path: str, written: str) -> str:
        """Title of a page: its name in the export, or its `# Title` when the name is that title
        shortened by Notion."""
        if path in self.titles:
            return self.titles[path]
        title = written
        start = self.head(self.entry[path]).decode("utf-8", "ignore").lstrip("﻿")
        first = start.split("\n", 1)[0].strip() if "\n" in start else ""
        if first.startswith("# "):
            full = " ".join(first[2:].split())
            if len(full) > len(written) and _key(full).startswith(_key(written)) and _key(written):
                title = full
        self.titles[path] = title
        return title

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
            match = named(posixpath.basename(full))
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
        columns = self.notion.columns.get(self.path)
        if columns:
            text = _without_properties(text, columns)
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


def _match_rows(titles: list[str], pages: dict[str, str]) -> list[str | None]:
    """Page of each row of a database. The CSV does not give it: rows are matched with the
    pages of the table's folder by title, exact first, then with case and punctuation
    ignored, then by its start (Notion shortens long file names)."""

    key = _key
    stems = {page: pages[page] for page in sorted(pages)}
    exact: dict[str, list[str]] = {}
    loose: dict[str, list[str]] = {}
    starts: dict[str, list[str]] = {}  # shortened names, by their key
    for page, stem in stems.items():
        exact.setdefault(clean_name(stem), []).append(page)
        loose.setdefault(key(stem), []).append(page)
        if 10 <= len(key(stem)) <= MAX_SHORTENED:
            starts.setdefault(key(stem), []).append(page)
    for candidates in (*exact.values(), *loose.values(), *starts.values()):
        candidates.reverse()  # the first one at the end: taken with `pop`
    lengths = sorted({len(start) for start in starts}, reverse=True)
    used: set[str] = set()

    def first(candidates: list[str]) -> str | None:
        """The first page not taken yet (those taken are dropped: each is looked at once)."""
        while candidates and candidates[-1] in used:
            candidates.pop()
        return candidates[-1] if candidates else None

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
        for length in lengths:  # the longest start first
            chosen = first(starts.get(wanted[:length], [])) if length <= len(wanted) else None
            if chosen is not None:
                used.add(chosen)
                found[row] = chosen
                break
    return found


def _key(text: str) -> str:
    """A title with case and punctuation ignored."""
    return "".join(char for char in text.lower() if char.isalnum())


_CSV_FIELD = re.compile(r'"([^"]*(?:""[^"]*)*)"|([^,"\r\n]*)')
_CSV_REST = re.compile(r"[^,\r\n]*")


def _csv_rows(text: str) -> tuple[list[list[str]], bool]:
    """Rows of a CSV, and whether cells were shortened. The csv module refuses a cell longer
    than its limit (128 KiB), a limit shared by the whole process: such a file is read again
    here, its long cells shortened to MAX_CELL characters."""
    try:
        return list(csv.reader(io.StringIO(text))), False
    except csv.Error:
        return _csv_fallback(text)


def _csv_fallback(text: str) -> tuple[list[list[str]], bool]:
    """Like the csv module (its default dialect), long cells shortened."""
    rows: list[list[str]] = []
    row: list[str] = []
    position, line, cut = 0, 0, False
    while True:
        match = _CSV_FIELD.match(text, position)
        assert match is not None  # the second branch matches the empty string
        if match.group(1) is not None:
            value, position = match.group(1).replace('""', '"'), match.end()
        elif text.startswith('"', position):  # a quote never closed: the rest of the text
            value, position = text[position + 1 :], len(text)
        else:
            value, position = match.group(2), match.end()
        # Text after a closing quote, up to the next separator, belongs to the cell.
        extra = _CSV_REST.match(text, position)
        if extra is not None and extra.group():
            value += extra.group()
            position = extra.end()
        if len(value) > MAX_CELL:
            value, cut = value[:MAX_CELL] + "…", True
        row.append(value)
        if position < len(text) and text[position] == ",":
            position += 1
            continue
        rows.append([] if row == [""] and position == line else row)  # an empty line
        row = []
        position += 2 if text.startswith("\r\n", position) else 1
        line = position
        if position >= len(text):
            return rows, cut


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
