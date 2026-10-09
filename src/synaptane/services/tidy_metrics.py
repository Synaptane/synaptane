"""Quality of a tidy-up run, measured without AI.

Computed on the working view (`Overlay`) just before the commit, recorded in the log
(`TidyRun.metrics`), shown in the "Quality" block and exposed through REST. Nothing is
blocked: the gaps are read in the log, and significant changes are already reported to the
administrators (`tidy.big_changes`, whose `Op.REDUCED` operations are reused here).

What is counted, and why:
- broken links before and after, on the pages touched and on those that pointed to a page
  moved or deleted: has the linking degraded?
- pages created without an incoming link, missing from their folder's index, untitled or
  empty: an isolated or shapeless page will not be found again;
- items filed without a page given: information possibly lost;
- volume removed from existing pages, moves, deletions, pages touched: abusive
  reorganisation or injection;
- tokens, cost and turns per filed item, share of items left: cost and clarity of the
  instructions.

The dictionary is plain JSON (versioned by `VERSION`): the template and the API read it as
it is, and a field added later does not invalidate older runs.
"""

import difflib
import posixpath
from decimal import Decimal
from typing import Any

from synaptane.ai.base import Usage
from synaptane.core.markdown import WikiIndex, broken_links, title_of
from synaptane.services.inbox import CONTENT_DIR, parse_front_matter
from synaptane.services.tidy_tools import MOVED_FROM, Op, Overlay, RunState

VERSION = 1
PREFIX = CONTENT_DIR + "/"
SHOWN = 20  # paths or links listed at most per heading (the numbers stay exact)
DIFF_LINES = 5_000  # beyond that, the volume removed is estimated from the size difference
INDEX = "README.md"


def _text(data: bytes | None) -> str:
    return data.decode("utf-8", "replace") if data is not None else ""


def _relative(path: str) -> str:
    return path.removeprefix(PREFIX)


def _moves(operations: list[dict[str, object]], current: set[str]) -> dict[str, str]:
    """Arrival path → starting path (relative to content/) of the moved pages still present,
    from the tools' log. A page moved twice goes back to its path at the starting commit."""
    steps: dict[str, str] = {}
    for op in operations:
        detail = str(op.get("detail", ""))
        if op.get("op") == Op.MOVED and detail.startswith(MOVED_FROM):
            steps[str(op["path"])] = detail.removeprefix(MOVED_FROM)
    found: dict[str, str] = {}
    for target in steps:
        if PREFIX + target not in current:
            continue
        origin, seen = steps[target], {target}
        while origin in steps and origin not in seen:
            seen.add(origin)
            origin = steps[origin]
        found[target] = origin
    return found


def _removed(before: str, after: str) -> int:
    """Characters of the lines removed (or replaced) from a page."""
    old, new = before.splitlines(), after.splitlines()
    if len(old) > DIFF_LINES or len(new) > DIFF_LINES:
        return max(0, len(before) - len(after))
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return sum(
        sum(len(line) + 1 for line in old[start:end])
        for tag, start, end, _, _ in matcher.get_opcodes()
        if tag in ("delete", "replace")
    )


def _empty(text: str) -> bool:
    """Nothing but the header and headings."""
    _, body = parse_front_matter(text)
    return not any(line.strip() and not line.lstrip().startswith("#") for line in body.split("\n"))


def _index_of(path: str) -> str | None:
    """Index that must list a page: the README of its folder; for a README, the one of the
    parent folder (none for the root one)."""
    folder = posixpath.dirname(path)
    if posixpath.basename(path) == INDEX:
        if not folder:
            return None
        folder = posixpath.dirname(folder)
    return posixpath.join(folder, INDEX) if folder else INDEX


def measure(overlay: Overlay, state: RunState) -> dict[str, Any]:
    """Structural metrics of the run (without the cost, only known to `tidy.run`)."""
    changes = {p: d for p, d in overlay.changes.items() if p.startswith(PREFIX)}
    current = overlay.current()
    base_names = WikiIndex.of(
        _relative(p) for p in current | set(changes) if p.startswith(PREFIX) and overlay.existed(p)
    )
    names = overlay.names()
    links = overlay.links()
    # Arrival → start; a page created then moved during the run remains a creation.
    moved = {
        target: origin
        for target, origin in _moves(state.operations, current).items()
        if overlay.existed(PREFIX + origin)
    }
    sources = set(moved.values())
    created = sorted(
        _relative(p)
        for p, d in changes.items()
        if d is not None and not overlay.existed(p) and _relative(p) not in moved
    )
    modified = sorted(
        _relative(p) for p, d in changes.items() if d is not None and overlay.existed(p)
    )
    deleted = sorted(
        _relative(p)
        for p, d in changes.items()
        if d is None and overlay.existed(p) and _relative(p) not in sources
    )

    # Broken links: pages touched, plus those that pointed to a page that has gone.
    gone = {_relative(p) for p, d in changes.items() if d is None}
    pages = {_relative(p) for p, d in changes.items() if d is not None and p.endswith(".md")}
    pages |= {
        _relative(src) for src, targets in links.items() if src not in changes and targets & gone
    }
    before_total = after_total = 0
    introduced: list[dict[str, str]] = []
    for page in sorted(pages):
        origin = moved.get(page, page)
        old = (
            broken_links(
                _text(overlay.repo.blob(PREFIX + origin, overlay.base)), origin, base_names
            )
            if overlay.existed(PREFIX + origin) and origin.endswith(".md")
            else []
        )
        new = broken_links(_text(overlay.read(PREFIX + page)), page, names)
        before_total, after_total = before_total + len(old), after_total + len(new)
        known = {written for written, _ in old}
        for written, suggestion in new:
            if written not in known:
                entry = {"page": page, "link": written}
                if suggestion:
                    entry["suggestion"] = suggestion
                introduced.append(entry)

    # Pages created: isolated, missing from the index, untitled, empty.
    inbound: dict[str, set[str]] = {}
    for src, targets in links.items():
        for target in targets:
            inbound.setdefault(target, set()).add(_relative(src))
    orphans = [p for p in created if not inbound.get(p, set()) - {p}]
    not_in_index, untitled, empty = [], [], []
    for page in (p for p in created if p.endswith(".md")):
        text = _text(overlay.read(PREFIX + page))
        index = _index_of(page)
        if index is not None and page not in links.get(PREFIX + index, set()):
            not_in_index.append(page)
        if not title_of(text, ""):
            untitled.append(page)
        if _empty(text):
            empty.append(page)

    # Volume removed from the pages kept (a move compares with the old path).
    removed = 0
    for page in modified + sorted(moved):
        origin = moved.get(page, page)
        if overlay.existed(PREFIX + origin):
            removed += _removed(
                _text(overlay.repo.blob(PREFIX + origin, overlay.base)),
                _text(overlay.read(PREFIX + page)),
            )
    deleted_chars = sum(len(overlay.repo.blob(PREFIX + p, overlay.base) or b"") for p in deleted)

    notes_done = [n for n in state.done if n not in state.changes]
    silent = sorted(state.items[n].title for n in notes_done if not state.integrated.get(n))
    reduced = sorted(str(op["path"]) for op in state.operations if op.get("op") == Op.REDUCED)
    done, left = len(state.done), len(state.left)
    result: dict[str, Any] = {
        "version": VERSION,
        "pages": {
            "created": len(created),
            "modified": len(modified),
            "moved": len(moved),
            "deleted": len(deleted),
            "touched": len(created) + len(modified) + len(moved) + len(deleted),
        },
        "broken_links": {
            "before": before_total,
            "after": after_total,
            "introduced": len(introduced),
            "examples": introduced[:SHOWN],
        },
        "orphans": orphans[:SHOWN],
        "orphans_count": len(orphans),
        "not_in_index": not_in_index[:SHOWN],
        "not_in_index_count": len(not_in_index),
        "untitled": untitled[:SHOWN],
        "empty": empty[:SHOWN],
        "closed_without_page": silent[:SHOWN],
        "closed_without_page_count": len(silent),
        "removed_chars": removed,
        "deleted_chars": deleted_chars,
        "reduced": reduced[:SHOWN],
        "items": {
            "done": done,
            "left": left,
            "left_rate": round(left / (done + left), 3) if done + left else 0.0,
        },
    }
    result["issues"] = issues(result)
    return result


def issues(metrics: dict[str, Any] | None) -> int:
    """Number of points to check (0: nothing to report), recorded with the metrics."""
    if not metrics:
        return 0
    links = metrics.get("broken_links") or {}
    return (
        int(links.get("introduced", 0))
        + int(metrics.get("orphans_count", 0))
        + int(metrics.get("not_in_index_count", 0))
        + len(metrics.get("untitled") or [])
        + len(metrics.get("empty") or [])
        + int(metrics.get("closed_without_page_count", 0))
        + len(metrics.get("reduced") or [])
    )


def with_usage(metrics: dict[str, Any], usage: Usage, cost: Decimal | None) -> dict[str, Any]:
    """Adds the usage, in total and per filed item (the cost per successful item is the right
    measure to compare two models or two sets of instructions, not the cost per request)."""
    done = int((metrics.get("items") or {}).get("done", 0))
    tokens = usage.input_tokens + usage.output_tokens + usage.cached_tokens
    per_item: dict[str, Any] | None = None
    if done:
        per_item = {
            "tokens": round(tokens / done),
            "turns": round(usage.turns / done, 1),
            "cost": str((cost / done).quantize(Decimal("0.0001"))) if cost is not None else None,
        }
    return {
        **metrics,
        "usage": {
            "tokens": tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "turns": usage.turns,
            "per_item": per_item,
        },
    }
