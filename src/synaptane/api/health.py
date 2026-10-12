"""REST API for wiki health: `GET …/spaces/{space}/health` (see `services.health`)."""

from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from synaptane.services import health

from .content import SPACE, _ws
from .routes import CurrentActor, DbSession, find_space

router = APIRouter(prefix="/api/v1")


class BrokenLinkOut(BaseModel):
    page: str
    link: str = Field(description="Link as written in the page")
    suggestion: str | None = Field(description="Link to a file with the same name, if there is one")


class StalePageOut(BaseModel):
    path: str
    date: str = Field(description="Last commit that touched the page (ISO 8601)")


class DuePageOut(BaseModel):
    path: str
    owner: str = Field(description="Page owner (empty: none)")
    verified: str = Field(description="Date of the last verification (YYYY-MM-DD)")
    due: str = Field(description="Verification due date, past (YYYY-MM-DD)")


class Counts(BaseModel):
    broken: int
    orphans: int
    empty: int
    untitled: int
    stale: int
    due: int = Field(description="Stale pages: verification overdue")
    unowned: int = Field(description="Verified pages without an owner")
    verified: int = Field(description="Verified and up-to-date pages")


class HealthOut(BaseModel):
    commit: str | None
    pages: int = Field(description="Markdown pages examined")
    months: int = Field(description="Age (in months) from which a page is old")
    counts: Counts = Field(description="Totals, before the list limit")
    broken: list[BrokenLinkOut]
    orphans: list[str] = Field(description="Pages without incoming links (excluding home pages)")
    empty: list[str]
    untitled: list[str] = Field(description="Pages without a level-1 title")
    stale: list[StalePageOut]
    due: list[DuePageOut] = Field(description="Stale pages, oldest due date first")
    unowned: list[str] = Field(description="Verified pages without an owner")


@router.get(SPACE + "/health", tags=["content"])
def space_health(
    org: str,
    space: str,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    months: Annotated[int, Query(ge=1, le=120)] = health.STALE_MONTHS,
    limit: Annotated[int, Query(ge=1, le=10_000, description="Items per list")] = 500,
) -> HealthOut:
    """Wiki health (reader): broken links, orphan, empty, untitled and old pages, stale pages
    (verification overdue) and verified pages without an owner. Deterministic computation,
    no AI, at the latest commit."""
    found = find_space(session, actor, org, space)
    report = health.space_health(actor, _ws(request), found, months)
    return HealthOut(
        commit=report.commit,
        pages=report.pages,
        months=report.months,
        counts=Counts(
            broken=len(report.broken),
            orphans=len(report.orphans),
            empty=len(report.empty),
            untitled=len(report.untitled),
            stale=len(report.stale),
            due=len(report.due),
            unowned=len(report.unowned),
            verified=report.verified,
        ),
        broken=[
            BrokenLinkOut(page=b.page, link=b.link, suggestion=b.suggestion)
            for b in report.broken[:limit]
        ],
        orphans=report.orphans[:limit],
        empty=report.empty[:limit],
        untitled=report.untitled[:limit],
        stale=[StalePageOut(path=s.path, date=s.date) for s in report.stale[:limit]],
        due=[
            DuePageOut(path=d.path, owner=d.owner, verified=d.verified, due=d.due)
            for d in report.due[:limit]
        ],
        unowned=report.unowned[:limit],
    )
