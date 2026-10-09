"""OAuth consent: the signed-in user authorizes an MCP application (Claude…)."""

from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from synaptane.core.errors import SynaptaneError, not_found
from synaptane.mcp import oauth
from synaptane.services import accounts, audit
from synaptane.web.pages import Csrf, WebPage, chosen_limits, member_spaces, redirect

router = APIRouter()


@router.get("/oauth/consent", response_class=HTMLResponse)
def consent_page(page: WebPage, request: str) -> Response:
    if page.actor is None:
        target = f"/oauth/consent?request={quote(request)}"
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    pending = oauth.pending_request(page.database, request)
    if pending is None:
        raise not_found(
            "Authorization request expired: start the connection again from your application."
        )
    return _consent(page, request)


def _consent(
    page: WebPage, request: str, status_code: int = 200, limits_error: SynaptaneError | None = None
) -> Response:
    pending = oauth.pending_request(page.database, request)
    if pending is None:
        raise not_found(
            "Authorization request expired: start the connection again from your application."
        )
    client, params = pending
    name = client.client_name or client.client_id
    return page.render(
        "consent.html",
        status_code=status_code,
        member_spaces=member_spaces(page, page.user_actor()),
        request_id=request,
        client_name=name,
        target=oauth.redirect_target(params.redirect_uri),
        lookalike=oauth.lookalike(name, params.redirect_uri),
        limits_error=limits_error,
    )


@router.post("/oauth/consent")
def consent_decision(
    page: WebPage,
    csrf: Csrf,
    request: Annotated[str, Form()],
    decision: Annotated[str, Form()],
    access: Annotated[str, Form()] = "write",
    scope: Annotated[str, Form()] = "all",
    space: Annotated[list[str] | None, Form()] = None,
) -> Response:
    actor = page.check_csrf(csrf)
    approved = decision == "allow"
    try:
        read_only, chosen = chosen_limits(access, scope, space)
        space_ids = accounts.checked_spaces(page.db, actor, chosen) if approved else None
    except SynaptaneError as error:
        # Incomplete choice: stay on the consent page, the request is not lost.
        return _consent(page, request, status_code=400, limits_error=error)
    target = oauth.decide(
        page.db,
        request,
        actor.user if approved else None,
        read_only=read_only,
        space_ids=space_ids,
    )
    if target is None:
        raise not_found(
            "Authorization request expired: start the connection again from your application."
        )
    audit(page.db, actor, "oauth.approved" if approved else "oauth.denied")
    return RedirectResponse(target, status_code=303)


@router.post("/account/apps/{family}/revoke")
def revoke_app(page: WebPage, family: UUID, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    oauth.revoke_user_family(page.db, actor.user.id, family)
    audit(page.db, actor, "oauth.revoked")
    return redirect("/account/assistants", page, "Application disconnected.")
