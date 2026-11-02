"""What the writing tools tell the assistant: where an `old` passage went wrong, and the
change actually saved.

An assistant that misquotes a passage (a space, an accent, a line break, a number it
remembered wrong) gets the closest passage of the page, to quote it again without re-reading
the whole page; one that quotes too little gets the lines of each occurrence. After a write,
the change itself comes back as a short diff: the assistant checks what it saved, a
verification field kept or a passage removed by mistake included.
"""

import difflib

CLOSEST = 0.6  # below this similarity, no passage is "close"
LINES = 10_000  # lines of the page looked through at most
LINE = 300  # characters of a line compared at most
WHOLE = 2_000  # characters of a passage compared at most
PASSAGE = 1_500  # characters of the closest passage shown at most
CHANGE = 4_000  # characters of the change shown at most (diff gives the whole one)


def lines_of(text: str, old: str) -> list[int]:
    """Line (from 1) where each occurrence of `old` starts."""
    found, start = [], text.find(old)
    while start != -1:
        found.append(text.count("\n", 0, start) + 1)
        start = text.find(old, start + 1)
    return found


def closest(text: str, old: str) -> tuple[int, int, str] | None:
    """The passage of `text` most like `old`, as many lines long: (first line, last line,
    passage), or None if nothing comes close. Its first, middle and last lines are looked for
    line by line (cheap), then the few passages they point to are compared whole."""
    lines = text.splitlines()[:LINES]
    wanted = old.strip("\n").splitlines() or [old]
    size = len(wanted)
    starts: set[int] = set()
    for anchor in sorted({0, size // 2, size - 1}):
        target = wanted[anchor].strip()[:LINE]
        if target:
            starts.update(max(i - anchor, 0) for i in _similar_lines(lines, target))
    best, found = CLOSEST, None
    for start in sorted(starts):
        passage = "\n".join(lines[start : start + size])
        matcher = difflib.SequenceMatcher(None, old[:WHOLE], passage[:WHOLE], autojunk=False)
        if passage.strip() and (ratio := matcher.ratio()) > best:
            best, found = ratio, (start + 1, min(start + size, len(lines)), passage)
    return found


def _similar_lines(lines: list[str], target: str, keep: int = 5) -> list[int]:
    """The `keep` lines most like `target`."""
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(target)  # analysed once, compared with each line
    scored: list[tuple[float, int]] = []
    for number, line in enumerate(lines):
        matcher.set_seq1(line.strip()[:LINE])
        floor = scored[-1][0] if len(scored) == keep else 0.0
        if matcher.real_quick_ratio() <= floor or matcher.quick_ratio() <= floor:
            continue
        scored.append((matcher.ratio(), number))
        scored = sorted(scored, reverse=True)[:keep]
    return [number for _, number in scored]


def short(passage: str, limit: int = PASSAGE) -> str:
    return passage if len(passage) <= limit else passage[:limit] + " […]"


def change(before: str, after: str) -> str:
    """The saved change as a unified diff (two lines of context), cut at `CHANGE`
    characters; empty if nothing changed."""
    lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), "before", "after", n=2, lineterm=""
        )
    )[2:]  # without the ---/+++ header
    text = "\n".join(lines)
    if len(text) <= CHANGE:
        return text
    cut = text[:CHANGE].rsplit("\n", 1)[0]
    return cut + f"\n[… {text.count(chr(10)) - cut.count(chr(10))} more lines: diff gives all]"
