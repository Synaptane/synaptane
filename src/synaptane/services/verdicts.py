"""Verdicts on the problems agent routines report, so that a false alarm does not come back
every week.

A routine (the consistency check, for one) deposits an inbox item per problem found. When
that item leaves the inbox, what became of it is recorded: judged **not a problem** by a
person (with why), **removed** from the inbox, or **handled** by the tidy-up. The verdict is
bound to the pages concerned as they were then (digest of their content): as long as they
have not changed, the routine is given the verdict and does not report the problem again; a
change to one of them makes it lapse, since the problem may then be real or new.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.services.inbox import CONTENT_DIR, InboxItem
from synaptane.services.workspace import Workspace
from synaptane.storage.models import FindingVerdict, Space, User

SOURCE = "routine"  # `source` of the items deposited by a routine (routine_agent.SOURCE)
PAGES_HINT = "Pages: "  # start of the `hint` of such an item, followed by its pages
KEPT = timedelta(days=365)  # older verdicts are forgotten
WITHOUT_PAGES = timedelta(days=90)  # a verdict bound to no page lapses sooner
SHOWN = 50  # verdicts given to a routine


@dataclass(frozen=True)
class Verdict:
    title: str
    kind: str  # dismissed, deleted or filed
    reason: str
    by: str
    at: datetime
    pages: tuple[str, ...]

    def line(self) -> str:
        """The verdict in words, for the routine."""
        what = {
            "dismissed": f"not a problem, according to {self.by or 'a member'}",
            "deleted": f"removed from the inbox by {self.by or 'a member'}",
            "filed": "handled by the tidy-up",
        }[self.kind]
        pages = f" ({', '.join(self.pages)})" if self.pages else ""
        why = f": {self.reason}" if self.reason else ""
        return f"- {self.title}{pages}: {what}, {self.at:%Y-%m-%d}{why}"


def pages_of(item: InboxItem) -> list[str]:
    """Pages concerned by an item a routine deposited (its `hint`)."""
    if not item.hint.startswith(PAGES_HINT):
        return []
    return [p.strip() for p in item.hint[len(PAGES_HINT) :].split(",") if p.strip()]


def record(
    session: Session,
    ws: Workspace,
    space: Space,
    item: InboxItem,
    kind: str,
    reason: str = "",
    by: UUID | None = None,
) -> None:
    """Records what became of an item, if a routine deposited it (otherwise nothing)."""
    if item.source != SOURCE or not item.origin:
        return
    try:
        repo = ws.local_repository(space)
    except SynaptaneError:
        return
    pages = {p: repo.blob_sha(f"{CONTENT_DIR}/{p}") for p in pages_of(item)}
    session.add(
        FindingVerdict(
            space_id=space.id,
            origin=item.origin[:200],
            title=item.title,
            kind=kind,
            reason=" ".join(reason.split())[:500],
            pages=pages,
            decided_by=by,
        )
    )


def standing(
    session: Session, ws: Workspace, space: Space, origins: Iterable[str]
) -> list[Verdict]:
    """Verdicts on a routine's problems that still hold: none of their pages changed since,
    most recent first. Older ones are forgotten (and deleted)."""
    now = datetime.now(UTC)
    session.execute(
        delete(FindingVerdict).where(
            FindingVerdict.space_id == space.id, FindingVerdict.decided_at < now - KEPT
        )
    )
    rows = session.execute(
        select(FindingVerdict, User.display_name)
        .outerjoin(User, User.id == FindingVerdict.decided_by)
        .where(FindingVerdict.space_id == space.id, FindingVerdict.origin.in_(list(origins)))
        .order_by(FindingVerdict.decided_at.desc())
        .limit(500)
    )
    try:
        current = {
            b.path.removeprefix(CONTENT_DIR + "/"): b.sha
            for b in ws.local_repository(space).files(CONTENT_DIR)
        }
    except SynaptaneError:
        return []
    found: list[Verdict] = []
    seen: set[str] = set()
    for verdict, name in rows:
        pages = dict(verdict.pages or {})
        if not pages and verdict.decided_at < now - WITHOUT_PAGES:
            continue
        if any(current.get(path) != sha for path, sha in pages.items()):
            continue  # a page changed: the problem may be real now
        if verdict.title in seen:
            continue
        seen.add(verdict.title)
        found.append(
            Verdict(
                verdict.title,
                verdict.kind,
                verdict.reason,
                name or "",
                verdict.decided_at,
                tuple(pages),
            )
        )
        if len(found) == SHOWN:
            break
    return found


def same_title(title: str, verdicts: Iterable[Verdict]) -> Verdict | None:
    """A standing verdict on a problem of the same title (case and spacing aside)."""
    wanted = " ".join(title.lower().split())
    return next((v for v in verdicts if " ".join(v.title.lower().split()) == wanted), None)
