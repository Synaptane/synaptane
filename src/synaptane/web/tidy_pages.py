"""Nightly tidy-up on the web: the organization's AI provider, the space's settings, manual
run, run journal and undo.

The journal ("Tidy-up" tab) says what to do before telling what happened: a banner when the
tidy-up fails, grouped retries, an "In progress" card as soon as a run is requested (the page
polls `/-/tidy/state` every 3 s, see `preferences.js`), next run."""

import zoneinfo
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import pass_context
from sqlalchemy.orm import object_session

from synaptane.ai.base import SUBSCRIPTION_PROVIDERS, AIConfig, AIError
from synaptane.ai.embeddings import check_embeddings
from synaptane.ai.providers import check_connection
from synaptane.core.errors import SynaptaneError, invalid
from synaptane.services import ai_settings, budget, organizations, semantic, tidy, tidy_journal
from synaptane.storage.models import Space
from synaptane.web.pages import (
    Csrf,
    WebPage,
    failure,
    find_space,
    org_url,
    redirect,
    render_space_settings,
    space_settings_url,
    templates,
)
from synaptane.web.wiki import space_url

router = APIRouter()
TIMEZONES = sorted(zoneinfo.available_timezones())
PAID_PROVIDERS = ("anthropic", "openai", "mistral")  # billed per token, even with no price set
JOURNAL_SIZE = 60  # runs read: grouped retries take up many of them


NEXT_LABELS = {
    "tonight": "Next tidy-up: tonight at {time}",
    "today": "Next tidy-up: today at {time}",
    "tomorrow": "Next tidy-up: tomorrow at {time}",
    "later": "Next tidy-up: on {date} at {time}",
}


@pass_context
def _tidy_next_label(context: Any, space: Space) -> str:
    """The label "Next tidy-up: tonight at 02:00" (space time), or "" if no run is scheduled.
    Template global: the inbox shows it too."""
    upcoming = tidy_journal.next_run(space)
    if upcoming is None:
        return ""
    translate = context["_"]
    if upcoming.imminent:
        return str(translate("Next tidy-up: in a moment"))
    label = NEXT_LABELS[upcoming.day(datetime.now(UTC))]
    return str(
        translate(label, time=upcoming.at.strftime("%H:%M"), date=upcoming.at.strftime("%d/%m"))
    )


def _tidy_failing(space: Space) -> bool:
    """Badge of the "Tidy-up" tab: the last run failed. The space comes from the request's
    session; when detached, no badge (never an error for an ornament)."""
    session = object_session(space)
    return session is not None and tidy_journal.failing(session, space.id)


templates.env.globals["tidy_next_label"] = _tidy_next_label
templates.env.globals["tidy_failing"] = _tidy_failing


@router.post("/o/{org_slug}/-/ai")
def org_ai_update(
    page: WebPage,
    org_slug: str,
    csrf: Csrf,
    provider: Annotated[str, Form()],
    model: Annotated[str, Form()] = "",
    base_url: Annotated[str, Form()] = "",
    effort: Annotated[str, Form()] = "medium",
    api_key: Annotated[str, Form()] = "",
    clear_key: Annotated[bool, Form()] = False,
    price_input: Annotated[str, Form()] = "",
    price_output: Annotated[str, Form()] = "",
    monthly_budget: Annotated[str, Form()] = "",
    budget_suggested: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        amount = ai_settings.amount(monthly_budget, "Monthly budget")
        new_key = bool(api_key) and not clear_key
        if (
            budget_suggested
            and amount == ai_settings.amount(budget_suggested, "Monthly budget")
            and not budget.metered(provider, new_key)
        ):
            amount = None  # budget suggested by the form, irrelevant without billing
        ai_settings.configure(
            page.db,
            actor,
            page.box,
            org,
            provider=provider,
            model=model,
            base_url=base_url,
            effort=effort,
            api_key="" if clear_key else (api_key or None),
            price_input=ai_settings.amount(price_input, "Input price"),
            price_output=ai_settings.amount(price_output, "Output price"),
            monthly_budget=amount,
        )
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/ai", page, error)
    return redirect(f"{org_url(org)}/-/ai", page, "AI provider saved.")


@router.post("/o/{org_slug}/-/ai/test")
async def org_ai_test(page: WebPage, org_slug: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    config = ai_settings.org_config(page.db, actor, page.box, org)
    if config is None:
        return redirect(f"{org_url(org)}/-/ai", page, "Save a provider first.", kind="error")
    if config.provider in SUBSCRIPTION_PROVIDERS:
        # The CLI and its sign-in live only in the worker: the test runs there, the page waits.
        if not ai_settings.request_test(page.db, actor, org):
            return redirect(f"{org_url(org)}/-/ai", page, "A test is already running.")
        return redirect(
            f"{org_url(org)}/-/ai", page, "Test started in the worker: the result will show here."
        )
    try:
        message = await run_in_threadpool(check_connection, config)
    except AIError as error:
        ai_settings.record_test(page.db, org, False, error.label)
        return redirect(f"{org_url(org)}/-/ai", page, error.label, kind="error")
    ai_settings.record_test(page.db, org, True, message)
    return redirect(f"{org_url(org)}/-/ai", page, message)


@router.get("/o/{org_slug}/-/ai/test-status")
def org_ai_test_status(page: WebPage, org_slug: str) -> JSONResponse:
    """Subscription test still awaited from the worker? The AI page polls it every 3 s."""
    actor = page.user_actor()
    org = organizations.get_organization(page.db, actor, org_slug)
    view = ai_settings.view(page.db, actor, page.box, org)  # access check
    return JSONResponse(
        {"pending": bool(view and view.test_pending)}, headers={"Cache-Control": "no-store"}
    )


@router.get("/o/{org_slug}/{space_slug}/-/tidy-settings", response_class=HTMLResponse)
def space_tidy_settings_page(page: WebPage, org_slug: str, space_slug: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    return render_space_settings(
        page,
        actor,
        space,
        "tidy",
        "space-tidy-settings.html",
        ai_configured=ai_settings.config_for(page.db, page.box, space) is not None,
        timezones=TIMEZONES,
    )


@router.get("/o/{org_slug}/{space_slug}/-/semantic", response_class=HTMLResponse)
def space_semantic_page(page: WebPage, org_slug: str, space_slug: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    return render_space_settings(
        page,
        actor,
        space,
        "semantic",
        "space-semantic.html",
        embeddings_ready=semantic.org_config(page.db, page.box, space.org_id) is not None,
        semantic_stats=semantic.stats(page.db, space.id),
    )


@router.post("/o/{org_slug}/{space_slug}/-/tidy-settings")
def space_tidy_settings(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    tidy_time: Annotated[str, Form()],
    tidy_timezone: Annotated[str, Form()],
    tidy_enabled: Annotated[bool, Form()] = False,
    ai_model: Annotated[str, Form()] = "",
    tidy_soon: Annotated[bool, Form()] = False,
    tidy_delay: Annotated[str, Form()] = "15",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        minutes = int(tidy_delay.strip() or "15") if tidy_soon else 0
        if tidy_soon and minutes < 1:
            raise ValueError
    except ValueError:
        return failure(
            space_settings_url(space, "tidy-settings"),
            page,
            invalid("Invalid delay (1 to 1,440 minutes, 0 to turn off)."),
        )
    try:
        tidy.update_settings(
            page.db,
            actor,
            space,
            enabled=tidy_enabled,
            at=tidy_time,
            timezone=tidy_timezone,
            model=ai_model,
            delay=minutes,
        )
    except SynaptaneError as error:
        return failure(space_settings_url(space, "tidy-settings"), page, error)
    return redirect(space_settings_url(space, "tidy-settings"), page, "Tidy-up settings saved.")


@router.post("/o/{org_slug}/{space_slug}/-/tidy")
def space_tidy_now(page: WebPage, org_slug: str, space_slug: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    target = f"{space_url(space)}/tidy"
    try:
        tidy.request_run(page.db, actor, space)
    except SynaptaneError as error:
        return failure(target, page, error)
    return redirect(target, page, "Tidy-up requested: it starts as soon as the worker is free.")


def run_now_context(
    page: WebPage,
    space: Space,
    config: AIConfig | None,
    spending: budget.Spending,
    streak: tidy_journal.Streak | None,
) -> dict[str, Any]:
    """What the "Tidy now" button needs (journal and inbox): `caution`, the button is subdued
    when the run is very likely to fail (better test the connection first); `paid`, it asks
    for confirmation if it costs money."""
    if streak is not None and streak.count >= tidy_journal.FAILURES_BEFORE_WARNING:
        caution = "failures"
    elif config is not None and tidy_journal.last_test_failed(page.db, space.org_id):
        caution = "test"
    else:
        caution = ""
    paid = config is not None and (
        config.provider in PAID_PROVIDERS
        or (spending.price is not None and not spending.subscription)
    )
    return {
        "caution": caution,
        "paid": paid,
        "average_cost": tidy_journal.average_cost(page.db, space.id) if paid else None,
    }


@router.get("/o/{org_slug}/{space_slug}/tidy", response_class=HTMLResponse)
def space_tidy_journal(page: WebPage, org_slug: str, space_slug: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    config = ai_settings.config_for(page.db, page.box, space)
    spending = budget.spending(page.db, space.org_id, space.ai_model or "")
    manage = actor.access.is_org_admin(space.org_id)
    streak = tidy_journal.streak(page.db, space.id, config)
    runs = tidy.runs(page.db, actor, space, limit=JOURNAL_SIZE)
    return page.render(
        "tidy.html",
        org=space.organization,
        space=space,
        entries=tidy_journal.entries(runs, tidy_journal.zone_of(space), config),
        streak=streak,
        activity=tidy_journal.activity(page.db, space.id),
        version=tidy_journal.version(page.db, space.id),
        **run_now_context(page, space, config, spending, streak),
        admins=tidy_journal.org_admins(page.db, space.org_id) if streak and not manage else [],
        contribute=page.can_contribute(actor, space),
        manage=manage,
        configured=config is not None,
        spending=spending,
    )


@router.get("/o/{org_slug}/{space_slug}/-/tidy/state")
def space_tidy_state(page: WebPage, org_slug: str, space_slug: str) -> JSONResponse:
    """Journal state, polled every 3 s by the page while a run is requested or in progress:
    `version` changes when the page must reload, `poll` says whether to keep polling."""
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)  # read access
    current = tidy_journal.activity(page.db, space.id)
    return JSONResponse(
        {
            "poll": bool(current and current.poll),
            "version": tidy_journal.version(page.db, space.id),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/o/{org_slug}/{space_slug}/-/tidy/{run_id}/revert")
def space_tidy_revert(
    page: WebPage, org_slug: str, space_slug: str, run_id: UUID, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    target = f"{space_url(space)}/tidy"
    try:
        tidy.revert(page.db, page.database, actor, page.workspace, space, run_id)
    except SynaptaneError as error:
        return failure(target, page, error)
    return redirect(target, page, "Run undone: filed items are back in the inbox.")


# --- Semantic search ------------------------------------------------------------------------------


@router.post("/o/{org_slug}/-/embeddings")
def org_embeddings_update(
    page: WebPage,
    org_slug: str,
    csrf: Csrf,
    provider: Annotated[str, Form()],
    model: Annotated[str, Form()] = "",
    base_url: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
    clear_key: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        semantic.configure(
            page.db,
            actor,
            page.box,
            org,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key="" if clear_key else (api_key or None),
        )
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/embeddings", page, error)
    return redirect(f"{org_url(org)}/-/embeddings", page, "Embedding provider saved.")


@router.post("/o/{org_slug}/-/embeddings/test")
async def org_embeddings_test(page: WebPage, org_slug: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    semantic.view(page.db, actor, page.box, org)  # access check
    config = semantic.org_config(page.db, page.box, org.id)
    if config is None:
        return redirect(
            f"{org_url(org)}/-/embeddings",
            page,
            "Save a provider first.",
            kind="error",
        )
    try:
        message = await run_in_threadpool(check_embeddings, config)
    except AIError as error:
        return redirect(f"{org_url(org)}/-/embeddings", page, error.label, kind="error")
    return redirect(f"{org_url(org)}/-/embeddings", page, message)


@router.post("/o/{org_slug}/{space_slug}/-/semantic")
def space_semantic(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    semantic.set_enabled(page.db, actor, space, enabled)
    message = (
        "Semantic search enabled: the index is being built."
        if enabled
        else "Semantic search disabled."
    )
    return redirect(space_settings_url(space, "semantic"), page, message)
