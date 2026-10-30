"""Activity of a space: who changed what, and through which channel, from the Git history.

Each repository commit that touches the content or the inbox becomes an event:
- `page`: pages created, edited, moved or deleted (web, REST, MCP, Git);
- `inbox`: a note deposited, corrected, deleted or moved;
- `tidy`: a tidy run;
`external` flags a commit pushed outside Synaptane (forge, workstation).

Its `origin` says who wrote it, person or AI: read from the author of the agents' commits and
from the `Source:` and `Via:` lines Synaptane adds to the others. A commit pushed from
elsewhere is `git`: its message is not Synaptane's, whatever it claims.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError
from synaptane.git.repo import Commit, Identity
from synaptane.services import Actor
from synaptane.services.inbox import CONTENT_DIR, INBOX_DIR, is_routine_agent, is_tidy_agent
from synaptane.services.workspace import Workspace
from synaptane.storage.models import Space

KINDS = ("page", "inbox", "tidy", "external", "ai")
PAGE_SIZE = 40
MAX_SCAN = 2_000  # at most this many commits read per page
# Who wrote a change: an AI (the tidy-up, a routine, an assistant over MCP) or a person (in
# the web interface, through the REST API, or pushed with Git from elsewhere).
AI_ORIGINS = ("tidy", "routine", "assistant")
ORIGINS = (*AI_ORIGINS, "web", "api", "git")
SHARE_DAYS = 30


@dataclass(frozen=True)
class Event:
    sha: str
    date: str
    author: str
    subject: str
    kind: str  # page, inbox, tidy or system (space initialization, merges)
    external: bool  # pushed outside Synaptane
    pages: tuple[str, ...]  # relative to content/
    origin: str = "web"  # see ORIGINS
    client: str = ""  # MCP application or token, API token

    @property
    def ai(self) -> bool:
        return self.origin in AI_ORIGINS


def origin_of(commit: Commit, committer: Identity) -> tuple[str, str]:
    """(origin, client) of a commit: see ORIGINS, or `system` (Synaptane itself); the client
    is the name of the MCP application or token, or of the API token."""
    if commit.committer_email != committer.email:
        return "git", ""
    if is_tidy_agent(commit.author.name):
        return "tidy", ""
    if is_routine_agent(commit.author.name):
        return "routine", ""
    recovery = committer.email.replace("@", "+recovery@", 1)  # see git_sync.RECOVERY_AUTHOR
    if commit.author.email in (committer.email, recovery):
        return "system", ""  # space initialization, recovery of local changes
    channel, _, client = commit.via.partition("·")
    if commit.source == "mcp":
        return "assistant", client.strip() if channel.strip() == "MCP" else ""
    if commit.source == "api":
        return "api", client.strip() if channel.strip() == "API" else ""
    return "web", ""


def written_by(commit: Commit, committer: Identity) -> str:
    """Author of a commit for an assistant: "Alice", "Alice via Claude (AI)", "Synaptane
    tidy-up (AI)", "Alice via the REST API (script)", "Alice (pushed with Git)"."""
    origin, client = origin_of(commit, committer)
    name = commit.author.name
    if origin == "assistant":
        return f"{name} via {client or 'an MCP assistant'} (AI)"
    if origin in AI_ORIGINS:
        return f"{name} (AI)"
    if origin == "api":
        return f"{name} via the REST API" + (f" ({client})" if client else "")
    return f"{name} (pushed with Git)" if origin == "git" else name


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
    origin, client = origin_of(commit, ws.committer)
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
        origin=origin,
        client=client,
    )


def _wanted(event: Event, kind: str) -> bool:
    if kind == "external":
        return event.external
    if kind == "ai":
        return event.ai
    return event.kind == kind if kind in KINDS else True


@dataclass(frozen=True)
class Share:
    """Changes to the pages over the last `days` days, by origin (see ORIGINS)."""

    days: int
    counts: dict[str, int]
    complete: bool  # False: more than MAX_SCAN commits, only the latest were counted

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def ai(self) -> int:
        return sum(self.counts.get(origin, 0) for origin in AI_ORIGINS)

    @property
    def ai_percent(self) -> int:
        return round(100 * self.ai / self.total) if self.total else 0


def ai_share(
    actor: Actor, ws: Workspace, space: Space, days: int = SHARE_DAYS, now: datetime | None = None
) -> Share:
    """Who wrote the pages lately: the commits that touched `content/` (the inbox is not
    counted: a note is not knowledge yet)."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    since = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        commits = ws.local_repository(space).recent(CONTENT_DIR, MAX_SCAN, since=since)
    except SynaptaneError:
        return Share(days, {}, True)
    origins = (origin_of(c, ws.committer)[0] for c in commits if c.paths)
    counts = Counter(origin for origin in origins if origin != "system")
    return Share(days, dict(counts), len(commits) < MAX_SCAN)
