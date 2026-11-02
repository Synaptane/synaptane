"""What AI assistants wrote in a space, as runs, each undoable page by page.

An assistant connected over MCP writes pages directly (`write_page`, `edit_page`, `move`…),
in the name of the person it works for. A run is what one assistant wrote for one person in
one go: their consecutive commits on the pages, with no pause over `GAP` and nobody else's
change in between (read on the line of commits made here: a merge of changes pushed from
elsewhere ends a run).

Undoing works as for a tidy run (`tidy_undo`): each chosen page goes back to its state before
the run, in a single commit in the person's name; a page changed since the run is refused,
its history restores it by hand without losing the later change.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError, conflict, invalid, not_found
from synaptane.git.repo import Commit
from synaptane.services import Actor, audit, jobs
from synaptane.services.activity import MAX_SCAN, origin_of
from synaptane.services.content import writable
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.tidy_undo import PREFIX, RunPage, page_states
from synaptane.services.workspace import Workspace, author_identity, trailers
from synaptane.storage.db import Database
from synaptane.storage.models import Space

GAP = timedelta(minutes=30)  # a longer pause starts a new run
DAYS = 30  # runs looked for over this many days at most
SHA = 12  # characters of each commit id in a run's id
# An undo made from a view goes through MCP too, but the person chose it: not the assistant's
# writing (a run made of it would undo the undo). A message copied by hand only splits a run.
UNDOS = ("Undo what ", "Undo the tidy-up ")


@dataclass(frozen=True)
class Run:
    """One assistant's consecutive commits for one person. `commits`: newest first;
    `start`: the commit before the run, where the pages go back."""

    space: str
    author: str
    client: str  # the MCP application or token ("" if unknown)
    commits: tuple[Commit, ...]
    start: str

    @property
    def id(self) -> str:
        return f"{self.commits[-1].sha[:SHA]}..{self.commits[0].sha[:SHA]}"

    @property
    def end(self) -> str:
        return self.commits[0].sha

    @property
    def started(self) -> str:
        return self.commits[-1].date

    @property
    def ended(self) -> str:
        return self.commits[0].date


def _when(commit: Commit) -> datetime:
    return datetime.fromisoformat(commit.date)


def runs(
    actor: Actor, ws: Workspace, space: Space, days: int = 7, now: datetime | None = None
) -> list[Run]:
    """The assistants' runs that ended in the last `days` days (at most `DAYS`), newest
    first. Always grouped over `DAYS`: a run keeps the same commits, and id, whatever `days`."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=DAYS)).isoformat(timespec="seconds")
    try:
        repo = ws.local_repository(space)
        commits = repo.recent(CONTENT_DIR, MAX_SCAN, since=since, first_parent=True)
    except SynaptaneError:
        return []
    found: list[Run] = []
    current: list[Commit] = []
    key: tuple[str, str] | None = None
    for commit in commits:  # newest first
        origin, client = origin_of(commit, ws.committer)
        written = origin == "assistant" and commit.paths and not commit.subject.startswith(UNDOS)
        mine = (commit.author.email, client) if written else None
        if mine is not None and mine == key and _when(current[-1]) - _when(commit) <= GAP:
            current.append(commit)
            continue
        if current and key is not None:
            found.append(_run(space, current, key[1]))
        current, key = ([commit], mine) if mine is not None else ([], None)
    if current and key is not None:
        found.append(_run(space, current, key[1]))
    cutoff = now - timedelta(days=min(max(days, 1), DAYS))
    return [
        replace(run, start=repo.rev(run.start) or run.start)
        for run in found
        if _when(run.commits[0]) >= cutoff
    ]


def _run(space: Space, commits: list[Commit], client: str) -> Run:
    oldest = commits[-1]  # the pages go back as in its parent on this line
    return Run(space.address, oldest.author.name, client, tuple(commits), f"{oldest.sha}^")


def find(actor: Actor, ws: Workspace, space: Space, run_id: str) -> Run:
    for run in runs(actor, ws, space, DAYS):
        if run.id == run_id.strip():
            return run
    raise not_found("Assistant run not found (the last {days} days).", days=DAYS)


def run_pages(ws: Workspace, space: Space, run: Run) -> list[RunPage]:
    """The pages the run changed (net: a page created then edited is created), with what can
    still be undone. Rights are the caller's."""
    repo = ws.local_repository(space)
    changes = repo.changes_between(run.start, run.end, CONTENT_DIR)
    return page_states(repo, run.start, run.end, changes)


@dataclass(frozen=True)
class Undone:
    commit: str | None
    pages: list[str]


def undo_pages(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    run_id: str,
    paths: list[str],
    *,
    source: str = "web",
) -> Undone:
    """Puts the chosen pages (relative to content/) back as they were before the run
    (contributor: the same changes could be made by hand)."""
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    wanted = {p.strip().strip("/") for p in paths if p.strip()}
    if not wanted:
        raise invalid("Choose the pages to undo.")
    with writable(session, database, actor, ws, space) as repo:
        run = find(actor, ws, space, run_id)
        pages = run_pages(ws, space, run)
        if unknown := sorted(wanted - {p.path for p in pages}):
            raise invalid("Not changed by this assistant run: {paths}.", paths=", ".join(unknown))
        chosen = [p for p in pages if p.path in wanted]
        if changed := [p.path for p in chosen if p.state == "changed"]:
            raise conflict(
                "Changed since the assistant run: {paths}. Restore them from their history.",
                paths=", ".join(changed),
            )
        todo = [p for p in chosen if p.state == "undoable"]
        if not todo:
            raise invalid("These pages are already undone.")
        files: dict[str, bytes | None] = {}
        for page in todo:
            for path in filter(None, (page.path, page.old)):
                files[PREFIX + path] = repo.blob(PREFIX + path, run.start)
        started = _when(run.commits[-1]).astimezone(UTC)
        message = (
            f"{UNDOS[0]}{run.client or 'an assistant'} wrote on {started:%Y-%m-%d %H:%M} for "
            + (todo[0].path if len(todo) == 1 else f"{len(todo)} pages")
            + trailers(source, actor.via)
        )
        sha = repo.commit_changes(files, author_identity(actor.user, ws.committer), message)
    jobs.enqueue(session, "sync", space.id)
    jobs.enqueue(session, "index", space.id)
    audit(session, actor, "assistant_run.pages_undone", space=space, run=run.id, count=len(todo))
    return Undone(sha, [p.path for p in todo])
