"""Activity of a space: who changed what, and through which channel, from the Git history.

Each repository commit that touches the content or the inbox becomes an event:
- `page`: pages created, edited, moved or deleted (web, REST, MCP, Git);
- `inbox`: a note deposited, corrected, deleted or moved;
- `tidy`: a tidy run;
`external` flags a commit pushed outside Synaptane (forge, workstation).
"""

from dataclasses import dataclass

from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError
from synaptane.git.repo import Commit
from synaptane.services import Actor
from synaptane.services.inbox import CONTENT_DIR, INBOX_DIR, is_tidy_agent
from synaptane.services.workspace import Workspace
from synaptane.storage.models import Space

KINDS = ("page", "inbox", "tidy", "external")
PAGE_SIZE = 40
MAX_SCAN = 2_000  # at most this many commits read per page


@dataclass(frozen=True)
class Event:
    sha: str
    date: str
    author: str
    subject: str
    kind: str  # page, inbox, tidy or system (space initialization, merges)
    external: bool  # pushed outside Synaptane
    pages: tuple[str, ...]  # relative to content/


def _kind(author: str, paths: tuple[str, ...], system: str) -> str:
    if is_tidy_agent(author):
        return "tidy"
    if any(p.startswith(CONTENT_DIR + "/") for p in paths):
        return "page"
    return "system" if author == system else "inbox"  # skeleton, Synaptane merges


def space_activity(
    actor: Actor, ws: Workspace, space: Space, *, kind: str = "", skip: int = 0
) -> tuple[list[Event], int | None]:
    """One page of events, newest first, and the position of the next page
    (None: this was the last one)."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    try:
        repo = ws.local_repository(space)
    except SynaptaneError:
        return [], None
    events: list[Event] = []
    offset = max(skip, 0)
    # Without a filter, one page is enough; with a filter, a single `git log` reads up to
    # MAX_SCAN commits at once (rerunning `--skip` in small batches rescanned the history).
    batch = PAGE_SIZE if kind not in KINDS else MAX_SCAN
    commits = repo.recent((CONTENT_DIR, INBOX_DIR), batch, offset)
    for commit in commits:
        offset += 1
        event = _event(ws, commit) if commit.paths else None
        if event is None or not _wanted(event, kind):
            continue
        events.append(event)
        if len(events) == PAGE_SIZE:
            return events, offset
    return events, (offset if len(commits) == batch else None)


def _event(ws: Workspace, commit: Commit) -> Event:
    return Event(
        sha=commit.sha,
        date=commit.date,
        author=commit.author.name,
        subject=commit.subject,
        kind=_kind(commit.author.name, commit.paths, ws.committer.name),
        external=commit.committer_email != ws.committer.email,
        pages=tuple(
            p[len(CONTENT_DIR) + 1 :] for p in commit.paths if p.startswith(CONTENT_DIR + "/")
        ),
    )


def _wanted(event: Event, kind: str) -> bool:
    if kind == "external":
        return event.external
    return event.kind == kind if kind in KINDS else True
