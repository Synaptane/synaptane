"""REST API for the tidy-up: the organization's AI provider, settings, runs."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from synaptane.ai.base import SUBSCRIPTION_PROVIDERS, AIError
from synaptane.ai.providers import check_connection
from synaptane.core.errors import invalid
from synaptane.services import (
    ai_settings,
    budget,
    night,
    organizations,
    semantic,
    tidy,
    tidy_undo,
)
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
    maintain: bool | None = Field(
        default=None,
        description="Nightly maintenance: fix the problems people introduced since the last "
        "run (broken links, new pages isolated, missing from their folder's index, untitled or "
        "empty), even with an empty inbox",
    )
    report_email: Literal["", "admins", "members"] | None = Field(
        default=None,
        description='Report of the night by email: "" (nobody), admins (of the organization) '
        "or members (of the space)",
    )
    report_always: bool | None = Field(
        default=None, description="Send the report even when nothing happened in the night"
    )
    report_webhook_url: str | None = Field(
        default=None,
        description="Slack (or compatible) incoming webhook for the report; null: unchanged, "
        '"": removed. Stored encrypted, never returned',
    )


class TidySettingsOut(BaseModel):
    enabled: bool
    time: str = Field(description="HH:MM, local time")
    timezone: str
    model: str = Field(description='"": the organization\'s model')
    delay: int = Field(description="Minutes between a drop and the continuous tidy-up; 0: off")
    maintain: bool
    report_email: str = Field(description='"", admins or members')
    report_always: bool
    report_webhook: bool = Field(
        description="A Slack webhook is configured for the report (its address is never returned)"
    )


class NightStepOut(BaseModel):
    kind: str = Field(description="routine or tidy")
    when: str = Field(description='before or after the tidy-up; "" for the tidy-up')
    name: str = Field(default="", description="File name of the routine")
    title: str = ""
    routine_id: UUID | None = None
    run_id: UUID | None = Field(
        default=None, description="Its run: GET …/tidy/runs or …/routines/{id}/runs"
    )
    status: str = Field(description="queued, running, done, failed, nothing or skipped")
    reason: str = Field(
        default="",
        description="Why skipped or failed: disabled, earlier, no_ai, budget, connection, "
        "changed, retry, interrupted, superseded, internal",
    )


class NightOut(BaseModel):
    id: UUID
    day: date = Field(description="Local day of the space")
    status: str = Field(description="queued, running or done")
    started_at: datetime | None
    finished_at: datetime | None
    steps: list[NightStepOut] = Field(description="In order")
    report: dict[str, object] = Field(
        description="What the report sent: quiet (nothing happened), emails (number), slack "
        "(queued, sent, or the error)"
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


@router.get(SPACE + "/tidy-settings", tags=["tidy"])
def get_tidy_settings(
    org: str, space: str, session: DbSession, actor: CurrentActor
) -> TidySettingsOut:
    found = find_space(session, actor, org, space)
    report = night.view(session, actor, found)  # organization admins
    return TidySettingsOut(
        enabled=found.tidy_enabled,
        time=found.tidy_time,
        timezone=found.tidy_timezone,
        model=found.ai_model or "",
        delay=found.tidy_delay or 0,
        maintain=found.tidy_maintain,
        report_email=report.email,
        report_always=report.always,
        report_webhook=report.webhook,
    )


@router.patch(SPACE + "/tidy-settings", status_code=status.HTTP_204_NO_CONTENT, tags=["tidy"])
def tidy_settings(
    org: str,
    space: str,
    body: TidySettingsIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    box: Box,
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
        maintain=body.maintain,
    )
    night.update_report(
        session,
        actor,
        box,
        found,
        email=body.report_email,
        always=body.report_always,
        webhook_url=body.report_webhook_url,
        allow_private=request.app.state.settings.allow_private_webhooks,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    SPACE + "/tidy-settings/test-report", status_code=status.HTTP_202_ACCEPTED, tags=["tidy"]
)
def tidy_test_report(
    org: str, space: str, request: Request, session: DbSession, actor: CurrentActor
) -> MessageOut:
    """The last night's report, marked as a test: by email to the caller only, and to the
    Slack webhook (queued)."""
    found = find_space(session, actor, org, space)
    emailed, posted = night.send_test(
        session, actor, _ws(request), found, request.app.state.settings.public_url
    )
    parts = (["email"] if emailed else []) + (["Slack"] if posted else [])
    return MessageOut(
        message=api_text(request, "Test report sent: {targets}.", targets=", ".join(parts))
    )


@router.get(SPACE + "/nights", tags=["tidy"])
def tidy_nights(org: str, space: str, session: DbSession, actor: CurrentActor) -> list[NightOut]:
    """The last nights of the space: routines of the night and tidy-up, in order."""
    found = find_space(session, actor, org, space)
    return [
        NightOut(
            id=n.id,
            day=n.day,
            status=n.status,
            started_at=n.started_at,
            finished_at=n.finished_at,
            steps=[NightStepOut.model_validate(step) for step in n.steps],
            report=dict(n.report or {}),
        )
        for n in night.nights(session, actor, found)
    ]


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


class RunPageOut(BaseModel):
    path: str = Field(description="Where the run left the page (relative to content/)")
    status: str = Field(description="created, edited, deleted or moved")
    old: str = Field(description="Former path of a moved page")
    state: str = Field(description="undoable, changed (since the run: refused) or undone")


@router.get(SPACE + "/tidy/runs/{run_id}/pages", tags=["tidy"])
def tidy_run_pages(
    org: str, space: str, run_id: UUID, request: Request, session: DbSession, actor: CurrentActor
) -> list[RunPageOut]:
    found = find_space(session, actor, org, space)
    _, pages = tidy_undo.run_pages(session, actor, _ws(request), found, run_id)
    return [RunPageOut(path=p.path, status=p.status, old=p.old, state=p.state) for p in pages]


class UndoPagesIn(BaseModel):
    paths: list[str] = Field(description="Pages to put back as they were before the run")


class UndoPagesOut(BaseModel):
    commit: str | None
    pages: list[str]
    notes_back: list[str] = Field(description="Titles of the notes back in the inbox")
    notes_kept: list[str] = Field(description="Notes still filed: other pages keep them")


@router.post(SPACE + "/tidy/runs/{run_id}/undo-pages", tags=["tidy"])
def tidy_undo_pages(
    org: str,
    space: str,
    run_id: UUID,
    body: UndoPagesIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
) -> UndoPagesOut:
    found = find_space(session, actor, org, space)
    done = tidy_undo.undo_pages(
        session, _db(request), actor, _ws(request), found, run_id, body.paths, source="api"
    )
    return UndoPagesOut(
        commit=done.commit,
        pages=done.pages,
        notes_back=done.notes_back,
        notes_kept=[title for title, _ in done.notes_kept],
    )


class EmbeddingsOut(BaseModel):
    provider: str
    model: str
    base_url: str
    has_key: bool
    meaning_min_similarity: float | None = None


class EmbeddingsIn(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    api_key: str | None = Field(default=None, description='null: key unchanged; "": key removed')
    meaning_min_similarity: float | None = Field(
        default=None,
        gt=0,
        lt=1,
        description="similarity the best page must reach for a page found only by meaning; "
        "null: it must stand out from the other neighbours. Dropped when the model changes, "
        "unless a new value is given.",
    )


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
