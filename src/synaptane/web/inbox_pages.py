"""A space's inbox on the web: pending items, dropping, reading."""

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse, Response
from markupsafe import Markup

from synaptane.core.access import SpaceRole
from synaptane.core.errors import SynaptaneError, invalid, not_found
from synaptane.core.markdown import WikiIndex
from synaptane.services import (
    Actor,
    activity,
    ai_settings,
    budget,
    content,
    deposits,
    spaces,
    tidy_journal,
)
from synaptane.services.inbox import InboxItem
from synaptane.storage.models import Space
from synaptane.web.pages import (
    Csrf,
    Page,
    WebPage,
    failure,
    find_space,
    linked_redirect,
    redirect,
    safe_next,
)
from synaptane.web.render import render_markdown, url_path
from synaptane.web.tidy_pages import run_now_context
from synaptane.web.wiki import page_url, space_url

router = APIRouter()


@router.get("/o/{org_slug}/{space_slug}/inbox", response_class=HTMLResponse)
def inbox_page(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    q: str = "",
    kind: str = "",
    start: int = 0,
    new: str = "",
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    return _inbox(page, actor, space, query=q, kind=kind, start=max(start, 0), new=new)


PAGE = 100  # items shown per inbox page


def _deposited(item: InboxItem) -> datetime:
    """Drop date (note) or modification date (page), comparable across time zones."""
    try:
        when = datetime.fromisoformat(item.deposited_at)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _inbox(
    page: WebPage,
    actor: Actor,
    space: Space,
    *,
    form: dict[str, str] | None = None,
    error: SynaptaneError | None = None,
    query: str = "",
    kind: str = "",
    start: int = 0,
    new: str = "",
) -> Response:
    """List (filterable, paginated), newest to oldest, and drop form; after an error, the input
    is kept. `new`: note that just arrived, highlighted."""
    counts = {"note": 0, "change": 0, "left": 0, "total": 0}
    matched = 0
    synced = True
    try:
        every = deposits.inbox(page.db, actor, page.workspace, space)
        # What was just dropped comes first (the tidy-up itself follows arrival order).
        every.sort(key=lambda i: (_deposited(i), i.path), reverse=True)
        counts = deposits.counts(every)
        found = deposits.matching(every, query, kind)
        matched = len(found)
        items: list[InboxItem] | None = found[start : start + PAGE]
    except SynaptaneError:
        items = None
        synced = False
    contribute = page.can_contribute(actor, space)
    config = ai_settings.config_for(page.db, page.box, space)
    # "Tidy now": subdued and confirmed as on the tidy-up journal.
    run_now = (
        run_now_context(
            page,
            space,
            config,
            budget.spending(page.db, space.org_id, space.ai_model or ""),
            tidy_journal.streak(page.db, space.id, config),
        )
        if contribute and items and config is not None
        else {}
    )
    return page.render(
        "inbox.html",
        status_code=400 if error else 200,
        org=space.organization,
        space=space,
        inbox_counts=counts if synced else None,  # left sidebar: already computed
        items=items,
        item=None,
        new=new,
        contribute=contribute,
        tidy_ready=config is not None,
        run_now=run_now,
        form=form,
        form_error=error,
        query=query,
        kind=kind,
        total=counts["total"],
        counts=counts,
        start=start,
        matched=matched,
        page_size=PAGE,
    )


@router.get("/o/{org_slug}/{space_slug}/inbox/{name}", response_class=HTMLResponse)
def inbox_item(
    page: WebPage, org_slug: str, space_slug: str, name: str, edit: bool = False
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    item = deposits.describe_item(page.db, actor, page.workspace, space, name)
    return _item_page(page, actor, space, item, _rendered(page, actor, space, item), editing=edit)


def _rendered(page: Page, actor: Actor, space: Space, item: InboxItem) -> Markup:
    try:  # a note's wiki links: to the space's pages
        names: WikiIndex | None = WikiIndex.of(b.path for b in page.space_files(actor, space))
    except SynaptaneError:
        names = None
    body = render_markdown(
        item.body,
        "",
        lambda p: page_url(space, p),
        lambda p: f"{space_url(space)}/raw/{url_path(p)}",
        names,
    )
    return Markup(body)  # noqa: S704 — sanitized by nh3


def _item_page(
    page: WebPage,
    actor: Actor,
    space: Space,
    item: InboxItem,
    body: Markup,
    *,
    form: dict[str, str] | None = None,
    error: SynaptaneError | None = None,
    editing: bool = False,
) -> Response:
    contribute = page.can_contribute(actor, space)
    targets = []
    if contribute and item.type == "note":
        targets = [
            s
            for s in spaces.list_all_visible_spaces(page.db, actor)
            if s.id != space.id and spaces.role_in(actor, s) is SpaceRole.CONTRIBUTOR
        ]
    return page.render(
        "inbox.html",
        status_code=400 if error else 200,
        org=space.organization,
        space=space,
        items=None,
        item=item,
        body=body,
        contribute=contribute,
        targets=targets,
        url_path=url_path,
        form=form,
        form_error=error,
        editing=editing or form is not None,
    )


def _item_url(space: Space, name: str = "") -> str:
    return f"{space_url(space)}/inbox" + (f"/{name}" if name else "")


@router.post("/o/{org_slug}/{space_slug}/-/inbox/{name}/edit")
def inbox_item_edit(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    name: str,
    csrf: Csrf,
    title: Annotated[str, Form()],
    body: Annotated[str, Form()] = "",
    hint: Annotated[str, Form()] = "",
    origin: Annotated[str, Form()] = "",
    allow_secrets: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        deposits.update_item(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            name,
            title=title,
            body=body.replace("\r\n", "\n"),
            hint=hint,
            origin=origin,
            allow_secrets=allow_secrets,
        )
    except SynaptaneError as error:
        page.db.rollback()
        item = deposits.describe_item(page.db, actor, page.workspace, space, name)
        form = {"title": title, "body": body, "hint": hint, "origin": origin}
        return _item_page(
            page, actor, space, item, _rendered(page, actor, space, item), form=form, error=error
        )
    return redirect(_item_url(space, name), page, "Note updated.")


@router.post("/o/{org_slug}/{space_slug}/-/inbox/{name}/delete")
def inbox_item_delete(
    page: WebPage, org_slug: str, space_slug: str, name: str, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        item = deposits.delete_item(page.db, page.database, actor, page.workspace, space, name)
    except SynaptaneError as error:
        return failure(_item_url(space, name), page, error)
    return redirect(_item_url(space), page, "Note “{title}” deleted.", title=item.title)


@router.post("/o/{org_slug}/{space_slug}/-/inbox/{name}/move")
def inbox_item_move(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    name: str,
    csrf: Csrf,
    target: Annotated[str, Form()],
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        destination = page.db.get(Space, UUID(target))
        if destination is None:
            raise not_found("Space not found.")
        deposits.move_item(page.db, page.database, actor, page.workspace, space, name, destination)
    except (SynaptaneError, ValueError) as error:
        if isinstance(error, ValueError):
            error = not_found("Space not found.")
        return failure(_item_url(space, name), page, error)
    return redirect(
        _item_url(space),
        page,
        "Note moved to the inbox of “{space}”.",
        space=destination.name,
    )


@router.post("/o/{org_slug}/{space_slug}/-/inbox/{name}/dismiss")
def inbox_change_dismiss(
    page: WebPage, org_slug: str, space_slug: str, name: str, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        deposits.dismiss_change(page.db, actor, page.workspace, space, name)
    except SynaptaneError as error:
        return failure(_item_url(space, name), page, error)
    return redirect(_item_url(space), page, "Change marked as seen.")


@router.get("/o/{org_slug}/{space_slug}/inbox/{name}/files/{filename}")
def inbox_attachment(
    page: WebPage, org_slug: str, space_slug: str, name: str, filename: str
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    data = deposits.read_attachment(actor, page.workspace, space, name, filename)
    return Response(
        data,
        media_type=content.mime_of(filename),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{url_path(filename)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


def _deposit(
    page: Page,
    actor: Actor,
    space: Space,
    *,
    title: str,
    body: str,
    hint: str,
    origin: str,
    files: list[UploadFile] | None,
    allow_secrets: bool,
) -> str:
    """Drop from the web (a space's inbox or the "Drop information" page); returns the name of
    the created note."""
    attachments = [
        (f.filename or "file", f.file.read(content.MAX_UPLOAD + 1))
        for f in files or []
        if f.filename
    ]
    path = deposits.deposit(
        page.db,
        page.database,
        actor,
        page.workspace,
        space,
        title=title,
        body=body.replace("\r\n", "\n"),
        source="web",
        origin=origin,
        hint=hint,
        attachments=attachments,
        allow_secrets=allow_secrets,
    )
    return PurePosixPath(path).name


def deposit_choices(page: Page, actor: Actor) -> list[Space]:
    """Spaces where the account can drop notes (contributor, or organization admin)."""
    return [
        s
        for s in spaces.list_all_visible_spaces(page.db, actor)
        if spaces.role_in(actor, s) is SpaceRole.CONTRIBUTOR
    ]


@router.get("/deposit", response_class=HTMLResponse)
def deposit_page(page: WebPage, space: str = "", next: str = "") -> Response:
    """Drop information from anywhere, choosing the space. `next`: page one comes from, to go
    back to once the note is dropped."""
    actor = page.user_actor()
    choices = deposit_choices(page, actor)
    known = {s.address for s in choices}
    chosen = space if space in known else (choices[0].address if len(choices) == 1 else "")
    return page.render(
        "deposit.html",
        choices=choices,
        chosen=chosen,
        form=None,
        form_error=None,
        back=safe_next(next) if next else "",
    )


@router.post("/deposit", response_class=HTMLResponse)
def deposit_anywhere(
    page: WebPage,
    csrf: Csrf,
    title: Annotated[str, Form()],
    space: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
    hint: Annotated[str, Form()] = "",
    origin: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
    allow_secrets: Annotated[bool, Form()] = False,
    back: Annotated[str, Form(alias="next")] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    choices = deposit_choices(page, actor)
    target = next((s for s in choices if s.address == space), None)
    try:
        if target is None:
            raise invalid("Choose a space you can drop into.")
        name = _deposit(
            page,
            actor,
            target,
            title=title,
            body=body,
            hint=hint,
            origin=origin,
            files=files,
            allow_secrets=allow_secrets,
        )
    except SynaptaneError as error:
        page.db.rollback()
        return page.render(
            "deposit.html",
            status_code=400,
            choices=choices,
            chosen=space,
            form={"title": title, "body": body, "hint": hint, "origin": origin},
            form_error=error,
            back=safe_next(back) if back else "",
        )
    message = "Dropped into the inbox of “{name}”: it will be tidied on the next run."
    if back:  # back to the original page, with a link to the note
        note = (_item_url(target, name), "View the note")
        return linked_redirect(safe_next(back), page, message, note, params={"name": target.name})
    return redirect(f"{_item_url(target)}?new={url_path(name)}", page, message, name=target.name)


@router.post("/o/{org_slug}/{space_slug}/-/inbox")
def inbox_deposit(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    title: Annotated[str, Form()],
    body: Annotated[str, Form()] = "",
    hint: Annotated[str, Form()] = "",
    origin: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
    allow_secrets: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        name = _deposit(
            page,
            actor,
            space,
            title=title,
            body=body,
            hint=hint,
            origin=origin,
            files=files,
            allow_secrets=allow_secrets,
        )
    except SynaptaneError as error:
        page.db.rollback()
        return _inbox(
            page,
            actor,
            space,
            form={"title": title, "body": body, "hint": hint, "origin": origin},
            error=error,
        )
    return redirect(
        f"{_item_url(space)}?new={url_path(name)}",
        page,
        "Dropped in the inbox: it will be filed on the next run.",
    )


# --- Space activity -------------------------------------------------------------------------------


@router.get("/o/{org_slug}/{space_slug}/activity", response_class=HTMLResponse)
def space_activity_page(
    page: WebPage, org_slug: str, space_slug: str, kind: str = "", skip: int = 0
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    events, following = activity.space_activity(actor, page.workspace, space, kind=kind, skip=skip)
    return page.render(
        "space-activity.html",
        org=space.organization,
        space=space,
        events=events,
        kind=kind if kind in activity.KINDS else "",
        following=following,
        skip=skip,
    )
