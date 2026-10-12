"""An organization's pages: settings, members, AI, embeddings, webhooks."""

from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, Response

from synaptane.ai import cli_agents, pricing
from synaptane.ai.base import DEFAULT_MODELS, EFFORTS
from synaptane.ai.embeddings import DEFAULT_EMBEDDING_MODELS, EMBEDDING_PROVIDERS
from synaptane.core.access import Action, OrgRole, can
from synaptane.core.errors import SynaptaneError, not_found
from synaptane.services import (
    Actor,
    accounts,
    ai_settings,
    budget,
    git_sync,
    invitations,
    jobs,
    mail,
    organizations,
    semantic,
    spaces,
    vocation,
    webhooks,
)
from synaptane.storage.models import Organization
from synaptane.web.pages import (
    Csrf,
    Page,
    WebPage,
    _confirmed,
    failure,
    org_url,
    redirect,
    space_settings_url,
)

router = APIRouter()


def _org_page(
    page: Page,
    actor: Actor,
    org: Organization,
    section: str,
    template: str,
    status_code: int = 200,
    **extra: object,
) -> HTMLResponse:
    manage = can(actor.access, Action.MANAGE_ORG, org_id=org.id)
    if section != "spaces" and not manage:
        raise not_found("Page not found.")
    return page.render(
        template,
        status_code=status_code,
        org=org,
        section=section,
        manage=manage,
        **extra,
    )


@router.get("/o/{org_slug}", response_class=HTMLResponse)
def organization_page(page: WebPage, org_slug: str) -> Response:
    actor = page.user_actor()
    org = organizations.get_organization(page.db, actor, org_slug)
    return _org_page(
        page,
        actor,
        org,
        "spaces",
        "org-spaces.html",
        org_spaces=spaces.list_spaces(page.db, actor, org),
    )


@router.get("/o/{org_slug}/-/new-space", response_class=HTMLResponse)
def organization_new_space(page: WebPage, org_slug: str) -> Response:
    actor = page.user_actor()
    org = organizations.get_organization(page.db, actor, org_slug)
    return _org_page(
        page,
        actor,
        org,
        "spaces",
        "org-new-space.html",
        templates=list(vocation.TEMPLATES.values()),
    )


@router.get("/o/{org_slug}/-/members", response_class=HTMLResponse)
def organization_members_page(page: WebPage, org_slug: str) -> Response:
    actor = page.user_actor()
    org = organizations.get_organization(page.db, actor, org_slug)
    manage = can(actor.access, Action.MANAGE_ORG, org_id=org.id)
    return _org_page(
        page,
        actor,
        org,
        "members",
        "org-members.html",
        members=organizations.list_members(page.db, actor, org) if manage else [],
        people=accounts.directory(page.db, actor) if manage else [],
        invitations=invitations.pending(page.db, actor, org) if manage else [],
        org_spaces=spaces.list_spaces(page.db, actor, org),
        mail_ready=mail.configured(page.db),
    )


@router.get("/o/{org_slug}/-/ai", response_class=HTMLResponse)
def organization_ai_page(page: WebPage, org_slug: str) -> Response:
    actor = page.user_actor()
    org = organizations.get_organization(page.db, actor, org_slug)
    manage = can(actor.access, Action.MANAGE_ORG, org_id=org.id)
    view = ai_settings.view(page.db, actor, page.box, org) if manage else None
    return _org_page(
        page,
        actor,
        org,
        "ai",
        "org-ai.html",
        ai=view,
        efforts=EFFORTS,
        spending=budget.spending(page.db, org.id) if view else None,
        suggested_budget=budget.SUGGESTED_BUDGET,
        public_price=pricing.public_price(view.model if view else DEFAULT_MODELS["anthropic"]),
        docker=cli_agents.in_docker(),
        cli_login={p: cli_agents.login_command(p) for p in cli_agents.LOGIN},
        # Old sign-in in the data volume (before the worker's volume): only the folders'
        # existence is tested, Synaptane reads nothing there and does not move them.
        legacy_logins=[str(p) for p in cli_agents.legacy_logins(page.settings.data_dir)],
        legacy_cleanup=cli_agents.legacy_cleanup(page.settings.data_dir),
    )


@router.get("/o/{org_slug}/-/embeddings", response_class=HTMLResponse)
def organization_embeddings_page(page: WebPage, org_slug: str) -> Response:
    actor = page.user_actor()
    org = organizations.get_organization(page.db, actor, org_slug)
    manage = can(actor.access, Action.MANAGE_ORG, org_id=org.id)
    return _org_page(
        page,
        actor,
        org,
        "semantic",
        "org-embeddings.html",
        embeddings=semantic.view(page.db, actor, page.box, org) if manage else None,
        embedding_providers=EMBEDDING_PROVIDERS,
        default_models=DEFAULT_EMBEDDING_MODELS,
    )


@router.get("/o/{org_slug}/-/webhooks", response_class=HTMLResponse)
def organization_webhooks_page(page: WebPage, org_slug: str) -> Response:
    actor = page.user_actor()
    org = organizations.get_organization(page.db, actor, org_slug)
    manage = can(actor.access, Action.MANAGE_ORG, org_id=org.id)
    return _org_page(
        page,
        actor,
        org,
        "webhooks",
        "org-webhooks.html",
        webhooks=webhooks.list_webhooks(page.db, actor, org) if manage else [],
        webhook_events=webhooks.EVENTS,
    )


@router.get("/o/{org_slug}/-/settings", response_class=HTMLResponse)
def organization_settings_page(page: WebPage, org_slug: str) -> Response:
    actor = page.user_actor()
    org = organizations.get_organization(page.db, actor, org_slug)
    return _org_page(
        page,
        actor,
        org,
        "settings",
        "org-settings.html",
        org_spaces=spaces.list_spaces(page.db, actor, org),
    )


@router.post("/o/{org_slug}/-/update")
def organization_update(
    page: WebPage,
    org_slug: str,
    csrf: Csrf,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        organizations.update_organization(page.db, actor, org, name=name, description=description)
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/settings", page, error)
    return redirect(f"{org_url(org)}/-/settings", page, "Organization updated.")


@router.post("/o/{org_slug}/-/delete")
def organization_delete(
    page: WebPage, org_slug: str, csrf: Csrf, confirm: Annotated[str, Form()] = ""
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        _confirmed(confirm, org.slug)
        organizations.delete_organization(page.db, actor, org)
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/settings", page, error)
    return redirect("/admin", page, "Organization deleted.")


@router.post("/o/{org_slug}/-/spaces")
def space_create(
    page: WebPage,
    org_slug: str,
    csrf: Csrf,
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    repo_url: Annotated[str, Form()] = "",
    branch: Annotated[str, Form()] = "main",
    vocation_template: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        chosen = vocation.template(vocation_template) if vocation_template else None
        space = spaces.create_space(
            page.db,
            actor,
            org,
            slug=slug,
            name=name,
            description=description,
            repo_url=repo_url,
            branch=branch,
        )
        if chosen is not None:
            vocation.start_with(page.db, actor, page.workspace, space, chosen)
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/new-space", page, error)
    if space.repo_url:
        jobs.enqueue(page.db, git_sync.SYNC, space.id)
    return redirect(
        space_settings_url(space, "git"),
        page,
        "Space created. Next step: give Synaptane access to the Git repository.",
    )


@router.post("/o/{org_slug}/-/members")
def organization_member_set(
    page: WebPage,
    org_slug: str,
    csrf: Csrf,
    username: Annotated[str, Form()],
    role: Annotated[OrgRole, Form()],
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        user = accounts.get_user(page.db, username)
        organizations.set_member(page.db, actor, org, user, role)
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/members", page, error)
    return redirect(f"{org_url(org)}/-/members", page, "Member saved.")


@router.post("/o/{org_slug}/-/members/{username}/remove")
def organization_member_remove(page: WebPage, org_slug: str, username: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        organizations.remove_member(page.db, actor, org, accounts.get_user(page.db, username))
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/members", page, error)
    return redirect(f"{org_url(org)}/-/members", page, "Member removed.")
