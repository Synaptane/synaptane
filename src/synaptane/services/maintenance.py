"""Nightly wiki maintenance: the problems people introduced since the last tidy-up run.

The health check (`services.health`) sees broken links and isolated, empty or untitled pages;
the run metrics (`services.tidy_metrics`) see pages missing from their folder's index. Yet
nobody fixed them as long as no note arrived. So the scheduled run (and "Tidy up now")
computes, without AI, the problems that appeared since the last run through changes made by
people (web, REST, MCP, imports, pushes), and gives them to the agent as one synthetic item,
on the model of the "Import: N pages" item: the run happens even with an empty inbox. The
agent fixes what is safe and leaves the rest, with a reason.

No flood, no repetition:
- the problems are counted from a watermark (`Space.maintain_watermark`): the head at the end
  of the last run that was given all of them, or the head when maintenance was turned on (or
  at the upgrade). The past is never caught up;
- only the commits of people count: Synaptane's own (tidy-up, routines…) are left out, since
  the tidy-up already fixes what it breaks (its end-of-run check);
- at most MAX_PROBLEMS per night; the others wait for the next nights (the watermark only moves
  once every problem of its window was given);
- a problem the agent leaves is remembered with its reason (`MaintenanceMark`), bound to the
  content of its page, as `services.verdicts` does for routines: it comes back only once the
  page has changed, and is forgotten once the problem has gone.

Nothing is computed without an AI provider, nor once the month's budget is used up: the
caller (`tidy.tidy_space`) only asks then.

Extension point: `SOURCES`, the sources of problems. The built-in one (`WikiChecks`) reports
what the health check would; another source (the team's own checks, for one) only has to
return `Problem`s for a `Window` and say which of them are still there.
"""

import posixpath
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from synaptane.core.errors import Label
from synaptane.core.markdown import broken_links, link_targets
from synaptane.git.repo import GitError, Repository
from synaptane.services import health
from synaptane.services import links as links_service
from synaptane.services.inbox import CONTENT_DIR, InboxItem
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
# the next morning. A normal day of edits introduces fewer; an import waits a few nights.
MAX_PROBLEMS = 10

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
        return f"{self.source}:{self.kind}:{self.page}:{self.detail}"

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
    origins: dict[str, str] = field(default_factory=dict)  # moved page → path at `since`

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
        before_view = Overlay(window.repo, window.since)
        before_names, after_names = before_view.names(), view.names()
        for page in sorted(p for p in candidates if p in current and _is_page(p)):
            text = _text(view.read(PREFIX + page))
            origin = window.origins.get(page, page)
            known: set[str] = set()
            if before_view.existed(PREFIX + origin):
                old = _text(before_view.read(PREFIX + origin))
                known = _broken_keys(old, origin, before_names)
            keys = {w: t or w for w, t in link_targets(text, page, after_names)}
            for written, suggestion in broken_links(text, page, after_names):
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
        for page in pages:
            data = view.read(PREFIX + page)
            if data is None:
                continue
            index = _index_of(page)
            listed = index is None or not view.exists(PREFIX + index)
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
            pages = sorted({p.page for p in form if view.exists(PREFIX + p.page)})
            now = {p.key for p in self._form(view, pages)}
            still |= {p.key for p in form if p.key in now}
        for problem in problems:
            if problem.kind != "broken_link":
                continue
            data = view.read(PREFIX + problem.page)
            if data is None:
                continue
            broken = {w for w, _ in broken_links(_text(data), problem.page, names)}
            if problem.detail in broken:
                still.add(problem.key)
        return still


SOURCES: list[Source] = [WikiChecks()]


def _source(name: str) -> Source | None:
    return next((s for s in SOURCES if s.name == name), None)


def _text(data: bytes | None) -> str:
    return data.decode("utf-8", "replace") if data is not None else ""


def window(repo: Repository, since: str, view: Overlay, own_email: str) -> Window:
    """Pages people changed from `since` to the view's commit (Synaptane's commits aside)."""
    found = Window(repo, since, view)
    for commit, entries in repo.changes_since(since, CONTENT_DIR, until=view.base):
        if commit.author.email == own_email:
            continue
        for status, path, old in entries:
            if not path.startswith(PREFIX):
                continue
            here = _rel(path)
            if status == "D":
                found.gone.add(here)
                found.created.discard(here)
                found.touched.discard(here)
                continue
            if status == "R" and old:
                before = _rel(old)
                found.gone.add(before)
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
            found.touched.add(here)
    found.gone -= found.touched
    return found


@dataclass
class Found:
    """Problems to give to the agent in this run."""

    problems: list[Problem]
    waiting: int  # problems waiting in all (the ones given included)
    watermark: str  # read before the run: moved after it only if it has not changed
    complete: bool  # every problem of the window is given: the watermark may move after

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
            "(edits, imports, pushes). Found by a check without AI.",
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
    """Starting point, set by the sync: the head when maintenance starts (at the upgrade, or
    once turned on again), so that nothing older is caught up."""
    if head and space.tidy_maintain and not space.maintain_watermark:
        _move(session, space, None, head)


def _problem(mark: MaintenanceMark) -> Problem:
    return Problem(mark.source, mark.kind, mark.page, mark.detail)


def pending(session: Session, ws: Workspace, space: Space, view: Overlay) -> Found | None:
    """Problems to give to the agent at the view's commit (the run's start), or None.

    Moves the watermark to the head right away when its window holds nothing to give: nothing
    is lost, and the next window stays short."""
    repo, head = view.repo, view.base
    watermark = session.scalar(select(Space.maintain_watermark).where(Space.id == space.id))
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
    shas = {_rel(b.path): b.sha for b in repo.files(CONTENT_DIR, head)}
    # Left problems: forgotten once gone, given again once their page has changed.
    again: list[Problem] = []
    by_source: dict[str, list[MaintenanceMark]] = {}
    for mark in marks.values():
        by_source.setdefault(mark.source, []).append(mark)
    for name, group in by_source.items():
        checker = _source(name)
        alive = [m for m in group if m.page in shas]
        still = checker.present(view, [_problem(m) for m in alive]) if checker else set()
        for mark in group:
            if mark.key not in still:
                session.delete(mark)
            elif shas.get(mark.page) != mark.sha and mark.key not in candidates:
                again.append(_problem(mark))
    fresh = [
        p
        for p in candidates.values()
        if (left := marks.get(p.key)) is None or shas.get(p.page) != left.sha
    ]
    if not fresh and watermark != head:
        _move(session, space, watermark, head)
        watermark = head
    everything = sorted(fresh + again, key=lambda p: (RANK.get(p.kind, 9), p.page, p.detail))
    given = everything[:MAX_PROBLEMS]
    if not given:
        return None
    keys = {p.key for p in given}
    return Found(given, len(everything), watermark, all(p.key in keys for p in fresh))


@dataclass
class Closure:
    """What became of the problems given to a run."""

    status: str  # done or left (the agent closed the item), untreated (run interrupted)
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
    agent gave for each, otherwise its summary), or untreated (run interrupted, page left in
    conflict: given again later). Log lines: the page and the problem in words."""
    if NAME in state.done:
        closure, general = Closure("done"), state.done[NAME]
    elif NAME in state.left:
        closure, general = Closure("left"), state.left[NAME]
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
        elif not closure.decided or problem.page in stuck:
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
                "detail": interruption or "item not closed by the agent",
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
    its start if people committed during the run, so that their changes are not skipped."""
    if not closure.decided:
        return
    repo = ws.local_repository(space)
    at = commit or base
    for problem, reason in closure.left:
        mark = session.get(MaintenanceMark, (space.id, problem.key))
        if mark is None:
            mark = MaintenanceMark(space_id=space.id, key=problem.key)
            session.add(mark)
        mark.source, mark.kind, mark.page = problem.source, problem.kind, problem.page
        mark.detail, mark.reason = problem.detail, " ".join(reason.split())[:500]
        mark.sha = repo.blob_sha(PREFIX + problem.page, at)
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
    """Maintenance turned on again: it starts from the head at the next sync or run, without
    catching up on the time it was off."""
    space.maintain_watermark = None


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
