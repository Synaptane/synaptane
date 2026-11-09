"""Team checks: rules a team writes in its repository (`.synaptane/checks.md`) and that every
page must follow, checked without AI.

A team that keeps a wiki accumulates rules after each incident ("no scratchpad path", "no
customer email", "a `type` in the front matter"). This module reads them and checks a page
against them; `services.checks` reads the file of a space and applies it (Health, writes,
tidy-up).

The file is Markdown, readable on any Git host. Each level-2 heading is a rule; the list
items `- key: value` right under it describe it; any other text is a comment:

    ## No scratchpad path
    - pattern: `/private/tmp|scratchpad`
    - level: block
    - message: Remove the local path; link to the page or the file instead.

Keys: `pattern` (regular expression on the text), `field` (front matter field) with `expect`
(`present`, `list`, `date`) or `pattern` (on its value), `preset` (`email`, `phone`, `card`,
`iban`), `allow` (values, or email domains, never reported), `paths` and `except` (globs
relative to `content/`), `level` (`warn` or `block`), `message` (what to do), `mask` (hide
the excerpt). A value between backticks is taken as is.

Patterns come from the team and pages from anyone: both are untrusted. Expressions run on
RE2 (linear time, no backtracking: no catastrophic pattern), with a memory cap; the number
of rules, the length of a pattern, the size of its compiled program (the cost per byte of
text in the worst case) and the size of the text Health examines are capped. A file that
does not parse never raises: each faulty rule is reported (`Checks.errors`) and left out, the
others apply.
"""

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import re2

from synaptane.core.errors import ErrorCode, Label, SynaptaneError

MAX_RULES = 50
MAX_PATTERN = 500  # characters of an expression
MAX_FILE = 64_000  # characters of the file read (a push may bring more)
MAX_TEXT = 1_000_000  # bytes of a page examined by Health; a write is checked in full
MAX_HITS = 20  # violations of one rule kept per page
MAX_COMPARED = 10_000  # violations of one rule compared between two versions of a page
MAX_PROGRAM = 3_000  # RE2 instructions of a pattern: `a.{0,1000}b` costs ~10 µs per byte
MAX_PROGRAMS = 15_000  # RE2 instructions of all the patterns of a file
MAX_EXCERPT = 80  # characters of an excerpt
MAX_FRONT = 65_536  # bytes searched for the front matter
RE2_MEMORY = 4 << 20  # bytes of memory for a compiled expression

LEVELS = ("warn", "block")
EXPECTS = ("present", "list", "date")
PRESETS = ("email", "phone", "card", "iban")
KEYS = ("pattern", "field", "expect", "preset", "allow", "paths", "except", "level", "message")
FLAGS = ("mask",)
MASK = "•••"

# Domains and top-level domains reserved for examples (RFC 2606): never a person's address.
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")
EXAMPLE_TLDS = ("example", "invalid", "test", "localhost")
# File extensions that look like a top-level domain: `logo@2x.png` is not an address.
FILE_TLDS = frozenset(
    ("png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "bmp", "ico", "pdf", "md", "txt", "csv")
    + ("json", "yaml", "yml", "html", "htm", "css", "js", "ts", "zip", "gz", "tar", "doc")
    + ("docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp")
)


def _options() -> Any:
    options = re2.Options()
    options.max_mem = RE2_MEMORY
    options.log_errors = False
    return options


def _compile(pattern: str) -> Any:
    """RE2 expression on UTF-8 bytes; `re2.error` if it is invalid or too large. `^` and `$`
    match at each line, as in an editor's search."""
    return re2.compile(("(?m)" + pattern).encode(), _options())


# --- Rules -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FileError:
    """A faulty line of the file: the rule it belongs to is left out."""

    line: int
    message: Label


@dataclass(frozen=True)
class Rule:
    title: str
    line: int  # of its heading, in the file
    level: str = "warn"
    message: str = ""
    pattern: str = ""
    field_name: str = ""
    expect: tuple[str, ...] = ()
    preset: str = ""
    allow: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()  # `except`
    mask: bool = False
    compiled: Any = field(default=None, compare=False, repr=False)
    scope: Any = field(default=None, compare=False, repr=False)  # paths, compiled
    outside: Any = field(default=None, compare=False, repr=False)  # except, compiled

    @property
    def blocks(self) -> bool:
        return self.level == "block"

    @property
    def masked(self) -> bool:
        return self.mask or bool(self.preset)

    def covers(self, path: str) -> bool:
        """Does the rule apply to a page (path relative to `content/`)?"""
        data = path.encode()
        if self.scope is not None and not self.scope.fullmatch(data):
            return False
        return self.outside is None or not self.outside.fullmatch(data)

    def what(self) -> Label:
        """What the rule checks, as an interface text."""
        if self.preset:
            label = Label(PRESET_LABELS[self.preset])
            if self.allow:
                return Label("{what}, except {allowed}", what=label, allowed=", ".join(self.allow))
            return label
        if self.field_name and self.expect:
            parts = {f"e{i}": Label(EXPECT_LABELS[e]) for i, e in enumerate(self.expect)}
            joined = ", ".join(f"{{{k}}}" for k in parts)
            return Label(
                "front matter field {field}: {expected}",
                field=self.field_name,
                expected=Label(joined, **parts),
            )
        if self.field_name:
            return Label(
                "front matter field {field} matching {pattern}",
                field=self.field_name,
                pattern=self.pattern,
            )
        return Label("text matching {pattern}", pattern=self.pattern)


PRESET_LABELS = {
    "email": "email addresses",
    "phone": "phone numbers",
    "card": "card numbers (Luhn check)",
    "iban": "IBANs (check digits)",
}
EXPECT_LABELS = {
    "present": "required, not empty",
    "list": "a YAML list",
    "date": "a date without a time",
}


@dataclass(frozen=True)
class Hit:
    """A violation in a text, whatever its path: the rule's index, the line, the excerpt as
    shown (masked if needed), and what identifies it from one version to the next."""

    rule: int
    line: int
    excerpt: str
    key: str = field(repr=False)


@dataclass(frozen=True)
class Violation:
    """A page that breaks a rule. `excerpt` is masked for personal data; `key` (never shown)
    tells the same violation apart from one version of the page to the next."""

    rule: str  # title
    level: str
    message: str
    page: str  # relative to content/
    line: int
    excerpt: str
    index: int = 0  # of the rule in the file
    key: str = field(default="", compare=False, repr=False)

    @property
    def blocks(self) -> bool:
        return self.level == "block"


@dataclass(frozen=True)
class Checks:
    rules: tuple[Rule, ...] = ()
    errors: tuple[FileError, ...] = ()
    digest: str = ""  # of the file's text: caches depend on it

    def __bool__(self) -> bool:
        return bool(self.rules)

    def hits(self, data: bytes, *, whole: bool = False) -> tuple[Hit, ...]:
        """Violations of every rule in a text, the rules' paths aside (a page's hits depend
        only on its content: they can be cached per digest). Health reads the first MAX_TEXT
        bytes and keeps MAX_HITS per rule; a write (`whole`) is read in full and keeps up to
        MAX_COMPARED per rule, so that neither padding nor a page already full of violations
        lets a new one through. A NUL byte changes nothing: a page is a page."""
        if not self.rules:
            return ()
        text = data if whole else data[:MAX_TEXT]
        limit = MAX_COMPARED if whole else MAX_HITS
        meta: _Front | None = None
        spaced: bytes | None = None
        found: list[Hit] = []
        for index, rule in enumerate(self.rules):
            if rule.field_name:
                if meta is None:
                    meta = _front(text)
                found += _field_hits(index, rule, meta)
            elif rule.preset:
                if spaced is None:
                    spaced = _spaced(text)
                found += _text_hits(index, rule, spaced, limit)
            else:
                found += _text_hits(index, rule, text, limit)
        return tuple(found)

    def violations(
        self,
        path: str,
        data: bytes,
        hits: tuple[Hit, ...] | None = None,
        *,
        whole: bool = False,
    ) -> list[Violation]:
        """Violations of a page at `path` (relative to `content/`); `whole`: see `hits`."""
        if not self.rules or not is_page(path):
            return []
        found = self.hits(data, whole=whole) if hits is None else hits
        return [self._violation(path, h) for h in found if self.rules[h.rule].covers(path)]

    def introduced(
        self, path: str, new: bytes, old: bytes | None, old_path: str | None = None
    ) -> list[Violation]:
        """Violations of the new version that the old one did not have (at `old_path` for a
        moved page): a page that already broke a rule stays editable, as with secrets. The
        same excerpt elsewhere in the page is the same violation (an edit above it does not
        make it new). Both versions are read in full (`hits`); MAX_HITS per rule are kept."""
        now = self.violations(path, new, whole=True)
        if not now:
            return []
        before = Counter(
            (v.index, v.key)
            for v in (self.violations(old_path or path, old, whole=True) if old else ())
        )
        fresh, kept = [], Counter[int]()
        for violation in now:
            identity = (violation.index, violation.key)
            if before[identity]:
                before[identity] -= 1
            elif kept[violation.index] < MAX_HITS:
                kept[violation.index] += 1
                fresh.append(violation)
        return fresh

    def _violation(self, path: str, hit: Hit) -> Violation:
        rule = self.rules[hit.rule]
        return Violation(
            rule.title,
            rule.level,
            rule.message,
            path,
            hit.line,
            hit.excerpt,
            hit.rule,
            hit.key,
        )


EMPTY = Checks()


def is_page(path: str) -> bool:
    """Checks apply to Markdown pages."""
    return path.lower().endswith((".md", ".markdown"))


# --- Parsing the file --------------------------------------------------------------------------

_OTHER_HEADING = re.compile(r"^#{1,6}(?:[ \t]|$)")
_ITEM = re.compile(r"^[-*+][ \t]+([A-Za-z_]+)[ \t]*:[ \t]*(.*)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DOMAIN = re.compile(r"^(?:\*\.)?[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_FIELD = re.compile(r"^[A-Za-z_][\w-]{0,63}$")


def _unquote(raw: str) -> str:
    """A value: as is between backticks (`` `a|b` ``, or ``` `` a`b `` ``` if it holds one),
    otherwise stripped."""
    value = raw.strip()
    ticks = len(value) - len(value.lstrip("`"))
    if ticks and value.endswith("`" * ticks) and len(value) > 2 * ticks:
        inner = value[ticks:-ticks]
        return (
            inner[1:-1]
            if inner.startswith(" ") and inner.endswith(" ") and inner.strip()
            else inner
        )
    return value


def _items(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _without_comments(text: str) -> str:
    """HTML comments blanked out, line numbers kept (an unclosed one runs to the end). A
    `<!--` inside backticks is a value (`- pattern: `<!--``), not a comment. By hand, in one
    pass: a lazy expression would be quadratic on a file full of `<!--`."""
    parts, copied, position = [], 0, 0
    seen, ticks = 0, 0  # backticks on the current line, before `seen`
    while (start := text.find("<!--", position)) >= 0:
        if (newline := text.rfind("\n", seen, start)) >= 0:
            seen, ticks = newline + 1, 0
        ticks += text.count("`", seen, start)
        seen = position = start + 4
        if ticks % 2:
            continue
        end = text.find("-->", start + 4)
        stop = len(text) if end < 0 else end + 3
        parts += [text[copied:start], "\n" * text.count("\n", start, stop)]
        copied = seen = position = stop
        ticks = 0
    return "".join(parts) + text[copied:]


def parse(text: str) -> Checks:
    """Rules of a checks file. Never raises: errors are listed, with their line."""
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    errors: list[FileError] = []
    if len(text) > MAX_FILE:
        errors.append(
            FileError(
                0,
                Label("File too long: only its first {limit} characters are read.", limit=MAX_FILE),
            )
        )
        text = text[:MAX_FILE]
    # Comments blanked out, line numbers kept.
    text = _without_comments(text.replace("\r\n", "\n"))
    sections: list[tuple[str, int, list[tuple[int, str, str]]]] = []
    fence = ""
    for number, row in enumerate(text.split("\n"), start=1):
        if fence:
            if row.strip().startswith(fence):
                fence = ""
            continue
        if opened := _FENCE.match(row):
            fence = opened.group(1)[:3]
            continue
        if row.startswith(("## ", "##\t")) and (title := row[2:].strip().rstrip("#").strip()):
            sections.append((title, number, []))
        elif _OTHER_HEADING.match(row) and not row.startswith("###"):
            sections.append(("", number, []))  # a level-1 heading closes the rule
        elif (item := _ITEM.match(row)) and sections and sections[-1][0]:
            sections[-1][2].append((number, item.group(1).lower(), item.group(2)))
    rules: list[Rule] = []
    titles: set[str] = set()
    budget = MAX_PROGRAMS
    for title, line, items in sections:
        if not title:
            continue
        if len(rules) >= MAX_RULES:
            errors.append(
                FileError(
                    line,
                    Label("More than {limit} rules: the next ones are ignored.", limit=MAX_RULES),
                )
            )
            break
        rule, problems = _rule(title, line, items)
        if rule is not None and title.casefold() in titles:
            problems.append(FileError(line, Label("Two rules are named “{title}”.", title=title)))
            rule = None
        cost = rule.compiled.programsize if rule is not None and rule.compiled is not None else 0
        if cost > budget:
            problems.append(
                FileError(
                    line,
                    Label(
                        "The patterns are too complex together ({limit} steps at most): "
                        "simplify this one or the ones above.",
                        limit=MAX_PROGRAMS,
                    ),
                )
            )
            rule = None
        budget -= cost if rule is not None else 0
        errors += problems
        if rule is not None:
            titles.add(title.casefold())
            rules.append(rule)
    return Checks(tuple(rules), tuple(sorted(errors, key=lambda e: e.line)), digest)


def _rule(
    title: str, line: int, items: list[tuple[int, str, str]]
) -> tuple[Rule | None, list[FileError]]:
    errors: list[FileError] = []
    values: dict[str, str] = {}
    lines: dict[str, int] = {}
    for number, key, raw in items:
        if key not in KEYS and key not in FLAGS:
            errors.append(
                FileError(
                    number,
                    Label(
                        "Unknown key “{key}” (expected: {keys}).",
                        key=key,
                        keys=", ".join(KEYS + FLAGS),
                    ),
                )
            )
            continue
        if key in values:
            errors.append(FileError(number, Label("The key “{key}” is given twice.", key=key)))
            continue
        values[key], lines[key] = (raw.strip() if key == "message" else _unquote(raw)), number

    def fail(key: str, message: Label) -> None:
        errors.append(FileError(lines.get(key, line), message))

    level = values.get("level", "warn").lower() or "warn"
    if level not in LEVELS:
        fail("level", Label("Unknown level “{level}”: warn or block.", level=level))
    preset = values.get("preset", "").lower()
    pattern = values.get("pattern", "")
    name = values.get("field", "")
    expect = tuple(e.lower() for e in _items(values.get("expect", "")))
    mask = values.get("mask", "no").lower() in ("yes", "true", "1", "oui")
    if preset and preset not in PRESETS:
        fail(
            "preset",
            Label(
                "Unknown preset “{preset}”: {presets}.", preset=preset, presets=", ".join(PRESETS)
            ),
        )
    if preset and (pattern or name):
        fail("preset", Label("A preset stands alone: no pattern or field with it."))
    if not (preset or pattern or name):
        fail("pattern", Label("Give a pattern, a field or a preset."))
    if expect and not name:
        fail("expect", Label("“expect” needs a field."))
    if name and not (expect or pattern):
        fail("field", Label("Say what the field must be (expect) or must not match (pattern)."))
    if name and not _FIELD.match(name):
        fail("field", Label("Invalid field name “{field}”.", field=name[:64]))
    for wanted in expect:
        if wanted not in EXPECTS:
            fail(
                "expect",
                Label(
                    "Unknown expectation “{expect}”: {expects}.",
                    expect=wanted,
                    expects=", ".join(EXPECTS),
                ),
            )
    compiled = None
    if pattern:
        if len(pattern) > MAX_PATTERN:
            fail(
                "pattern", Label("Pattern too long: {limit} characters at most.", limit=MAX_PATTERN)
            )
        else:
            try:
                compiled = _compile(pattern)
            except re2.error as error:
                reason: object = error.args[0] if error.args else b""
                detail = (
                    reason.decode("utf-8", "replace") if isinstance(reason, bytes) else str(reason)
                )
                fail("pattern", Label("Invalid pattern: {detail}.", detail=detail[:200]))
            else:
                if compiled.programsize > MAX_PROGRAM:
                    fail(
                        "pattern",
                        Label(
                            "Pattern too complex ({size} steps, {limit} at most): shorten its "
                            "counted repetitions.",
                            size=compiled.programsize,
                            limit=MAX_PROGRAM,
                        ),
                    )
                elif compiled.search(b"") is not None:
                    fail(
                        "pattern",
                        Label("The pattern matches an empty text: it would report every page."),
                    )
    allow = _items(values.get("allow", ""))
    if preset == "email":
        allow = tuple(a.lower().lstrip("@") for a in allow)
        for entry in allow:
            if "@" not in entry and not _DOMAIN.match(entry):
                fail("allow", Label("Invalid domain “{domain}”.", domain=entry[:100]))
    scope, outside = None, None
    paths, skip = _items(values.get("paths", "")), _items(values.get("except", ""))
    try:
        scope = _globs(paths) if paths else None
        outside = _globs(skip) if skip else None
    except re2.error:
        fail("paths", Label("Invalid paths."))
    if errors:
        return None, errors
    return (
        Rule(
            title[:200],
            line,
            level,
            values.get("message", "")[:500],
            pattern,
            name,
            expect,
            preset,
            allow,
            paths,
            skip,
            mask,
            compiled,
            scope,
            outside,
        ),
        [],
    )


def _glob(glob: str) -> str:
    """A glob relative to `content/` as an expression: `*` within a folder, `**` across
    folders, `?` one character; a folder (no wildcard, or a final `/`) covers everything
    under it."""
    glob = glob.strip().lstrip("/").removeprefix("content/")
    if glob.endswith("/"):
        glob += "**"
    elif not any(c in glob for c in "*?"):
        glob += "/**" if not is_page(glob) else ""
    out, index = [], 0
    while index < len(glob):
        if glob.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif glob.startswith("/**", index) and index + 3 == len(glob):
            out.append("(?:/.*)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return "".join(out)


def _globs(globs: tuple[str, ...]) -> Any:
    """Paths matching one of `globs` (RE2: a glob of many stars stays linear)."""
    return re2.compile("|".join(f"(?:{_glob(g)})" for g in globs).encode(), _options())


# --- Front matter ------------------------------------------------------------------------------

_FRONT_START = re.compile(rb"\A(?:\xef\xbb\xbf)?---[ \t]*\r?\n")
_FRONT_END = re.compile(rb"^(?:---|\.\.\.)[ \t]*\r?$", re.M)
_KEY = re.compile(r"^([A-Za-z_][\w-]*)[ \t]*:(?:[ \t]+(.*)|[ \t]*)$")
_DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LIST_ITEM = re.compile(r"^[ \t]*-(?:[ \t]+.*|[ \t]*)$")


@dataclass(frozen=True)
class _Entry:
    line: int  # in the page
    raw: str  # value on the key's line
    more: tuple[str, ...]  # following lines (list, block of text)


@dataclass(frozen=True)
class _Front:
    found: bool
    entries: dict[str, _Entry]


def _front(data: bytes) -> _Front:
    """Top-level keys of the YAML front matter (lenient: what is not a key is ignored)."""
    start = _FRONT_START.match(data)
    if start is None:
        return _Front(False, {})
    end = _FRONT_END.search(data, start.end(), min(len(data), MAX_FRONT))
    if end is None:
        return _Front(False, {})
    lines = data[start.end() : end.start()].decode("utf-8", "replace").split("\n")
    entries: dict[str, _Entry] = {}
    index = 0
    while index < len(lines):
        match = _KEY.match(lines[index].rstrip("\r"))
        if match is None:
            index += 1
            continue
        stop = index + 1
        while stop < len(lines) and (
            lines[stop][:1] in (" ", "\t", "-") or not lines[stop].strip()
        ):
            stop += 1
        more = tuple(line.rstrip("\r") for line in lines[index + 1 : stop] if line.strip())
        entries.setdefault(match.group(1), _Entry(index + 2, (match.group(2) or "").strip(), more))
        index = stop
    return _Front(True, entries)


def _plain(raw: str) -> str:
    """A scalar without its comment and quotes."""
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        return raw[1:end] if end > 0 else raw[1:]
    cuts = [i for i in (raw.find(" #"), raw.find("\t#")) if i >= 0]
    return (raw[: min(cuts)] if cuts else raw).strip()


def _flow_list(raw: str) -> bool:
    """`[a, "b, c", 'd']`: brackets and quotes balanced, nothing after the closing one."""
    depth, quote = 0, ""
    for index, char in enumerate(raw):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                return False
            if depth == 0:
                rest = raw[index + 1 :].strip()
                return not rest or rest.startswith("#")
    return False


def _shape(entry: _Entry) -> str:
    """`empty`, `list`, `bad-list` (meant as a list, but not a valid one) or `scalar`."""
    if entry.raw.startswith("["):
        return "list" if _flow_list(entry.raw) and not entry.more else "bad-list"
    items = [bool(_LIST_ITEM.match(line)) for line in entry.more]
    value = _plain(entry.raw)
    if not value or value in ("~", "null", "Null", "NULL"):
        if not entry.more:
            return "empty"
        if all(items):
            return "list"
        return "bad-list" if any(items) else "scalar"
    return "bad-list" if any(items) else "scalar"


def _field_hits(index: int, rule: Rule, meta: _Front) -> list[Hit]:
    entry = meta.entries.get(rule.field_name)
    shape = _shape(entry) if entry is not None else "missing"
    line = entry.line if entry is not None else 1
    raw = entry.raw if entry is not None else ""
    excerpt = _cut(f"{rule.field_name}: {raw}" if entry is not None else "")
    found: list[Hit] = []
    for wanted in rule.expect:
        if wanted == "present" and (shape in ("missing", "empty") or raw in ("[]", "{}")):
            found.append(Hit(index, line, excerpt, f"{rule.field_name}:missing"))
        elif wanted == "list" and shape in ("scalar", "bad-list"):
            found.append(Hit(index, line, excerpt, f"{rule.field_name}:list:{raw}"))
        elif wanted == "date" and shape not in ("missing", "empty"):
            value = _plain(raw)
            if shape != "scalar" or not _DATE_VALUE.match(value) or not _valid_date(value):
                found.append(Hit(index, line, excerpt, f"{rule.field_name}:date:{raw}"))
    if rule.compiled is not None and entry is not None and not found:
        written = "\n".join((entry.raw, *entry.more)).encode()
        if (match := rule.compiled.search(written)) is not None:
            text = match.group(0).decode("utf-8", "replace")
            if text.strip() not in rule.allow:
                shown = _mask(text) if rule.mask else excerpt
                found.append(Hit(index, line, shown, f"{rule.field_name}:{text}"))
    return found


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# --- Text: patterns and presets ------------------------------------------------------------------


def _cut(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_EXCERPT else flat[: MAX_EXCERPT - 1] + "…"


def _mask(text: str) -> str:
    """Generic mask: the first and last two characters."""
    flat = " ".join(text.split())
    return f"{flat[:2]}{MASK}{flat[-2:]}" if len(flat) > 6 else MASK


class _Lines:
    """Line number of byte offsets, counted forward (matches come in order)."""

    def __init__(self, data: bytes) -> None:
        self.data, self.offset, self.line = data, 0, 1

    def at(self, offset: int) -> int:
        if offset < self.offset:
            self.offset, self.line = 0, 1
        self.line += self.data.count(b"\n", self.offset, offset)
        self.offset = offset
        return self.line


def _text_hits(index: int, rule: Rule, data: bytes, limit: int = MAX_HITS) -> list[Hit]:
    found: list[Hit] = []
    lines = _Lines(data)
    if rule.preset:
        finder = PRESET_FINDERS[rule.preset]
        for start, value, shown in finder(data, rule.allow):
            found.append(Hit(index, lines.at(start), shown, value))
            if len(found) >= limit:
                break
        return found
    for match in rule.compiled.finditer(data):
        text = match.group(0).decode("utf-8", "replace")
        if not text or text.strip() in rule.allow:
            continue
        shown = _mask(text) if rule.mask else _cut(text)
        found.append(Hit(index, lines.at(match.start()), shown, text))
        if len(found) >= limit:
            break
    return found


# Characters a word processor puts between digits, read by the presets as an ASCII space or
# hyphen (no-break, narrow, thin, figure spaces…; no-break hyphen, dashes, minus), or ignored
# (zero-width ones, soft hyphen): `06 12 34 56 78` typed with no-break spaces is still a phone
# number. Lines do not move: no line break is touched.
_AS_SPACE = (
    "\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
)
_AS_HYPHEN = "\u2010\u2011\u2012\u2013\u2212"
_IGNORED = "\u00ad\u200b\u200c\u200d\u2060\ufeff"


def _spaced(data: bytes) -> bytes:
    """The text the presets read (see above)."""
    if data.isascii():
        return data
    for chars, by in ((_AS_SPACE, b" "), (_AS_HYPHEN, b"-"), (_IGNORED, b"")):
        for char in chars:
            data = data.replace(char.encode(), by)
    return data


Finder = Callable[[bytes, tuple[str, ...]], Iterable[tuple[int, str, str]]]


def _before(data: bytes, start: int) -> str:
    """The character before `start` (a space at the start of the text), decoded: `°` or `«`
    next to a number is not a letter."""
    begin = start - 1
    while begin > 0 and start - begin < 4 and 0x80 <= data[begin] < 0xC0:
        begin -= 1
    return data[max(begin, 0) : start].decode("utf-8", "replace")[-1:] or " "


def _after(data: bytes, end: int) -> str:
    """The character at `end` (a space at the end of the text), decoded."""
    return data[end : end + 4].decode("utf-8", "replace")[:1] or " "


def _alnum(char: str) -> bool:
    return char.isalnum()


def _digit(char: str) -> bool:
    return "0" <= char <= "9"


_EMAIL = _compile(
    r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?){0,8}\.[A-Za-z]{2,24}"
)


def _emails(data: bytes, allow: tuple[str, ...]) -> Iterable[tuple[int, str, str]]:
    for match in _EMAIL.finditer(data):
        address = match.group(0).decode("ascii", "replace").lower().strip(".")
        local, _, domain = address.rpartition("@")
        tld = domain.rsplit(".", 1)[-1]
        if tld in FILE_TLDS or tld in EXAMPLE_TLDS or not local:
            continue
        allowed = (*EXAMPLE_DOMAINS, *allow)
        if address in allowed or any(domain == d or domain.endswith("." + d) for d in allowed):
            continue
        yield match.start(), address, f"{local[:1]}{MASK}@{domain}"


_PHONE = _compile(
    r"\+[1-9][0-9]{0,2}(?:[ .\-]?\(0\))?(?:[ .\-]?\(?[0-9]{1,4}\)?){2,6}"
    r"|0[1-9](?:[ .\-]?[0-9]{2}){4}"
    r"|\([2-9][0-9]{2}\)[ .\-]?[0-9]{3}[ .\-][0-9]{4}"
)


def _phones(data: bytes, allow: tuple[str, ...]) -> Iterable[tuple[int, str, str]]:
    """International numbers (`+33 6 12 34 56 78`), French national ones (`06 12 34 56 78`)
    and North American ones (`(555) 123-4567`)."""
    allowed = {re.sub(r"\D", "", a) for a in allow}
    for match in _PHONE.finditer(data):
        start, end = match.span()
        text = match.group(0).decode("ascii", "replace").rstrip(")")
        if _alnum(_before(data, start)) or _before(data, start) == "+":
            continue
        if _digit(_after(data, end)) or (
            _after(data, end) in (".", "-") and _digit(_after(data, end + 1))
        ):
            continue
        digits = re.sub(r"\D", "", text.replace("(0)", ""))
        if not 8 <= len(digits) <= 15 or digits in allowed:
            continue
        yield start, digits, f"{text[:3]}{MASK}{digits[-2:]}"


_CARD = _compile(r"[0-9](?:[ \-]?[0-9]){12,18}")


def luhn(digits: str) -> bool:
    """Luhn check (card numbers)."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _card_number(text: str) -> str | None:
    """Card number in a run of digits: the whole run, or its first groups (a number followed
    by other figures)."""
    separators = {c for c in text if not c.isdigit()}
    if len(separators) > 1:
        return None
    groups = re.split(r"[ \-]", text) if separators else [text]
    for count in range(len(groups), 0, -1):
        digits = "".join(groups[:count])
        if 13 <= len(digits) <= 19 and len(set(digits)) > 1 and luhn(digits):
            return digits
        if len(groups) == 1:
            break
    return None


def _cards(data: bytes, allow: tuple[str, ...]) -> Iterable[tuple[int, str, str]]:
    allowed = {re.sub(r"\D", "", a) for a in allow}
    for match in _CARD.finditer(data):
        start, end = match.span()
        if _alnum(_before(data, start)) or _alnum(_after(data, end)):
            continue
        digits = _card_number(match.group(0).decode("ascii"))
        if digits is None or digits in allowed:
            continue
        yield start, digits, f"{MASK} {digits[-4:]}"


# Length of an IBAN per country (ISO 13616 registry).
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22,
    "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28,
    "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23,
    "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26,
    "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20,
    "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18,
    "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24,
    "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23, "TN": 24,
    "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}  # fmt: skip
_IBAN = _compile(r"[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]){11,30}")


def iban_valid(iban: str) -> bool:
    """Check digits of an IBAN (ISO 7064 mod 97-10), with its country's length."""
    if IBAN_LENGTHS.get(iban[:2]) != len(iban) or not iban.isalnum():
        return False
    moved = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _end_of(text: str, length: int) -> int | None:
    """End of the IBAN's own characters in a match that may run on into the next word."""
    taken = 0
    for index, char in enumerate(text):
        taken += char != " "
        if taken == length:
            return index + 1
    return None


def _ibans(data: bytes, allow: tuple[str, ...]) -> Iterable[tuple[int, str, str]]:
    allowed = {a.replace(" ", "").upper() for a in allow}
    data = data.upper()  # ASCII letters only: offsets do not move; `fr76 …` is an IBAN too
    for match in _IBAN.finditer(data):
        start = match.start()
        if _alnum(_before(data, start)):
            continue
        text = match.group(0).decode("ascii")
        length = IBAN_LENGTHS.get(text[:2])
        if length is None:
            continue
        end = _end_of(text, length)
        if end is None:
            continue
        if end < len(text):
            if text[end] != " ":  # cut in the middle of a group
                continue
        elif _alnum(_after(data, match.end())):
            continue
        iban = text[:end].replace(" ", "")
        if iban in allowed or not iban_valid(iban):
            continue
        yield start, iban, f"{iban[:4]} {MASK} {iban[-3:]}"


PRESET_FINDERS: dict[str, Finder] = {
    "email": _emails,
    "phone": _phones,
    "card": _cards,
    "iban": _ibans,
}


# --- Refusal -------------------------------------------------------------------------------------


def describe(violations: list[Violation]) -> Label:
    """“Rule” (page, line 3): message, …: a text to translate, made of the violations."""
    parts: dict[str, object] = {}
    for index, v in enumerate(violations[:3]):
        parts[f"p{index}"] = Label(
            "“{rule}” ({page}, line {line}){excerpt}: {message}",
            rule=v.rule,
            page=v.page,
            line=v.line,
            excerpt=f" `{v.excerpt}`" if v.excerpt else "",
            message=v.message or v.rule,
        )
    template = "; ".join(f"{{{name}}}" for name in parts)
    if len(violations) > 3:
        parts["more"] = Label("and {count} more", count=len(violations) - 3)
        template += " {more}"
    return Label(template, **parts)


def refuse(violations: list[Violation]) -> None:
    """Refuses a write that breaks a `block` rule, with the team's message."""
    blocking = [v for v in violations if v.blocks]
    if blocking:
        raise SynaptaneError(
            ErrorCode.CHECK,
            "Refused by a team check: {detail}. Fix the text, then save again.",
            detail=describe(blocking),
        )
