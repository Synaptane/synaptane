"""REST API for the tidy-up: the organization's AI provider, settings, runs."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from synaptane.ai.base import SUBSCRIPTION_PROVIDERS, AIError
from synaptane.ai.providers import check_connection
from synaptane.core.errors import invalid
from synaptane.services import ai_settings, budget, organizations, semantic, tidy
from synaptane.storage.models import TidyRun

from .content import SPACE, _db, _ws
from .routes import Box, CurrentActor, DbSession, api_text, find_space

router = APIRouter(prefix="/api/v1")


class AIOut(BaseModel):
    provider: str
    model: str
    base_url: str
    effort: str
    has_key: bool
    price_input: Decimal | None
    price_output: Decimal | None
    monthly_budget: Decimal | None
    month_spent: Decimal = Field(description="Estimated cost of the current month's runs, in €")
    tested_at: datetime | None = Field(default=None, description="Last connection test")
    test_ok: bool | None = None
    test_message: str = ""
    test_pending: bool = Field(
        default=False, description="Subscription test requested, not yet done by the worker"
    )
    metered: bool = Field(
        default=False,
        description="Provider billed per use (API key): without monthly_budget, spending "
        "has no limit",
    )


class AIIn(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    effort: str = "medium"
    api_key: str | None = Field(default=None, description='null: key unchanged; "": key removed')
    price_input: Decimal | None = Field(
        default=None, ge=0, description="€ per million input tokens; null: public price"
    )
    price_output: Decimal | None = Field(
        default=None, ge=0, description="€ per million output tokens; null: public price"
    )
    monthly_budget: Decimal | None = Field(
        default=None,
        ge=0,
        description="€ per month; null: no budget. Refused if the organization's model "
        "or a space's model has no known price and no price is entered",
    )


class TidySettingsIn(BaseModel):
    enabled: bool | None = None
    time: str | None = Field(default=None, description="HH:MM, local time")
    timezone: str | None = Field(default=None, description="IANA time zone, e.g. Europe/Paris")
    model: str | None = Field(default=None, description='"": the organization\'s model')
    delay: int | None = Field(
        default=None,
        description="Minutes between a drop and the continuous tidy-up; 0: off",
    )


class RunOut(BaseModel):
    id: UUID
    trigger: str
    status: str
    model: str
    started_at: datetime
    finished_at: datetime | None
    items_done: int
    items_left: int
    summary: str
    operations: list[dict[str, object]]
    commit: str | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost: Decimal | None = Field(
        description="Estimated cost in €; null: subscription or unknown price"
    )
    quota: dict[str, object] | None = Field(
        default=None,
        description="Subscription: share used (%) of each window (five_hour, seven_day) "
        "at the start (before) and end (after) of the run, and its reset (resets_at)",
    )
    metrics: dict[str, object] | None = Field(
        default=None,
        description="Run quality, measured without AI before the commit: pages created, "
        "modified, moved, deleted (pages); broken links before, after and introduced "
        "(broken_links); created pages without incoming links (orphans), missing from the "
        "folder index (not_in_index), untitled (untitled) or empty (empty); items filed without "
        "a stated page (closed_without_page); characters removed from kept pages "
        "(removed_chars); tokens, turns and cost per filed item (usage.per_item). null: "
        "failed run or run predating the measurement",
    )
    error: str | None
    reverted_at: datetime | None

    @classmethod
    def of(cls, run: TidyRun) -> "RunOut":
        return cls.model_validate(run, from_attributes=True)


class MessageOut(BaseModel):
    message: str


@router.get("/organizations/{org}/ai", tags=["tidy"])
def get_ai(
    org: str, request: Request, session: DbSession, actor: CurrentActor, box: Box
) -> AIOut | None:
    found = organizations.get_organization(session, actor, org)
    view = ai_settings.view(session, actor, box, found)
    if view is None:
        return None
    message = api_text(request, view.test_message)
    return AIOut(
        **{**vars(view), "test_message": message}, month_spent=budget.spent(session, found.id)
    )


@router.put("/organizations/{org}/ai", tags=["tidy"])
def put_ai(org: str, body: AIIn, session: DbSession, actor: CurrentActor, box: Box) -> AIOut:
    found = organizations.get_organization(session, actor, org)
    view = ai_settings.configure(session, actor, box, found, **body.model_dump())
    return AIOut(**vars(view), month_spent=budget.spent(session, found.id))


@router.post(
    "/organizations/{org}/ai/test",
    tags=["tidy"],
    responses={202: {"description": "Subscription: test left to the worker, result in GET …/ai"}},
)
async def test_ai(
    org: str,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    box: Box,
    response: Response,
) -> MessageOut:
    found = organizations.get_organization(session, actor, org)
    config = ai_settings.org_config(session, actor, box, found)
    if config is None:
        raise invalid("No AI provider configured.")
    if config.provider in SUBSCRIPTION_PROVIDERS:
        # The CLI and its login only exist in the worker: deferred test.
        ai_settings.request_test(session, actor, found)
        response.status_code = status.HTTP_202_ACCEPTED
        return MessageOut(
            message=api_text(
                request,
                "Test started in the worker: result in GET /organizations/{org}/ai "
                "(test_pending, test_ok, test_message).",
                org=org,
            )
        )
    try:
        return MessageOut(
            message=api_text(request, await run_in_threadpool(check_connection, config))
        )
    except AIError as error:
        raise invalid(error.label.template, **error.label.params) from None


@router.patch(SPACE + "/tidy-settings", status_code=status.HTTP_204_NO_CONTENT, tags=["tidy"])
def tidy_settings(
    org: str, space: str, body: TidySettingsIn, session: DbSession, actor: CurrentActor
) -> Response:
    found = find_space(session, actor, org, space)
    tidy.update_settings(
        session,
        actor,
        found,
        enabled=body.enabled,
        at=body.time,
        timezone=body.timezone,
        model=body.model,
        delay=body.delay,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(SPACE + "/tidy", status_code=status.HTTP_202_ACCEPTED, tags=["tidy"])
def tidy_now(org: str, space: str, session: DbSession, actor: CurrentActor) -> Response:
    tidy.request_run(session, actor, find_space(session, actor, org, space))
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get(SPACE + "/tidy/runs", tags=["tidy"])
def tidy_runs(org: str, space: str, session: DbSession, actor: CurrentActor) -> list[RunOut]:
    return [RunOut.of(r) for r in tidy.runs(session, actor, find_space(session, actor, org, space))]


@router.post(SPACE + "/tidy/runs/{run_id}/revert", tags=["tidy"])
def tidy_revert(
    org: str, space: str, run_id: UUID, request: Request, session: DbSession, actor: CurrentActor
) -> MessageOut:
    found = find_space(session, actor, org, space)
    sha = tidy.revert(session, _db(request), actor, _ws(request), found, run_id)
    return MessageOut(message=api_text(request, "Run reverted ({sha}).", sha=sha[:10]))


class EmbeddingsOut(BaseModel):
    provider: str
    model: str
    base_url: str
    has_key: bool


class EmbeddingsIn(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    api_key: str | None = Field(default=None, description='null: key unchanged; "": key removed')


class SemanticIn(BaseModel):
    enabled: bool


class SemanticOut(BaseModel):
    enabled: bool
    files: int
    chunks: int


@router.get("/organizations/{org}/embeddings", tags=["search"])
def get_embeddings(
    org: str, session: DbSession, actor: CurrentActor, box: Box
) -> EmbeddingsOut | None:
    found = organizations.get_organization(session, actor, org)
    view = semantic.view(session, actor, box, found)
    return EmbeddingsOut(**vars(view)) if view else None


@router.put("/organizations/{org}/embeddings", tags=["search"])
def put_embeddings(
    org: str, body: EmbeddingsIn, session: DbSession, actor: CurrentActor, box: Box
) -> EmbeddingsOut:
    found = organizations.get_organization(session, actor, org)
    return EmbeddingsOut(
        **vars(semantic.configure(session, actor, box, found, **body.model_dump()))
    )


@router.put(SPACE + "/semantic", tags=["search"])
def put_semantic(
    org: str, space: str, body: SemanticIn, session: DbSession, actor: CurrentActor
) -> SemanticOut:
    found = find_space(session, actor, org, space)
    semantic.set_enabled(session, actor, found, body.enabled)
    files, chunks = semantic.stats(session, found.id)
    return SemanticOut(enabled=found.embeddings_enabled, files=files, chunks=chunks)
