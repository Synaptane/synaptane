"""Import of a zip (Obsidian vault, Markdown files, Notion or Confluence export): send, check
what would happen, confirm (`services.imports`)."""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse, Response

from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError
from synaptane.core.secret_scan import describe
from synaptane.services import imports
from synaptane.storage.models import Space
from synaptane.web.pages import Csrf, WebPage, failure, find_space, redirect
from synaptane.web.wiki import page_url

router = APIRouter()
SHOWN = 200  # lines per list on the check page


def _url(space: Space, token: str = "", folder: str = "", format: str = "") -> str:
    url = f"/o/{space.organization.slug}/{space.slug}/-/import"
    if token:
        url += f"/{token}"
    query = {key: value for key, value in (("folder", folder), ("format", format)) if value}
    return url + (f"?{urlencode(query)}" if query else "")


def _format(value: str) -> str:
    return value if value in imports.FORMATS else ""


@router.get("/o/{org_slug}/{space_slug}/-/import", response_class=HTMLResponse)
def import_form(page: WebPage, org_slug: str, space_slug: str, folder: str = "") -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    return page.render(
        "import.html",
        org=space.organization,
        space=space,
        folder=folder,
        plan=None,
        formats=imports.FORMATS,
        max_archive=imports.MAX_ARCHIVE // 1024 // 1024,
    )


@router.post("/o/{org_slug}/{space_slug}/-/import")
def import_send(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    archive: Annotated[UploadFile, File()],
    folder: Annotated[str, Form()] = "",
    format: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        token = imports.receive(actor, page.workspace, space, archive.file)
    except SynaptaneError as error:
        return failure(_url(space, folder=folder), page, error)
    return redirect(_url(space, token, folder, _format(format)), page)


@router.get("/o/{org_slug}/{space_slug}/-/import/{token}", response_class=HTMLResponse)
def import_check(
    page: WebPage, org_slug: str, space_slug: str, token: str, folder: str = "", format: str = ""
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    try:
        plan = imports.plan(actor, page.workspace, space, token, folder, _format(format))
    except SynaptaneError as error:
        return failure(_url(space, folder=folder), page, error)
    return page.render(
        "import.html",
        org=space.organization,
        space=space,
        folder=plan.folder,
        plan=plan,
        formats=imports.FORMATS,
        converted=plan.format in imports.CONVERTED,
        chosen=_format(format),
        shown=SHOWN,
        describe=describe,
    )


@router.post("/o/{org_slug}/{space_slug}/-/import/{token}")
def import_confirm(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    token: str,
    csrf: Csrf,
    folder: Annotated[str, Form()] = "",
    format: Annotated[str, Form()] = "",
    keep: Annotated[list[str] | None, Form()] = None,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        written = imports.apply(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            token,
            folder,
            frozenset(keep or ()),
            _format(format),
        )
    except SynaptaneError as error:
        return failure(_url(space, token, folder, _format(format)), page, error)
    return redirect(page_url(space, folder), page, "{count} files imported.", count=written)


@router.post("/o/{org_slug}/{space_slug}/-/import/{token}/cancel")
def import_cancel(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    token: str,
    csrf: Csrf,
    folder: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        imports.cancel(actor, page.workspace, space, token)
    except SynaptaneError as error:
        return failure(_url(space, folder=folder), page, error)
    return redirect(page_url(space, folder), page, "Import cancelled.")
