"""REST API for a space's agent routines: settings, launching, run log.

Same rules as the web UI: organization admins create, edit, launch and delete (a commit of the
routine's file `.synaptane/routines/<name>.md` in their name); the space's contributors read
the routines and their runs. The memory of a routine (`.synaptane/memory/<name>.md`) is read
by contributors and cleared by admins; only the routine's runs write it."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field

from synaptane.core.errors import invalid
from synaptane.services import routine_files, routine_memory, routines
from synaptane.storage.models import Routine, RoutineRun, Space

from .content import SPACE, _db, _ws
from .routes import CurrentActor, DbSession, api_text, find_space

router = APIRouter(prefix="/api/v1")
ROUTINES = SPACE + "/routines"
Frequency = Literal["daily", "weekly", "monthly"]
EmailTarget = Literal["", "admins", "members"]


class RoutineRunOut(BaseModel):
    id: UUID
    routine_id: UUID | None = Field(description="null: routine deleted since")
    name: str
    trigger: str = Field(description="schedule (scheduled) or manual (launched by hand)")
    status: str = Field(description="running, done or failed")
    model: str
    started_at: datetime
    finished_at: datetime | None
    summary: str
    report: str = Field(description="Output text (page, email, webhook)")
    outputs: dict[str, object] = Field(
        description="What was output: page (path), inbox (titles of the dropped items), "
        "written (pages written in the folders it keeps: path, change created, modified, "
        "moved with from, or deleted), conflicts (pages a person changed during the run: "
        "their version kept), emails (number sent), webhook (true if sent), memory (saved, "
        "cleared, or changed: changed by someone during the run, so kept as it was)"
    )
    commit: str | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost: Decimal | None = Field(description="Estimated cost in €; null: subscription or unknown")
    quota: dict[str, object] | None = Field(
        default=None, description="Subscription: share of the windows used (see the runs)"
    )
    error: str | None

    @classmethod
    def of(cls, run: RoutineRun) -> "RoutineRunOut":
        return cls.model_validate(run, from_attributes=True)


class RoutineOut(BaseModel):
    id: UUID
    name: str = Field(description="File name, without `.md`: `.synaptane/routines/<name>.md`")
    title: str
    description: str
    path: str = Field(description="File of the routine in the repository")
    sha: str | None = Field(
        description="Hash of the file (to send back in `base_sha`); null: not in the "
        "repository yet, written by the next synchronization"
    )
    pending: bool = Field(description="Waiting to be written to the repository")
    error: str | None = Field(
        description="Why the file is invalid (the routine does not run); null: valid"
    )
    warnings: list[str] = Field(description="What was ignored in the file (unknown field…)")
    template: str = Field(description='Original template (coherence, digest, archive); "": blank')
    instructions: str
    frequency: str
    weekday: int | None = Field(description="Weekly: 0 = Monday … 6 = Sunday")
    monthday: int | None = Field(description="Monthly: 1 to 28")
    at: str = Field(description="HH:MM, the space's local time (tidy-up time zone)")
    scope: list[str] = Field(description="Folders relative to content/; empty: the whole space")
    writes: list[str] = Field(
        description="Folders the routine keeps (relative to content/): it writes its pages "
        "there, and reads them even outside the scope; empty: it only writes its outputs"
    )
    output_page: str = Field(description='Page rewritten on each run; "": none')
    output_inbox: bool
    output_email: str = Field(description='"" (none), admins or members')
    output_webhook: bool
    enabled: bool
    memory: bool = Field(
        description="Keeps notes from one run to the next (`memory: true` in the file), in "
        "`.synaptane/memory/<name>.md`: see `GET …/memory`"
    )
    next_run: datetime | None = Field(description="Next scheduled run")
    last_run: RoutineRunOut | None

    @classmethod
    def of(
        cls,
        request: Request,
        routine: Routine,
        space_next: datetime | None,
        last: RoutineRun | None,
        writes: list[str],
        memory: bool = False,
    ) -> "RoutineOut":
        error = routine_files.error_of(routine)
        return cls(
            id=routine.id,
            name=routine.name,
            title=routine.title,
            description=routine.description,
            path=routines.path_of(routine),
            sha=routine.sha,
            pending=routine.sha is None,
            error=api_text(request, error) if error else None,
            warnings=[api_text(request, w) for w in routine_files.warnings_of(routine)],
            template=routine.template,
            instructions=routine.instructions,
            frequency=routine.frequency,
            weekday=routine.weekday,
            monthday=routine.monthday,
            at=routine.at,
            scope=list(routine.scope or []),
            writes=writes,
            output_page=routine.output_page,
            output_inbox=routine.output_inbox,
            output_email=routine.output_email,
            output_webhook=routine.output_webhook,
            enabled=routine.enabled,
            memory=memory,
            next_run=space_next,
            last_run=RoutineRunOut.of(last) if last else None,
        )


class RoutineIn(BaseModel):
    """Creation: `template` starts from a provided template (its settings, then the given
    fields); update: only the given fields change. Every write is a commit of the routine's
    file in the caller's name."""

    template: Literal["", "coherence", "digest", "archive"] = ""
    name: str | None = Field(
        default=None,
        description="Creation: file name (lowercase letters, digits, dashes); default: made "
        "from the title. A free text (as before routines were files) is taken as the title.",
    )
    title: str | None = None
    description: str | None = None
    instructions: str | None = None
    frequency: Frequency | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    monthday: int | None = Field(default=None, ge=1, le=28)
    at: str | None = Field(default=None, description="HH:MM")
    scope: list[str] | None = None
    writes: list[str] | None = Field(
        default=None,
        description="Folders the routine keeps (5 at most; never the whole wiki nor a hidden "
        "folder); [] removes them; omitted: unchanged",
    )
    output_page: str | None = None
    output_inbox: bool | None = None
    output_email: EmailTarget | None = None
    output_webhook: bool | None = None
    enabled: bool | None = None
    memory: bool | None = Field(
        default=None, description="Keep notes from one run to the next (`memory: true`)"
    )
    base_sha: str | None = Field(
        default=None,
        description="Update: the file's `sha` read before modifying; conflict (409) if it "
        "changed in the meantime (push, another admin). Omitted: no check.",
    )
    allow_secrets: bool = Field(
        default=False, description="Save despite a detected likely secret (422 otherwise)."
    )

    def fields(self) -> routines.Fields:
        return routines.Fields(
            **self.model_dump(
                exclude={"template", "name", "base_sha", "allow_secrets"}, exclude_unset=False
            )
        )


class MemoryOut(BaseModel):
    enabled: bool = Field(description="The routine's file asks for a memory (`memory: true`)")
    path: str = Field(description="File of the memory in the repository")
    text: str = Field(description='What the routine noted for its next runs; "": none')
    sha: str | None = Field(description="Hash of the file; null: no memory yet")
    size: int = Field(description="Bytes of the file")
    too_large: bool = Field(description="File too large to be shown (nor read by the routine)")


def _memory(request: Request, space: Space, routine: Routine) -> bool:
    return routine_files.memory_enabled(_ws(request), space, routine.name)


def _out(request: Request, session: DbSession, space: Space, routine: Routine) -> RoutineOut:
    last = routines.last_runs(session, space).get(routine.id)
    return RoutineOut.of(
        request,
        routine,
        routines.next_run(routine, space),
        last,
        routine_files.writes_of(_ws(request), space, routine),
        _memory(request, space, routine),
    )


@router.get(ROUTINES, tags=["routines"])
def list_routines(
    org: str, space: str, request: Request, session: DbSession, actor: CurrentActor
) -> list[RoutineOut]:
    found = find_space(session, actor, org, space)
    listed = routines.list_routines(session, actor, found)
    last = routines.last_runs(session, found)
    ws = _ws(request)
    memory = routine_files.memory_flags(ws, found)  # one read, not one per routine
    return [
        RoutineOut.of(
            request,
            r,
            routines.next_run(r, found),
            last.get(r.id),
            routine_files.writes_of(ws, found, r),
            memory.get(r.name, False),
        )
        for r in listed
    ]


@router.post(ROUTINES, status_code=status.HTTP_201_CREATED, tags=["routines"])
def create_routine(
    org: str,
    space: str,
    body: RoutineIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> RoutineOut:
    found = find_space(session, actor, org, space)
    fields, name = body.fields(), (body.name or "").strip()
    if name and not routine_files.NAME.fullmatch(name.removesuffix(".md")) and not body.title:
        fields.title, name = name, ""  # a title, as in the API before routines were files
    routine = routines.create(
        session,
        _db(request),
        actor,
        _ws(request),
        found,
        fields,
        name=name,
        template=body.template,
        allow_secrets=body.allow_secrets,
        source="api",
    )
    return _out(request, session, found, routine)


@router.get(ROUTINES + "/{routine_id}", tags=["routines"])
def get_routine(
    org: str,
    space: str,
    routine_id: UUID,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> RoutineOut:
    found = find_space(session, actor, org, space)
    return _out(request, session, found, routines.get(session, actor, found, routine_id))


@router.patch(ROUTINES + "/{routine_id}", tags=["routines"])
def update_routine(
    org: str,
    space: str,
    routine_id: UUID,
    body: RoutineIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> RoutineOut:
    found = find_space(session, actor, org, space)
    routine = routines.get(session, actor, found, routine_id)
    if body.name is not None and body.name.strip().removesuffix(".md") != routine.name:
        raise invalid(
            "The file name of a routine cannot be changed: change its title, or rename the "
            "file in the repository."
        )
    if routine.error is not None:
        raise invalid("The routine's file is invalid: fix it before changing the routine.")
    routine = routines.update(
        session,
        _db(request),
        actor,
        _ws(request),
        found,
        routine,
        body.fields(),
        base_sha=body.base_sha,
        allow_secrets=body.allow_secrets,
        source="api",
    )
    return _out(request, session, found, routine)


@router.delete(
    ROUTINES + "/{routine_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["routines"]
)
def delete_routine(
    org: str,
    space: str,
    routine_id: UUID,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    base_sha: Annotated[
        str | None,
        Query(description="The file's `sha` read before; conflict (409) if it changed since"),
    ] = None,
) -> Response:
    found = find_space(session, actor, org, space)
    routine = routines.get(session, actor, found, routine_id)
    routines.delete(
        session, _db(request), actor, _ws(request), found, routine, base_sha=base_sha, source="api"
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    ROUTINES + "/{routine_id}/run", status_code=status.HTTP_202_ACCEPTED, tags=["routines"]
)
def run_routine(
    org: str, space: str, routine_id: UUID, session: DbSession, actor: CurrentActor
) -> Response:
    found = find_space(session, actor, org, space)
    routines.request_run(session, actor, found, routines.get(session, actor, found, routine_id))
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get(ROUTINES + "/{routine_id}/runs", tags=["routines"])
def routine_runs(
    org: str, space: str, routine_id: UUID, session: DbSession, actor: CurrentActor
) -> list[RoutineRunOut]:
    found = find_space(session, actor, org, space)
    routine = routines.get(session, actor, found, routine_id)
    return [RoutineRunOut.of(r) for r in routines.runs(session, actor, found, routine.id)]


@router.get(ROUTINES + "/{routine_id}/memory", tags=["routines"])
def routine_memory_get(
    org: str,
    space: str,
    routine_id: UUID,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> MemoryOut:
    """What the routine remembers from one run to the next (contributors)."""
    found = find_space(session, actor, org, space)
    routine = routines.get(session, actor, found, routine_id)
    view = routine_memory.view(session, actor, _ws(request), found, routine)
    return MemoryOut(
        enabled=view.enabled,
        path=view.path,
        text=view.text,
        sha=view.sha,
        size=view.size,
        too_large=view.too_large,
    )


@router.delete(
    ROUTINES + "/{routine_id}/memory", status_code=status.HTTP_204_NO_CONTENT, tags=["routines"]
)
def routine_memory_clear(
    org: str,
    space: str,
    routine_id: UUID,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> Response:
    """Clears the routine's memory (organization admins): a commit in the caller's name. A
    run in progress keeps it cleared."""
    found = find_space(session, actor, org, space)
    routine = routines.get(session, actor, found, routine_id)
    routine_memory.clear(session, _db(request), actor, _ws(request), found, routine, source="api")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
