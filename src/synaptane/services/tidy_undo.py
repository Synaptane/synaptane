"""Undoing a tidy run for some pages only (the whole run: `tidy.revert`).

Each chosen page goes back to its state before the run, in a single commit in the person's
name: a created page is removed, an edited or deleted one comes back as it was, a moved one
returns to its former path. A page changed since the run is refused: its history restores it
by hand, without losing the later change.

A note the run filed comes back to the inbox once every page it was integrated into is
undone: brought back sooner, the next run would write it again into the pages kept.
"""

from dataclasses import dataclass
from datetime import UTC
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import conflict, invalid, not_found
from synaptane.git.repo import Repository
from synaptane.services import Actor, audit, jobs, tidy_journal
from synaptane.services.content import writable
from synaptane.services.inbox import CONTENT_DIR, INBOX_DIR, attachments_dir, parse_front_matter
from synaptane.services.tidy_tools import Op
from synaptane.services.workspace import Workspace, author_identity, trailers
from synaptane.storage.db import Database
from synaptane.storage.models import Space, TidyRun

PREFIX = CONTENT_DIR + "/"
STATUS = {"A": "created", "M": "edited", "D": "deleted", "R": "moved"}


@dataclass(frozen=True)
class RunPage:
    """A page the run changed. `path`: where the run left it (the deleted page for a
    deletion), relative to content/; `old`: its former path, for a move."""

    path: str
    status: str  # created, edited, deleted, moved
    old: str
    state: str  # undoable; changed (since the run: refused); undone


@dataclass(frozen=True)
class Note:
    """A note the run filed: its title, its files in the inbox, the pages it went into."""

    title: str
    files: tuple[str, ...]
    pages: tuple[str, ...]


@dataclass(frozen=True)
class Undone:
    commit: str | None
    pages: list[str]
    notes_back: list[str]  # titles of the notes back in the inbox
    notes_kept: list[tuple[str, tuple[str, ...]]]  # (title, pages still holding it)


def _run(session: Session, space: Space, run_id: UUID) -> TidyRun:
    record = session.get(TidyRun, run_id)
    if record is None or record.space_id != space.id or not record.commit:
        raise not_found("Run not found.")
    return record


def _shas(repo: Repository, rev: str) -> dict[str, str]:
    """Digest of each file of content/ and of the inbox at `rev`, in one call each."""
    return {b.path: b.sha for prefix in (CONTENT_DIR, INBOX_DIR) for b in repo.files(prefix, rev)}


def _pages(repo: Repository, commit: str) -> list[RunPage]:
    before, after, now = _shas(repo, f"{commit}^"), _shas(repo, commit), _shas(repo, "HEAD")
    pages = []
    for status, path, old in repo.changes_in(commit):
        if not path.startswith(PREFIX):
            continue
        paths = (path, old) if old else (path,)
        if all(now.get(p) == before.get(p) for p in paths):
            state = "undone"
        elif all(now.get(p) == after.get(p) for p in paths):
            state = "undoable"
        else:
            state = "changed"
        pages.append(
            RunPage(
                path[len(PREFIX) :],
                STATUS.get(status, "edited"),
                old[len(PREFIX) :] if old else "",
                state,
            )
        )
    return sorted(pages, key=lambda p: p.path)


def run_pages(
    session: Session, actor: Actor, ws: Workspace, space: Space, run_id: UUID
) -> tuple[TidyRun, list[RunPage]]:
    """The run and the pages it changed, with what can still be undone."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    record = _run(session, space, run_id)
    return record, _pages(ws.local_repository(space), str(record.commit))


def _notes(repo: Repository, record: TidyRun) -> list[Note]:
    """Notes removed from the inbox by the run, matched by title to the operations that
    give their pages (the log names a note by its title)."""
    parent = f"{record.commit}^"
    pages: dict[str, tuple[str, ...]] = {}
    for entry in record.operations or []:
        operation = tidy_journal.operation(entry)
        if operation.op == Op.FILED and operation.pages:
            pages[operation.path] = pages.get(operation.path, ()) + operation.pages
    removed = [p for s, p, _ in repo.changes_in(str(record.commit)) if s == "D"]
    notes = []
    for path in removed:
        if str(PurePosixPath(path).parent) != INBOX_DIR or not path.endswith(".md"):
            continue
        fields, _ = parse_front_matter((repo.blob(path, parent) or b"").decode("utf-8", "replace"))
        title = str(fields.get("title") or "")
        if title not in pages:
            continue
        folder = attachments_dir(path) + "/"
        files = (path, *(p for p in removed if p.startswith(folder)))
        notes.append(Note(title, files, pages[title]))
    return notes


def undo_pages(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    run_id: UUID,
    paths: list[str],
    *,
    source: str = "web",
) -> Undone:
    """Puts the chosen pages (relative to content/) back as they were before the run
    (contributor: the same changes could be made by hand)."""
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    record = _run(session, space, run_id)
    if record.reverted_at:
        raise invalid("This run has already been undone.")
    wanted = {p.strip().strip("/") for p in paths if p.strip()}
    if not wanted:
        raise invalid("Choose the pages to undo.")
    commit = str(record.commit)
    with writable(session, database, actor, ws, space) as repo:
        pages = _pages(repo, commit)
        known = {p.path for p in pages}
        if unknown := sorted(wanted - known):
            raise invalid("Not changed by this run: {paths}.", paths=", ".join(unknown))
        chosen = [p for p in pages if p.path in wanted]
        if changed := [p.path for p in chosen if p.state == "changed"]:
            raise conflict(
                "Changed since the run: {paths}. Restore them from their history instead.",
                paths=", ".join(changed),
            )
        todo = [p for p in chosen if p.state == "undoable"]
        if not todo:
            raise invalid("These pages are already undone.")
        parent = f"{commit}^"
        files: dict[str, bytes | None] = {}
        for page in todo:
            for path in filter(None, (page.path, page.old)):
                files[PREFIX + path] = repo.blob(PREFIX + path, parent)
        undone = {p.path for p in pages if p.state == "undone"} | {p.path for p in todo}
        back, kept = [], []
        for note in _notes(repo, record):
            if not set(note.pages) & {p.path for p in todo}:
                continue  # not concerned by this undo
            if set(note.pages) <= undone and repo.blob_sha(note.files[0]) is None:
                files |= {path: repo.blob(path, parent) for path in note.files}
                back.append(note.title)
            else:
                kept.append((note.title, tuple(p for p in note.pages if p not in undone)))
        started = record.started_at.astimezone(UTC)
        message = (
            f"Undo the tidy-up of {started:%Y-%m-%d %H:%M} for "
            + (todo[0].path if len(todo) == 1 else f"{len(todo)} pages")
            + trailers(source, actor.via)
        )
        sha = repo.commit_changes(files, author_identity(actor.user, ws.committer), message)
    jobs.enqueue(session, "sync", space.id)
    jobs.enqueue(session, "index", space.id)
    audit(session, actor, "tidy.pages_undone", space=space, run=str(run_id), count=len(todo))
    return Undone(sha, [p.path for p in todo], back, kept)
