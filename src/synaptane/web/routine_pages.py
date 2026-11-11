"""Agent routines in a space's settings: list, adding from a template or blank, editing,
enabling, running, deleting, the run journal, and the routine's memory (shown to whoever sees
the routine, cleared by the admins).

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
from synaptane.core.errors import ErrorCode, SynaptaneError, forbidden, invalid
from synaptane.services import (
    Actor,
    ai_settings,
    budget,
    routine_files,
    routine_memory,
    routine_writes,
    routines,
    tidy_journal,
)
from synaptane.storage.models import Routine, RoutineRun, Space
from synaptane.web.pages import (
    ERROR_STATUS,
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

    title: str = ""
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
    name: str = ""  # file name; empty on creation: made from the title
    description: str = ""
    base_sha: str = ""  # hash of the file when the form was opened
    writes: str = ""  # folders the routine keeps, one per line
    memory: bool = False  # in the file only (`routine_files.memory_enabled`)

    @classmethod
    def of(cls, routine: Routine, writes: list[str] | None = None, memory: bool = False) -> "Draft":
        """`writes` and `memory`: read from its file (the database keeps no copy of them)."""
        return cls(
            routine.title,
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
            routine.name,
            routine.description,
            routine.sha or "",
            "\n".join(writes or []),
            memory,
        )

    @classmethod
    def from_template(cls, key: str, locale: str) -> "Draft":
        """A template's settings, its title, instructions and report page in the user's
        language."""
        if key not in routines.TEMPLATES:
            return cls()
        config = routines.from_template(key, locale)
        return cls(
            config.title,
            config.instructions,
            config.frequency,
            config.weekday,
            config.monthday or 1,
            config.at,
            "",
            config.output_page,
            config.output_inbox,
            config.output_email,
            config.output_webhook,
            True,
            key,
            writes="\n".join(config.writes or []),
            memory=bool(config.memory),
        )

    def fields(self) -> routines.Fields:
        return routines.Fields(
            title=self.title,
            description=self.description,
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
            writes=[line.strip() for line in self.writes.splitlines() if line.strip()],
            memory=self.memory,
        )


def _day(value: str) -> int | None:
    try:
        return int(value) if value.strip() else None
    except ValueError:
        raise invalid("Invalid day.") from None


def posted(
    title: Annotated[str, Form()] = "",
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
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    base_sha: Annotated[str, Form()] = "",
    writes: Annotated[str, Form()] = "",
    memory: Annotated[bool, Form()] = False,
) -> Draft:
    """A routine's form (FastAPI dependency)."""
    return Draft(
        title,
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
        name,
        description,
        base_sha,
        writes,
        memory,
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
        "routine_error": routine_files.error_of,
        "routine_warnings": routine_files.warnings_of,
        "routine_path": routines.path_of,
        "max_written": routine_writes.MAX_PAGES,
        "max_deletions": routine_writes.MAX_DELETIONS,
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
    changed = error is not None and error.code == ErrorCode.CONFLICT and routine is not None
    return render_space_settings(
        page,
        actor,
        space,
        "routines",
        "space-routine-form.html",
        status_code=ERROR_STATUS[error.code] if error else 200,
        routine=routine,
        draft=draft,
        error=error,
        # The file changed since the form was opened: offer to replace its current version.
        conflict_sha=routine_files.current_sha(page.workspace, space, routine.name)
        if changed and routine is not None
        else None,
        **_common(page, space),
    )


@router.get(BASE + "/new", response_class=HTMLResponse)
def routine_new_page(page: WebPage, org_slug: str, space_slug: str, template: str = "") -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    return _form(page, actor, space, None, Draft.from_template(template, page.locale))


Posted = Annotated[Draft, Depends(posted)]


Confirmed = Annotated[bool, Form()]


@router.post(BASE)
def routine_create(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    draft: Posted,
    allow_secrets: Confirmed = False,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        routine = routines.create(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            draft.fields(),
            name=draft.name,
            template=draft.template,
            allow_secrets=allow_secrets,
        )
    except SynaptaneError as error:
        page.db.rollback()
        return _form(page, actor, space, None, draft, error)
    return redirect(_url(space, routine), page, _saved(routine))


def _saved(routine: Routine) -> str:
    if routine.sha is None:
        return "Routine saved. It will be written to the repository at the first synchronization."
    return "Routine saved."


@router.post(BASE + "/from/{template}")
def routine_from_template(
    page: WebPage, org_slug: str, space_slug: str, template: str, csrf: Csrf
) -> Response:
    """One-click activation of a bundled template, with its default settings."""
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        routine = routines.create(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            routines.Fields(),
            template=template,
        )
    except SynaptaneError as error:
        page.db.rollback()
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
        writes=routine_files.writes_of(page.workspace, space, routine),
        runs=_views(page, space, runs),
        version=_version(routine, runs),
        poll=routine.requested is not None or bool(runs and runs[0].status == "running"),
        memory=routine_memory.view(page.db, actor, page.workspace, space, routine),
        **_common(page, space),
    )


@router.get(BASE + "/{routine_id}/edit", response_class=HTMLResponse)
def routine_edit_page(page: WebPage, org_slug: str, space_slug: str, routine_id: UUID) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    writes = routine_files.writes_of(page.workspace, space, routine)
    memory = routine_files.memory_enabled(page.workspace, space, routine.name)
    return _form(page, actor, space, routine, Draft.of(routine, writes, memory))


@router.post(BASE + "/{routine_id}")
def routine_update(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    routine_id: UUID,
    csrf: Csrf,
    draft: Posted,
    overwrite_sha: Annotated[str, Form()] = "",
    allow_secrets: Confirmed = False,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    try:
        routine = routines.update(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            routine,
            draft.fields(),
            base_sha=overwrite_sha or draft.base_sha,
            allow_secrets=allow_secrets,
        )
    except SynaptaneError as error:
        page.db.rollback()
        routine = routines.get(page.db, actor, space, routine_id)
        draft.template, draft.name = routine.template, routine.name
        return _form(page, actor, space, routine, draft, error)
    return redirect(_url(space, routine), page, _saved(routine))


@router.post(BASE + "/{routine_id}/toggle")
def routine_toggle(
    page: WebPage, org_slug: str, space_slug: str, routine_id: UUID, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    enable = not routine.enabled
    try:
        if routine.error is not None:  # rewriting would replace the file with old settings
            raise invalid("The routine's file is invalid: fix it before changing the routine.")
        routines.update(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            routine,
            routines.Fields(enabled=enable),
            base_sha=routine.sha,
        )
    except SynaptaneError as error:
        page.db.rollback()
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
    target = _url(space, routine)
    try:
        routines.delete(page.db, page.database, actor, page.workspace, space, routine)
    except SynaptaneError as error:
        page.db.rollback()
        return failure(target, page, error)
    return redirect(_url(space), page, "Routine deleted.")


@router.post(BASE + "/{routine_id}/memory/clear")
def routine_memory_clear(
    page: WebPage, org_slug: str, space_slug: str, routine_id: UUID, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    routine = routines.get(page.db, actor, space, routine_id)
    target = _url(space, routine)
    try:
        cleared = routine_memory.clear(
            page.db, page.database, actor, page.workspace, space, routine
        )
    except SynaptaneError as error:
        page.db.rollback()
        return failure(target, page, error)
    return redirect(target, page, "Memory cleared." if cleared else "The memory was already empty.")


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
