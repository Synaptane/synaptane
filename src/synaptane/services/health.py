"""Wiki health of a space: a deterministic check, without AI (the "lint" of the LLM Wiki
pattern).

For a space, at the last commit:
- **broken links**: page, link as written, and a suggestion (closest file with the same name)
  when there is one;
- **orphan pages**: no other page leads to them (folder landing pages, README.md or index.md,
  are excluded: they are reached through the folder);
- **empty pages** (nothing outside the front matter) and **untitled pages** (neither a level-1
  heading nor `title:` in the front matter);
- **old pages**: not changed for N months, from the date of the last commit;
- **pages to review**: verified, but their verification due date has passed, and **pages
  without an owner** among the verified pages (`services.freshness`).

Computed live, with two caches: what a page says about itself (its links, its title) is kept
per content digest, and the result per commit. A new commit only rereads the changed pages, and
the Git history is only walked since the previous commit.
"""

import posixpath
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from synaptane.core.access import Action, require
from synaptane.core.markdown import (
    WikiIndex,
    broken_among,
    link_refs,
    resolve,
    resolve_wiki,
    without_code,
)
from synaptane.git.repo import GitError, Repository
from synaptane.services import Actor, freshness
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.workspace import Workspace
from synaptane.storage.models import Space

PREFIX = CONTENT_DIR + "/"
MARKDOWN = (".md", ".markdown")
STALE_MONTHS = 12
MAX_PAGE = 2 * 1024 * 1024  # beyond that, a page is not analyzed (as for rendering)
# Landing pages of a folder: reached through the folder, without an incoming link.
FOLDER_INDEXES = {"readme.md", "index.md"}


@dataclass(frozen=True)
class BrokenLink:
    page: str
    link: str
    suggestion: str | None


@dataclass(frozen=True)
class StalePage:
    path: str
    date: str  # ISO 8601, last commit that touched the page


@dataclass(frozen=True)
class DuePage:
    path: str
    owner: str
    verified: str  # ISO date of the last verification
    due: str  # ISO date of the due date, passed


@dataclass(frozen=True)
class Health:
    """Wiki health of a space. Paths relative to content/."""

    commit: str | None
    pages: int
    months: int
    broken: list[BrokenLink] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    untitled: list[str] = field(default_factory=list)
    stale: list[StalePage] = field(default_factory=list)
    due: list[DuePage] = field(default_factory=list)  # to review, oldest due date first
    unowned: list[str] = field(default_factory=list)  # verified, without an owner
    verified: int = 0  # verified, up-to-date pages
    review_every: str = ""  # default interval of the space

    @property
    def problems(self) -> int:
        return (
            len(self.broken)
            + len(self.orphans)
            + len(self.empty)
            + len(self.untitled)
            + len(self.stale)
            + len(self.due)
        )


@dataclass(frozen=True)
class _Facts:
    """What a page says about itself, whatever its path: its links as written, and whether it
    is empty or titled."""

    links: tuple[tuple[bool, str], ...]  # (wiki link, target as written)
    empty: bool
    titled: bool


@dataclass(frozen=True)
class _Resolved:
    """Links of a page at a given path: (as written, target or None), and the names targeted
    by its wiki links, whose target changes when a file of that name comes or goes."""

    targets: tuple[tuple[str, str | None], ...]
    keys: frozenset[str]


@dataclass(frozen=True)
class _Base:
    pages: list[str]
    broken: list[BrokenLink]
    orphans: list[str]
    empty: list[str]
    untitled: list[str]


_LOCK = threading.Lock()
_FACTS: OrderedDict[str, _Facts] = OrderedDict()  # blob digest → facts
_FACTS_MAX = 100_000
_BASES: OrderedDict[tuple[str, str], _Base] = OrderedDict()  # (copy, commit) → result
_BASES_MAX = 16
_DATES: dict[str, tuple[str, dict[str, str]]] = {}  # copy → (commit, dates of the files)
# copy → (files, links resolved by (path, digest)) of the last computation
_RESOLVED: dict[str, tuple[set[str], dict[tuple[str, str], _Resolved]]] = {}

_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^(?:---|\.\.\.)[ \t]*$\r?\n?", re.S | re.M)
_TITLE_FIELD = re.compile(r"^title[ \t]*:[ \t]*\S", re.M)
_ATX_H1 = re.compile(r"^ {0,3}#[ \t]+(?!#*[ \t]*$)\S", re.M)
_SETEXT_H1 = re.compile(r"^ {0,3}\S[^\n]*\r?\n {0,3}=+[ \t]*\r?$", re.M)


def space_health(actor: Actor, ws: Workspace, space: Space, months: int = STALE_MONTHS) -> Health:
    """Wiki health of a space (reader). `months`: age from which a page is reported as old
    (1 to 120)."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    return analyze(
        ws.local_repository(space),
        months,
        review_every=space.review_every or "",
        today=freshness.today_for(space),
    )


def analyze(
    repo: Repository,
    months: int = STALE_MONTHS,
    now: datetime | None = None,
    *,
    review_every: str = "",
    today: date | None = None,
) -> Health:
    """Wiki health at the last commit. `review_every`: default verification interval of the
    space; `today`: today's date (in the space's time zone)."""
    months = max(1, min(int(months), 120))
    head = repo.head()
    if head is None:
        return Health(None, 0, months)
    base = _base(repo, head)
    cutoff = (now or datetime.now(UTC)) - timedelta(days=round(months * 30.44))
    dates = _dates(repo, head)
    stale = []
    for page in base.pages:
        date = dates.get(PREFIX + page)
        if date and datetime.fromisoformat(date) < cutoff:
            stale.append(StalePage(page, date))
    stale.sort(key=lambda s: (datetime.fromisoformat(s.date), s.path))
    day = today or (now or datetime.now(UTC)).date()
    states = freshness.states(repo, review_every, day)
    due = sorted(
        (
            DuePage(path, state.owner, state.verified.isoformat(), state.due.isoformat())
            for path, state in states.items()
            if state.stale and state.verified and state.due
        ),
        key=lambda d: (d.due, d.path),
    )
    unowned = sorted(p for p, s in states.items() if s.verified and not s.owner)
    return Health(
        head,
        len(base.pages),
        months,
        base.broken,
        base.orphans,
        base.empty,
        base.untitled,
        stale,
        due,
        unowned,
        sum(1 for s in states.values() if s.is_verified),
        review_every,
    )


def _base(repo: Repository, head: str) -> _Base:
    """Everything that only depends on the commit: broken links, orphans, empty, untitled."""
    key = (str(repo.path), head)
    with _LOCK:
        if (cached := _BASES.get(key)) is not None:
            _BASES.move_to_end(key)
            return cached
    blobs = [b for b in repo.files(CONTENT_DIR, head) if b.path.startswith(PREFIX)]
    index = WikiIndex.of(b.path[len(PREFIX) :] for b in blobs)
    pages = sorted((b for b in blobs if b.path.endswith(MARKDOWN)), key=lambda b: b.path)
    with _LOCK:
        missing = [b for b in pages if b.sha not in _FACTS and b.size <= MAX_PAGE]
    read = repo.read_objects([b.sha for b in missing]) if missing else {}
    facts: dict[str, _Facts] = {}
    with _LOCK:
        for blob in missing:
            if (data := read.get(blob.sha)) is not None:
                _FACTS[blob.sha] = _facts(data.decode("utf-8", "replace"))
        for blob in pages:
            found = _FACTS.get(blob.sha)
            if found is not None:
                _FACTS.move_to_end(blob.sha)
                facts[blob.path] = found
        while len(_FACTS) > _FACTS_MAX:
            _FACTS.popitem(last=False)
    # Resolved links of each page, taken from the previous computation when neither the page
    # nor the files its wiki links target have changed (relative links only depend on the
    # path).
    previous_paths, previous = _RESOLVED.get(str(repo.path), (set(), {}))
    touched = {k for p in previous_paths ^ index.paths for k in _keys(p)}
    resolved: dict[tuple[str, str], _Resolved] = {}
    broken: list[BrokenLink] = []
    incoming: set[str] = set()
    empty, untitled = [], []
    for blob in pages:
        here = blob.path[len(PREFIX) :]
        page = facts.get(blob.path)
        if page is None:  # too big to be analyzed: neither empty nor untitled
            continue
        if page.empty:
            empty.append(here)
        elif not page.titled:
            untitled.append(here)
        entry = previous.get((here, blob.sha))
        if entry is None or entry.keys & touched:
            entry = _resolve(page, here, index)
        resolved[(here, blob.sha)] = entry
        incoming.update(t for _, t in entry.targets if t and t != here)
        if any(t is None or t not in index.paths for _, t in entry.targets):
            for written, suggestion in broken_among(entry.targets, here, index):
                broken.append(BrokenLink(here, written, suggestion))
    with _LOCK:
        _RESOLVED[str(repo.path)] = (set(index.paths), resolved)
        while len(_RESOLVED) > 8:
            _RESOLVED.pop(next(iter(_RESOLVED)))
    names = [b.path[len(PREFIX) :] for b in pages]
    orphans = [
        p for p in names if p not in incoming and p.rsplit("/", 1)[-1].lower() not in FOLDER_INDEXES
    ]
    result = _Base(names, broken, orphans, empty, untitled)
    with _LOCK:
        _BASES[key] = result
        while len(_BASES) > _BASES_MAX:
            _BASES.popitem(last=False)
    return result


def _keys(name: str) -> set[str]:
    """Keys under which a wiki link looks for a file (see `resolve_wiki`)."""
    base = posixpath.basename(name.strip().strip("/")).lower()
    return {base, base.removesuffix(".md")}


def _resolve(page: _Facts, here: str, index: WikiIndex) -> _Resolved:
    targets: list[tuple[str, str | None]] = []
    keys: set[str] = set()
    for wiki, written in page.links:
        if wiki:
            targets.append((f"[[{written}]]", resolve_wiki(written, here, index)))
            keys |= _keys(written)
        elif target := resolve(here, written):
            targets.append((written, target))
    return _Resolved(tuple(targets), frozenset(keys))


def _facts(text: str) -> _Facts:
    links = tuple((ref.kind == "wiki", ref.target) for ref in link_refs(text))
    body, titled = text, False
    if match := _FRONT_MATTER.match(text):
        body, titled = text[match.end() :], bool(_TITLE_FIELD.search(match.group(1)))
    plain = without_code(body)
    titled = titled or bool(_ATX_H1.search(plain) or _SETEXT_H1.search(plain))
    return _Facts(links, not body.strip(), titled)


def _dates(repo: Repository, head: str) -> dict[str, str]:
    """Date of the last commit of each file of content/, kept up to date from one commit to
    the next (only new commits are walked)."""
    known = _DATES.get(str(repo.path))
    if known is not None and known[0] == head:
        return known[1]
    try:
        if known is not None and repo.is_ancestor(known[0], head):
            dates = {**known[1], **repo.last_changes(CONTENT_DIR, head, since=known[0])}
        else:
            dates = repo.last_changes(CONTENT_DIR, head)
    except GitError:
        return {}
    _DATES[str(repo.path)] = (head, dates)
    while len(_DATES) > 16:
        _DATES.pop(next(iter(_DATES)))
    return dates
