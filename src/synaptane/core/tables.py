"""Markdown tables, and where two of them disagree.

Two tables are **aligned** when they describe the same things: their first columns have the
same heading, they share at least `SHARED_KEYS` row keys (the first cell) and another column.
A **drift** is a row of the same key whose value differs, in two pages, in a column of the
same heading: the NAS has 8 TB in one page and 12 TB in the other. One of them is probably
outdated, unless both are right (a history, a dated note): a person or the consistency check
weighs it. Deterministic, without AI.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from synaptane.core.names import ascii_text

SHARED_KEYS = 2  # row keys two tables must share to be aligned
MAX_TABLES = 200  # tables of the same first heading compared at most, pair by pair
# Cells that say nothing: no drift with them.
EMPTY = frozenset({"", "-", "--", "—", "–", "?", "n/a", "na", "nc", "tbd", "...", "x"})

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DELIMITER = re.compile(r"^ *\|? *:?-+:? *(\| *:?-+:? *)*\|? *$")
_PIPE = re.compile(r"(?<!\\)\|")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_WIKI = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
_MARKS = re.compile(r"[*_`~]|<[^>]+>")
_NUMBER = re.compile(r"^[#n°no.]*\s*\d+[.)]?$")
_DATE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})(?!\d)")


@dataclass(frozen=True)
class Table:
    line: int  # of its header (from 1)
    headings: tuple[str, ...]  # as written
    rows: tuple[tuple[int, tuple[str, ...]], ...]  # (line, cells as written)


@dataclass(frozen=True)
class Cell:
    page: str
    line: int
    value: str  # as written


@dataclass(frozen=True)
class Drift:
    key: str  # the row's first cell, as written in the first page
    column: str  # heading, as written in the first page
    first: Cell
    second: Cell


def cells(line: str) -> tuple[str, ...]:
    """The cells of a table line, outer pipes removed (an escaped one stays in its cell)."""
    text = line.strip()
    text = text[1:] if text.startswith("|") else text
    text = text[:-1] if text.endswith("|") and not text.endswith("\\|") else text
    return tuple(c.strip().replace("\\|", "|") for c in _PIPE.split(text))


def tables_of(text: str) -> tuple[Table, ...]:
    """The tables of a Markdown page (GFM), outside code blocks."""
    lines = text.splitlines()
    found: list[Table] = []
    fence = ""
    number = 0
    while number < len(lines):
        line = lines[number]
        if match := _FENCE.match(line):
            mark = match.group(1)[0]
            fence = mark if not fence else "" if fence == mark else fence
            number += 1
            continue
        following = lines[number + 1] if number + 1 < len(lines) else ""
        if fence or "|" not in line or not _DELIMITER.match(following):
            number += 1
            continue
        headings = cells(line)
        if len(cells(following)) != len(headings):
            number += 1
            continue
        rows = []
        number += 2
        while number < len(lines) and "|" in lines[number] and lines[number].strip():
            rows.append((number + 1, cells(lines[number])))
            number += 1
        found.append(Table(number - len(rows) - 1, headings, tuple(rows)))
    return tuple(found)


def plain(cell: str) -> str:
    """A cell as compared: link text only, without emphasis, accents, case or extra spaces."""
    text = _WIKI.sub(r"\1", _LINK.sub(r"\1", cell))
    return " ".join(ascii_text(_MARKS.sub("", text)).split())


def _rows(table: Table) -> dict[str, tuple[int, tuple[str, ...]]]:
    """Rows by key; a key repeated in the table is ambiguous, left out, as are row numbers."""
    rows: dict[str, tuple[int, tuple[str, ...]]] = {}
    repeated = set()
    for line, row in table.rows:
        key = plain(row[0]) if row else ""
        if key in EMPTY or _NUMBER.match(key):
            continue
        if key in rows:
            repeated.add(key)
        rows[key] = (line, row)
    return {k: v for k, v in rows.items() if k not in repeated}


def drifts(tables: Mapping[str, Sequence[Table]], limit: int = 500) -> list[Drift]:
    """The drifts between aligned tables of different pages (`tables`: by page). Two tables
    that disagree more than they agree describe different things (two companies' sheets, the
    same idea worded twice): no drift between them. Nor between two dated notes (a daily
    watch, minutes): each holds on its day."""
    groups: dict[str, list[tuple[str, Table]]] = {}
    for page, found in sorted(tables.items()):
        for table in found:
            first = plain(table.headings[0])
            if first and len(table.headings) > 1 and len(table.rows) >= SHARED_KEYS:
                groups.setdefault(first, []).append((page, table))
    result: list[Drift] = []
    for group in groups.values():
        for (page_a, a), (page_b, b) in combinations(group[:MAX_TABLES], 2):
            if page_a == page_b or (dated(page_a) and dated(page_b)):
                continue
            pair = _compare(page_a, a, page_b, b)
            result.extend(pair[: limit - len(result)])
            if len(result) >= limit:
                return result
    return result


def dated(page: str) -> bool:
    """A page whose file name carries a date: a note of that day."""
    return bool(_DATE.search(page.rsplit("/", 1)[-1]))


def _compare(page_a: str, a: Table, page_b: str, b: Table) -> list[Drift]:
    columns = [
        (i, j)
        for i, heading in enumerate(a.headings[1:], 1)
        for j, other in enumerate(b.headings[1:], 1)
        if plain(heading) and plain(heading) == plain(other)
    ]
    rows_a, rows_b = _rows(a), _rows(b)
    shared = [k for k in rows_a if k in rows_b]
    if not columns or len(shared) < SHARED_KEYS:
        return []
    agree = 0
    found: list[Drift] = []
    for key in shared:
        (line_a, row_a), (line_b, row_b) = rows_a[key], rows_b[key]
        for i, j in columns:
            value_a = plain(row_a[i]) if i < len(row_a) else ""
            value_b = plain(row_b[j]) if j < len(row_b) else ""
            if value_a in EMPTY or value_b in EMPTY:
                continue
            if value_a == value_b:
                agree += 1
            else:
                found.append(
                    Drift(
                        row_a[0],
                        a.headings[i],
                        Cell(page_a, line_a, row_a[i]),
                        Cell(page_b, line_b, row_b[j]),
                    )
                )
    return found if agree >= max(len(found), 1) else []
