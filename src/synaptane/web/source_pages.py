"""External sources of a space's routines, in its settings (organization admins only): list,
adding, changing the address or the authentication, replacing the secret, removing, and a test
call that shows the status only.

A secret is typed in a password field and never shown again: the page only says when it was
saved (`services.routine_sources`)."""

from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, Response

from synaptane.core.access import Action, can
from synaptane.core.errors import SynaptaneError, not_found
from synaptane.services import Actor, routine_fetch, routine_sources
from synaptane.storage.models import Space
from synaptane.web.pages import (
    ERROR_STATUS,
    Csrf,
    WebPage,
    failure,
    find_space,
    redirect,
    render_space_settings,
    space_settings_url,
)

router = APIRouter()
BASE = "/o/{org_slug}/{space_slug}/-/sources"


def _url(space: Space) -> str:
    return space_settings_url(space, "sources")


def _admin(actor: Actor, space: Space) -> None:
    if not can(actor.access, Action.MANAGE_ORG, org_id=space.org_id):
        raise not_found("Page not found.")


def _render(
    page: WebPage,
    actor: Actor,
    space: Space,
    error: SynaptaneError | None = None,
    draft: dict[str, str] | None = None,
) -> Response:
    sources = routine_sources.list_sources(page.db, actor, space)
    return render_space_settings(
        page,
        actor,
        space,
        "sources",
        "space-sources.html",
        status_code=ERROR_STATUS[error.code] if error else 200,
        sources=sources,
        used_by={s.name: routine_sources.used_by(page.db, space, s.name) for s in sources},
        secret_saved=routine_sources.secret_saved,
        enabled=page.workspace.routine_sources,
        max_calls=routine_fetch.MAX_CALLS,
        error=error,
        draft=draft or {},
    )


@router.get(BASE, response_class=HTMLResponse)
def sources_page(page: WebPage, org_slug: str, space_slug: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    _admin(actor, space)
    return _render(page, actor, space)


Text = Annotated[str, Form()]


@router.post(BASE)
def source_create(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    name: Text = "",
    url: Text = "",
    auth: Text = "",
    header: Text = "",
    secret: Text = "",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    _admin(actor, space)
    try:
        source = routine_sources.create(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            name=name,
            url=url,
            auth=auth,
            header=header,
            secret=secret,
        )
    except SynaptaneError as error:
        page.db.rollback()
        # Shown again without the secret: it is never written back into a page.
        draft = {"name": name, "url": url, "auth": auth, "header": header}
        return _render(page, actor, space, error, draft)
    return redirect(_url(space), page, "External source {name} added.", name=source.name)


@router.post(BASE + "/{name}")
def source_update(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    name: str,
    csrf: Csrf,
    url: Text = "",
    auth: Text = "",
    header: Text = "",
    secret: Text = "",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    _admin(actor, space)
    try:
        source = routine_sources.get(page.db, actor, space, name)
        routine_sources.update(
            page.db,
            actor,
            page.workspace,
            space,
            source,
            url=url,
            auth=auth,
            header=header,
            secret=secret,
        )
    except SynaptaneError as error:
        return failure(_url(space), page, error)
    return redirect(_url(space), page, "External source {name} saved.", name=name)


@router.post(BASE + "/{name}/delete")
def source_delete(page: WebPage, org_slug: str, space_slug: str, name: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    _admin(actor, space)
    try:
        source = routine_sources.get(page.db, actor, space, name)
        users = routine_sources.delete(page.db, page.database, actor, page.workspace, space, source)
    except SynaptaneError as error:
        return failure(_url(space), page, error)
    if users:
        return redirect(
            _url(space),
            page,
            "External source {name} removed. These routines no longer run until their file "
            "stops naming it: {routines}.",
            kind="warning",
            name=name,
            routines=", ".join(users),
        )
    return redirect(_url(space), page, "External source {name} removed.", name=name)


@router.post(BASE + "/{name}/test")
def source_test(page: WebPage, org_slug: str, space_slug: str, name: str, csrf: Csrf) -> Response:
    """A GET on the base address, with the secret: the status is shown, never the content."""
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    _admin(actor, space)
    try:
        source = routine_sources.get(page.db, actor, space, name)
        probe = routine_sources.check(page.db, actor, page.workspace, space, source)
    except SynaptaneError as error:
        return failure(_url(space), page, error)
    if probe.error is not None:
        return redirect(
            _url(space),
            page,
            "Test of {name}: {error}",
            kind="error",
            name=name,
            error=probe.error,
        )
    ok = probe.status is not None and 200 <= probe.status < 300
    return redirect(
        _url(space),
        page,
        "Test of {name}: HTTP {status} ({type}).",
        kind="ok" if ok else "warning",
        name=name,
        status=probe.status,
        type=probe.mime or "?",
    )
