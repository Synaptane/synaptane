"""A space's "Health" tab: broken links, orphan, empty, untitled, stale (verification overdue)
or old pages.

The computation is `services.health`'s, without AI; the page only displays it, with a link to
each page and, for contributors, to its editor."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.services import health, reports
from synaptane.web.pages import WebPage, find_space
from synaptane.web.wiki import owner_label

router = APIRouter()


@router.get("/o/{org_slug}/{space_slug}/health", response_class=HTMLResponse)
def space_health_page(
    page: WebPage, org_slug: str, space_slug: str, months: int = health.STALE_MONTHS
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    try:
        report = health.space_health(actor, page.workspace, space, months)
    except SynaptaneError as error:
        if error.code is not ErrorCode.NOT_FOUND:
            raise
        report = None  # space not synced yet
    return page.render(
        "space-health.html",
        org=space.organization,
        space=space,
        report=report,
        reported=reports.space_reports(page.db, actor, space),
        contribute=page.can_contribute(actor, space),
        owner_label=lambda owner: owner_label(page, owner),
    )
