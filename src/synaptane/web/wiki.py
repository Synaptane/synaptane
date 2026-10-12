"""A space's wiki: the `content/` tree on the left, the page on the right; editing; search.

URLs: `/o/<org>/<space>/p/<path>` (page or folder), `/raw/<path>` (raw file),
`/history/<path>`, `/diff/<commit>/<path>`, `/edit/<path>`, `/new`, `/-/verify`
(mark a page as verified, choose its owner and its cadence).
"""

import difflib
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from markupsafe import Markup, escape
from sqlalchemy import func, or_, select

from synaptane.core.errors import (
    ErrorCode,
    SynaptaneError,
    conflict,
    forbidden,
    invalid,
    not_found,
)
from synaptane.core.markdown import WikiIndex, outline, title_of
from synaptane.services import Actor, content, deposits, freshness, related, spaces
from synaptane.services import search as search_service
from synaptane.services.content import Folder, build_tree, folder_index
from synaptane.services.inbox import CONTENT_DIR, InboxItem
from synaptane.storage.models import Space, User
from synaptane.web.i18n import plural
from synaptane.web.pages import Csrf, Page, WebPage, failure, find_space, linked_redirect, redirect
from synaptane.web.render import highlight_file, pygments_css, render_markdown, url_path

router = APIRouter()
_CODE_CSS = pygments_css()
SANDBOXED = content.SANDBOXED


def space_url(space: Space) -> str:
    return f"/o/{space.address}"


def page_url(space: Space, path: str) -> str:
    return f"{space_url(space)}/p/{url_path(path)}" if path else space_url(space)


def raw_url(space: Space, path: str) -> str:
    return f"{space_url(space)}/raw/{url_path(path)}"


def _crumbs(path: str) -> list[tuple[str, str]]:
    parts = PurePosixPath(path).parts
    return [(part, "/".join(parts[: i + 1])) for i, part in enumerate(parts)]


def _render_md(space: Space, text: str, current: str, index: WikiIndex | None = None) -> Markup:
    html = render_markdown(
        text, current, lambda p: page_url(space, p), lambda p: raw_url(space, p), index
    )
    return Markup(html)  # noqa: S704 — sanitized by nh3 in render_markdown


def _wiki(
    page: Page, actor: Actor, space: Space, path: str, status_code: int = 200, **extra: object
) -> HTMLResponse:
    try:
        files = page.space_files(actor, space)
    except SynaptaneError:
        files = None
    return page.render(
        "wiki.html",
        status_code=status_code,
        org=space.organization,
        space=space,
        path=path,
        crumbs=_crumbs(path),
        space_tree=build_tree(files) if files is not None else None,
        synced=files is not None,
        all_folders=sorted({b.path.rsplit("/", 1)[0] for b in files or [] if "/" in b.path})[:500],
        contribute=page.can_contribute(actor, space),
        page_url=lambda p: page_url(space, p),
        raw_url=lambda p: raw_url(space, p),
        **extra,
    )


def owner_label(page: Page, owner: str) -> str:
    """Displayed name of a page's owner: that of the account it designates (username or email
    address), otherwise the free-form name as written."""
    wanted = owner.strip().lstrip("@")
    if not wanted:
        return ""
    cache: dict[str, str] = page.__dict__.setdefault("_owner_cache", {})
    if wanted not in cache:
        folded = wanted.casefold()
        user = page.db.scalar(
            select(User).where(
                or_(func.lower(User.username) == folded, func.lower(User.email) == folded)
            )
        )
        cache[wanted] = user.display_name if user is not None else wanted
    return cache[wanted]


PERIODS = ("1m", "3m", "6m", "1y", "2y", "never")  # choices offered on the page


def _verification(
    page: Page, actor: Actor, space: Space, file: content.File, last: object
) -> dict[str, object] | None:
    """What the page shows of its verification: state, owner, due date, and what is needed to
    mark it verified. None for a file that is not a Markdown page."""
    if file.kind != "markdown" or file.text is None:
        return None
    state = freshness.of_text(file.text, space.review_every or "", freshness.today_for(space))
    meta = freshness.read_meta(file.text)
    modified = False
    date, subject = getattr(last, "date", ""), getattr(last, "subject", "")
    if state.verified is not None and date and date[:10] > state.verified.isoformat():
        modified = not subject.startswith(freshness.VERIFICATION_SUBJECTS)
    people: list[str] = []
    if page.can_contribute(actor, space):
        people = [u.username for u, _ in spaces.list_members(page.db, actor, space)]
        people = sorted({*people, actor.user.username})
    own = str(freshness.parse_period(meta.review_every) or "") if meta.review_every else ""
    choices = list(PERIODS) + ([own] if own and own not in PERIODS else [])
    return {
        "state": state,
        "owner": state.owner,
        "owner_name": owner_label(page, state.owner),
        "modified": modified,
        "default": space.review_every or "",
        "own": own,
        "choices": choices,
        "people": people,
    }


def _folder(tree: Folder, path: str) -> Folder | None:
    node = tree
    for part in PurePosixPath(path).parts:
        found = node.folders.get(part)
        if found is None:
            return None
        node = found
    return node


@router.get("/assets/code.css", include_in_schema=False)
def code_css() -> Response:
    return Response(_CODE_CSS, media_type="text/css", headers={"Cache-Control": "max-age=3600"})


@router.get("/o/{org_slug}/{space_slug}", response_class=HTMLResponse)
def wiki_home(page: WebPage, org_slug: str, space_slug: str) -> Response:
    return wiki_page(page, org_slug, space_slug, "")


HTML_PAGES = (".html", ".htm")


@router.get("/o/{org_slug}/{space_slug}/p/{path:path}", response_class=HTMLResponse)
def wiki_page(
    page: WebPage, org_slug: str, space_slug: str, path: str, source: bool = False
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    path = path.strip("/")
    ws = page.workspace
    try:
        files = page.space_files(actor, space)
    except SynaptaneError:
        return _wiki(page, actor, space, "", view="unsynced")
    tree = build_tree(files)
    names = WikiIndex.of(blob.path for blob in files)  # wiki links: [[name]]
    folder = _folder(tree, path) if path else tree
    if folder is not None:
        index = folder_index(files, path)
        intro = None
        intro_title = ""  # title of the folder's index page: in the bar, not duplicated
        if index:
            readme = content.read_file(actor, ws, space, index)
            intro = _render_md(space, readme.text or "", index, names)
            intro_title = title_of(readme.text or "", "")
        return _wiki(
            page,
            actor,
            space,
            path,
            view="folder",
            folder=folder,
            intro=intro,
            intro_title=intro_title,
            index=index,
            states=freshness.space_states(ws, space),  # verified or stale pages
            owner_label=lambda owner: owner_label(page, owner),
        )
    try:
        file = content.read_file(actor, ws, space, path)
    except SynaptaneError as error:
        if error.code != ErrorCode.NOT_FOUND:
            raise
        # Link to a page that does not exist yet: stay in the wiki, and a contributor can
        # create it here.
        return _wiki(page, actor, space, path, status_code=404, view="missing")
    rendered: Markup | None = None
    title = file.name
    toc: list[tuple[int, str, str]] = []
    has_title = False  # the page carries its own level-1 heading
    # An HTML page is shown rendered (isolated frame); its source stays one click away.
    html_page = file.name.lower().endswith(HTML_PAGES) and not source
    if file.kind == "markdown" and file.text is not None:
        rendered = _render_md(space, file.text, file.path, names)
        heading = title_of(file.text, "")
        has_title, title = bool(heading), heading or file.name
        toc = outline(file.text)
    elif file.kind == "text" and file.text is not None and not html_page:
        rendered = Markup(highlight_file(file.name, file.text))  # noqa: S704 — escaped by Pygments
    extracted = None
    if file.kind in ("pdf", "office", "image"):
        text = ws.extracted_text(space.id, f"{CONTENT_DIR}/{file.path}")
        extracted = text[:20000] if text is not None else None
    backlinks = (
        content.backlinks(actor, ws, space, file.path, names) if file.kind == "markdown" else []
    )
    # "See also": loaded after the page (preferences.js), the URL changes with every commit
    # (the browser keeps the response as long as the wiki does not move).
    head = ws.local_repository(space).head() or ""
    related_url = f"{space_url(space)}/-/related/{url_path(file.path)}?v={head[:12]}"
    last = content.last_change(actor, ws, space, file.path)
    return _wiki(
        page,
        actor,
        space,
        path,
        view="file",
        file=file,
        rendered=rendered,
        title=title,
        has_title=has_title,
        toc=toc if len(toc) >= 2 else [],
        html_page=html_page,
        last_change=last,
        extracted=extracted,
        backlinks=backlinks,
        verification=_verification(page, actor, space, file, last),
        related_url=related_url,
    )


@router.get("/o/{org_slug}/{space_slug}/-/related/{path:path}", response_class=HTMLResponse)
def wiki_related(page: WebPage, org_slug: str, space_slug: str, path: str) -> Response:
    """A page's "See also" panel (HTML fragment); 204 if nothing is related."""
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    found = related.for_page(page.db, actor, page.workspace, space, path)
    if not found:
        return Response(status_code=204, headers={"Cache-Control": "private, max-age=300"})
    contribute = page.can_contribute(actor, space)
    waiting = (
        related.proposed(actor, page.workspace, space, path)
        if contribute and any(not n.linked for n in found)
        else set()
    )
    response = page.render(
        "wiki-related.html",
        space_tree=None,
        inbox_counts=None,
        base=space_url(space),
        path=path.strip("/"),
        neighbours=found,
        labels=related.LABELS,
        contribute=contribute,
        proposed=waiting,
        page_url=lambda p: page_url(space, p),
    )
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@router.post("/o/{org_slug}/{space_slug}/-/related/propose")
def wiki_propose_link(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    path: Annotated[str, Form()],
    target: Annotated[str, Form()],
) -> Response:
    """The "Suggest a link" action: a note in the inbox, which the nightly tidy-up will handle."""
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    back = page_url(space, path.strip("/"))
    try:
        note, already = related.propose_link(
            page.db, page.database, actor, page.workspace, space, path, target
        )
    except SynaptaneError as error:
        return failure(back, page, error)
    link = (f"{space_url(space)}/inbox/{url_path(PurePosixPath(note).name)}", "View the note")
    if already:
        return linked_redirect(
            back, page, "Already suggested: the note is waiting for the tidy-up.", link
        )
    return linked_redirect(
        back,
        page,
        "Suggestion dropped in the inbox: the nightly tidy-up will link the two pages.",
        link,
    )


@router.get("/o/{org_slug}/{space_slug}/-/tree", response_class=HTMLResponse)
def wiki_tree_fragment(page: WebPage, org_slug: str, space_slug: str, dir: str = "") -> Response:
    """Contents of a tree folder, expanded on demand in the left sidebar."""
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    tree = build_tree(page.space_files(actor, space))
    folder = _folder(tree, dir.strip("/")) if dir.strip("/") else tree
    if folder is None:
        raise not_found("Folder not found.")
    response = page.render(
        "space-tree-fragment.html", folder=folder, base=space_url(space), space_tree=None
    )
    response.headers["Cache-Control"] = "private, max-age=30"
    return response


@router.get("/o/{org_slug}/{space_slug}/raw/{path:path}")
def wiki_raw(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    path: str,
    download: bool = False,
    preview: bool = False,
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    file = content.open_raw(actor, page.workspace, space, path)
    mime = content.mime_of(file.path)
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=60",
        "Content-Length": str(file.size),
    }
    # An HTML or SVG file from the repository must never run in Synaptane's origin.
    if preview and mime == "text/html" and not download:
        # Preview of an HTML page, only inside the wiki page's frame: opened full page, at
        # Synaptane's address, it could impersonate it (fake sign-in screen). The browser says
        # where the response will be displayed.
        if page.request.headers.get("sec-fetch-dest") not in ("iframe", "frame"):
            return RedirectResponse(page_url(space, file.path), status_code=303)
        # Opaque origin (sandbox without allow-same-origin): no cookies, no access to Synaptane.
        # Its scripts and external resources (fonts, CDN, images) stay allowed so that it
        # displays; but no network request from scripts and no form submission, through which
        # typed input could leave.
        headers["Content-Security-Policy"] = (
            "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
            "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'; "
            "connect-src 'none'; form-action 'none'; frame-ancestors 'self'"
        )
        # Never served again from the cache when opened full page.
        headers["Cache-Control"] = "no-store"
        headers["Vary"] = "Sec-Fetch-Dest"
        headers["Content-Disposition"] = f"inline; filename*=UTF-8''{url_path(file.name)}"
        return StreamingResponse(
            file.chunks, media_type="text/html; charset=utf-8", headers=headers
        )
    if mime in SANDBOXED or mime.startswith("text/") or file.kind == "text":
        headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'"
        )
        if mime in SANDBOXED:
            download = True
        else:
            mime = "text/plain; charset=utf-8"
    disposition = "attachment" if download or file.kind in ("office", "binary") else "inline"
    headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{url_path(file.name)}"
    return StreamingResponse(file.chunks, media_type=mime, headers=headers)


@router.get("/o/{org_slug}/{space_slug}/history/{path:path}", response_class=HTMLResponse)
def wiki_history(page: WebPage, org_slug: str, space_slug: str, path: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    commits = content.history(actor, page.workspace, space, path)
    if not commits:
        raise not_found("Page not found.")
    return _wiki(page, actor, space, path, view="history", commits=commits)


@router.get("/o/{org_slug}/{space_slug}/diff/{sha}/{path:path}", response_class=HTMLResponse)
def wiki_diff(page: WebPage, org_slug: str, space_slug: str, sha: str, path: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    patch = content.diff(actor, page.workspace, space, path, sha)
    lines = [
        ("add" if line.startswith("+") else "del" if line.startswith("-") else "ctx", line)
        for line in patch.splitlines()
        if not line.startswith(("diff --git", "index ", "--- ", "+++ "))
    ]
    return _wiki(page, actor, space, path, view="diff", sha=sha, diff_lines=lines)


# --- Editing --------------------------------------------------------------------------------------


def _require_contributor(page: Page, actor: Actor, space: Space) -> None:
    """Editing forms: refused to a reader as soon as they are displayed (shared link, bookmark,
    former role), rather than at save time."""
    if not page.can_contribute(actor, space):
        raise forbidden(
            "You are a reader of the “{space}” space: ask an administrator for the contributor "
            "role to edit its pages.",
            space=space.name,
        )


def _write_status(error: SynaptaneError) -> int:
    """403 for a permission refusal (not 400), as elsewhere in the application."""
    return 403 if error.code == ErrorCode.FORBIDDEN else 400


def _already_exists(error: SynaptaneError, path: str) -> SynaptaneError:
    """Creation without a sha (`base_sha=""`): the only possible conflict is a file with the same
    name. The generic message ("the page changed…") would suggest a modification."""
    if error.code != ErrorCode.CONFLICT:
        return error
    name = PurePosixPath(path).name
    return conflict("A file named “{name}” already exists in this folder.", name=name)


@router.get("/o/{org_slug}/{space_slug}/edit/{path:path}", response_class=HTMLResponse)
def wiki_edit_form(page: WebPage, org_slug: str, space_slug: str, path: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    _require_contributor(page, actor, space)
    file = content.read_file(actor, page.workspace, space, path)
    if file.text is None:
        raise invalid("Only text files can be edited in the browser.")
    return _wiki(page, actor, space, path, view="edit", file=file, draft=file.text, message="")


@router.post("/o/{org_slug}/{space_slug}/edit/{path:path}", response_class=HTMLResponse)
def wiki_edit(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    path: str,
    csrf: Csrf,
    text: Annotated[str, Form()],
    base_sha: Annotated[str, Form()],
    message: Annotated[str, Form()] = "",
    overwrite_sha: Annotated[str, Form()] = "",
    allow_secrets: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    data = text.replace("\r\n", "\n").encode()
    if overwrite_sha:  # explicit choice, after seeing the conflict
        base_sha = overwrite_sha
    try:
        content.write_file(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            path,
            data,
            message=message,
            base_sha=base_sha,
            allow_secrets=allow_secrets,
        )
    except SynaptaneError as error:
        page.db.rollback()
        file = content.read_file(actor, page.workspace, space, path)
        conflict = None
        if error.code == "conflict" and file.sha != base_sha:
            # The base stays the original one: a new "Save" cannot silently overwrite. The
            # difference is shown; replacing requires an explicit choice.
            diff = difflib.unified_diff(
                text.replace("\r\n", "\n").splitlines(),
                (file.text or "").splitlines(),
                lineterm="",
                n=2,
            )
            conflict = {
                "sha": file.sha,
                "diff": [
                    ("add" if d.startswith("+") else "del" if d.startswith("-") else "ctx", d)
                    for d in diff
                    if not d.startswith(("---", "+++"))
                ],
            }
        return _wiki(
            page,
            actor,
            space,
            path,
            status_code=409 if conflict else _write_status(error),
            view="edit",
            file=file,
            draft=text,
            message=message,
            error=error,
            base_sha=base_sha,
            conflict=conflict,
        )
    return redirect(page_url(space, path), page, "Page saved.")


@router.get("/o/{org_slug}/{space_slug}/new", response_class=HTMLResponse)
def wiki_new_form(
    page: WebPage, org_slug: str, space_slug: str, folder: str = "", name: str = ""
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    _require_contributor(page, actor, space)
    return _wiki(
        page, actor, space, folder.strip("/"), view="new", folder=folder.strip("/"), name=name
    )


def _page_path(folder: str, name: str) -> str:
    name = name.strip().strip("/")
    if not name:
        raise invalid("Enter the page name.")
    if not PurePosixPath(name).suffix:
        name += ".md"
    return f"{folder.strip('/')}/{name}" if folder.strip("/") else name


@router.post("/o/{org_slug}/{space_slug}/-/new")
def wiki_new(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    name: Annotated[str, Form()],
    folder: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    message: Annotated[str, Form()] = "",
    allow_secrets: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        path = _page_path(folder, name)
        body = text.replace("\r\n", "\n") or f"# {PurePosixPath(path).stem}\n"
        content.write_file(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            path,
            body.encode(),
            message=message,
            base_sha="",
            allow_secrets=allow_secrets,
        )
    except SynaptaneError as error:
        page.db.rollback()
        if error.code == ErrorCode.CONFLICT:
            error = _already_exists(error, path)
        return _wiki(
            page,
            actor,
            space,
            folder.strip("/"),
            status_code=_write_status(error),
            view="new",
            folder=folder.strip("/"),
            name=name,
            draft=text,
            message=message,
            error=error,
        )
    return redirect(page_url(space, path), page, "Page created.")


@router.post("/o/{org_slug}/{space_slug}/-/upload")
def wiki_upload(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    upload: Annotated[UploadFile, File()],
    folder: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        name = PurePosixPath(upload.filename or "").name
        if not name:
            raise invalid("Choose a file.")
        data = upload.file.read(content.MAX_UPLOAD + 1)
        path = f"{folder.strip('/')}/{name}" if folder.strip("/") else name
        content.write_file(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            path,
            data,
            message=f"Ajout de {name}",
            base_sha="",
        )
    except SynaptaneError as error:
        if error.code != ErrorCode.CONFLICT:
            return failure(page_url(space, folder.strip("/")), page, error)
        page.db.rollback()
        error = _already_exists(error, path)
        return linked_redirect(
            page_url(space, folder.strip("/")),
            page,
            error.template,
            (page_url(space, path), "Open this file"),
            kind="error",
            params=error.params,
        )
    return redirect(page_url(space, path), page, "File added.")


@router.post("/o/{org_slug}/{space_slug}/-/move")
def wiki_move(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    path: Annotated[str, Form()],
    target: Annotated[str, Form()],
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        updated = content.move_file(
            page.db, page.database, actor, page.workspace, space, path, target
        )
    except SynaptaneError as error:
        return failure(page_url(space, path), page, error)
    url = page_url(space, target.strip("/"))
    if not updated:
        return redirect(url, page, "Page moved.")
    message = plural(
        page.locale,
        len(updated),
        "Page moved. Links updated in {count} page: {pages}.",
        "Page moved. Links updated in {count} pages: {pages}.",
    )
    shown = ", ".join(updated[:5]) + (" …" if len(updated) > 5 else "")
    return redirect(url, page, message, count=len(updated), pages=shown)


@router.post("/o/{org_slug}/{space_slug}/-/verify")
def wiki_verify(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    path: Annotated[str, Form()],
    owner: Annotated[str, Form()] = "",
    review_every: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "verify",
) -> Response:
    """The "Mark as verified" action (contributor); `action=save`: only the owner and the
    cadence. A commit that touches only the page's front matter."""
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    verify = action != "save"
    try:
        freshness.verify_page(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            path,
            owner=owner,
            review_every=review_every,
            verify=verify,
        )
    except SynaptaneError as error:
        return failure(page_url(space, path), page, error)
    message = "Page marked as verified." if verify else "Page follow-up saved."
    return redirect(page_url(space, path), page, message)


@router.post("/o/{org_slug}/{space_slug}/-/delete-file")
def wiki_delete(
    page: WebPage, org_slug: str, space_slug: str, csrf: Csrf, path: Annotated[str, Form()]
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    parent = PurePosixPath(path.strip("/")).parent.as_posix()
    parent = "" if parent == "." else parent
    try:
        content.delete_file(page.db, page.database, actor, page.workspace, space, path)
    except SynaptaneError as error:
        return failure(page_url(space, path), page, error)
    return redirect(page_url(space, parent), page, "File deleted.")


# --- Search ---------------------------------------------------------------------------------------


def highlight_terms(text: str, terms: list[str]) -> Markup:
    """Escaped text, searched terms wrapped in <mark>.

    Positions are computed on the **raw** text (accents and ligatures folded, stems included),
    merged, then each piece is escaped separately: a term can fall neither inside an entity
    ("&amp;") nor inside a <mark> tag already placed."""
    pieces: list[str] = []
    last = 0
    for start, end in search_service.highlight_spans(text, terms):
        pieces += [escape(text[last:start]), "<mark>", escape(text[start:end]), "</mark>"]
        last = end
    pieces.append(escape(text[last:]))
    return Markup("".join(pieces))  # noqa: S704 — pieces escaped one by one


@router.get("/search", response_class=HTMLResponse)
def search_page(page: WebPage, q: str = "", space: str = "", regex: bool = False) -> Response:
    actor = page.user_actor()
    hits: list[search_service.Hit] = []
    pending: list[tuple[Space, InboxItem]] = []
    error = None
    terms: list[str] = []
    if q.strip():
        try:
            if not regex:  # notes not tidied yet: findable as soon as they are dropped
                visible = [
                    s for s in content_spaces(page, actor) if not space or s.address == space
                ]
                pending = deposits.pending_notes(actor, page.workspace, visible, q)
            hits = search_service.search(
                page.db,
                actor,
                page.workspace,
                q,
                addresses=[space] if space else [],
                regex=regex,
                limit=50,
            )
            terms = [] if regex else list(search_service.Query.parse(q).terms)
        except SynaptaneError as err:
            error = err
    return page.render(
        "search.html",
        q=q,
        selected=space,
        regex=regex,
        hits=hits,
        pending=pending,
        error=error,
        terms=terms,
        highlight=highlight_terms,
        all_spaces=[
            (s.address, f"{s.organization.name} / {s.name}") for s in content_spaces(page, actor)
        ],
        url_path=url_path,
    )


def content_spaces(page: Page, actor: Actor) -> list[Space]:
    from synaptane.services import spaces

    return spaces.list_all_visible_spaces(page.db, actor)


@router.get("/o/{org_slug}/{space_slug}/-/text/{path:path}", include_in_schema=False)
def wiki_extracted_text(page: WebPage, org_slug: str, space_slug: str, path: str) -> Response:
    """Text extracted from a PDF, Office document or image (as search sees it)."""
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    content.read_file(actor, page.workspace, space, path)  # access and existence
    text = page.workspace.extracted_text(space.id, f"{CONTENT_DIR}/{path}")
    if text is None:
        raise not_found("No text extracted from this file.")
    return PlainTextResponse(text)
