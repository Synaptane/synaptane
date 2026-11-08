"""Groups of an organization's members (Groups tab of the organization), and the roles of the
groups on a space (from the space's Members page)."""

from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, Response

from synaptane.core.access import Action, OrgRole, SpaceRole, can, higher_role
from synaptane.core.errors import Label, SynaptaneError, not_found
from synaptane.services import Actor, accounts, groups, organizations, spaces
from synaptane.services.spaces import SpaceAccess
from synaptane.storage.models import Group, Organization, Space
from synaptane.web.org_pages import _org_page
from synaptane.web.pages import (
    Csrf,
    Page,
    WebPage,
    failure,
    find_space,
    org_url,
    redirect,
    space_settings_url,
)

router = APIRouter()

ROLE_LABELS = {
    SpaceRole.READER: Label("Reader"),
    SpaceRole.CONTRIBUTOR: Label("Contributor"),
}


def access_label(entry: SpaceAccess) -> Label:
    """Where someone's access to a space comes from (Members page of the space)."""
    if entry.effective == OrgRole.ADMIN:
        return Label("Organization administrator")
    role = ROLE_LABELS[SpaceRole(entry.effective)]
    if entry.source == "group":
        names = ", ".join(entry.via_groups)
        if len(entry.via_groups) == 1:
            return Label("{role}, through the group {names}", role=role, names=names)
        return Label("{role}, through the groups {names}", role=role, names=names)
    if entry.source == "default":
        return Label("{role}, by default", role=role)
    return role


def group_url(group: Group, org: Organization) -> str:
    return f"{org_url(org)}/-/groups/{group.id}"


def _find_group(
    page: Page, actor: Actor, org_slug: str, group_id: str
) -> tuple[Organization, Group]:
    """The group, for the organization's administrators: like the organization's other tabs,
    the page does not exist for its members."""
    org = organizations.get_organization(page.db, actor, org_slug)
    if not can(actor.access, Action.MANAGE_ORG, org_id=org.id):
        raise not_found("Page not found.")
    return org, groups.get_group(page.db, actor, org, group_id)


def _role_saved(page: Page, target: str, group: Group, space: Space, role: SpaceRole) -> Response:
    """Confirmation of a group's role on a space: says when it opens a restricted space, or
    when the organization's default role already gives as much to everyone."""
    if space.restricted:
        return redirect(
            target,
            page,
            "Access saved. {space} is restricted: the group opens it to its members.",
            space=space.name,
        )
    default = spaces.default_role(space)
    if default is not None and higher_role(default, role) == default:
        return redirect(
            target,
            page,
            "Access saved. The organization's default role already gives every member as much "
            "on {space}: the group adds nothing there for now.",
            kind="warning",
            space=space.name,
        )
    return redirect(target, page, "Access saved.")


# --- Groups of the organization ----------------------------------------------------------------


@router.get("/o/{org_slug}/-/groups", response_class=HTMLResponse)
def groups_page(page: WebPage, org_slug: str) -> Response:
    actor = page.user_actor()
    org = organizations.get_organization(page.db, actor, org_slug)
    return _org_page(
        page,
        actor,
        org,
        "groups",
        "org-groups.html",
        org_groups=(
            groups.list_groups(page.db, actor, org)
            if can(actor.access, Action.MANAGE_ORG, org_id=org.id)
            else []
        ),
    )


@router.post("/o/{org_slug}/-/groups")
def group_create(
    page: WebPage,
    org_slug: str,
    csrf: Csrf,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        group = groups.create_group(page.db, actor, org, name=name, description=description)
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/groups", page, error)
    return redirect(
        group_url(group, org),
        page,
        "Group created. Add its members, then the spaces it opens to them.",
    )


@router.get("/o/{org_slug}/-/groups/{group_id}", response_class=HTMLResponse)
def group_page(page: WebPage, org_slug: str, group_id: str) -> Response:
    actor = page.user_actor()
    org, group = _find_group(page, actor, org_slug, group_id)
    members = groups.list_members(page.db, actor, group)
    inside = {user.username for user in members}
    return _org_page(
        page,
        actor,
        org,
        "groups",
        "org-group.html",
        group=group,
        members=members,
        group_spaces=groups.list_spaces(page.db, actor, group),
        # Only the members of the organization join its groups.
        people=[
            (user.username, user.display_name)
            for user, _ in organizations.list_members(page.db, actor, org)
            if user.username not in inside
        ],
        org_spaces=spaces.list_spaces(page.db, actor, org),
    )


@router.post("/o/{org_slug}/-/groups/{group_id}/update")
def group_update(
    page: WebPage,
    org_slug: str,
    group_id: str,
    csrf: Csrf,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    org, group = _find_group(page, actor, org_slug, group_id)
    try:
        groups.update_group(page.db, actor, group, name=name, description=description)
    except SynaptaneError as error:
        return failure(group_url(group, org), page, error)
    return redirect(group_url(group, org), page, "Group saved.")


@router.post("/o/{org_slug}/-/groups/{group_id}/delete")
def group_delete(page: WebPage, org_slug: str, group_id: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    org, group = _find_group(page, actor, org_slug, group_id)
    name = group.name
    groups.delete_group(page.db, actor, group)
    return redirect(
        f"{org_url(org)}/-/groups",
        page,
        "Group {name} deleted: its members no longer have the access it gave them, unless they "
        "have it otherwise.",
        name=name,
    )


@router.post("/o/{org_slug}/-/groups/{group_id}/members")
def group_member_add(
    page: WebPage,
    org_slug: str,
    group_id: str,
    csrf: Csrf,
    username: Annotated[str, Form()],
) -> Response:
    actor = page.check_csrf(csrf)
    org, group = _find_group(page, actor, org_slug, group_id)
    try:
        user = accounts.get_user(page.db, username)
        added = groups.add_member(page.db, actor, group, user)
    except SynaptaneError as error:
        return failure(group_url(group, org), page, error)
    if not added:
        return redirect(
            group_url(group, org), page, "{name} is already in the group.", name=user.display_name
        )
    return redirect(
        group_url(group, org),
        page,
        "{name} added to the group: they now access its spaces.",
        name=user.display_name,
    )


@router.post("/o/{org_slug}/-/groups/{group_id}/members/{username}/remove")
def group_member_remove(
    page: WebPage, org_slug: str, group_id: str, username: str, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    org, group = _find_group(page, actor, org_slug, group_id)
    try:
        user = accounts.get_user(page.db, username)
        groups.remove_member(page.db, actor, group, user)
    except SynaptaneError as error:
        return failure(group_url(group, org), page, error)
    return redirect(
        group_url(group, org),
        page,
        "{name} removed from the group.",
        name=user.display_name,
    )


@router.post("/o/{org_slug}/-/groups/{group_id}/spaces")
def group_space_set(
    page: WebPage,
    org_slug: str,
    group_id: str,
    csrf: Csrf,
    space: Annotated[str, Form()],
    role: Annotated[SpaceRole, Form()],
) -> Response:
    actor = page.check_csrf(csrf)
    org, group = _find_group(page, actor, org_slug, group_id)
    try:
        target = spaces.get_space(page.db, actor, org, space)
        groups.set_space_role(page.db, actor, group, target, role)
    except SynaptaneError as error:
        return failure(group_url(group, org), page, error)
    return _role_saved(page, group_url(group, org), group, target, role)


@router.post("/o/{org_slug}/-/groups/{group_id}/spaces/{space_slug}/remove")
def group_space_remove(
    page: WebPage, org_slug: str, group_id: str, space_slug: str, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    org, group = _find_group(page, actor, org_slug, group_id)
    try:
        target = spaces.get_space(page.db, actor, org, space_slug)
        groups.remove_space_role(page.db, actor, group, target)
    except SynaptaneError as error:
        return failure(group_url(group, org), page, error)
    return redirect(group_url(group, org), page, "Access removed.")


# --- Groups of a space (its Members page) --------------------------------------------------------


@router.post("/o/{org_slug}/{space_slug}/-/groups")
def space_group_set(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    group: Annotated[str, Form()],
    role: Annotated[SpaceRole, Form()],
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    target = space_settings_url(space, "members")
    try:
        found = groups.get_group(page.db, actor, space.organization, group)
        groups.set_space_role(page.db, actor, found, space, role)
    except SynaptaneError as error:
        return failure(target, page, error)
    return _role_saved(page, target, found, space, role)


@router.post("/o/{org_slug}/{space_slug}/-/groups/{group_id}/remove")
def space_group_remove(
    page: WebPage, org_slug: str, space_slug: str, group_id: str, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    target = space_settings_url(space, "members")
    try:
        found = groups.get_group(page.db, actor, space.organization, group_id)
        groups.remove_space_role(page.db, actor, found, space)
    except SynaptaneError as error:
        return failure(target, page, error)
    return redirect(target, page, "Access removed.")
