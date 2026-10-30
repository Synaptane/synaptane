"""A space's settings: its purpose and its tidy-up rules (two files of the repository).

Reserved for the organization's admins. A template pre-fills the purpose without saving anything
(with JS, in place after confirmation; without JS, through `?template=…`); "Preview" renders the
typed text, without saving it.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, Response

from synaptane.core.access import Action, can
from synaptane.core.errors import ErrorCode, SynaptaneError, invalid, not_found
from synaptane.services import Actor, team_prompts, vocation
from synaptane.storage.models import Space
from synaptane.web.pages import (
    Csrf,
    Page,
    WebPage,
    failure,
    find_space,
    redirect,
    render_space_settings,
    space_settings_url,
)
from synaptane.web.render import render_markdown
from synaptane.web.wiki import page_url, raw_url

router = APIRouter()

SAVED = {
    "vocation": "Purpose saved.",
    "tidy-rules": "Filing instructions saved.",
}
PENDING = (
    "Text put on hold: it will be written to the repository at the space's first synchronization."
)


def _render(
    page: Page,
    actor: Actor,
    space: Space,
    *,
    drafts: dict[str, str] | None = None,
    chosen: str = "",
    error: SynaptaneError | None = None,
    error_kind: str = "",
    preview: tuple[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    if not can(actor.access, Action.MANAGE_ORG, org_id=space.org_id):
        raise not_found("Page not found.")
    documents = {
        key: vocation.document(actor, page.workspace, space, kind)
        for key, kind in vocation.KINDS.items()
    }
    current = documents[error_kind].sha if error_kind and error_kind in documents else None
    return render_space_settings(
        page,
        actor,
        space,
        "vocation",
        "space-vocation.html",
        status_code=status_code,
        documents=documents,
        history={
            key: [
                (datetime.fromisoformat(c.date), c.author.name, c.subject)
                for c in doc.history
                if c.date
            ]
            for key, doc in documents.items()
        },
        drafts=drafts or {},
        chosen=chosen,
        templates=list(vocation.TEMPLATES.values()),
        template_texts={key: t.text_for(page.locale) for key, t in vocation.TEMPLATES.items()},
        error=error,
        error_kind=error_kind,
        conflict_sha=current if error is not None and error.code == ErrorCode.CONFLICT else None,
        preview_kind=preview[0] if preview else "",
        preview_html=preview[1] if preview else "",
    )


@router.get("/o/{org_slug}/{space_slug}/-/vocation", response_class=HTMLResponse)
def space_vocation_page(
    page: WebPage, org_slug: str, space_slug: str, template: str = ""
) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    drafts: dict[str, str] = {}
    if template:
        try:
            drafts["vocation"] = vocation.template(template).text_for(page.locale)
        except SynaptaneError as error:
            return failure(space_settings_url(space, "vocation"), page, error)
    return _render(page, actor, space, drafts=drafts, chosen=template)


def _save(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    key: str,
    csrf: str,
    text: str,
    base_sha: str,
    action: str,
    overwrite_sha: str,
    allow_secrets: bool,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    kind = vocation.kind_of(key)
    if action == "preview":
        html = render_markdown(
            vocation.for_prompt(text) if key == "vocation" else text,
            "README.md",
            lambda path: page_url(space, path),
            lambda path: raw_url(space, path),
        )
        return _render(page, actor, space, drafts={key: text}, preview=(key, html))
    try:
        sha = vocation.save(
            page.db,
            page.database,
            actor,
            page.workspace,
            space,
            kind,
            text,
            base_sha=overwrite_sha or base_sha,
            allow_secrets=allow_secrets,
        )
    except SynaptaneError as error:
        page.db.rollback()
        status = (
            403
            if error.code == ErrorCode.FORBIDDEN
            else 409
            if error.code == ErrorCode.CONFLICT
            else 400
        )
        return _render(
            page,
            actor,
            space,
            drafts={key: text},
            error=error,
            error_kind=key,
            status_code=status,
        )
    target = space_settings_url(space, "vocation") + ("" if key == "vocation" else f"#{key}")
    document = vocation.document(actor, page.workspace, space, kind)
    if not document.synced:
        return redirect(target, page, PENDING)
    return redirect(target, page, SAVED[key] if sha else "Nothing changed.")


@router.post("/o/{org_slug}/{space_slug}/-/vocation", response_class=HTMLResponse)
def space_vocation_save(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    text: Annotated[str, Form()] = "",
    base_sha: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
    overwrite_sha: Annotated[str, Form()] = "",
    allow_secrets: Annotated[bool, Form()] = False,
) -> Response:
    return _save(
        page,
        org_slug,
        space_slug,
        "vocation",
        csrf,
        text,
        base_sha,
        action,
        overwrite_sha,
        allow_secrets,
    )


@router.post("/o/{org_slug}/{space_slug}/-/vocation/tidy-rules", response_class=HTMLResponse)
def space_rules_save(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    text: Annotated[str, Form()] = "",
    base_sha: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
    overwrite_sha: Annotated[str, Form()] = "",
    allow_secrets: Annotated[bool, Form()] = False,
) -> Response:
    return _save(
        page,
        org_slug,
        space_slug,
        "tidy-rules",
        csrf,
        text,
        base_sha,
        action,
        overwrite_sha,
        allow_secrets,
    )


# --- Team prompts (`services.team_prompts`) ---------------------------------------------------


def _prompts(
    page: Page,
    actor: Actor,
    space: Space,
    *,
    drafts: dict[str, str] | None = None,
    new_name: str = "",
    error: SynaptaneError | None = None,
    error_for: str = "",
    status_code: int = 200,
) -> Response:
    if not can(actor.access, Action.MANAGE_ORG, org_id=space.org_id):
        raise not_found("Page not found.")
    found = team_prompts.of_space(page.workspace, space)
    documents = {
        p.name: vocation.document(actor, page.workspace, space, team_prompts.kind(p.name))
        for p in found
    }
    return render_space_settings(
        page,
        actor,
        space,
        "prompts",
        "space-prompts.html",
        status_code=status_code,
        prompts=found,
        documents=documents,
        drafts=drafts or {},
        new_name=new_name,
        example=team_prompts.EXAMPLES.get(page.locale, team_prompts.EXAMPLES["en"]),
        error=error,
        error_for=error_for,
        limit=team_prompts.MAX_PROMPTS,
    )


@router.get("/o/{org_slug}/{space_slug}/-/prompts", response_class=HTMLResponse)
def space_prompts_page(page: WebPage, org_slug: str, space_slug: str) -> Response:
    actor = page.user_actor()
    return _prompts(page, actor, find_space(page, actor, org_slug, space_slug))


def _save_prompt(
    page: WebPage, space: Space, actor: Actor, name: str, text: str, base_sha: str
) -> Response:
    kind = team_prompts.kind(name)
    sha = vocation.save(
        page.db, page.database, actor, page.workspace, space, kind, text, base_sha=base_sha
    )
    target = space_settings_url(space, "prompts")
    if not vocation.document(actor, page.workspace, space, kind).synced:
        return redirect(target, page, PENDING)
    if not text.strip():
        return redirect(target, page, "Prompt removed.")
    return redirect(f"{target}#prompt-{name}", page, "Prompt saved." if sha else "Nothing changed.")


@router.post("/o/{org_slug}/{space_slug}/-/prompts", response_class=HTMLResponse)
def space_prompt_create(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    name: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        clean = team_prompts.check_name(name)
        existing = {p.name for p in team_prompts.of_space(page.workspace, space)}
        if clean in existing:
            raise invalid("A prompt named {name} already exists: edit it below.", name=clean)
        if len(existing) >= team_prompts.MAX_PROMPTS:
            raise invalid("{limit} prompts at most per space.", limit=team_prompts.MAX_PROMPTS)
        if not text.strip():
            raise invalid("Write the prompt's text.")
        return _save_prompt(page, space, actor, clean, text, "")
    except SynaptaneError as error:
        page.db.rollback()
        return _prompts(
            page,
            actor,
            space,
            drafts={"": text},
            new_name=name,
            error=error,
            error_for="",
            status_code=400,
        )


@router.post("/o/{org_slug}/{space_slug}/-/prompts/{name}", response_class=HTMLResponse)
def space_prompt_save(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    name: str,
    csrf: Csrf,
    text: Annotated[str, Form()] = "",
    base_sha: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        return _save_prompt(page, space, actor, name, text, base_sha)
    except SynaptaneError as error:
        page.db.rollback()
        status = 409 if error.code == ErrorCode.CONFLICT else 400
        return _prompts(
            page,
            actor,
            space,
            drafts={name: text},
            error=error,
            error_for=name,
            status_code=status,
        )
