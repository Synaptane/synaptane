"""Getting into Synaptane without going through the super-admin: invitations, forgotten
password, email sending settings."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, Response
from starlette.background import BackgroundTask

from synaptane.core.access import Action, OrgRole, require
from synaptane.core.errors import SynaptaneError
from synaptane.core.security import SecretBox
from synaptane.core.throttle import LoginThrottle
from synaptane.services import accounts as accounts_service
from synaptane.services import invitations, mail, organizations
from synaptane.storage.db import Database
from synaptane.storage.models import User
from synaptane.web.pages import (
    Csrf,
    Page,
    WebPage,
    failure,
    org_url,
    redirect,
    reveal,
    sign_in,
)

log = logging.getLogger("synaptane.mail")
router = APIRouter()


# --- Invitations (organization admins) ------------------------------------------------------------


@router.post("/o/{org_slug}/-/invitations")
def org_invite(
    page: WebPage,
    org_slug: str,
    csrf: Csrf,
    email: Annotated[str, Form()],
    org_role: Annotated[OrgRole, Form()] = OrgRole.MEMBER,
    space_role: Annotated[list[str] | None, Form()] = None,
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    target = f"{org_url(org)}/-/members"
    # One menu per space: "<id>:<role>", or empty for no access.
    chosen = dict(value.split(":", 1) for value in space_role or [] if ":" in value)
    try:
        created = invitations.invite(
            page.db, actor, org, email=email, org_role=org_role, space_roles=chosen
        )
    except SynaptaneError as error:
        return failure(target, page, error)
    if created.existing is not None:
        _notify_added(page, created.existing, email, org.name, actor.user.display_name)
        if mail.configured(page.db):  # same response as an unknown address: nothing leaks
            return redirect(target, page, "Invitation sent to {email}.", email=email)
        return redirect(target, page, "Existing account: access granted to {name}.", name=email)
    assert created.token is not None and created.invitation is not None
    email = created.invitation.email  # normalized address (lowercase, no spaces)
    link = f"{page.settings.public_url}/invite/{created.token}"
    subject, body = mail.compose(
        "invitation",
        actor.user.locale,
        inviter=actor.user.display_name,
        org=org.name,
        days=invitations.TTL.days,
        link=link,
    )
    if mail.configured(page.db):
        try:
            mail.send(page.db, page.box, email, subject, body)
        except mail.MailError as error:
            log.warning("Invitation not sent to %s: %s", email, error)
            return reveal(
                target,
                page,
                actor,
                "The email could not be sent: pass on this invitation link yourself.",
                link,
            )
        return redirect(target, page, "Invitation sent to {email}.", email=email)
    return reveal(
        target,
        page,
        actor,
        "Invitation created. As email sending is not configured, pass on this link.",
        link,
    )


def _notify_added(page: Page, user: User, email: str, org: str, inviter: str) -> None:
    """Notice to an existing account, in its language."""
    if not mail.configured(page.db):
        return
    email = user.email or email
    subject, body = mail.compose(
        "added", user.locale, inviter=inviter, org=org, link=page.settings.public_url
    )
    try:
        mail.send(page.db, page.box, email, subject, body)
    except mail.MailError as error:
        log.warning("Access notice not sent to %s: %s", email, error)


@router.post("/o/{org_slug}/-/invitations/{invitation_id}/revoke")
def org_invitation_revoke(
    page: WebPage, org_slug: str, invitation_id: UUID, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        invitations.revoke(page.db, actor, org, invitation_id)
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/members", page, error)
    return redirect(f"{org_url(org)}/-/members", page, "Invitation canceled.")


@router.get("/invite/{token}", response_class=HTMLResponse)
def invitation_page(page: WebPage, token: str) -> Response:
    try:
        invitation = invitations.find(page.db, token)
    except SynaptaneError as error:
        return page.render("invite.html", status_code=410, invitation=None, error=error)
    from synaptane.storage.models import Organization

    org = page.db.get(Organization, invitation.org_id)
    inviter, granted = invitations.describe(page.db, invitation)
    return page.render(
        "invite.html",
        invitation=invitation,
        org=org,
        token=token,
        error=None,
        inviter=inviter,
        granted=granted,
    )


@router.post("/invite/{token}", response_class=HTMLResponse)
def invitation_accept(
    page: WebPage,
    token: str,
    username: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    try:
        user = invitations.accept(
            page.db,
            token,
            username=username,
            display_name=display_name,
            password=password,
            locale=page.locale,
        )
    except SynaptaneError as error:
        page.db.rollback()
        try:
            invitation = invitations.find(page.db, token)
        except SynaptaneError:
            invitation = None
        from synaptane.storage.models import Organization

        org = page.db.get(Organization, invitation.org_id) if invitation else None
        return page.render(
            "invite.html",
            status_code=400,
            invitation=invitation,
            org=org,
            token=token,
            error=error,
            form={"username": username, "display_name": display_name},
        )
    return sign_in(page, user, "/", "Welcome to Synaptane!")


# --- Forgotten password ---------------------------------------------------------------------------


@router.get("/login/forgot", response_class=HTMLResponse)
def forgot_page(page: WebPage) -> Response:
    return page.render("forgot.html", sent=False, mail_ready=mail.configured(page.db))


@router.post("/login/forgot", response_class=HTMLResponse)
def forgot(page: WebPage, login: Annotated[str, Form()]) -> Response:
    throttle: LoginThrottle = page.request.app.state.login_throttle
    address = page.request.client.host if page.request.client else "?"
    # Separate counter ("reset" prefix): requesting a link does not block signing in.
    account = "reset:" + accounts_service.login_key(page.db, login)
    if throttle.wait(account, address):  # no burst of emails to the same person
        return page.render("forgot.html", sent=True, mail_ready=mail.configured(page.db))
    throttle.failed(account, address)
    request = accounts_service.request_password_reset(page.db, login)
    response = page.render("forgot.html", sent=True, mail_ready=mail.configured(page.db))
    if request is not None and mail.configured(page.db):
        user, token = request
        page.db.commit()
        subject, body = mail.compose(
            "reset", user.locale, link=f"{page.settings.public_url}/reset/{token}"
        )
        # Sent after the response: its duration does not reveal whether the account exists.
        response.background = BackgroundTask(
            _send_quietly, page.database, page.box, user.email or "", subject, body
        )
    # Always the same response: which accounts exist is not revealed.
    return response


def _send_quietly(database: Database, box: SecretBox, to: str, subject: str, body: str) -> None:
    with database.session() as session:
        try:
            mail.send(session, box, to, subject, body)
        except mail.MailError as error:
            log.warning("Reset link not sent: %s", error)


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_page(page: WebPage, token: str) -> Response:
    try:
        user = accounts_service.reset_target(page.db, token)
    except SynaptaneError as error:
        return page.render("reset.html", status_code=410, account=None, error=error, token=token)
    return page.render("reset.html", account=user, error=None, token=token)


@router.post("/reset/{token}", response_class=HTMLResponse)
def reset(page: WebPage, token: str, password: Annotated[str, Form()]) -> Response:
    try:
        user = accounts_service.reset_password(page.db, token, password)
    except SynaptaneError as error:
        page.db.rollback()
        try:
            account = accounts_service.reset_target(page.db, token)
        except SynaptaneError:
            account = None
        return page.render("reset.html", status_code=400, account=account, error=error, token=token)
    return sign_in(page, user, "/", "Password changed.")


# --- Email sending (super-admin) -----------------------------------------------------------------


@router.get("/admin/mail", response_class=HTMLResponse)
def admin_mail_page(page: WebPage) -> Response:
    actor = page.user_actor()
    require(actor.access, Action.MANAGE_INSTANCE)
    return page.render("admin-mail.html", section="mail", settings_view=mail.view(page.db, actor))


@router.post("/admin/mail")
def admin_mail_save(
    page: WebPage,
    csrf: Csrf,
    host: Annotated[str, Form()],
    sender: Annotated[str, Form()],
    port: Annotated[int, Form()] = 587,
    security: Annotated[str, Form()] = "starttls",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    clear_password: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        mail.configure(
            page.db,
            actor,
            page.box,
            host=host,
            port=port,
            security=security,
            sender=sender,
            username=username,
            password="" if clear_password else (password or None),
        )
    except SynaptaneError as error:
        return failure("/admin/mail", page, error)
    return redirect("/admin/mail", page, "Email sending saved.")


@router.post("/admin/mail/test")
def admin_mail_test(page: WebPage, csrf: Csrf, to: Annotated[str, Form()] = "") -> Response:
    actor = page.check_csrf(csrf)
    require(actor.access, Action.MANAGE_INSTANCE)
    address = to.strip() or (actor.user.email or "")
    if "@" not in address:
        return redirect("/admin/mail", page, "Enter the test address.", kind="error")
    subject, body = mail.compose("test", actor.user.locale)
    try:
        mail.send(page.db, page.box, address, subject, body)
    except mail.MailError as error:
        return redirect("/admin/mail", page, error.label, kind="error")
    return redirect("/admin/mail", page, "Test email sent to {email}.", email=address)


@router.post("/admin/mail/delete")
def admin_mail_delete(page: WebPage, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    mail.remove(page.db, actor)
    return redirect("/admin/mail", page, "Email sending disabled.")
