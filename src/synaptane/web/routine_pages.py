"""Agent routines in a space's settings: list, adding from a template or blank, editing,
enabling, running, deleting, and the run journal.

The organization's admins manage them; contributors see the routines and their journal (the
cause of a failure, translated by `tidy_journal.explain`, without the technical detail). After
"Run now", the page waits for the result (`…/state`, polled every 3 s by `preferences.js`, like
the tidy-up journal)."""

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response

from synaptane.core.access import Action, can
from synaptane.core.errors import SynaptaneError, forbidden, invalid
from synaptane.services import Actor, ai_settings, budget, routines, tidy_journal
from synaptane.storage.models import Routine, RoutineRun, Space
from synaptane.web.pages import (
    Csrf,
    WebPage,
    failure,
    find_space,
    redirect,
    render_space_settings,
    space_settings_url,
)

router = APIRouter()
BASE = "/o/{org_slug}/{space_slug}/-/routines"
JOURNAL_SIZE = 30
# Monday = 0, like `date.weekday()`.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class RunView:
    """A run as the journal shows it."""

    run: RoutineRun
    explanation: tidy_journal.Explanation | None
    tokens: int


@dataclass
class Draft:
    """Form values: from a routine, a template, or typed in (shown again with the error,
    without losing anything that was typed)."""

    name: str = ""
    instructions: str = ""
    frequency: str = "weekly"
    weekday: int | None = 0
    monthday: int | None = 1
    at: str = "06:00"
    scope: str = ""  # one folder per line
    output_page: str = "rapports/"
    output_inbox: bool = False
    output_email: str = ""
    output_webhook: bool = False
    enabled: bool = True
    template: str = ""

    @classmethod
    def of(cls, routine: Routine) -> "Draft":
        return cls(
            routine.name,
            routine.instructions,
            routine.frequency,
            routine.weekday,
            routine.monthday or 1,
            routine.at,
            "\n".join(routine.scope or []),
            routine.output_page,
            routine.output_inbox,
            routine.output_email,
            routine.output_webhook,
            routine.enabled,
            routine.template,
        )

    @classmethod
    def from_template(cls, key: str, locale: str) -> "Draft":
        """A template's settings, its instructions and report page in the user's language."""
        model = routines.TEMPLATES.get(key)
        if model is None:
            return cls()
        text = model.text_for(locale)
        return cls(
            model.name,
            text.instructions,
            model.frequency,
            model.weekday,
            model.monthday or 1,
            model.at,
            "",
            text.output_page,
            model.output_inbox,
            model.output_email,
            model.output_webhook,
            True,
            model.key,
        )

    def fields(self) -> routines.Fields:
        return routines.Fields(
            name=self.name,
            instructions=self.instructions,
            frequency=self.frequency,
            weekday=self.weekday if self.frequency == "weekly" else None,
            monthday=self.monthday if self.frequency == "monthly" else None,
            at=self.at,
            scope=[line.strip() for line in self.scope.splitlines() if line.strip()],
            output_page=self.output_page,
            output_inbox=self.output_inbox,
            output_email=self.output_email,
            output_webhook=self.output_webhook,
            enabled=self.enabled,
        )


def _day(value: str) -> int | None:
    try:
        return int(value) if value.strip() else None
    except ValueError:
        raise invalid("Invalid day.") from None


def posted(
    name: Annotated[str, Form()] = "",
    instructions: Annotated[str, Form()] = "",
    frequency: Annotated[str, Form()] = "weekly",
    weekday: Annotated[str, Form()] = "",
    monthday: Annotated[str, Form()] = "",
    at: Annotated[str, Form()] = "06:00",
    scope: Annotated[str, Form()] = "",
    output_page: Annotated[str, Form()] = "",
    output_inbox: Annotated[bool, Form()] = False,
    output_email: Annotated[str, Form()] = "",
    output_webhook: Annotated[bool, Form()] = False,
    enabled: Annotated[bool, Form()] = False,
    template: Annotated[str, Form()] = "",
) -> Draft:
    """A routine's form (FastAPI dependency)."""
    return Draft(
        name,
        instructions,
        frequency,
        _day(weekday) if weekday.strip().isdigit() else None,
        _day(monthday) if monthday.strip().isdigit() else None,
        at,
        scope,
        output_page,
        output_inbox,
        output_email,
        output_webhook,
        enabled,
        template if template in routines.TEMPLATES else "",
    )


def _views(page: WebPage, space: Space, runs: list[RoutineRun]) -> list[RunView]:
    config = ai_settings.config_for(page.db, page.box, space)
    return [
        RunView(
            run,
            tidy_journal.explain(run.error, config) if run.status == "failed" else None,
            run.input_tokens + run.output_tokens + run.cached_tokens,
        )
        for run in runs
    ]


def _url(space: Space, routine: Routine | None = None, suffix: str = "") -> str:
    base = space_settings_url(space, "routines")
    return f"{base}/{routine.id}{suffix}" if routine is not None else base + suffix


def _common(page: WebPage, space: Space) -> dict[str, Any]:
    return {
        "ai_configured": ai_settings.config_for(page.db, page.box, space) is not None,
        "spending": budget.spending(page.db, space.org_id, space.ai_model or ""),
        "weekdays": WEEKDAYS,
        "next_run": routines.next_run,
    }


@router.get(BASE, response_class=HTMLResponse)
def routines_page(page: WebPage, org_slug: str, space_slug: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    found = routines.list_routines(page.db, actor, space)
    return render_space_settings(
        page,
        actor,
        space,
        "routines",
        "space-routines.html",
        routines=found,
        last_runs=routines.last_runs(page.db, space),
        templates=list(routines.TEMPLATES.values()),
        used_templates={r.template for r in found if r.template},
        **_common(page, space),
    )


def _form(
    page: WebPage,
    actor: Actor,
    space: Space,
    routine: Routine | None,
    draft: Draft,
    error: SynaptaneError | None = None,
) -> Response:
    if not can(actor.access, Action.MANAGE_ORG, org_id=space.org_id):
        raise forbidden()
    return render_space_settings(
        page,
        actor,
        space,
        "routines",
        "space-routine-form.html",
        status_code=400 if error else 200,
        routine=routine,
        draft=draft,
        error=error,
        **_common(page, space),
    )


@router.get(BASE + "/new", response_class=HTMLResponse)
def routine_new_page(page: WebPage, org_slug: str, space_slug: str, template: str = "") -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    return _form(page, actor, space, None, Draft.from_template(template, page.locale))


Posted = Annotated[Draft, Depends(posted)]


@router.post(BASE)
def routine_create(
    page: WebPage, org_slug: str, space_slug: str, csrf: Csrf, draft: Posted
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        routine = routines.create(page.db, actor, space, draft.fields(), template=draft.template)
    except SynaptaneError as error:
        page.db.rollback()
        return _form(page, actor, space, None, draft, error)
    return redirect(_url(space, routine), page, "Routine saved.")


@router.post(BASE + "/from/{template}")
def routine_from_template(
    page: WebPage, org_slug: str, space_slug: str, template: str, csrf: Csrf
) -> Response:
    """One-click activation of a bundled template, with its default settings."""
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        routine = routines.create(page.db, actor, space, routines.Fields(), template=template)
    except SynaptaneError as error:
        return failure(_url(space), page, error)
    return redirect(
        _url(space, routine),
        page,
        "Routine enabled. Adjust its instructions if needed, or run it now.",
    )


def _version(routine: Routine, runs: list[RoutineRun]) -> str:
    """Changes when the page must reload: request taken, run finished…"""
    latest = runs[0] if runs else None
    return (
        f"{routine.requested or ''}:{latest.id if latest else ''}:{latest.status if latest else ''}"
    )


@router.get(BASE + "/{routine_id}", response_class=HTMLResponse)
def routine_page(page: WebPage, org_slug: str, space_slug: str, routine_id: UUID) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    runs = routines.runs(page.db, actor, space, routine.id, limit=JOURNAL_SIZE)
    return render_space_settings(
        page,
        actor,
        space,
        "routines",
        "space-routine.html",
        routine=routine,
        runs=_views(page, space, runs),
        version=_version(routine, runs),
        poll=routine.requested is not None or bool(runs and runs[0].status == "running"),
        **_common(page, space),
    )


@router.get(BASE + "/{routine_id}/edit", response_class=HTMLResponse)
def routine_edit_page(page: WebPage, org_slug: str, space_slug: str, routine_id: UUID) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    return _form(page, actor, space, routine, Draft.of(routine))


@router.post(BASE + "/{routine_id}")
def routine_update(
    page: WebPage, org_slug: str, space_slug: str, routine_id: UUID, csrf: Csrf, draft: Posted
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    try:
        routines.update(page.db, actor, space, routine, draft.fields())
    except SynaptaneError as error:
        page.db.rollback()
        routine = routines.get(page.db, actor, space, routine_id)
        draft.template = routine.template
        return _form(page, actor, space, routine, draft, error)
    return redirect(_url(space, routine), page, "Routine saved.")


@router.post(BASE + "/{routine_id}/toggle")
def routine_toggle(
    page: WebPage, org_slug: str, space_slug: str, routine_id: UUID, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    enable = not routine.enabled
    try:
        routines.update(page.db, actor, space, routine, routines.Fields(enabled=enable))
    except SynaptaneError as error:
        return failure(_url(space), page, error)
    return redirect(_url(space), page, "Routine enabled." if enable else "Routine disabled.")


@router.post(BASE + "/{routine_id}/run")
def routine_run(
    page: WebPage, org_slug: str, space_slug: str, routine_id: UUID, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    target = _url(space, routine)
    try:
        routines.request_run(page.db, actor, space, routine)
    except SynaptaneError as error:
        return failure(target, page, error)
    return redirect(target, page, "Routine requested: it starts as soon as the worker is free.")


@router.post(BASE + "/{routine_id}/delete")
def routine_delete(
    page: WebPage, org_slug: str, space_slug: str, routine_id: UUID, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    try:
        routines.delete(page.db, actor, space, routine)
    except SynaptaneError as error:
        return failure(_url(space, routine), page, error)
    return redirect(_url(space), page, "Routine deleted.")


@router.get(BASE + "/{routine_id}/state")
def routine_state(page: WebPage, org_slug: str, space_slug: str, routine_id: UUID) -> JSONResponse:
    """Polled every 3 s while a run is requested or in progress."""
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    runs = routines.runs(page.db, actor, space, routine.id, limit=1)
    running = bool(runs and runs[0].status == "running")
    return JSONResponse(
        {"poll": routine.requested is not None or running, "version": _version(routine, runs)},
        headers={"Cache-Control": "no-store"},
    )
