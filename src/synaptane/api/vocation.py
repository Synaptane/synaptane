"""REST API for a space's purpose and its tidy-up rules (files in the repository).

The purpose can be read by any member of the space; the rules, and any write, are reserved
to organization admins. Same rules as the web UI: sha read before modifying (`base_sha`),
likely secret refused, text pending until the space is synced.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from synaptane.services import vocation
from synaptane.storage.models import Space

from .content import SPACE, _db, _ws
from .routes import CurrentActor, DbSession, find_space

router = APIRouter(prefix="/api/v1")


class ChangeOut(BaseModel):
    commit: str
    author: str
    date: str
    subject: str


class DocumentOut(BaseModel):
    path: str = Field(description="File path in the repository")
    text: str = Field(description='Markdown content; "" if the file does not exist')
    sha: str | None = Field(
        description="File sha (to send back in `base_sha`); null if it does not exist"
    )
    pending: bool = Field(
        description="Pending text: the space is not synced yet, it will be written "
        "to the repository on the first sync"
    )
    history: list[ChangeOut] = Field(description="Latest changes (5 at most)")

    @classmethod
    def of(cls, document: vocation.Document) -> "DocumentOut":
        return cls(
            path=document.path,
            text=document.text,
            sha=document.sha,
            pending=document.pending,
            history=[
                ChangeOut(commit=c.sha, author=c.author.name, date=c.date, subject=c.subject)
                for c in document.history
            ],
        )


class DocumentIn(BaseModel):
    text: str = Field(
        description='Markdown content (20,000 characters at most); "" removes the file'
    )
    base_sha: str | None = Field(
        default=None,
        description='Sha read before modifying; "" to require that the file does not exist '
        "yet. Conflict (409) if it changed in the meantime (push, another admin).",
    )
    allow_secrets: bool = Field(
        default=False, description="Save despite a detected likely secret (422 otherwise)."
    )


class SavedOut(BaseModel):
    commit: str | None = Field(description="Commit created; null if nothing changed or pending")
    pending: bool = Field(description="Waiting for the space's first sync")


class TemplateOut(BaseModel):
    key: str
    label: str
    summary: str
    text: str


def _save(
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    space: Space,
    kind: vocation.Kind,
    body: DocumentIn,
) -> SavedOut:
    ws = _ws(request)
    sha = vocation.save(
        session,
        _db(request),
        actor,
        ws,
        space,
        kind,
        body.text,
        base_sha=body.base_sha,
        allow_secrets=body.allow_secrets,
        source="api",
    )
    return SavedOut(commit=sha, pending=vocation.document(actor, ws, space, kind).pending)


@router.get("/vocation-templates", tags=["spaces"])
def vocation_templates(actor: CurrentActor) -> list[TemplateOut]:
    """Purpose templates offered when creating a space, in the caller's language."""
    return [
        TemplateOut(key=t.key, label=t.label, summary=t.summary, text=t.text_for(actor.user.locale))
        for t in vocation.TEMPLATES.values()
    ]


@router.get(SPACE + "/vocation", tags=["spaces"])
def get_vocation(
    org: str, space: str, request: Request, session: DbSession, actor: CurrentActor
) -> DocumentOut:
    found = find_space(session, actor, org, space)
    return DocumentOut.of(vocation.document(actor, _ws(request), found, vocation.VOCATION))


@router.put(SPACE + "/vocation", tags=["spaces"])
def put_vocation(
    org: str,
    space: str,
    body: DocumentIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> SavedOut:
    found = find_space(session, actor, org, space)
    return _save(request, session, actor, found, vocation.VOCATION, body)


@router.get(SPACE + "/tidy-rules", tags=["tidy"])
def get_tidy_rules(
    org: str, space: str, request: Request, session: DbSession, actor: CurrentActor
) -> DocumentOut:
    found = find_space(session, actor, org, space)
    return DocumentOut.of(vocation.document(actor, _ws(request), found, vocation.RULES))


@router.put(SPACE + "/tidy-rules", tags=["tidy"])
def put_tidy_rules(
    org: str,
    space: str,
    body: DocumentIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> SavedOut:
    found = find_space(session, actor, org, space)
    return _save(request, session, actor, found, vocation.RULES, body)
