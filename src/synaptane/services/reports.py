"""Pages reported as outdated by the people who read them.

A reader cannot change a page, but often sees first that it no longer holds: they report it,
with what is wrong. The report is shown on the page to everyone, flags it in search and in
the space's health, and reaches assistants (`read`, `search`, `fetch`). It stays open until
the page is verified ("Mark as verified": someone reread it), a contributor dismisses it, or
its author withdraws it. One open report per person and page: reporting again updates it.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import invalid, not_found
from synaptane.services import Actor, audit
from synaptane.services.workspace import Workspace
from synaptane.storage.models import PageReport, Space, User, utcnow

MAX_REASON = 500
VERIFIED, DISMISSED, WITHDRAWN = "verified", "dismissed", "withdrawn"


@dataclass(frozen=True)
class Report:
    id: UUID
    path: str
    reason: str
    by: str  # display name of its author ("" if the account was deleted)
    by_id: UUID | None
    at: datetime


def report(
    session: Session, actor: Actor, ws: Workspace, space: Space, path: str, reason: str
) -> PageReport:
    """Reports a Markdown page as outdated (reader): `reason` says what no longer holds."""
    from synaptane.services import content, freshness  # content imports this module

    file = content.read_file(actor, ws, space, path)  # read right, and the page exists
    if not freshness.is_page(file.path):
        raise invalid("Only Markdown pages can be reported as outdated.")
    reason = reason.strip()
    if not reason:
        raise invalid("Say what is outdated in this page.")
    if len(reason) > MAX_REASON:
        raise invalid("Reason too long ({max} characters maximum).", max=MAX_REASON)
    found = session.scalar(
        select(PageReport).where(
            PageReport.space_id == space.id,
            PageReport.path == file.path,
            PageReport.reported_by == actor.user.id,
            PageReport.resolved_at.is_(None),
        )
    )
    if found is None:
        found = PageReport(
            space_id=space.id, path=file.path, reported_by=actor.user.id, reason=reason
        )
        session.add(found)
    else:
        found.reason = reason
    session.flush()
    audit(session, actor, "page.reported", space=space, path=file.path)
    return found


def open_reports(session: Session, actor: Actor, space: Space, path: str) -> list[Report]:
    """Open reports of a page, oldest first."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    rows = session.execute(
        select(PageReport, User.display_name)
        .outerjoin(User, User.id == PageReport.reported_by)
        .where(
            PageReport.space_id == space.id,
            PageReport.path == path,
            PageReport.resolved_at.is_(None),
        )
        .order_by(PageReport.created_at)
    )
    return [
        Report(r.id, r.path, r.reason, name or "", r.reported_by, r.created_at) for r, name in rows
    ]


def space_reports(session: Session, actor: Actor, space: Space) -> list[tuple[str, list[Report]]]:
    """Pages of a space with open reports, the most recently reported first."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    rows = session.execute(
        select(PageReport, User.display_name)
        .outerjoin(User, User.id == PageReport.reported_by)
        .where(PageReport.space_id == space.id, PageReport.resolved_at.is_(None))
        .order_by(PageReport.created_at.desc())
        .limit(1000)
    )
    pages: dict[str, list[Report]] = {}
    for r, name in rows:
        pages.setdefault(r.path, []).append(
            Report(r.id, r.path, r.reason, name or "", r.reported_by, r.created_at)
        )
    return list(pages.items())


def open_counts(session: Session, space_ids: Iterable[UUID]) -> dict[tuple[UUID, str], int]:
    """Number of open reports per (space, page), in one query. Rights are the caller's."""
    ids = list(space_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(PageReport.space_id, PageReport.path, func.count())
        .where(PageReport.space_id.in_(ids), PageReport.resolved_at.is_(None))
        .group_by(PageReport.space_id, PageReport.path)
    )
    return {(space_id, path): count for space_id, path, count in rows}


def resolve(session: Session, actor: Actor, space: Space, path: str, resolution: str) -> int:
    """Closes the open reports of a page; returns how many. `verified` and `dismissed`
    close them all (contributor); `withdrawn`, only the person's own."""
    if resolution == WITHDRAWN:
        require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    else:
        require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    query = update(PageReport).where(
        PageReport.space_id == space.id,
        PageReport.path == path,
        PageReport.resolved_at.is_(None),
    )
    if resolution == WITHDRAWN:
        query = query.where(PageReport.reported_by == actor.user.id)
    result = session.execute(
        query.values(resolved_at=utcnow(), resolved_by=actor.user.id, resolution=resolution)
    )
    count = int(getattr(result, "rowcount", 0) or 0)
    if count == 0 and resolution != VERIFIED:
        raise not_found("No open report on this page.")
    if count:
        audit(session, actor, f"page.report_{resolution}", space=space, path=path, count=count)
    return count


def follow_move(session: Session, space: Space, old: str, new: str) -> None:
    """A moved page keeps its open reports."""
    session.execute(
        update(PageReport)
        .where(
            PageReport.space_id == space.id,
            PageReport.path == old,
            PageReport.resolved_at.is_(None),
        )
        .values(path=new)
    )
