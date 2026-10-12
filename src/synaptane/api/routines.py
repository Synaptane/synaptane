"""REST API for a space's agent routines: settings, launching, run log.

Same rules as the web UI: organization admins create, edit, launch and delete; the space's
contributors read the routines and their runs."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from synaptane.services import routines
from synaptane.storage.models import Routine, RoutineRun, Space

from .content import SPACE
from .routes import CurrentActor, DbSession, find_space

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
        "emails (number sent), webhook (true if sent)"
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
    name: str
    template: str = Field(description='Original template (coherence, digest); "": blank')
    instructions: str
    frequency: str
    weekday: int | None = Field(description="Weekly: 0 = Monday … 6 = Sunday")
    monthday: int | None = Field(description="Monthly: 1 to 28")
    at: str = Field(description="HH:MM, the space's local time (tidy-up time zone)")
    scope: list[str] = Field(description="Folders relative to content/; empty: the whole space")
    output_page: str = Field(description='Page rewritten on each run; "": none')
    output_inbox: bool
    output_email: str = Field(description='"" (none), admins or members')
    output_webhook: bool
    enabled: bool
    next_run: datetime | None = Field(description="Next scheduled run")
    last_run: RoutineRunOut | None

    @classmethod
    def of(
        cls, routine: Routine, space_next: datetime | None, last: RoutineRun | None
    ) -> "RoutineOut":
        return cls(
            id=routine.id,
            name=routine.name,
            template=routine.template,
            instructions=routine.instructions,
            frequency=routine.frequency,
            weekday=routine.weekday,
            monthday=routine.monthday,
            at=routine.at,
            scope=list(routine.scope or []),
            output_page=routine.output_page,
            output_inbox=routine.output_inbox,
            output_email=routine.output_email,
            output_webhook=routine.output_webhook,
            enabled=routine.enabled,
            next_run=space_next,
            last_run=RoutineRunOut.of(last) if last else None,
        )


class RoutineIn(BaseModel):
    """Creation: `template` starts from a provided template (its settings, then the given
    fields); update: only the given fields change."""

    template: Literal["", "coherence", "digest"] = ""
    name: str | None = None
    instructions: str | None = None
    frequency: Frequency | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    monthday: int | None = Field(default=None, ge=1, le=28)
    at: str | None = Field(default=None, description="HH:MM")
    scope: list[str] | None = None
    output_page: str | None = None
    output_inbox: bool | None = None
    output_email: EmailTarget | None = None
    output_webhook: bool | None = None
    enabled: bool | None = None

    def fields(self) -> routines.Fields:
        return routines.Fields(**self.model_dump(exclude={"template"}))


def _out(session: DbSession, space: Space, routine: Routine) -> RoutineOut:
    last = routines.last_runs(session, space).get(routine.id)
    return RoutineOut.of(routine, routines.next_run(routine, space), last)


@router.get(ROUTINES, tags=["routines"])
def list_routines(
    org: str, space: str, session: DbSession, actor: CurrentActor
) -> list[RoutineOut]:
    found = find_space(session, actor, org, space)
    last = routines.last_runs(session, found)
    return [
        RoutineOut.of(r, routines.next_run(r, found), last.get(r.id))
        for r in routines.list_routines(session, actor, found)
    ]


@router.post(ROUTINES, status_code=status.HTTP_201_CREATED, tags=["routines"])
def create_routine(
    org: str, space: str, body: RoutineIn, session: DbSession, actor: CurrentActor
) -> RoutineOut:
    found = find_space(session, actor, org, space)
    routine = routines.create(session, actor, found, body.fields(), template=body.template)
    return _out(session, found, routine)


@router.get(ROUTINES + "/{routine_id}", tags=["routines"])
def get_routine(
    org: str, space: str, routine_id: UUID, session: DbSession, actor: CurrentActor
) -> RoutineOut:
    found = find_space(session, actor, org, space)
    return _out(session, found, routines.get(session, actor, found, routine_id))


@router.patch(ROUTINES + "/{routine_id}", tags=["routines"])
def update_routine(
    org: str,
    space: str,
    routine_id: UUID,
    body: RoutineIn,
    session: DbSession,
    actor: CurrentActor,
) -> RoutineOut:
    found = find_space(session, actor, org, space)
    routine = routines.get(session, actor, found, routine_id)
    routines.update(session, actor, found, routine, body.fields())
    return _out(session, found, routine)


@router.delete(
    ROUTINES + "/{routine_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["routines"]
)
def delete_routine(
    org: str, space: str, routine_id: UUID, session: DbSession, actor: CurrentActor
) -> Response:
    found = find_space(session, actor, org, space)
    routines.delete(session, actor, found, routines.get(session, actor, found, routine_id))
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
