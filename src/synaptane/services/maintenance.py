"""Nightly wiki maintenance: the problems people introduced since the last tidy-up run.

The health check (`services.health`) sees broken links and isolated, empty or untitled pages;
the run metrics (`services.tidy_metrics`) see pages missing from their folder's index. Yet
nobody fixed them as long as no note arrived. So the scheduled run (and "Tidy up now")
computes, without AI, the problems that appeared since the last run through changes made by
people (web, REST, MCP, pushes), and gives them to the agent as one synthetic item,
on the model of the "Import: N pages" item: the run happens even with an empty inbox. The
agent fixes what is safe and leaves the rest, with a reason.

No flood, no repetition:
- the problems are counted from a watermark (`Space.maintain_watermark`): the head at the end
  of the last run that was given all of them, or the last synchronized head when maintenance
  was turned on (or at the upgrade). The past is never caught up;
- only the commits of people count: Synaptane's own (tidy-up, routines…) are left out, since
  the tidy-up already fixes what it breaks (its end-of-run check); so are imports (a commit
  of more than `changes.BULK` pages), already reviewed as a whole by the "Import: N pages"
  item: only what people change after them counts. Except the pages a routine keeps in its
  own folders (`services.routine_writes`): its commit names them, and they count like a
  person's change (not its report page, rewritten at each run);
- at most MAX_PROBLEMS per night; the others wait for the next nights (the watermark only moves
  once every problem of its window was given);
- a problem the agent leaves is remembered with its reason (`MaintenanceMark`), and forgotten
  once it has gone. It comes back only if it is introduced again: it went away and came back,
  or a commit of people wrote it again (`Source.reintroduced`: the same broken link added
  once more, the page created anew). Editing its page is not enough: a log page edited every
  day, with a link broken on purpose, must not cost a run every night.

Nothing is computed without an AI provider, nor once the month's budget is used up: the
caller (`tidy.tidy_space`) only asks then.

Extension point: `SOURCES`, the sources of problems. The built-in one (`WikiChecks`) reports
what the health check would; another source (the team's own checks, for one) only has to
return `Problem`s for a `Window`, say which of them are still there, and which were
introduced again.
"""

import hashlib
import posixpath
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from synaptane.core.errors import Label
from synaptane.core.markdown import WikiIndex, broken_among, broken_links, link_targets
from synaptane.git.repo import Commit, GitError, Repository
from synaptane.services import changes, health
from synaptane.services import links as links_service
from synaptane.services.inbox import CONTENT_DIR, InboxItem, is_routine_agent
from synaptane.services.routine_writes import kept_folders
from synaptane.services.tidy_tools import FOLDER_INDEXES, Op, Overlay, RunState, _broken_keys
from synaptane.services.workspace import Workspace
from synaptane.storage.models import MaintenanceMark, Space

PREFIX = CONTENT_DIR + "/"
TYPE = "maintenance"  # type of the synthetic item
NAME = "maintenance"  # its name in the run (one per run)
INDEX = "README.md"

# Problems given to the agent per night. Each one costs a few tool calls (read the page,
# search, one edit): ten stay a small share of a run's 120 steps, beside up to 25 inbox items,
# cost about as much as two or three notes, and make a list that someone can check in the log
# the next morning. A normal day of edits introduces fewer.
MAX_PROBLEMS = 10

# Runs interrupted in a row before the problems given are left, as for an inbox item
# (`tidy.MAX_ITEM_ATTEMPTS`): otherwise a model that never closes the item would be paid for
# a run every night, for nothing.
MAX_ATTEMPTS = 3

# Order in which the problems are given: a broken link misleads readers right away, an
# isolated page is only hard to find, form comes last.
RANK = {"broken_link": 0, "not_in_index": 1, "orphan": 2, "untitled": 3, "empty": 4}


@dataclass(frozen=True)
class Problem:
    """A problem in a page. `detail`: what identifies it in the page (the link as written,
    the index concerned); `suggestion`: a possible fix (closest file of the same name)."""

    source: str
    kind: str
    page: str  # relative to content/
    detail: str = ""
    suggestion: str = ""

    @property
    def key(self) -> str:
        """Identity of the problem, also the primary key of its mark: a digest, since a link
        as written and a page's path have no length limit, while an index entry has one (a
        longer key would make every run that leaves the problem fail, night after night)."""
        identity = "\0".join((self.source, self.kind, self.page, self.detail))
        return hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()

    def describe(self) -> str:
        """The problem in words (English): for the agent, and for the log (`localize`)."""
        if self.kind == "broken_link":
            if self.suggestion:
                return f"broken link {self.detail} (did you mean {self.suggestion}?)"
            return f"broken link {self.detail}"
        if self.kind == "not_in_index":
            return f"missing from the folder index {self.detail}"
        return {"orphan": "no incoming link", "untitled": "no title", "empty": "empty page"}.get(
            self.kind, self.detail or self.kind
        )


@dataclass
class Window:
    """What people changed between the watermark and the head. Paths relative to content/."""

    repo: Repository
    since: str
    view: Overlay  # the wiki at the head
    created: set[str] = field(default_factory=set)  # new pages, still there
    touched: set[str] = field(default_factory=set)  # created, modified or moved here, still there
    gone: set[str] = field(default_factory=set)  # deleted, or the old path of a move
    origins: dict[str, str] = field(default_factory=dict)  # moved page → path at its baseline
    # Page last written by an import (`changes.bulk`) → that commit: its problems are the
    # import's, reviewed as a whole by the tidy-up; only what people did after counts.
    baselines: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.touched or self.gone)


class Source(Protocol):
    """A source of problems (extension point)."""

    name: str

    def introduced(self, window: Window) -> list[Problem]:
        """Problems that appeared in the window through the changes made by people."""
        ...

    def present(self, view: Overlay, problems: list[Problem]) -> set[str]:
        """Keys of those problems (from this source) still there in `view`."""
        ...

    def reintroduced(
        self, repo: Repository, touches: list["Touch"], problems: list[Problem]
    ) -> set[str]:
        """Keys of those problems (left by the agent) that people introduced again in the
        commits `touches`, made after the decision. Editing their page is not enough."""
        ...


@dataclass(frozen=True)
class Touch:
    """A page (relative to content/) touched by a commit of people (status A, M or R)."""

    commit: str
    status: str
    page: str


def _rel(path: str) -> str:
    return path.removeprefix(PREFIX)


def _is_page(path: str) -> bool:
    return path.endswith(health.MARKDOWN)


def _index_of(page: str) -> str | None:
    """Index that must list a page: its folder's README; for a README, the parent folder's
    (none for the root one). Same rule as the run metrics."""
    folder = posixpath.dirname(page)
    if posixpath.basename(page) == INDEX:
        if not folder:
            return None
        folder = posixpath.dirname(folder)
    return posixpath.join(folder, INDEX) if folder else INDEX


def _stem(path: str) -> str:
    return posixpath.basename(path.strip().strip("/")).lower().removesuffix(".md")


class WikiChecks:
    """The checks of the health tab and of the run metrics, limited to what people changed:
    broken links introduced in the pages they touched (or that pointed to a page they deleted
    or moved), and their new pages isolated, missing from their folder's index (when the
    folder has one), untitled or empty."""

    name = "wiki"

    def introduced(self, window: Window) -> list[Problem]:
        view = window.view
        found: list[Problem] = []
        current = {_rel(p) for p in view.current() if p.startswith(PREFIX)}
        links = view.links()
        candidates = {p for p in window.touched if _is_page(p)}
        gone_stems = {_stem(g) for g in window.gone}
        if window.gone:  # pages left as they were, whose target went away
            candidates |= {_rel(src) for src, targets in links.items() if targets & window.gone}
            try:
                before = links_service.base_links(
                    window.repo,
                    window.since,
                    {b.path for b in window.repo.files(CONTENT_DIR, window.since)},
                )
            except GitError:
                before = {}
            candidates |= {
                _rel(src)
                for src, targets in before.items()
                if targets & window.gone and _rel(src) in current
            }
        after_names = view.names()
        pages = sorted(p for p in candidates if p in current and _is_page(p))
        texts = _read(view, pages)
        # State each page is compared with: the watermark, or the import that last wrote it.
        befores: dict[str, tuple[Overlay, dict[str, bytes]]] = {}
        for commit in {window.baselines.get(p, window.since) for p in pages}:
            before_view = Overlay(window.repo, commit)
            group = [p for p in pages if window.baselines.get(p, window.since) == commit]
            befores[commit] = (
                before_view,
                _read(before_view, [window.origins.get(p, p) for p in group]),
            )
        names_at = {commit: v.names() for commit, (v, _) in befores.items()}
        for page in pages:
            text = _text(texts.get(page))
            origin = window.origins.get(page, page)
            commit = window.baselines.get(page, window.since)
            olds = befores[commit][1]
            known: set[str] = set()
            if origin in olds:  # the page existed at its baseline
                known = _broken_keys(_text(olds[origin]), origin, names_at[commit])
            targets = link_targets(text, page, after_names)
            keys = {w: t or w for w, t in targets}
            for written, suggestion in broken_among(targets, page, after_names):
                key = keys.get(written, written)
                if key in known:
                    continue  # already broken before: not introduced
                if page not in window.touched and not (
                    key in window.gone
                    or (written.startswith("[[") and _stem(written[2:-2]) in gone_stems)
                ):
                    continue  # broken by someone else than people (the tidy-up): not ours
                found.append(Problem(self.name, "broken_link", page, written, suggestion or ""))
        found += self._form(view, sorted(p for p in window.created if _is_page(p)))
        return found

    def _form(self, view: Overlay, pages: list[str]) -> list[Problem]:
        """Isolated, not indexed, untitled or empty, among new pages."""
        if not pages:
            return []
        links = view.links()
        incoming: set[str] = set()
        for src, targets in links.items():
            incoming |= {t for t in targets if t != _rel(src)}
        found = []
        current = view.current()
        contents = _read(view, pages)
        for page in pages:
            data = contents.get(page)
            if data is None:
                continue
            index = _index_of(page)
            listed = index is None or PREFIX + index not in current
            if not listed and index is not None:
                listed = page in links.get(PREFIX + index, set())
            if not listed:
                found.append(Problem(self.name, "not_in_index", page, str(index)))
            elif page not in incoming and posixpath.basename(page).lower() not in {
                n.lower() for n in FOLDER_INDEXES
            }:
                found.append(Problem(self.name, "orphan", page))
            if len(data) <= health.MAX_PAGE:
                empty, titled = health.page_form(_text(data))
                if empty:
                    found.append(Problem(self.name, "empty", page))
                elif not titled:
                    found.append(Problem(self.name, "untitled", page))
        return found

    def present(self, view: Overlay, problems: list[Problem]) -> set[str]:
        still: set[str] = set()
        names = view.names()
        form = [p for p in problems if p.kind != "broken_link"]
        if form:
            now = {p.key for p in self._form(view, sorted({p.page for p in form}))}
            still |= {p.key for p in form if p.key in now}
        links = [p for p in problems if p.kind == "broken_link"]
        contents = _read(view, {p.page for p in links})
        for problem in links:
            data = contents.get(problem.page)
            if data is None:
                continue
            broken = {w for w, _ in broken_links(_text(data), problem.page, names)}
            if problem.detail in broken:
                still.add(problem.key)
        return still

    def reintroduced(
        self, repo: Repository, touches: list[Touch], problems: list[Problem]
    ) -> set[str]:
        """A page created again (deleted, then written anew) brings its form problems back; a
        commit that adds a copy of a broken link (more occurrences of it in the page than
        before the commit) brings that link back. Rewording the line that holds the link, or
        writing elsewhere in the page (a log edited every day), does not."""
        found: set[str] = set()
        by_page: dict[str, list[Touch]] = {}
        for touch in touches:
            by_page.setdefault(touch.page, []).append(touch)
        for problem in problems:
            mine = by_page.get(problem.page, [])
            if problem.kind != "broken_link":
                if any(t.status == "A" for t in mine):
                    found.add(problem.key)
                continue
            for touch in mine:
                after = repo.blob(PREFIX + problem.page, touch.commit)
                before = (
                    None
                    if touch.status != "M"
                    else repo.blob(PREFIX + problem.page, f"{touch.commit}^")
                )
                if _count(after, problem) > _count(before, problem):
                    found.add(problem.key)
                    break
        return found


_NO_INDEX = WikiIndex()  # wiki links counted as written, without resolving them


def _count(data: bytes | None, problem: Problem) -> int:
    """Occurrences of a link, as written, in a page."""
    if data is None:
        return 0
    targets = link_targets(_text(data), problem.page, _NO_INDEX)
    return sum(1 for written, _ in targets if written == problem.detail)


SOURCES: list[Source] = [WikiChecks()]


def _source(name: str) -> Source | None:
    return next((s for s in SOURCES if s.name == name), None)


def _read(view: Overlay, pages: Iterable[str]) -> dict[str, bytes]:
    """Contents of pages (relative to content/) in `view`, the missing ones left out. In one
    Git call: one process per page would make the nightly computation of a window of
    thousands of pages, done again every night until its problems are all given, last
    minutes."""
    found: dict[str, bytes] = {}
    stored: list[str] = []
    for page in set(pages):
        full = PREFIX + page
        if full in view.changes:
            if (data := view.changes[full]) is not None:
                found[page] = data
        elif view.existed(full):
            stored.append(full)
    batch = view.repo.read_many(sorted(stored), view.base)
    for full in stored:
        data = batch[full] if full in batch else view.read(full)  # a name Git cannot batch
        if data is not None:
            found[_rel(full)] = data
    return found


def _text(data: bytes | None) -> str:
    return data.decode("utf-8", "replace") if data is not None else ""


def counted(commit: Commit, own_email: str) -> Callable[[str], bool] | None:
    """Which paths of a commit are people's changes: all of a person's commit; none of
    Synaptane's (None); of a routine's commit, those in the folders it keeps, named by its
    trailers (`routine_writes.commit_trailers`)."""
    if commit.author.email != own_email:
        return lambda path: True
    folders = kept_folders(commit.source, commit.via)
    if folders is None or not is_routine_agent(commit.author.name):
        return None
    prefixes = tuple(f"{PREFIX}{f}/" for f in folders)
    return lambda path: path.startswith(prefixes)


def window(repo: Repository, since: str, view: Overlay, own_email: str) -> Window:
    """Pages people changed from `since` to the view's commit (Synaptane's commits aside,
    except the pages a routine keeps: `counted`)."""
    found = Window(repo, since, view)
    for commit, entries in repo.changes_since(since, CONTENT_DIR, until=view.base):
        keep = counted(commit, own_email)
        if keep is None:
            continue
        if changes.bulk(entries):
            _imported(found, commit.sha, entries)
            continue
        for status, path, old in entries:
            if not path.startswith(PREFIX) or not keep(path):
                continue
            here = _rel(path)
            if status == "D":
                found.gone.add(here)
                found.created.discard(here)
                found.touched.discard(here)
                found.baselines.pop(here, None)
                continue
            if status == "R" and old:
                before = _rel(old)
                found.gone.add(before)
                if before in found.baselines:
                    found.baselines[here] = found.baselines.pop(before)
                origin = found.origins.pop(before, before)
                if before in found.created:
                    found.created.discard(before)
                    found.created.add(here)
                else:
                    found.origins[here] = origin
                found.touched.discard(before)
            elif status == "A":
                found.created.add(here)
                found.gone.discard(here)
                found.baselines.pop(here, None)
            found.touched.add(here)
    found.gone -= found.touched
    return found


def _imported(found: Window, commit: str, entries: list[tuple[str, str, str]]) -> None:
    """An import (or bulk reorganization) commit: what it wrote becomes the baseline of those
    pages, nothing of it counts as people's change."""
    for status, path, old in entries:
        if not path.startswith(PREFIX):
            continue
        here = _rel(path)
        if old and status == "R":
            before = _rel(old)
            found.created.discard(before)
            found.touched.discard(before)
            found.baselines.pop(before, None)
            found.origins.pop(before, None)
        found.created.discard(here)
        found.touched.discard(here)
        found.origins.pop(here, None)
        if status == "D":
            found.baselines.pop(here, None)
        else:
            found.baselines[here] = commit


@dataclass
class Found:
    """Problems to give to the agent in this run."""

    problems: list[Problem]
    waiting: int  # problems waiting in all (the ones given included)
    watermark: str  # read before the run: moved after it only if it has not changed
    complete: bool  # every problem of the window is given: the watermark may move after
    attempts: int = 0  # runs interrupted in a row before (`MAX_ATTEMPTS`)

    @property
    def item(self) -> InboxItem:
        return InboxItem(
            path="",
            type=TYPE,
            title=Label("Wiki maintenance ({count})", count=len(self.problems)),
            deposited_by="",
            deposited_at=datetime.now(UTC).isoformat(timespec="seconds"),
            source="synaptane",
            status="pending",
            item_name=NAME,
        )

    def describe(self) -> str:
        """Text of the item for the agent: the problems, numbered (P1, P2…)."""
        lines = [
            f"# Wiki maintenance: {len(self.problems)} problem(s)",
            "",
            "Problems that appeared since the last tidy-up run, in pages changed by people "
            "(edits, pushes). Found by a check without AI.",
            "",
        ]
        for number, problem in enumerate(self.problems, start=1):
            lines.append(f"- P{number} — {problem.page}: {problem.describe()}")
        if self.waiting > len(self.problems):
            lines += ["", f"{self.waiting - len(self.problems)} more will come in the next runs."]
        return "\n".join(lines)


def _move(session: Session, space: Space, old: str | None, new: str) -> bool:
    """Moves the watermark from `old` to `new`, only if it is still `old` (a sync or another
    run may have moved it in the meantime)."""
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Space)
            .where(Space.id == space.id, Space.maintain_watermark.is_not_distinct_from(old))
            .values(maintain_watermark=new)
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        return False
    set_committed_value(space, "maintain_watermark", new)
    return True


def start(session: Session, space: Space, head: str | None) -> None:
    """Starting point, set by the sync when there is none (a space never synchronized at the
    upgrade or when turned on): its first head, so that nothing older is caught up."""
    if head and space.tidy_maintain and not space.maintain_watermark:
        _move(session, space, None, head)


def _problem(mark: MaintenanceMark) -> Problem:
    return Problem(mark.source, mark.kind, mark.page, mark.detail)


def pending(session: Session, ws: Workspace, space: Space, view: Overlay) -> Found | None:
    """Problems to give to the agent at the view's commit (the run's start), or None.

    Moves the watermark to the head right away when its window holds nothing to give: nothing
    is lost, and the next window stays short."""
    repo, head = view.repo, view.base
    watermark, attempts = session.execute(
        select(Space.maintain_watermark, Space.maintain_attempts).where(Space.id == space.id)
    ).one()
    if not watermark or repo.rev(watermark) is None or not repo.is_ancestor(watermark, head):
        _move(session, space, watermark, head)  # first time, or history rewritten
        return None
    candidates: dict[str, Problem] = {}
    if watermark != head:
        changed = window(repo, watermark, view, ws.committer.email)
        if not changed.empty:
            for source in SOURCES:
                for problem in source.introduced(changed):
                    candidates.setdefault(problem.key, problem)
    marks = {
        m.key: m
        for m in session.scalars(
            select(MaintenanceMark).where(MaintenanceMark.space_id == space.id)
        )
    }
    current = {_rel(p) for p in view.current() if p.startswith(PREFIX)}
    # Left problems: forgotten once gone (so one that comes back later is new), given again
    # only if people introduced them again since the decision; an edit of the page is not
    # enough, or a page edited every day would cost a run every night.
    standing: list[MaintenanceMark] = []
    by_source: dict[str, list[MaintenanceMark]] = {}
    for mark in marks.values():
        by_source.setdefault(mark.source, []).append(mark)
    for name, group in by_source.items():
        checker = _source(name)
        alive = [m for m in group if m.page in current]
        still = checker.present(view, [_problem(m) for m in alive]) if checker else set()
        for mark in group:
            if mark.key not in still:
                session.delete(mark)
            else:
                standing.append(mark)
    back = _reintroduced(repo, head, ws.committer.email, standing)
    again = [_problem(m) for m in standing if m.key in back and m.key not in candidates]
    fresh = [p for p in candidates.values() if p.key not in marks or p.key in back]
    if not fresh and watermark != head:
        _move(session, space, watermark, head)
        watermark = head
    everything = sorted(fresh + again, key=lambda p: (RANK.get(p.kind, 9), p.page, p.detail))
    given = everything[:MAX_PROBLEMS]
    if not given:
        return None
    keys = {p.key for p in given}
    return Found(given, len(everything), watermark, all(p.key in keys for p in fresh), attempts)


def _reintroduced(
    repo: Repository, head: str, own_email: str, marks: list[MaintenanceMark]
) -> set[str]:
    """Keys of the left problems that people introduced again after their decision: one
    `git log` per decision commit (one per run, in practice), imports aside like elsewhere."""
    found: set[str] = set()
    by_until: dict[str, list[MaintenanceMark]] = {}
    for mark in marks:
        by_until.setdefault(mark.until, []).append(mark)
    for until, group in by_until.items():
        if until == head or repo.rev(until) is None or not repo.is_ancestor(until, head):
            continue  # nothing since, or history rewritten: the decision stands
        pages = {m.page for m in group}
        touches = [
            Touch(commit.sha, status, _rel(path))
            for commit, entries in repo.changes_since(until, CONTENT_DIR, until=head)
            if (keep := counted(commit, own_email)) is not None and not changes.bulk(entries)
            for status, path, _ in entries
            if status in ("A", "M", "R") and _rel(path) in pages and keep(path)
        ]
        if not touches:
            continue
        for name in {m.source for m in group}:
            if (source := _source(name)) is not None:
                problems = [_problem(m) for m in group if m.source == name]
                found |= source.reintroduced(repo, touches, problems)
    return found


@dataclass
class Closure:
    """What became of the problems given to a run."""

    # done or left (the agent closed the item), untreated (run interrupted), given_up (left
    # after MAX_ATTEMPTS interrupted runs in a row)
    status: str
    operations: list[dict[str, object]] = field(default_factory=list)
    fixed: list[Problem] = field(default_factory=list)
    left: list[tuple[Problem, str]] = field(default_factory=list)  # with the reason
    blocked: bool = False  # some problem neither fixed nor decided: the watermark stays

    @property
    def decided(self) -> bool:
        return self.status != "untreated"


_SEPARATORS = (":", "—", "–", "-", ")", ".")  # after "P2"


def reasons(text: str) -> dict[int, str]:
    """Reasons the agent gave per problem ("P2: no page on this topic; P4: …"), by number.

    Plain string handling, no regular expression: the text is the model's."""
    found: dict[int, str] = {}
    for part in text[:4000].replace("\n", ";").split(";"):
        start = next(
            (
                i
                for i, char in enumerate(part)
                if char in "Pp"
                and (i == 0 or not part[i - 1].isalnum())
                and part[i + 1 : i + 2].isdigit()
            ),
            None,
        )
        if start is None:
            continue
        digits = ""
        for char in part[start + 1 : start + 4]:
            if not char.isdigit():
                break
            digits += char
        rest = part[start + 1 + len(digits) :].lstrip()
        if rest[:1] not in _SEPARATORS:
            continue
        if reason := rest[1:].strip():
            found.setdefault(int(digits), reason[:300])
    return found


def close(
    found: Found,
    state: RunState,
    view: Overlay,
    *,
    interruption: str | None = None,
    conflicts: Iterable[str] = (),
) -> Closure:
    """Fixed problems (gone from `view`, the run's final state), left ones (with the reason the
    agent gave for each, otherwise its summary), or untreated (run interrupted, or some write
    left in conflict: given again later; left after MAX_ATTEMPTS interrupted runs in a row).
    Log lines: the page and the problem in words.

    A write dropped by a conflict may have been the fix of a problem in another page (the
    folder's index for a page missing from it): the problems still there are then not taken
    as left, which would remember them as decided and never give them again."""
    if NAME in state.done:
        closure, general = Closure("done"), state.done[NAME]
    elif NAME in state.left:
        closure, general = Closure("left"), state.left[NAME]
    elif found.attempts + 1 >= MAX_ATTEMPTS:
        why = interruption or "item not closed by the agent"
        closure = Closure("given_up")
        general = f"Not processed after {found.attempts + 1} interrupted runs (last one: {why})"
    else:
        closure, general = Closure("untreated"), ""
    stuck = {_rel(p) for p in conflicts}
    still: set[str] = set()
    for name in {p.source for p in found.problems}:
        if (source := _source(name)) is not None:
            still |= source.present(view, [p for p in found.problems if p.source == name])
    given = reasons(general)
    for number, problem in enumerate(found.problems, start=1):
        if problem.key not in still and problem.page not in stuck:
            closure.fixed.append(problem)
            closure.operations.append(
                {"op": Op.FIXED, "path": problem.page, "detail": problem.describe()}
            )
        elif not closure.decided or stuck:
            closure.blocked = True
        else:
            reason = given.get(number) or general or "not fixed by the agent"
            closure.left.append((problem, reason))
            closure.operations.append(
                {
                    "op": Op.PROBLEM_LEFT,
                    "path": problem.page,
                    "detail": f"{problem.describe()} — {reason}",
                }
            )
    if not closure.decided:
        closure.operations.append(
            {
                "op": Op.UNTREATED,
                "path": str(found.item.title),
                "detail": (
                    f"{interruption or 'item not closed by the agent'} "
                    f"(attempt {found.attempts + 1})"
                ),
            }
        )
    return closure


def settle(
    session: Session,
    ws: Workspace,
    space: Space,
    found: Found,
    closure: Closure,
    base: str,
    commit: str | None,
) -> None:
    """After the run: the left problems are remembered, and the watermark moves on if every
    problem of its window was given and decided.

    It moves to the run's commit, so that what the run itself changed is never counted; but to
    its start if people committed during the run, so that their changes are not skipped.
    An interrupted run is counted, up to MAX_ATTEMPTS (`close`)."""
    if not closure.decided:
        space.maintain_attempts = found.attempts + 1
        return
    space.maintain_attempts = 0
    repo = ws.local_repository(space)
    at = commit or base
    for problem, reason in closure.left:  # decided on the run's starting state
        mark = session.get(MaintenanceMark, (space.id, problem.key))
        if mark is None:
            mark = MaintenanceMark(space_id=space.id, key=problem.key)
            session.add(mark)
        mark.source, mark.kind, mark.page = problem.source, problem.kind, problem.page
        mark.detail, mark.reason = problem.detail, " ".join(reason.split())[:500]
        mark.until = base
        mark.decided_at = datetime.now(UTC)
    if not found.complete or closure.blocked:
        return
    target = at
    if commit:
        try:
            during = repo.changes_since(base, CONTENT_DIR, until=commit)
        except GitError:
            during = []
        if any(c.author.email != ws.committer.email for c, _ in during):
            target = base
    _move(session, space, found.watermark, target)


def turned_on(space: Space) -> None:
    """Maintenance turned on again: it starts from the last synchronized head, without catching
    up on the time it was off. Not from the next sync: the edit that triggers it (every web
    edit does) would be taken for the past. Never synchronized: from the first sync."""
    space.maintain_watermark = space.remote_head
    space.maintain_attempts = 0


# --- Log: the problems in the reader's language ----------------------------------------------

_SEPARATOR = " — "


def localize(text: str, translate: Callable[..., str]) -> str:
    """A text of a tidy-up log written by this module (problem in words, maintenance item's
    title), translated; the agent's reason after " — " as it is; any other text as it is."""
    if text.startswith("Wiki maintenance (") and text.endswith(")"):
        count = text[len("Wiki maintenance (") : -1]
        if count.isdigit():
            return translate("Wiki maintenance ({count})", count=count)
        return text
    phrase, separator, reason = text.partition(_SEPARATOR)
    words: str | None = None
    if phrase.startswith("broken link "):
        link = phrase[len("broken link ") :]
        target, mean, suggestion = link.partition(" (did you mean ")
        if mean and suggestion.endswith("?)"):
            words = translate(
                "broken link {link} (did you mean {suggestion}?)",
                link=target,
                suggestion=suggestion[:-2],
            )
        else:
            words = translate("broken link {link}", link=link)
    elif phrase.startswith("missing from the folder index "):
        words = translate(
            "missing from the folder index {index}",
            index=phrase[len("missing from the folder index ") :],
        )
    elif phrase == "no incoming link":
        words = translate("no incoming link")
    elif phrase == "no title":
        words = translate("no title")
    elif phrase == "empty page":
        words = translate("empty page")
    if words is None:
        return text
    return words + separator + reason
