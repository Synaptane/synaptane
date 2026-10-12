"""Changes to `content/` for the tidy run to review, computed from Git.

Nothing is written to the inbox for a change: a web, REST or MCP write, like a push from
elsewhere, is already a commit. Commits are listed since a watermark (`Space.change_watermark`),
except Synaptane's own (tidy, merges, skeleton), grouped by page. Changes that only touch
whitespace or blank lines, or only a page's verification fields (`services.freshness`), are
ignored.

The agent closes a page (`done`) or leaves it (`left`, with a reason): the decision is recorded
up to a commit (`ChangeMark.until`). A more recent change to the page brings it back. When
nothing is pending any more, the watermark moves forward and closed decisions are erased; left
pages stay listed until they are closed.
"""

import hashlib
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from synaptane.core.errors import Label, SynaptaneError
from synaptane.git.repo import Commit, GitError, Repository
from synaptane.services import freshness
from synaptane.services.inbox import CONTENT_DIR, InboxItem
from synaptane.services.workspace import Workspace
from synaptane.storage.models import ChangeMark, Space

PREFIX = CONTENT_DIR + "/"
MAX_DIFF = 12_000  # diff characters shown to the agent per page
BULK = 20  # pages touched by one commit beyond which it is an import: a single item
IMPORT = "@import/"  # fake path of an "import" item


@dataclass
class Change:
    path: str  # relative to content/ (or IMPORT + commit for a bulk import)
    status: str  # created, modified, deleted, moved, import
    previous: str = ""  # old path (move)
    commits: list[Commit] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)  # import: pages touched

    @property
    def last(self) -> str:
        return self.commits[-1].sha

    @property
    def name(self) -> str:
        return "change-" + hashlib.sha1(self.path.encode()).hexdigest()[:12]


def _relative(path: str) -> str:
    return path[len(PREFIX) :] if path.startswith(PREFIX) else path


def collect(repo: Repository, since: str, own_email: str) -> dict[str, Change]:
    """Pages changed since `since`, except Synaptane's commits (author `own_email`)."""
    found: dict[str, Change] = {}
    for commit, entries in repo.changes_since(since, CONTENT_DIR):
        if commit.author.email == own_email:
            continue
        touched = [path for _, path, _ in entries if path.startswith(PREFIX)]
        if len(touched) > BULK:  # bulk import: one item, not one per page
            key = IMPORT + commit.sha[:12]
            found[key] = Change(
                key, "import", commits=[commit], pages=[_relative(p) for p in touched]
            )
            continue
        for status, path, old in entries:
            if not path.startswith(PREFIX):
                continue
            relative = _relative(path)
            change = found.pop(_relative(old), None) if old else None
            change = change or found.get(relative) or Change(relative, "modified")
            change.path = relative
            if status == "D" and change.status == "created":
                found.pop(relative, None)  # created then deleted: nothing to review
                continue
            if status == "A":
                change.status = "created" if change.status != "deleted" else "modified"
            elif status == "D":
                change.status = "deleted"
            elif status == "R":
                change.status, change.previous = "moved", change.previous or _relative(old)
            elif change.status not in ("created", "moved"):
                change.status = "modified"
            change.commits.append(commit)
            found[relative] = change
    return found


def _repo(ws: Workspace, space: Space) -> Repository | None:
    try:
        return ws.local_repository(space)
    except SynaptaneError:
        return None


_TITLES = (
    "Page created: {path}",
    "Page deleted: {path}",
    "Page moved: {path}",
    "Page changed: {path}",
)
_TITLE = re.compile(r"^Page (?:created|deleted|moved|changed): (?P<path>.*)$", re.S)
_IMPORT = re.compile(r"^Import: (?P<count>\d+) pages \((?P<subject>.*)\)$", re.S)


def localize_title(text: str, translate: Callable[..., str]) -> str:
    """The title of a change as stored in a tidy-up log (English text of `_item`), translated;
    any other text (a note's title, a title stored in French) as it is."""
    if match := _TITLE.match(text):
        verb = text.split(":", 1)[0].removeprefix("Page ")
        return translate(f"Page {verb}: {{path}}", path=match["path"])
    if match := _IMPORT.match(text):
        return translate("Import: {count} pages ({subject})", **match.groupdict())
    return text


def _item(change: Change, status: str = "pending", reason: str = "") -> InboxItem:
    authors = sorted({c.author.name for c in change.commits})
    # Computed on each read, never stored: a text to translate (`Label`), English as is.
    template = {"created": _TITLES[0], "deleted": _TITLES[1], "moved": _TITLES[2]}.get(
        change.status, _TITLES[3]
    )
    title = Label(template, path=change.path)
    if change.status == "import":
        subject = change.commits[0].subject if change.commits else ""
        title = Label(
            "Import: {count} pages ({subject})", count=len(change.pages), subject=subject[:80]
        )
    return InboxItem(
        path="",
        type="change",
        title=title,
        deposited_by=", ".join(authors),
        deposited_at=change.commits[-1].date if change.commits else "",
        source="git",
        status=status,
        reason=reason,
        hint=change.path,
        item_name=change.name,
    )


_CACHE: OrderedDict[tuple[object, ...], list[tuple[InboxItem, Change]]] = OrderedDict()
_CACHE_SIZE = 64


def pending(session: Session, ws: Workspace, space: Space) -> list[tuple[InboxItem, Change]]:
    """Changes to review (new ones, then left ones), with their inbox items.

    Computed in bulk (one `git log`, one `git diff` and one `rev-list`, whatever the number of
    pages and decisions), cached as long as the head, the watermark and the decisions do not
    change."""
    repo = _repo(ws, space)
    # Read from the database, not the object: synchronization may have moved it meanwhile.
    watermark = session.scalar(select(Space.change_watermark).where(Space.id == space.id))
    if repo is None or not watermark:
        return []
    head = repo.head()
    if head is None or repo.rev(watermark) is None:  # history rewritten (force-push)
        return []
    marks = {
        m.path: m
        for m in session.scalars(select(ChangeMark).where(ChangeMark.space_id == space.id))
    }
    key = (
        space.id,
        head,
        watermark,
        tuple(sorted((m.path, m.until, m.status, m.reason) for m in marks.values())),
    )
    if key in _CACHE:
        _CACHE.move_to_end(key)
    else:
        _CACHE[key] = _compute(repo, ws, watermark, head, marks)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return [(replace(item), change) for item, change in _CACHE[key]]


def _compute(
    repo: Repository, ws: Workspace, watermark: str, head: str, marks: dict[str, ChangeMark]
) -> list[tuple[InboxItem, Change]]:
    changes = collect(repo, watermark, ws.committer.email)
    if not changes:
        return [
            (_left(mark), Change(mark.path, str(mark.info.get("status", "modified"))))
            for mark in marks.values()
            if mark.status == "left"
        ]
    real = repo.real_changes(watermark, head, CONTENT_DIR)
    graph = _Ancestry(repo.ancestry(watermark, head)) if marks else None
    checked = _verification_only(repo, watermark, head, changes, real)
    fresh, left = [], []
    for path, change in changes.items():
        mark = marks.pop(path, None)
        if mark is not None and mark.status == "pending":
            mark = None  # run interrupted before a decision: still to review
        if mark is not None and graph is not None and graph.reaches(mark.until, change.last):
            if mark.status == "left":
                left.append((_item(change, "left", mark.reason), change))
            continue  # already reviewed, nothing new since
        if change.status == "modified" and PREFIX + path not in real:
            continue  # whitespace and blank lines only
        if path in checked:  # verification only (owner, date, interval)
            if mark is not None and mark.status == "left":  # left before: stays left
                left.append((_left(mark), Change(path, str(mark.info.get("status", "modified")))))
            continue
        fresh.append((_item(change), change))
    for mark in marks.values():  # left, unchanged since the watermark
        if mark.status == "left":
            left.append((_left(mark), Change(mark.path, str(mark.info.get("status", "modified")))))
    return fresh + left


def _verification_only(
    repo: Repository, watermark: str, head: str, changes: dict[str, Change], real: set[str]
) -> set[str]:
    """Changed pages where only the verification fields of the front matter changed since the
    watermark ("Mark as verified", or the same thing pushed from Git).

    The criterion is on the content, not on the commit's author or message: nothing else can
    slip through, and an edit followed by a verification is still to review. Two grouped
    reads, whatever the number of pages."""
    wanted = [
        PREFIX + path
        for path, change in changes.items()
        if change.status == "modified" and PREFIX + path in real and freshness.is_page(path)
    ]
    if not wanted:
        return set()
    before, after = repo.read_many(wanted, watermark), repo.read_many(wanted, head)
    found = set()
    for full in wanted:
        old, new = before.get(full), after.get(full)
        if old is None or new is None or max(len(old), len(new)) > freshness.MAX_PAGE:
            continue
        if freshness.same_but_verification(
            old.decode("utf-8", "replace"), new.decode("utf-8", "replace")
        ):
            found.add(_relative(full))
    return found


class _Ancestry:
    """Graph of the commits since the watermark, read in a single `git rev-list`: tells whether
    a decision (`until`) covers the last commit of a page, without one Git call per decision.

    Topological order: an ancestor always comes after its descendants. A commit placed before
    `until` is therefore never one of its ancestors (page changed after the decision), and the
    walk does not go past the target."""

    def __init__(self, commits: list[tuple[str, tuple[str, ...]]]) -> None:
        self.parents = dict(commits)
        self.order = {sha: position for position, (sha, _) in enumerate(commits)}

    def reaches(self, until: str, target: str) -> bool:
        """True if `target` is `until` or one of its ancestors more recent than the watermark."""
        start, goal = self.order.get(until), self.order.get(target)
        if start is None or goal is None or goal < start:
            return False
        seen, stack = {until}, [until]
        while stack:
            sha = stack.pop()
            if sha == target:
                return True
            for parent in self.parents[sha]:
                position = self.order.get(parent)  # missing: the watermark or older
                if position is not None and position <= goal and parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        return False


def _left(mark: ChangeMark) -> InboxItem:
    item = _item(Change(mark.path, str(mark.info.get("status", "modified"))), "left", mark.reason)
    item.deposited_by = str(mark.info.get("authors", ""))
    item.deposited_at = str(mark.info.get("date", ""))
    return item


def describe(repo: Repository, space: Space, change: Change) -> str:
    """Text presented to the agent: commits, then cumulative diff (truncated)."""
    if change.status == "import":
        listing = "\n".join(f"- {p}" for p in change.pages[:200])
        more = f"\n… and {len(change.pages) - 200} more." if len(change.pages) > 200 else ""
        return (
            f"# {_item(change).title}\n\nA single commit touched {len(change.pages)} pages: "
            "a bulk import or reorganization. Do not reread every page: check the overall "
            "organization (folders, landing pages, links, obvious duplicates) and fix it if "
            f"needed, then close the item.\n\nPages:\n{listing}{more}\n"
        )
    lines = [f"# {_item(change).title}", ""]
    if change.previous:
        lines += [f"Previous location: {change.previous}", ""]
    for commit in change.commits[-20:]:
        lines.append(f"- {commit.date[:16]} {commit.author.name}: {commit.subject}")
    if change.commits:
        try:
            diff = repo.compare_range(
                f"{change.commits[0].sha}~1", change.last, PREFIX + change.path
            )
        except GitError:
            diff = ""
        if len(diff) > MAX_DIFF:
            diff = diff[:MAX_DIFF] + "\n[… diff truncated: read the page with read_page.]"
        lines += ["", "Diff since the last tidy run:", "", "```diff", diff or "(empty)", "```"]
    lines += [
        "",
        "The change is already made. Check the location, the title, the links, duplicates and "
        "consistency with neighbouring pages; fix if needed, otherwise close the item.",
    ]
    return "\n".join(lines)


def record(
    session: Session, space: Space, decisions: dict[str, tuple[str, str, Change]], until: str
) -> None:
    """Records the agent's decisions: path → (done or left, reason, change).

    `pending` is not a decision: the run stopped before; the page is still to review and the
    number of interrupted runs is counted (`attempts`)."""
    for path, (status, reason, change) in decisions.items():
        mark = session.get(ChangeMark, (space.id, path))
        previous = attempts_of(mark)
        if mark is None:
            mark = ChangeMark(space_id=space.id, path=path, until=until, status=status)
            session.add(mark)
        mark.until, mark.status, mark.reason = until, status, reason
        mark.info = {
            "status": change.status,
            "authors": ", ".join(sorted({c.author.name for c in change.commits})),
            "date": change.commits[-1].date if change.commits else "",
        }
        if status == "pending":
            mark.info["attempts"] = previous + 1
        mark.updated_at = datetime.now(UTC)


def _move_watermark(session: Session, space: Space, old: str | None, new: str) -> bool:
    """Moves the watermark from `old` to `new`, only if it is still `old` in the database.

    The watermark is written outside the space lock, by synchronization and by the tidy run:
    without this comparison, a run ending after a force-push would put back a watermark from
    before the rewrite. False if someone moved it in the meantime."""
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Space)
            .where(Space.id == space.id, Space.change_watermark.is_not_distinct_from(old))
            .values(change_watermark=new)
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        return False
    set_committed_value(space, "change_watermark", new)
    return True


def attempts_of(mark: ChangeMark | None) -> int:
    """Runs interrupted before a decision on this page (0 without a `pending` mark)."""
    if mark is None or mark.status != "pending":
        return 0
    value = mark.info.get("attempts", 0)
    return value if isinstance(value, int) else 0


def attempts(session: Session, space: Space, path: str) -> int:
    return attempts_of(session.get(ChangeMark, (space.id, path)))


def compact(session: Session, ws: Workspace, space: Space) -> None:
    """Moves the watermark forward when no new change is waiting any more."""
    repo = _repo(ws, space)
    head = repo.head() if repo else None
    if repo is None or head is None:
        return
    old = session.scalar(select(Space.change_watermark).where(Space.id == space.id))
    if any(item.status != "left" for item, _ in pending(session, ws, space)):
        return
    if not _move_watermark(session, space, old, head):
        return  # moved in the meantime (history rewritten): the computation above is stale
    session.execute(
        delete(ChangeMark).where(
            ChangeMark.space_id == space.id, ChangeMark.status.in_(("done", "pending"))
        )
    )


def start(session: Session, space: Space, head: str | None) -> None:
    """First watermark, on the first clone: the existing history is not "new"."""
    if head and not space.change_watermark:
        _move_watermark(session, space, None, head)


def reset(session: Session, space: Space, watermark: str | None) -> None:
    """History changed (other repository, force-push): new watermark, decisions forgotten.

    Without comparison: synchronization is authoritative on the history, and a tidy run ending
    afterwards can no longer move the watermark (see `_move_watermark`)."""
    space.change_watermark = watermark
    session.execute(delete(ChangeMark).where(ChangeMark.space_id == space.id))


def survives(repo: Repository, watermark: str | None) -> bool:
    """True if the watermark (and thus the decisions made since) still holds in the copy's
    history: same repository under another address (https → ssh), branch that contains it."""
    head = repo.head()
    return bool(watermark and head and repo.rev(watermark) and repo.is_ancestor(watermark, head))
