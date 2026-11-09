"""An organization's pages: settings, members, AI, embeddings, webhooks."""

from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, Response

from synaptane.ai import cli_agents, pricing
from synaptane.ai.base import DEFAULT_MODELS, EFFORTS
from synaptane.ai.embeddings import DEFAULT_EMBEDDING_MODELS, EMBEDDING_PROVIDERS
from synaptane.core.access import Action, OrgRole, can
from synaptane.core.errors import Label, SynaptaneError, invalid, not_found
from synaptane.services import (
    Actor,
    accounts,
    ai_settings,
    budget,
    forges,
    git_sync,
    groups,
    invitations,
    jobs,
    mail,
    organizations,
    semantic,
    spaces,
    vocation,
    webhooks,
)
from synaptane.storage.models import Organization, Space
from synaptane.web.i18n import format_day
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
    members = organizations.list_members(page.db, actor, org) if manage else []
    joined = groups.memberships(page.db, actor, org) if manage else {}
    pending = invitations.pending(page.db, actor, org) if manage else []
    notes: dict[str, Label] = {}
    removal: dict[str, Label] = {}
    for user, _ in members:
        names = ", ".join(joined.get(user.id, []))
        if len(joined.get(user.id, [])) == 1:
            notes[user.username] = Label("Group: {names}", names=names)
            removal[user.username] = Label("They also leave the group {names}.", names=names)
        elif names:
            notes[user.username] = Label("Groups: {names}", names=names)
            removal[user.username] = Label("They also leave the groups {names}.", names=names)
    return _org_page(
        page,
        actor,
        org,
        "members",
        "org-members.html",
        members=members,
        member_notes=notes,
        remove_notes=removal,
        people=accounts.directory(page.db, actor) if manage else [],
        invitations=pending,
        invitation_groups=invitations.invited_groups(page.db, (i.id for i in pending)),
        org_groups=groups.details(page.db, actor, org) if manage else [],
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
        public_price=(
            pricing.tariff(view.provider, view.model)
            if view
            else pricing.tariff("anthropic", DEFAULT_MODELS["anthropic"])
        ),
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


@router.post("/o/{org_slug}/-/default-role")
def organization_default_role(
    page: WebPage,
    org_slug: str,
    csrf: Csrf,
    default_space_role: Annotated[str, Form()],
) -> Response:
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    try:
        organizations.update_organization(
            page.db, actor, org, default_space_role=default_space_role
        )
    except SynaptaneError as error:
        return failure(f"{org_url(org)}/-/settings", page, error)
    return redirect(f"{org_url(org)}/-/settings", page, "Default role saved.")


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
    restricted: Annotated[bool, Form()] = False,
    repo_mode: Annotated[str, Form()] = "existing",
    provider: Annotated[str, Form()] = "github",
    gitlab_url: Annotated[str, Form()] = "",
    repo_owner: Annotated[str, Form()] = "",
    repo_name: Annotated[str, Form()] = "",
    repo_token: Annotated[str, Form()] = "",
) -> Response:
    """`repo_mode`: "existing" (address entered) or "create" (repository created by Synaptane
    on GitHub or GitLab with `repo_token`, which is never saved)."""
    actor = page.check_csrf(csrf)
    org = organizations.get_organization(page.db, actor, org_slug)
    created = None
    started: list[UUID] = []  # spaces whose purpose waits on disk for the first sync
    try:
        chosen = vocation.template(vocation_template) if vocation_template else None

        def start_with(space: Space) -> None:
            if chosen is not None:
                started.append(space.id)  # before: a failure may come after the writing
                vocation.start_with(page.db, actor, page.workspace, space, chosen)

        if repo_mode == "create":
            space, created = forges.create_space_with_repository(
                page.db,
                actor,
                page.box,
                org,
                forges.NewRepository(provider, repo_token, repo_owner, repo_name, gitlab_url),
                slug=slug,
                name=name,
                description=description,
                branch=branch,
                restricted=restricted,
                allow_private=page.settings.allow_private_webhooks,
                public_host=urlsplit(page.settings.public_url).netloc,
                then=start_with,
            )
        else:
            # Required in the form too; checked here for a browser without JavaScript.
            if not repo_url.strip():
                raise invalid(
                    "Enter the address of the Git repository, or choose to have it created."
                )
            space = spaces.create_space(
                page.db,
                actor,
                org,
                slug=slug,
                name=name,
                description=description,
                repo_url=repo_url,
                branch=branch,
                restricted=restricted,
            )
            start_with(space)
    except SynaptaneError as error:
        # The space was not created after all: its pending purpose goes too.
        for space_id in started:
            vocation.pending_file(page.workspace.clone_path(space_id)).unlink(missing_ok=True)
        return failure(f"{org_url(org)}/-/new-space", page, error)
    if space.repo_url:
        jobs.enqueue(page.db, git_sync.SYNC, space.id)
    if created is None:
        return redirect(
            space_settings_url(space, "git"),
            page,
            "Space created. Next step: give Synaptane access to the Git repository.",
        )
    if created.access_error is not None:
        return redirect(
            space_settings_url(space, "git"),
            page,
            "Space created, and its repository {address} created on {provider}, but Synaptane "
            "could not set up its access: {reason} The repository was not deleted: enter its "
            "credentials below.",
            kind="warning",
            address=created.web_url,
            provider=created.provider_name,
            reason=Label(created.access_error.template, **created.access_error.params),
        )
    if created.token_expires is not None:
        return redirect(
            space_settings_url(space, "git"),
            page,
            "Space created, and its private repository {address} created on {provider}: Synaptane "
            "is preparing it. Its access token expires on {date}: renew it before then.",
            address=created.web_url,
            provider=created.provider_name,
            date=format_day(created.token_expires, page.locale),
        )
    return redirect(
        space_settings_url(space, "git"),
        page,
        "Space created, and its private repository {address} created on {provider}: Synaptane is "
        "preparing it.",
        address=created.web_url,
        provider=created.provider_name,
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
