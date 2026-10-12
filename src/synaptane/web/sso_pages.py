"""SSO sign-in, administration of providers and of organization webhooks."""

import hmac
from typing import Annotated
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from synaptane.core.errors import SynaptaneError
from synaptane.services import accounts, organizations, sso, webhooks
from synaptane.web.pages import (
    SESSION_COOKIE,
    Csrf,
    WebPage,
    failure,
    org_url,
    redirect,
    reveal,
    safe_next,
)

router = APIRouter()


def callback_url(page: WebPage, slug: str) -> str:
    return f"{page.settings.public_url}/sso/{slug}/callback"


@router.get("/sso/{slug}/start")
def sso_start(page: WebPage, slug: str, next: str = "/") -> Response:
    try:
        url = sso.start(page.db, slug, callback_url(page, slug), safe_next(next))
    except SynaptaneError as error:
        return failure(f"/login?next={quote(safe_next(next))}", page, error)
    return _to_provider(page, url)


@router.post("/account/identities/link/{slug}")
def account_link(page: WebPage, slug: str, csrf: Csrf) -> Response:
    """Links a provider to the signed-in account (useful when linking by email is off)."""
    actor = page.check_csrf(csrf)
    try:
        url = sso.start(
            page.db, slug, callback_url(page, slug), "/account/security", link_user=actor.user.id
        )
    except SynaptaneError as error:
        return failure("/account/security", page, error)
    return _to_provider(page, url)


def _to_provider(page: WebPage, url: str) -> Response:
    response = RedirectResponse(url, status_code=303)
    # The provider's callback must come from this browser: otherwise a third party could make
    # someone open the third party's own callback and sign them into the third party's account.
    state = parse_qs(urlsplit(url).query).get("state", [""])[0]
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=600,
        path="/sso/",
        httponly=True,
        samesite="lax",
        secure=page.settings.secure_cookies,
    )
    return response


STATE_COOKIE = "synaptane_sso_state"


@router.get("/sso/{slug}/callback")
def sso_callback(
    page: WebPage, slug: str, code: str = "", state: str = "", error: str = ""
) -> Response:
    if error or not code:
        page.db.rollback()
        return redirect("/login", page, "Sign-in canceled at the provider.", kind="error")
    expected = page.request.cookies.get(STATE_COOKIE, "")
    if not expected or not hmac.compare_digest(expected, state):
        page.db.rollback()
        return redirect(
            "/login", page, "Sign-in expired or tampered with: please start again.", kind="error"
        )
    back = "/account/security" if page.actor is not None else "/login"
    try:
        user, next_url, linking = sso.complete(
            page.db, page.box, slug, code, state, callback_url(page, slug), page.locale
        )
    except SynaptaneError as failure_:
        page.db.rollback()
        return redirect(back, page, failure_.template, kind="error", **failure_.params)
    if linking:  # the link must come back in the session of the account that requested it
        if page.actor is None or page.actor.user.id != user.id:
            page.db.rollback()
            return redirect(back, page, "Linking refused: sign in again and retry.", kind="error")
        response = redirect(next_url, page, "Provider account linked.")
        response.delete_cookie(STATE_COOKIE, path="/sso/")
        return response
    token, _ = accounts.open_web_session(page.db, user, page.settings.session_days)
    response = RedirectResponse(safe_next(next_url), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=page.settings.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=page.settings.secure_cookies,
    )
    response.delete_cookie(STATE_COOKIE, path="/sso/")
    return response


@router.post("/admin/sso")
def admin_sso_save(
    page: WebPage,
    csrf: Csrf,
    slug: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    name: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    client_secret: Annotated[str, Form()] = "",
    issuer: Annotated[str, Form()] = "",
    auto_create: Annotated[bool, Form()] = False,
    enabled: Annotated[bool, Form()] = False,
    link_by_email: Annotated[bool, Form()] = False,
    allowed_domains: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        provider = sso.save_provider(
            page.db,
            actor,
            page.box,
            slug=slug,
            kind=kind,
            name=name,
            client_id=client_id,
            client_secret=client_secret or None,
            issuer=issuer,
            auto_create=auto_create,
            enabled=enabled,
            link_by_email=link_by_email,
            allowed_domains=[allowed_domains],
        )
    except SynaptaneError as error:
        return failure("/admin/sso", page, error)
    if sso.open_to_anyone(provider):
        return redirect(
            "/admin/sso",
            page,
            "Provider saved, but open to everyone: any {name} account can create an account "
            "here. Enter allowed domains or turn off automatic account creation.",
            kind="warning",
            name=provider.name,
        )
    return redirect("/admin/sso", page, "Sign-in provider saved.")


@router.post("/admin/sso/{slug}/delete")
def admin_sso_delete(page: WebPage, slug: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    try:
        sso.delete_provider(page.db, actor, slug)
    except SynaptaneError as error:
        return failure("/admin/sso", page, error)
    return redirect("/admin/sso", page, "Sign-in provider deleted.")


@router.post("/account/identities/{identity_id}/unlink")
def account_unlink(page: WebPage, identity_id: UUID, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    try:
        sso.unlink(page.db, actor, identity_id)
    except SynaptaneError as error:
        return failure("/account/security", page, error)
    return redirect("/account/security", page, "Identity unlinked.")


# --- Webhooks ---------------------------------------------------------------------------------


@router.post("/o/{org_slug}/-/webhooks", response_class=HTMLResponse)
def org_webhook_create(
    page: WebPage,
    org_slug: str,
    csrf: Csrf,
    url: Annotated[str, Form()],
    events: Annotated[list[str] | None, Form()] = None,
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        _, secret = webhooks.create(
            page.db,
            actor,
            page.box,
            org,
            url,
            events or [],
            allow_private=page.settings.allow_private_webhooks,
        )
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/webhooks", page, error)
    return reveal(f"{org_url(org)}/-/webhooks", page, actor, "Webhook created.", secret)


@router.post("/o/{org_slug}/-/webhooks/{webhook_id}/delete")
def org_webhook_delete(page: WebPage, org_slug: str, webhook_id: UUID, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    webhooks.delete(page.db, actor, org, webhook_id)
    return redirect(f"{org_url(org)}/-/webhooks", page, "Webhook deleted.")


@router.post("/o/{org_slug}/-/webhooks/{webhook_id}/test")
def org_webhook_test(page: WebPage, org_slug: str, webhook_id: UUID, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    webhooks.send_test(page.db, actor, org, webhook_id)
    return redirect(
        f"{org_url(org)}/-/webhooks",
        page,
        "Test sent: the status updates in a few seconds.",
    )


@router.post("/o/{org_slug}/-/webhooks/{webhook_id}/reactivate")
def org_webhook_reactivate(page: WebPage, org_slug: str, webhook_id: UUID, csrf: Csrf) -> Response:
    """Puts back into service a webhook cut off by the circuit breaker (repeated failures)."""
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    webhooks.reactivate(page.db, actor, org, webhook_id)
    return redirect(f"{org_url(org)}/-/webhooks", page, "Webhook re-enabled.")
