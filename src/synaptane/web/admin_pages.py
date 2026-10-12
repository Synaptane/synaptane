"""Instance administration (super-admin)."""

from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, Response

from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError
from synaptane.services import (
    accounts,
    organizations,
    sso,
)
from synaptane.web.pages import (
    Csrf,
    WebPage,
    failure,
    org_url,
    redirect,
    send_email_check,
)

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_page(page: WebPage) -> Response:
    actor = page.user_actor()
    require(actor.access, Action.MANAGE_INSTANCE)
    return page.render(
        "admin-orgs.html", section="orgs", orgs=organizations.list_organizations(page.db, actor)
    )


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(page: WebPage) -> Response:
    actor = page.user_actor()
    return page.render(
        "admin-users.html", section="users", users=accounts.list_users(page.db, actor)
    )


@router.get("/admin/users/{username}", response_class=HTMLResponse)
def admin_user_page(page: WebPage, username: str) -> Response:
    actor = page.user_actor()
    require(actor.access, Action.MANAGE_INSTANCE)
    return page.render(
        "admin-user.html", section="users", account=accounts.get_user(page.db, username)
    )


@router.get("/admin/sso", response_class=HTMLResponse)
def admin_sso_page(page: WebPage, edit: str = "") -> Response:
    """Providers, and the form: empty to add one, filled with a provider's settings (never
    its secret) when `edit` names it."""
    actor = page.user_actor()
    require(actor.access, Action.MANAGE_INSTANCE)
    providers = sso.list_providers(page.db)
    return page.render(
        "admin-sso.html",
        section="sso",
        sso_providers=providers,
        editing=next((p for p in providers if p.slug == edit), None),
        open_providers={p.slug for p in providers if sso.open_to_anyone(p)},
        sso_kinds=sso.KINDS,
        public_url=page.settings.public_url,
    )


@router.post("/admin/users")
def admin_user_create(
    page: WebPage,
    csrf: Csrf,
    username: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
    email: Annotated[str, Form()] = "",
    is_superadmin: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        user = accounts.create_user(
            page.db,
            actor,
            username=username,
            display_name=display_name,
            password=password,
            email=email,
            is_superadmin=is_superadmin,
            locale=page.settings.default_locale,
        )
    except SynaptaneError as error:
        return failure("/admin/users", page, error)
    if user.email:
        send_email_check(page, user)
    return redirect("/admin/users", page, "Account created.")


@router.post("/admin/users/{username}")
def admin_user_update(
    page: WebPage,
    username: str,
    csrf: Csrf,
    is_active: Annotated[bool, Form()] = False,
    is_superadmin: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        user = accounts.get_user(page.db, username)
        accounts.update_user(page.db, actor, user, is_active=is_active, is_superadmin=is_superadmin)
    except SynaptaneError as error:
        return failure(f"/admin/users/{username}", page, error)
    return redirect(f"/admin/users/{username}", page, "Account updated.")


@router.post("/admin/users/{username}/password")
def admin_user_password(
    page: WebPage, username: str, csrf: Csrf, password: Annotated[str, Form()]
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        accounts.set_password(page.db, actor, accounts.get_user(page.db, username), password)
    except SynaptaneError as error:
        return failure(f"/admin/users/{username}", page, error)
    return redirect(f"/admin/users/{username}", page, "Password reset.")


@router.post("/admin/organizations")
def admin_org_create(
    page: WebPage,
    csrf: Csrf,
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    join: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        org = organizations.create_organization(
            page.db, actor, slug=slug, name=name, description=description, join=join
        )
    except SynaptaneError as error:
        return failure("/admin", page, error)
    return redirect(org_url(org), page, "Organization created.")
