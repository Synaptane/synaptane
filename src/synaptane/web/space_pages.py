"""A space's settings: general, Git, members, comparison."""

from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response

from synaptane.core.access import Action, SpaceRole, can
from synaptane.core.errors import SynaptaneError, forbidden
from synaptane.git.credentials import GitCredentials, scan_host_keys
from synaptane.services import (
    accounts,
    git_sync,
    jobs,
    spaces,
)
from synaptane.web.pages import (
    Csrf,
    WebPage,
    _confirmed,
    _render_git,
    failure,
    find_space,
    org_url,
    redirect,
    render_space_settings,
    reveal,
    space_settings_url,
)

router = APIRouter()


@router.get("/o/{org_slug}/{space_slug}/-/settings", response_class=HTMLResponse)
def space_settings_page(page: WebPage, org_slug: str, space_slug: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    if not can(actor.access, Action.MANAGE_ORG, org_id=space.org_id):
        return _render_git(page, actor, space)  # readers and contributors: the repository state
    return render_space_settings(page, actor, space, "general", "space-general.html")


@router.get("/o/{org_slug}/{space_slug}/-/git", response_class=HTMLResponse)
def space_git_page(page: WebPage, org_slug: str, space_slug: str) -> Response:
    actor = page.user_actor()
    return _render_git(page, actor, find_space(page, actor, org_slug, space_slug))


@router.get("/o/{org_slug}/{space_slug}/-/members", response_class=HTMLResponse)
def space_members_page(page: WebPage, org_slug: str, space_slug: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    manage = can(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    return render_space_settings(
        page,
        actor,
        space,
        "members",
        "space-members.html",
        members=spaces.list_members(page.db, actor, space),
        people=accounts.directory(page.db, actor) if manage else [],
    )


@router.get("/o/{org_slug}/{space_slug}/-/compare", response_class=HTMLResponse)
def space_compare_page(page: WebPage, org_slug: str, space_slug: str, path: str) -> Response:
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)
    patch = git_sync.compare_with_remote(actor, page.workspace, space, path)
    lines = [
        ("add" if line.startswith("+") else "del" if line.startswith("-") else "ctx", line)
        for line in patch.splitlines()
        if not line.startswith(("diff --git", "index ", "--- ", "+++ "))
    ]
    return render_space_settings(
        page, actor, space, "git", "space-compare.html", path=path, diff_lines=lines
    )


@router.post("/o/{org_slug}/{space_slug}/-/sync")
def space_sync(page: WebPage, org_slug: str, space_slug: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        git_sync.request_sync(page.db, actor, space)
    except SynaptaneError as error:
        return failure(space_settings_url(space, "git"), page, error)
    return redirect(space_settings_url(space, "git"), page, "Synchronization requested.")


@router.get("/o/{org_slug}/{space_slug}/-/git/state")
def space_git_state(page: WebPage, org_slug: str, space_slug: str) -> JSONResponse:
    """Sync state, polled every 3 s by the "Git repository" page while a sync is requested or
    running: `version` changes when the page must reload."""
    actor = page.user_actor()
    space = find_space(page, actor, org_slug, space_slug)  # read access
    current = git_sync.sync_activity(page.db, space.id)
    return JSONResponse(
        {"poll": bool(current and current.poll), "version": git_sync.sync_version(page.db, space)},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/o/{org_slug}/{space_slug}/-/git/https")
def space_git_https(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    token: Annotated[str, Form()],
    username: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        credentials = GitCredentials.https(token, username)
        git_sync.set_credentials(page.db, actor, page.box, space, credentials)
    except SynaptaneError as error:
        return failure(space_settings_url(space, "git"), page, error)
    return redirect(space_settings_url(space, "git"), page, "Repository access saved.")


@router.post("/o/{org_slug}/{space_slug}/-/git/ssh")
def space_git_ssh(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    private_key: Annotated[str, Form()],
    known_hosts: Annotated[str, Form()] = "",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        credentials = GitCredentials.ssh(private_key, known_hosts)
        git_sync.set_credentials(page.db, actor, page.box, space, credentials)
    except SynaptaneError as error:
        return failure(space_settings_url(space, "git"), page, error)
    return redirect(space_settings_url(space, "git"), page, "Repository access saved.")


@router.post("/o/{org_slug}/{space_slug}/-/git/scan", response_class=HTMLResponse)
def space_git_scan(page: WebPage, org_slug: str, space_slug: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    if not can(actor.access, Action.MANAGE_ORG, org_id=space.org_id):
        raise forbidden()
    try:
        keys, fingerprints = scan_host_keys(space.repo_url or "")
    except SynaptaneError as error:
        return failure(space_settings_url(space, "git"), page, error)
    return _render_git(page, actor, space, scanned_keys=keys, fingerprints=fingerprints)


@router.post("/o/{org_slug}/{space_slug}/-/git/clear")
def space_git_clear(page: WebPage, org_slug: str, space_slug: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    git_sync.clear_credentials(page.db, actor, space)
    return redirect(space_settings_url(space, "git"), page, "Repository access deleted.")


@router.post("/o/{org_slug}/{space_slug}/-/webhook")
def space_webhook_rotate(page: WebPage, org_slug: str, space_slug: str, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    secret = git_sync.rotate_webhook_secret(page.db, actor, page.box, space)
    return reveal(
        space_settings_url(space, "git"), page, actor, "New webhook secret generated.", secret
    )


@router.post("/o/{org_slug}/{space_slug}/-/conflicts")
def space_conflicts(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    paths: Annotated[list[str], Form(alias="path")],
    choice: Annotated[list[str], Form()],
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    choices = dict(zip(paths, choice, strict=False))
    try:
        git_sync.request_resolution(page.db, actor, space, choices)
    except SynaptaneError as error:
        return failure(space_settings_url(space, "git"), page, error)
    return redirect(
        space_settings_url(space, "git"),
        page,
        "Resolution requested: it will be applied shortly.",
    )


@router.post("/o/{org_slug}/{space_slug}/-/update")
def space_update(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    repo_url: Annotated[str | None, Form()] = None,
    branch: Annotated[str | None, Form()] = None,
    review_every: Annotated[str | None, Form()] = None,
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        spaces.update_space(
            page.db,
            actor,
            space,
            name=name,
            description=description,
            repo_url=repo_url,
            branch=branch,
            review_every=review_every,
        )
    except SynaptaneError as error:
        return failure(space_settings_url(space), page, error)
    return redirect(space_settings_url(space), page, "Space updated.")


@router.post("/o/{org_slug}/{space_slug}/-/repository")
def space_repository(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    repo_url: Annotated[str, Form()],
    branch: Annotated[str, Form()] = "main",
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        spaces.update_space(page.db, actor, space, repo_url=repo_url, branch=branch)
    except SynaptaneError as error:
        return failure(space_settings_url(space, "git"), page, error)
    jobs.enqueue(page.db, git_sync.SYNC, space.id)
    return redirect(space_settings_url(space, "git"), page, "Repository address saved.")


@router.post("/o/{org_slug}/{space_slug}/-/delete")
def space_delete(
    page: WebPage, org_slug: str, space_slug: str, csrf: Csrf, confirm: Annotated[str, Form()] = ""
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    back = org_url(space.organization)
    try:
        _confirmed(confirm, space.slug)
        spaces.delete_space(page.db, actor, space)
    except SynaptaneError as error:
        return failure(space_settings_url(space), page, error)
    return redirect(back, page, "Space deleted.")


@router.post("/o/{org_slug}/{space_slug}/-/members")
def space_member_set(
    page: WebPage,
    org_slug: str,
    space_slug: str,
    csrf: Csrf,
    username: Annotated[str, Form()],
    role: Annotated[SpaceRole, Form()],
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        spaces.set_member(page.db, actor, space, accounts.get_user(page.db, username), role)
    except SynaptaneError as error:
        return failure(space_settings_url(space, "members"), page, error)
    return redirect(space_settings_url(space, "members"), page, "Member saved.")


@router.post("/o/{org_slug}/{space_slug}/-/members/{username}/remove")
def space_member_remove(
    page: WebPage, org_slug: str, space_slug: str, username: str, csrf: Csrf
) -> Response:
    actor = page.check_csrf(csrf)
    space = find_space(page, actor, org_slug, space_slug)
    try:
        spaces.remove_member(page.db, actor, space, accounts.get_user(page.db, username))
    except SynaptaneError as error:
        return failure(space_settings_url(space, "members"), page, error)
    return redirect(space_settings_url(space, "members"), page, "Member removed.")
