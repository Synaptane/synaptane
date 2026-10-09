"""My account: profile, security, tokens, connected applications."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from synaptane.core.errors import SynaptaneError
from synaptane.services import (
    accounts,
    assistants,
    mail,
    sso,
)
from synaptane.web.pages import (
    LANG_COOKIE,
    Csrf,
    WebPage,
    chosen_limits,
    failure,
    log,
    member_spaces,
    redirect,
    reveal,
    send_email_check,
    sign_in,
)

router = APIRouter()


@router.get("/account", response_class=HTMLResponse)
def account_page(page: WebPage) -> Response:
    page.user_actor()
    return page.render("account-profile.html", section="profile")


@router.get("/account/security", response_class=HTMLResponse)
def account_security_page(page: WebPage) -> Response:
    actor = page.user_actor()
    return page.render(
        "account-security.html",
        section="security",
        identities=(linked := sso.identities(page.db, actor.user.id)),
        linkable=[
            p
            for p in sso.list_providers(page.db, enabled_only=True)
            if p.slug not in {provider.slug for _, provider in linked}
        ],
    )


@router.get("/account/assistants/activity")
def account_assistants_activity(page: WebPage, since: str = "") -> JSONResponse:
    """MCP calls received since `since` (ISO 8601): the connection test waits for them."""
    actor = page.user_actor()
    try:
        after = datetime.fromisoformat(since) if since else None
    except ValueError:
        after = None
    uses = assistants.recent(page.db, actor.user.id, after)
    from synaptane.mcp.oauth import connected_apps

    names = {t.name for t in accounts.list_api_tokens(page.db, actor)}
    apps = {name for _, name, _ in connected_apps(page.db, actor.user.id)}
    translate = page.translate
    return JSONResponse(
        {
            "uses": [
                {
                    "via": u.via,
                    "label": _client_text(translate, *client_label(u.via, names, apps)),
                    "at": u.last_at.isoformat(),
                }
                for u in uses
            ]
        },
        headers={"Cache-Control": "no-store"},
    )


def client_label(via: str, token_names: set[str], app_names: set[str]) -> tuple[str, str]:
    """Client of an MCP call as shown: (name, kind). `via` is "MCP · name", where name is a
    personal token's name or the name the OAuth application gave itself. The kind ("token",
    "app") is given only if a current token or application has that name (otherwise "", e.g.
    for a revoked token); empty name if the client is unknown."""
    name = via.removeprefix("MCP · ").removeprefix("MCP").strip()
    kind = "token" if name in token_names else "app" if name in app_names else ""
    return name, kind


def _client_text(translate: Callable[..., str], name: str, kind: str) -> str:
    if not name:
        return translate("an MCP client")
    if kind == "token":
        return translate("token “{name}”", name=name)
    return name


@router.get("/account/assistants", response_class=HTMLResponse)
def account_assistants_page(page: WebPage) -> Response:
    actor = page.user_actor()
    from synaptane.mcp.oauth import connected_apps

    reachable = member_spaces(page, actor)
    tokens = accounts.list_api_tokens(page.db, actor)
    apps = connected_apps(page.db, actor.user.id)
    names, app_names = {t.name for t in tokens}, {name for _, name, _ in apps}
    return page.render(
        "account-assistants.html",
        section="assistants",
        uses=[
            (*client_label(u.via, names, app_names), u.last_at, u.calls)
            for u in assistants.recent(page.db, actor.user.id)
        ],
        now=datetime.now(UTC),
        tokens=tokens,
        apps=apps,
        mcp_url=f"{page.settings.public_url}/mcp",
        member_spaces=reachable,
        space_labels={str(s.id): f"{s.organization.name} / {s.name}" for s in reachable},
    )


@router.post("/account")
def account_update(
    page: WebPage,
    csrf: Csrf,
    display_name: Annotated[str, Form()],
    locale: Annotated[str, Form()] = "",  # chosen separately, under "Preferences"
    email: Annotated[str, Form()] = "",
    current: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    previous = (actor.user.email, actor.user.email_verified_at)
    try:
        if email.strip().lower() not in ("", previous[0] or ""):
            # Refused upfront rather than a changed address whose link could not be sent.
            accounts.check_email_quota(page.db, actor.user)
        accounts.update_user(
            page.db,
            actor,
            actor.user,
            display_name=display_name,
            email=email,
            locale=locale or None,
            current_password=current,
        )
    except SynaptaneError as error:
        return failure("/account", page, error)
    page.locale = actor.user.locale
    message = "Profile updated."
    if actor.user.email != previous[0]:
        old_email, was_verified = previous
        if old_email and was_verified and mail.configured(page.db):
            subject, body = mail.compose(
                "email_changed", actor.user.locale, new=actor.user.email or "—"
            )
            try:
                mail.send(page.db, page.box, old_email, subject, body)  # notify the old one
            except mail.MailError as error:
                log.warning("E-mail change notice not sent: %s", error)
        if actor.user.email:
            message = (
                "Profile updated. A verification link was sent to your new address."
                if send_email_check(page, actor.user)
                else "Profile updated. The address can only be verified once e-mail sending is "
                "set up."
            )
    response = redirect("/account", page, message)
    response.set_cookie(LANG_COOKIE, actor.user.locale, max_age=365 * 86400, samesite="lax")
    return response


@router.post("/account/locale")
def account_locale(page: WebPage, csrf: Csrf, locale: Annotated[str, Form()]) -> Response:
    """Account language, saved separately from the profile (it applies on every device)."""
    actor = page.check_csrf(csrf)
    try:
        accounts.update_user(page.db, actor, actor.user, locale=locale)
    except SynaptaneError as error:
        return failure("/account", page, error)
    page.locale = actor.user.locale
    response = redirect("/account", page, "Language saved.")
    response.set_cookie(LANG_COOKIE, actor.user.locale, max_age=365 * 86400, samesite="lax")
    return response


@router.post("/account/notifications")
def account_notifications(
    page: WebPage,
    csrf: Csrf,
    notify_tidy: Annotated[bool, Form()] = False,
    notify_routines: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        accounts.set_tidy_notices(page.db, actor, notify_tidy)
        accounts.set_routine_notices(page.db, actor, notify_routines)
    except SynaptaneError as error:
        return failure("/account", page, error)
    return redirect("/account", page, "Preference saved.")


@router.post("/account/email/verify")
def account_email_verify(page: WebPage, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    if not actor.user.email or actor.user.email_verified_at is not None:
        return redirect("/account", page, "Nothing to verify.")
    try:
        sent = send_email_check(page, actor.user)
    except SynaptaneError as error:  # sending limit reached
        return failure("/account", page, error)
    if not sent:
        return redirect(
            "/account",
            page,
            "Cannot send: e-mail sending is not set up.",
            kind="error",
        )
    return redirect("/account", page, "Verification link sent to {email}.", email=actor.user.email)


@router.get("/verify-email/{token}", response_class=HTMLResponse)
def verify_email_page(page: WebPage, token: str) -> Response:
    """Link received by e-mail: asks for confirmation, signed in to the account that requested it.

    Opening the link verifies nothing (mail antivirus scanners open links on their own): the
    button, sent from that account's session, is what verifies the address."""
    try:
        check, owner = accounts.email_check_target(page.db, token)
    except SynaptaneError as error:
        return page.render("verify-email.html", status_code=410, error=error, token=token)
    if page.actor is None:
        return RedirectResponse(f"/login?next={quote(f'/verify-email/{token}')}", 303)
    page.user_actor()  # password to change first
    return page.render(
        "verify-email.html", error=None, token=token, email=check.email, account=owner
    )


@router.post("/verify-email/{token}", response_class=HTMLResponse)
def verify_email(page: WebPage, csrf: Csrf, token: str) -> Response:
    actor = page.check_csrf(csrf)
    try:
        accounts.confirm_email(page.db, token, actor.user)
    except SynaptaneError as error:
        page.db.rollback()
        return page.render("verify-email.html", status_code=403, error=error, token=token)
    return redirect("/account", page, "E-mail address verified.")


@router.post("/account/password")
def account_password(
    page: WebPage,
    csrf: Csrf,
    password: Annotated[str, Form()],
    current: Annotated[str, Form()] = "",  # empty for an account created through SSO
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        accounts.set_password(page.db, actor, actor.user, password, current=current)
    except SynaptaneError as error:
        return failure("/account/security", page, error)
    # Other sessions and tokens are closed; this one is reopened right away.
    return sign_in(page, actor.user, "/", "Password changed. Other sessions are closed.")


@router.post("/account/tokens", response_class=HTMLResponse)
def account_token_create(
    page: WebPage,
    csrf: Csrf,
    name: Annotated[str, Form()],
    days: Annotated[int | None, Form()] = 90,
    access: Annotated[str, Form()] = "write",
    scope: Annotated[str, Form()] = "all",
    space: Annotated[list[str] | None, Form()] = None,
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        read_only, space_ids = chosen_limits(access, scope, space)
        value, _ = accounts.create_api_token(
            page.db, actor, name, days or None, read_only=read_only, space_ids=space_ids
        )
    except SynaptaneError as error:
        return failure("/account/assistants", page, error)
    # No extra message: the token card, at the top of the page, already says it was created.
    return reveal("/account/assistants", page, actor, "", value)


@router.post("/account/tokens/{token_id}/revoke")
def account_token_revoke(page: WebPage, token_id: UUID, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    try:
        accounts.revoke_api_token(page.db, actor, token_id)
    except SynaptaneError as error:
        return failure("/account/assistants", page, error)
    return redirect("/account/assistants", page, "Token revoked.")
