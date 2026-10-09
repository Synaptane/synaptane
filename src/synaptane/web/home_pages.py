"""Home page (what is waiting, what just changed) and the palette's quick search.

The home page answers "what is going on?": inbox notes per space, failed tidy-ups, Git
conflicts, stale pages one owns, then the most recently modified pages. The palette (Ctrl+K,
`/`, the magnifier in the top bar) queries `/search/titles`, which returns only titles as JSON.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import func, select

from synaptane.core.errors import SynaptaneError
from synaptane.services import Actor, freshness
from synaptane.services import search as search_service
from synaptane.services.inbox import CONTENT_DIR, is_tidy_agent
from synaptane.storage.models import Space, TidyRun
from synaptane.web.pages import ERROR_STATUS, Page, WebPage
from synaptane.web.render import url_path

router = APIRouter()

RECENT = 8  # "Recently modified" pages
COMMITS_PER_SPACE = 30  # commits read per space to find them
PALETTE_HITS = 8  # titles offered by the palette
REVIEWS = 5  # stale pages shown under "Waiting"


@dataclass(frozen=True)
class Waiting:
    """A row of the "Waiting" column."""

    kind: str  # inbox, tidy (failed tidy-up), sync (Git error or conflict)
    space: Space
    url: str
    counts: dict[str, int] | None = None
    status: str = ""  # sync: error or conflict
    when: datetime | None = None


@dataclass(frozen=True)
class Review:
    """A stale page owned by the signed-in person."""

    space: Space
    path: str  # relative to content/
    url: str
    due: date


@dataclass(frozen=True)
class Recent:
    space: Space
    path: str  # relative to content/
    url: str
    author: str
    date: str
    by_agent: bool


def _parse(date: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(date)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _waiting(page: Page, visible: list[Space]) -> list[Waiting]:
    """Problems first (conflict, failed tidy-up), then the fullest inboxes."""
    base = {s.id: f"/o/{s.address}" for s in visible}
    problems: list[Waiting] = []
    for space in visible:
        if space.sync_status in ("error", "conflict"):
            url = f"{base[space.id]}/-/git"
            problems.append(Waiting("sync", space, url, status=space.sync_status))
    # Latest run of each space, in one query: only a recent failure counts.
    newest = (
        select(TidyRun.space_id, func.max(TidyRun.started_at).label("at"))
        .where(TidyRun.space_id.in_(list(base)))
        .group_by(TidyRun.space_id)
        .subquery()
    )
    latest = page.db.scalars(
        select(TidyRun).join(
            newest, (TidyRun.space_id == newest.c.space_id) & (TidyRun.started_at == newest.c.at)
        )
    )
    by_id = {s.id: s for s in visible}
    for run in latest:
        if run.status == "failed":
            space = by_id[run.space_id]
            problems.append(Waiting("tidy", space, f"{base[space.id]}/tidy", when=run.finished_at))
    inboxes = []
    for space in visible:
        counts = page.inbox_counts(space)
        if counts and counts["total"]:
            inboxes.append(Waiting("inbox", space, f"{base[space.id]}/inbox", counts=counts))
    inboxes.sort(key=lambda w: -(w.counts or {}).get("total", 0))
    return problems + inboxes


def _reviews(page: Page, actor: Actor, visible: list[Space]) -> list[Review]:
    """Stale pages (verification overdue) one owns, the oldest due date first. The owner is
    recognized by username or e-mail address."""
    names = (actor.user.username, actor.user.email or "")
    found = [
        Review(space, path, f"/o/{space.address}/p/{url_path(path)}", state.due)
        for space in visible
        for path, state in freshness.space_states(page.workspace, space).items()
        if state.stale and state.due and freshness.owned_by(state.owner, names)
    ]
    return sorted(found, key=lambda r: (r.due, r.space.name, r.path))


def _recent(page: Page, actor: Actor, visible: list[Space]) -> list[Recent]:
    """Most recently modified pages, across all spaces, from the Git history. A page deleted
    since is not offered."""
    found: dict[tuple[object, str], Recent] = {}
    for space in visible:
        try:
            existing = {b.path for b in page.space_files(actor, space)}
            commits = page.workspace.local_repository(space).recent(CONTENT_DIR, COMMITS_PER_SPACE)
        except SynaptaneError:
            continue  # space not synced yet
        for commit in commits:
            for full in commit.paths:
                path = full[len(CONTENT_DIR) + 1 :]
                key = (space.id, path)
                if path not in existing or key in found:
                    continue
                found[key] = Recent(
                    space=space,
                    path=path,
                    url=f"/o/{space.address}/p/{url_path(path)}",
                    author=commit.author.name,
                    date=commit.date,
                    by_agent=is_tidy_agent(commit.author.name),
                )
    return sorted(found.values(), key=lambda r: _parse(r.date), reverse=True)[:RECENT]


@router.get("/", response_class=HTMLResponse)
def home(page: WebPage) -> Response:
    actor = page.user_actor()
    visible = [space for _, org_spaces in page.navigation() for space in org_spaces]
    reviews = _reviews(page, actor, visible)
    return page.render(
        "home.html",
        waiting=_waiting(page, visible),
        reviews=reviews[:REVIEWS],
        more_reviews=len(reviews) - REVIEWS,
        recent=_recent(page, actor, visible),
        inbox_of=page.inbox_counts,  # counter of each space card (already computed)
    )


@router.get("/search/titles", include_in_schema=False)
def search_titles(page: WebPage, q: str = "", space: str = "") -> Response:
    """Titles for the search palette, live while typing.

    Word search only (`mode="text"`): no call to the embeddings provider on every keystroke.
    The `/search` page remains the full view (meaning, pending notes)."""
    actor = page.user_actor()
    text = q.strip()
    more = "/search?" + "&".join(
        f"{key}={url_path(value)}" for key, value in (("q", text), ("space", space)) if value
    )
    if len(text) < 2:
        return JSONResponse({"results": [], "all": more})
    try:
        hits = search_service.search(
            page.db,
            actor,
            page.workspace,
            text,
            addresses=[space] if space else [],
            limit=PALETTE_HITS,
            mode="text",
        )
    except SynaptaneError as error:
        return JSONResponse(
            {"results": [], "all": more, "error": page.translate(error.template, **error.params)},
            status_code=ERROR_STATUS[error.code],
        )
    return JSONResponse(
        {
            "results": [
                {
                    "title": hit.title,
                    "path": hit.path,
                    "space": hit.space_name,
                    "url": f"/o/{hit.org}/{hit.space}/p/{url_path(hit.path)}",
                }
                for hit in hits[:PALETTE_HITS]
            ],
            "all": more,
        },
        headers={"Cache-Control": "no-store"},
    )
