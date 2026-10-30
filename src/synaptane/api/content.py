"""REST API for a space's content (`content/`), search and the inbox.

Paths are relative to `content/`: `GET …/spaces/infra/files/servers/proxmox.md`. The rules
are those of the web UI and MCP: a file is not replaced without having been read
(`base_sha`), and search reports dropped notes not yet tidied.
"""

import base64
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from synaptane.core.errors import ErrorCode, SynaptaneError, conflict, invalid
from synaptane.core.names import without_email
from synaptane.services import content, deposits, freshness, related, reports, spaces
from synaptane.services import search as search_service
from synaptane.services.inbox import CONTENT_DIR, InboxItem
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database

from .routes import CurrentActor, DbSession, find_space

router = APIRouter(prefix="/api/v1")


def _ws(request: Request) -> Workspace:
    workspace: Workspace = request.app.state.workspace
    return workspace


def _db(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


class FileEntry(BaseModel):
    path: str
    size: int
    sha: str


class FreshnessOut(BaseModel):
    state: str = Field(description="verified, stale (due for review) or never")
    verified: bool = Field(description="Verified and not yet due for review")
    stale: bool = Field(description="Stale: the verification due date has passed")
    verified_at: str | None = Field(description="Date of the last verification")
    owner: str | None = Field(description="Owner (account username or free-form name)")
    review_due: str | None = Field(description="Verification due date; null: none")
    review_every: str | None = Field(description="Effective review cadence (6m, 1y, never…)")
    reconfirmed: int = Field(
        default=0, description="Times verified again with no change: cadence doubled each time"
    )


def _freshness(state: freshness.Freshness | None) -> FreshnessOut | None:
    return FreshnessOut.model_validate(state.as_dict()) if state is not None else None


class FileOut(BaseModel):
    path: str
    kind: str = Field(description="markdown, text, pdf, image, office or binary")
    sha: str
    size: int
    text: str | None = Field(
        description="Content (Markdown, text) or extracted text (PDF, Office, image)"
    )
    freshness: FreshnessOut | None = Field(
        default=None, description="Markdown page: verification (owner, date, due date)"
    )


class VerifyIn(BaseModel):
    owner: str | None = Field(
        default=None,
        description="Owner; absent: unchanged (the caller, if there is none)",
    )
    review_every: str | None = Field(
        default=None,
        description='Review cadence (90d, 6m, 1y, never); "": the space\'s; absent: unchanged',
    )
    verify: bool = Field(
        default=True,
        description="false: only the owner and the cadence, without dating",
    )


class VerifyOut(BaseModel):
    commit: str | None = Field(description="Commit created, null if nothing changed")
    path: str
    freshness: FreshnessOut


class FileIn(BaseModel):
    content: str | None = Field(default=None, description="Text content (UTF-8)")
    content_base64: str | None = Field(default=None, description="Binary content in base64")
    message: str = ""
    base_sha: str | None = Field(
        default=None,
        description='Sha read before modifying; "" to require a creation. '
        "Conflict (409) if the file changed in the meantime.",
    )
    allow_secrets: bool = Field(
        default=False, description="Save despite a detected likely secret (422 otherwise)."
    )
    force: bool = Field(
        default=False,
        description="Knowingly replace an existing file without `base_sha` (409 otherwise).",
    )


class WriteOut(BaseModel):
    commit: str | None = Field(description="Commit created, null if nothing changed")


class MoveIn(BaseModel):
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    message: str = ""


class RestoreIn(BaseModel):
    path: str
    commit: str = Field(description="Version to put back (sha from …/history/{path})")
    message: str = ""
    base_sha: str | None = Field(
        default=None,
        description='Current sha, to refuse (409) a change made since; "" for a deleted file.',
    )
    allow_secrets: bool = Field(
        default=False, description="Restore despite a detected likely secret (422 otherwise)."
    )


class CommitOut(BaseModel):
    sha: str
    author: str
    date: str
    subject: str


class SnippetOut(BaseModel):
    line: int
    text: str
    before: list[str]
    after: list[str]


class HitOut(BaseModel):
    space: str = Field(description="org/space")
    path: str
    title: str
    score: float
    extracted: bool
    match: str = Field(description="text (words), meaning or both")
    verified: bool = Field(description="Page verified and up to date (ranked slightly higher)")
    stale: bool = Field(description="Stale page: verification overdue (to be reported)")
    freshness: FreshnessOut | None = Field(description="Markdown page: verification details")
    edited: str | None = Field(description="Date of the last change (ISO 8601)")
    edited_by: str | None = Field(description="Author of the last change")
    edited_by_ai: str | None = Field(
        description="tidy, routine or assistant when an AI wrote the last change; null: a person"
    )
    reported_outdated: int = Field(description="Open reports that the page is outdated")
    snippets: list[SnippetOut]


SPACE = "/organizations/{org}/spaces/{space}"


@router.get(SPACE + "/tree", tags=["content"])
def tree(
    org: str, space: str, request: Request, session: DbSession, actor: CurrentActor
) -> list[FileEntry]:
    found = find_space(session, actor, org, space)
    return [
        FileEntry(path=b.path, size=b.size, sha=b.sha)
        for b in content.list_files(actor, _ws(request), found)
    ]


class RelatedOut(BaseModel):
    path: str
    title: str
    score: float = Field(description="0 to 1; nothing below 0.2 is returned")
    reason: str = Field(
        description="Main signal: meaning (same topic), words (words in common), "
        "backlink (cites this page), link (linked by a link), shared (links in common), "
        "folder (same folder)"
    )
    reasons: list[str] = Field(description="All signals, strongest to weakest")
    label: str = Field(description="The reason, in plain language")
    linked: bool = Field(description="The page already links to this one")


# Before `read`: otherwise "…/files/a.md/related" would be read as the file "a.md/related".
@router.get(SPACE + "/files/{path:path}/related", tags=["content"])
def related_pages(
    org: str,
    space: str,
    path: str,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    limit: Annotated[int, Query(ge=1, le=related.MAX_LIMIT)] = related.LIMIT,
) -> list[RelatedOut]:
    """Pages related to a page: same topic (embeddings, if the space has them), words in
    common, links, folder. Empty list if nothing is related."""
    found = find_space(session, actor, org, space)
    return [
        RelatedOut(
            path=n.path,
            title=n.title,
            score=n.score,
            reason=n.reason,
            reasons=list(n.reasons),
            label=n.label,
            linked=n.linked,
        )
        for n in related.for_page(session, actor, _ws(request), found, path, limit=limit)
    ]


@router.get(SPACE + "/files/{path:path}", tags=["content"])
def read(
    org: str, space: str, path: str, request: Request, session: DbSession, actor: CurrentActor
) -> FileOut:
    found = find_space(session, actor, org, space)
    ws = _ws(request)
    file = content.read_file(actor, ws, found, path)
    text = file.text
    if text is None and file.kind in ("pdf", "office", "image"):
        text = ws.extracted_text(found.id, f"{CONTENT_DIR}/{file.path}")
    state = None
    if file.kind == "markdown" and file.text is not None:
        state = freshness.of_text(file.text, found.review_every or "", freshness.today_for(found))
    return FileOut(
        path=file.path,
        kind=file.kind,
        sha=file.sha,
        size=file.size,
        text=text,
        freshness=_freshness(state),
    )


@router.post(SPACE + "/verify/{path:path}", tags=["content"])
def verify(
    org: str,
    space: str,
    path: str,
    body: VerifyIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> VerifyOut:
    """Mark a Markdown page as verified today (contributor): a commit that only changes its
    front matter (`verified`, and `owner` if empty: the caller). This commit is not a change
    for the tidy-up to review."""
    found = find_space(session, actor, org, space)
    result = freshness.verify_page(
        session,
        _db(request),
        actor,
        _ws(request),
        found,
        path,
        owner=body.owner,
        review_every=body.review_every,
        verify=body.verify,
        source="api",
    )
    out = _freshness(result.freshness)
    assert out is not None
    return VerifyOut(commit=result.commit, path=result.path, freshness=out)


@router.get(SPACE + "/raw/{path:path}", tags=["content"])
def raw(
    org: str, space: str, path: str, request: Request, session: DbSession, actor: CurrentActor
) -> Response:
    file = content.open_raw(actor, _ws(request), find_space(session, actor, org, space), path)
    mime = content.mime_of(file.path)
    headers = {"Content-Length": str(file.size), "X-Content-Type-Options": "nosniff"}
    if mime in content.SANDBOXED:  # HTML or SVG from the repository: downloaded, never run
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        headers["Content-Disposition"] = "attachment"
    return StreamingResponse(file.chunks, media_type=mime, headers=headers)


@router.put(SPACE + "/files/{path:path}", tags=["content"])
def write(
    org: str,
    space: str,
    path: str,
    body: FileIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> WriteOut:
    if (body.content is None) == (body.content_base64 is None):
        raise invalid("Provide `content` or `content_base64`.")
    try:
        data = (
            body.content.encode()
            if body.content is not None
            else base64.b64decode(body.content_base64 or "", validate=True)
        )
    except ValueError:
        raise invalid("Invalid `content_base64`.") from None
    found = find_space(session, actor, org, space)
    # Without `base_sha` or `force`, only a creation is allowed: the service checks it
    # under the space lock, with no window between the check and the write.
    blind = body.base_sha is None and not body.force
    try:
        sha = content.write_file(
            session,
            _db(request),
            actor,
            _ws(request),
            found,
            path,
            data,
            message=body.message,
            base_sha="" if blind else body.base_sha,
            source="api",
            allow_secrets=body.allow_secrets,
        )
    except SynaptaneError as error:
        if blind and error.code is ErrorCode.CONFLICT:
            raise conflict(
                "This file already exists: pass its `base_sha` (read with GET …/files/{path}) "
                "to replace it, or `force: true` to overwrite it knowingly.",
                path=path,
            ) from None
        raise
    return WriteOut(commit=sha)


@router.delete(
    SPACE + "/files/{path:path}", status_code=status.HTTP_204_NO_CONTENT, tags=["content"]
)
def delete(
    org: str,
    space: str,
    path: str,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    message: str = "",
) -> Response:
    found = find_space(session, actor, org, space)
    content.delete_file(
        session, _db(request), actor, _ws(request), found, path, message=message, source="api"
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(SPACE + "/move", status_code=status.HTTP_204_NO_CONTENT, tags=["content"])
def move(
    org: str, space: str, body: MoveIn, request: Request, session: DbSession, actor: CurrentActor
) -> Response:
    found = find_space(session, actor, org, space)
    content.move_file(
        session,
        _db(request),
        actor,
        _ws(request),
        found,
        body.source,
        body.target,
        message=body.message,
        source="api",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(SPACE + "/history/{path:path}", tags=["content"])
def history(
    org: str, space: str, path: str, request: Request, session: DbSession, actor: CurrentActor
) -> list[CommitOut]:
    found = find_space(session, actor, org, space)
    return [
        CommitOut(sha=c.sha, author=c.author.name, date=c.date, subject=c.subject)
        for c in content.history(actor, _ws(request), found, path)
    ]


@router.post(SPACE + "/restore", tags=["content"])
def restore(
    org: str, space: str, body: RestoreIn, request: Request, session: DbSession, actor: CurrentActor
) -> WriteOut:
    """Puts a file back as it was in a past version, a deleted file too: a new commit."""
    found = find_space(session, actor, org, space)
    sha = content.restore_version(
        session,
        _db(request),
        actor,
        _ws(request),
        found,
        body.path,
        body.commit,
        message=body.message,
        base_sha=body.base_sha,
        source="api",
        allow_secrets=body.allow_secrets,
    )
    return WriteOut(commit=sha)


class ReportIn(BaseModel):
    reason: str = Field(description="What no longer holds (500 characters at most)")


class ReportOut(BaseModel):
    reason: str
    by: str
    at: str


@router.get(SPACE + "/reports/{path:path}", tags=["content"])
def page_reports(
    org: str, space: str, path: str, session: DbSession, actor: CurrentActor
) -> list[ReportOut]:
    """Open reports that a page is outdated."""
    found = find_space(session, actor, org, space)
    return [
        ReportOut(reason=r.reason, by=r.by, at=r.at.isoformat())
        for r in reports.open_reports(session, actor, found, path)
    ]


@router.post(SPACE + "/reports/{path:path}", status_code=status.HTTP_201_CREATED, tags=["content"])
def report_page(
    org: str,
    space: str,
    path: str,
    body: ReportIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> Response:
    """Reports a page as outdated (reader); again: updates the reason."""
    found = find_space(session, actor, org, space)
    reports.report(session, actor, _ws(request), found, path, body.reason)
    return Response(status_code=status.HTTP_201_CREATED)


@router.delete(
    SPACE + "/reports/{path:path}", status_code=status.HTTP_204_NO_CONTENT, tags=["content"]
)
def close_reports(
    org: str, space: str, path: str, session: DbSession, actor: CurrentActor, all: bool = False
) -> Response:
    """Withdraws one's own report, or with `all=true` dismisses them all (contributor)."""
    found = find_space(session, actor, org, space)
    resolution = reports.DISMISSED if all else reports.WITHDRAWN
    reports.resolve(session, actor, found, path, resolution)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class PendingOut(BaseModel):
    space: str = Field(description="org/space")
    name: str = Field(description="Note name, for GET …/inbox/{name}")
    title: str
    deposited_by: str
    deposited_at: str


SpacesFilter = Annotated[list[str], Query(description="Restrict to these spaces (org/space)")]


def _pending(
    request: Request, session: DbSession, actor: CurrentActor, q: str, wanted: list[str]
) -> list[PendingOut]:
    visible = spaces.list_all_visible_spaces(session, actor)
    if wanted:
        visible = [s for s in visible if s.address in set(wanted)]
    return [
        PendingOut(
            space=s.address,
            name=note.name,
            title=note.title,
            deposited_by=without_email(note.deposited_by),
            deposited_at=note.deposited_at,
        )
        for s, note in deposits.pending_notes(actor, _ws(request), visible, q, limit=20)
    ]


@router.get("/search", tags=["search"])
def search(
    request: Request,
    response: Response,
    session: DbSession,
    actor: CurrentActor,
    q: str,
    spaces: SpacesFilter = [],  # noqa: B006
    regex: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    mode: Annotated[str, Query(pattern="^(auto|text)$")] = "auto",
) -> list[HitOut]:
    """Pages ranked by relevance. Matching dropped notes not yet tidied are counted in
    `X-Pending-Count` and listed by `GET /search/pending` (the response stays a list, so as
    not to break existing clients)."""
    hits = search_service.search(
        session, actor, _ws(request), q, addresses=spaces, regex=regex, limit=limit, mode=mode
    )
    if not regex:
        response.headers["X-Pending-Count"] = str(len(_pending(request, session, actor, q, spaces)))
    return [
        HitOut(
            space=h.address,
            path=h.path,
            title=h.title,
            score=h.score,
            extracted=h.extracted,
            match=h.match,
            verified=h.verified,
            stale=h.stale,
            freshness=_freshness(h.freshness),
            edited=h.edit.date if h.edit else None,
            edited_by=h.edit.author if h.edit else None,
            edited_by_ai=h.edit.origin if h.edit and h.edit.ai else None,
            reported_outdated=h.reported,
            snippets=[SnippetOut(**vars(s)) for s in h.snippets],
        )
        for h in hits
    ]


@router.get("/search/pending", tags=["search"])
def search_pending(
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    q: str,
    spaces: SpacesFilter = [],  # noqa: B006
) -> list[PendingOut]:
    """Notes awaiting tidy-up that contain all the words of `q` (20 at most, newest to
    oldest): what was just dropped can be found right away."""
    return _pending(request, session, actor, q, spaces)


# --- Inbox ------------------------------------------------------------------------------------


class AttachmentIn(BaseModel):
    name: str
    content_base64: str


class InboxIn(BaseModel):
    title: str
    content: str = ""
    hint: str = ""
    source: str = ""
    attachments: list[AttachmentIn] = []
    allow_secrets: bool = Field(
        default=False, description="Drop despite a detected likely secret (422 otherwise)."
    )


class InboxItemOut(BaseModel):
    name: str
    type: str
    title: str
    deposited_by: str
    deposited_at: str
    status: str
    reason: str
    hint: str
    source: str
    via: str
    origin: str
    attachments: list[str]


class DepositOut(BaseModel):
    path: str


@router.get(SPACE + "/inbox", tags=["inbox"])
def inbox(
    org: str,
    space: str,
    request: Request,
    response: Response,
    session: DbSession,
    actor: CurrentActor,
    kind: Literal["", "note", "change", "left"] = Query(
        "", description="note, change or left (left by the tidy-up); empty: all"
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[InboxItemOut]:
    """Pending items, paginated; the total (after filtering) is in `X-Total-Count`."""
    found = find_space(session, actor, org, space)
    items = deposits.matching(deposits.inbox(session, actor, _ws(request), found), "", kind)
    response.headers["X-Total-Count"] = str(len(items))
    return [
        InboxItemOut(
            name=i.name,
            type=i.type,
            title=i.title,
            deposited_by=without_email(i.deposited_by),
            deposited_at=i.deposited_at,
            status=i.status,
            reason=i.reason,
            hint=i.hint,
            source=i.source,
            via=i.via,
            origin=i.origin,
            attachments=i.attachments,
        )
        for i in items[offset : offset + limit]
    ]


@router.post(SPACE + "/inbox", status_code=status.HTTP_201_CREATED, tags=["inbox"])
def deposit(
    org: str, space: str, body: InboxIn, request: Request, session: DbSession, actor: CurrentActor
) -> DepositOut:
    try:
        files = [
            (a.name, base64.b64decode(a.content_base64, validate=True)) for a in body.attachments
        ]
    except ValueError:
        raise invalid("Attachment: invalid base64.") from None
    path = deposits.deposit(
        session,
        _db(request),
        actor,
        _ws(request),
        find_space(session, actor, org, space),
        title=body.title,
        body=body.content,
        source="api",
        origin=body.source,
        hint=body.hint,
        attachments=files,
        allow_secrets=body.allow_secrets,
    )
    return DepositOut(path=path)


class InboxItemDetail(InboxItemOut):
    body: str = Field(description="Note text; for a changed page, commits and diff")


class InboxPatch(BaseModel):
    title: str | None = None
    content: str | None = None
    hint: str | None = None
    source: str | None = Field(default=None, description="Provenance (stored in `origin`)")
    allow_secrets: bool = Field(
        default=False, description="Save despite a detected likely secret (422 otherwise)."
    )


class InboxMove(BaseModel):
    space: str = Field(description='Target space, "org/space"')


class MovedOut(BaseModel):
    space: str
    path: str


def _detail(item: InboxItem) -> InboxItemDetail:
    return InboxItemDetail(
        name=item.name,
        type=item.type,
        title=item.title,
        deposited_by=without_email(item.deposited_by),
        deposited_at=item.deposited_at,
        status=item.status,
        reason=item.reason,
        hint=item.hint,
        source=item.source,
        via=item.via,
        origin=item.origin,
        attachments=item.attachments,
        body=item.body,
    )


ITEM = SPACE + "/inbox/{name}"


@router.get(ITEM, tags=["inbox"])
def inbox_item(
    org: str, space: str, name: str, request: Request, session: DbSession, actor: CurrentActor
) -> InboxItemDetail:
    """An inbox item: note (with its text) or changed page (with its commits and diff), by
    the `name` given by `GET …/inbox` or `GET /search/pending`."""
    found = find_space(session, actor, org, space)
    return _detail(deposits.describe_item(session, actor, _ws(request), found, name))


@router.patch(ITEM, tags=["inbox"])
def update_inbox_item(
    org: str,
    space: str,
    name: str,
    body: InboxPatch,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> InboxItemDetail:
    """Correct a note: only the given fields change. A note left by the tidy-up becomes
    pending again."""
    found = find_space(session, actor, org, space)
    ws = _ws(request)
    note = deposits.read_item(actor, ws, found, name)
    deposits.update_item(
        session,
        _db(request),
        actor,
        ws,
        found,
        name,
        title=note.title if body.title is None else body.title,
        body=note.body if body.content is None else body.content,
        hint=note.hint if body.hint is None else body.hint,
        origin=note.origin if body.source is None else body.source,
        allow_secrets=body.allow_secrets,
    )
    return _detail(deposits.read_item(actor, ws, found, name))


@router.delete(ITEM, status_code=status.HTTP_204_NO_CONTENT, tags=["inbox"])
def delete_inbox_item(
    org: str, space: str, name: str, request: Request, session: DbSession, actor: CurrentActor
) -> Response:
    """Remove a note and its attachments (the history keeps them)."""
    found = find_space(session, actor, org, space)
    deposits.delete_item(session, _db(request), actor, _ws(request), found, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(ITEM + "/move", tags=["inbox"])
def move_inbox_item(
    org: str,
    space: str,
    name: str,
    body: InboxMove,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> MovedOut:
    """Move a note (and its attachments) to another space's inbox."""
    found = find_space(session, actor, org, space)
    target_org, _, target_space = body.space.strip().strip("/").partition("/")
    target = find_space(session, actor, target_org, target_space)
    path = deposits.move_item(session, _db(request), actor, _ws(request), found, name, target)
    return MovedOut(space=target.address, path=path)


@router.post(ITEM + "/seen", status_code=status.HTTP_204_NO_CONTENT, tags=["inbox"])
def mark_change_seen(
    org: str, space: str, name: str, request: Request, session: DbSession, actor: CurrentActor
) -> Response:
    """Mark a changed page (`change` item) as seen: it asks nothing of the tidy-up until
    its next change."""
    found = find_space(session, actor, org, space)
    deposits.dismiss_change(session, actor, _ws(request), found, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(ITEM + "/files/{filename}", tags=["inbox"])
def inbox_attachment(
    org: str,
    space: str,
    name: str,
    filename: str,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> Response:
    """A note's attachment, as raw bytes (downloaded, never run)."""
    found = find_space(session, actor, org, space)
    data = deposits.read_attachment(actor, _ws(request), found, name, filename)
    return Response(
        data,
        media_type=content.mime_of(filename),
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )
