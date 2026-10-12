"""REST API `/api/v1`, authenticated by personal token (`Authorization: Bearer syn_…`).

Organizations and spaces are referred to by their slugs: `/organizations/acme/spaces/infra`.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole, SpaceRole
from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.core.security import SecretBox
from synaptane.git.credentials import GitCredentials
from synaptane.git.repo import describe_error
from synaptane.services import Actor, accounts, git_sync, load_actor, organizations, spaces
from synaptane.storage.db import Database
from synaptane.storage.models import ApiToken, Organization, Space, User
from synaptane.web.i18n import SOURCE, negotiate, translator

router = APIRouter(prefix="/api/v1")


def api_locale(request: Request) -> str:
    """Language of the API's messages: the `Accept-Language` header's, English by default (the
    `error` codes stay the same in every language)."""
    return negotiate(request.headers.get("accept-language"), SOURCE)


def api_text(request: Request, text: str, **values: object) -> str:
    """A message of the API (a template or a `Label`), in the caller's language."""
    return translator(api_locale(request))(text, **values)


# --- Dependencies -----------------------------------------------------------------------------


def db_session(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.database
    with database.session() as session:
        yield session


# Committed before the response is sent (see WebPage): a write is visible as of the 2xx.
DbSession = Annotated[Session, Depends(db_session, scope="function")]


def api_actor(session: DbSession, authorization: Annotated[str | None, Header()] = None) -> Actor:
    scheme, _, token = (authorization or "").partition(" ")
    found = accounts.resolve_api_token(session, token) if scheme.lower() == "bearer" else None
    if found is None:
        raise SynaptaneError(ErrorCode.UNAUTHENTICATED, "Missing, invalid or expired token.")
    user, api_token = found
    limits = accounts.token_limits(api_token.read_only, api_token.space_ids)
    return load_actor(session, user, via=f"API · {api_token.name}", limits=limits)


CurrentActor = Annotated[Actor, Depends(api_actor)]


def secret_box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


Box = Annotated[SecretBox, Depends(secret_box)]


# --- Schemas ----------------------------------------------------------------------------------


class UserOut(BaseModel):
    username: str
    display_name: str
    email: str | None
    locale: str
    is_superadmin: bool
    is_active: bool

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls.model_validate(user, from_attributes=True)


class UserCreate(BaseModel):
    username: str
    display_name: str
    password: str | None = None
    email: str | None = None
    is_superadmin: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = None
    email: str | None = None
    locale: str | None = None
    is_active: bool | None = None
    is_superadmin: bool | None = None


class OrgOut(BaseModel):
    slug: str
    name: str
    description: str
    role: OrgRole | None = Field(description="Role of the calling account in the organization.")

    @classmethod
    def of(cls, org: Organization, actor: Actor) -> "OrgOut":
        return cls(
            slug=org.slug,
            name=org.name,
            description=org.description,
            role=actor.access.org_roles.get(org.id),
        )


class OrgCreate(BaseModel):
    slug: str
    name: str
    description: str = ""
    join: bool = Field(default=True, description="The creator becomes an admin.")


class OrgUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class SpaceOut(BaseModel):
    organization: str
    slug: str
    name: str
    description: str
    repo_url: str | None
    branch: str
    review_every: str = Field(
        description="Default review cadence for pages (6m, 1y…); empty: never"
    )
    role: SpaceRole | None = Field(description="Calling account's effective role in the space.")

    @classmethod
    def of(cls, space: Space, actor: Actor) -> "SpaceOut":
        return cls(
            organization=space.organization.slug,
            slug=space.slug,
            name=space.name,
            description=space.description,
            repo_url=space.repo_url,
            branch=space.branch,
            review_every=space.review_every or "",
            role=spaces.role_in(actor, space),
        )


class SpaceCreate(BaseModel):
    slug: str
    name: str
    description: str = ""
    repo_url: str | None = None
    branch: str = "main"


class SpaceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    review_every: str | None = Field(
        default=None,
        description="Default review cadence (90d, 6m, 1y, never); empty: never",
    )


class OrgMemberOut(BaseModel):
    username: str
    display_name: str
    role: OrgRole


class SpaceMemberOut(BaseModel):
    username: str
    display_name: str
    role: SpaceRole


class OrgRoleIn(BaseModel):
    role: OrgRole


class SpaceRoleIn(BaseModel):
    role: SpaceRole


class TokenOut(BaseModel):
    id: UUID
    name: str
    hint: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    read_only: bool
    space_ids: list[UUID] | None = Field(description="null: all spaces, future ones included")

    @classmethod
    def of(cls, token: ApiToken) -> "TokenOut":
        return cls.model_validate(token, from_attributes=True)


class TokenCreate(BaseModel):
    name: str
    days: int | None = Field(default=90, description="Validity in days; null = no expiry.")
    read_only: bool = Field(default=False, description="Read-only: no writes or drops.")
    spaces: list[str] | None = Field(
        default=None, description='Spaces as "org/space"; null: all, including future ones.'
    )


class TokenCreated(TokenOut):
    token: str = Field(description="Token value, shown only once.")


# --- Current account --------------------------------------------------------------------------


@router.get("/me", tags=["account"])
def me(actor: CurrentActor) -> UserOut:
    return UserOut.of(actor.user)


@router.get("/me/tokens", tags=["account"])
def my_tokens(session: DbSession, actor: CurrentActor) -> list[TokenOut]:
    return [TokenOut.of(t) for t in accounts.list_api_tokens(session, actor)]


@router.post("/me/tokens", status_code=status.HTTP_201_CREATED, tags=["account"])
def create_token(body: TokenCreate, session: DbSession, actor: CurrentActor) -> TokenCreated:
    chosen = None
    if body.spaces is not None:
        chosen = []
        for address in body.spaces:
            org_slug, _, space_slug = address.strip().strip("/").partition("/")
            chosen.append(find_space(session, actor, org_slug, space_slug).id)
    value, token = accounts.create_api_token(
        session, actor, body.name, body.days, read_only=body.read_only, space_ids=chosen
    )
    return TokenCreated(token=value, **TokenOut.of(token).model_dump())


@router.delete("/me/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["account"])
def revoke_token(token_id: UUID, session: DbSession, actor: CurrentActor) -> Response:
    accounts.revoke_api_token(session, actor, token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/spaces", tags=["spaces"])
def all_my_spaces(session: DbSession, actor: CurrentActor) -> list[SpaceOut]:
    """All accessible spaces, across all organizations."""
    return [SpaceOut.of(s, actor) for s in spaces.list_all_visible_spaces(session, actor)]


# --- Accounts (super-admin) -------------------------------------------------------------------


@router.get("/users", tags=["users"])
def list_users(session: DbSession, actor: CurrentActor) -> list[UserOut]:
    return [UserOut.of(u) for u in accounts.list_users(session, actor)]


@router.post("/users", status_code=status.HTTP_201_CREATED, tags=["users"])
def create_user(
    body: UserCreate, request: Request, session: DbSession, actor: CurrentActor
) -> UserOut:
    locale = request.app.state.settings.default_locale
    return UserOut.of(accounts.create_user(session, actor, **body.model_dump(), locale=locale))


@router.patch("/users/{username}", tags=["users"])
def update_user(
    username: str, body: UserUpdate, session: DbSession, actor: CurrentActor
) -> UserOut:
    user = accounts.get_user(session, username)
    return UserOut.of(
        accounts.update_user(session, actor, user, **body.model_dump(exclude_unset=True))
    )


# --- Organizations ----------------------------------------------------------------------------


@router.get("/organizations", tags=["organizations"])
def list_organizations(session: DbSession, actor: CurrentActor) -> list[OrgOut]:
    return [OrgOut.of(o, actor) for o in organizations.list_organizations(session, actor)]


@router.post("/organizations", status_code=status.HTTP_201_CREATED, tags=["organizations"])
def create_organization(body: OrgCreate, session: DbSession, actor: CurrentActor) -> OrgOut:
    org = organizations.create_organization(session, actor, **body.model_dump())
    return OrgOut.of(org, actor)


@router.get("/organizations/{org}", tags=["organizations"])
def get_organization(org: str, session: DbSession, actor: CurrentActor) -> OrgOut:
    return OrgOut.of(organizations.get_organization(session, actor, org), actor)


@router.patch("/organizations/{org}", tags=["organizations"])
def update_organization(
    org: str, body: OrgUpdate, session: DbSession, actor: CurrentActor
) -> OrgOut:
    found = organizations.get_organization(session, actor, org)
    organizations.update_organization(session, actor, found, **body.model_dump(exclude_unset=True))
    return OrgOut.of(found, actor)


@router.delete(
    "/organizations/{org}", status_code=status.HTTP_204_NO_CONTENT, tags=["organizations"]
)
def delete_organization(org: str, session: DbSession, actor: CurrentActor) -> Response:
    organizations.delete_organization(
        session, actor, organizations.get_organization(session, actor, org)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations/{org}/members", tags=["organizations"])
def list_org_members(org: str, session: DbSession, actor: CurrentActor) -> list[OrgMemberOut]:
    found = organizations.get_organization(session, actor, org)
    return [
        OrgMemberOut(username=u.username, display_name=u.display_name, role=role)
        for u, role in organizations.list_members(session, actor, found)
    ]


@router.put("/organizations/{org}/members/{username}", tags=["organizations"])
def set_org_member(
    org: str, username: str, body: OrgRoleIn, session: DbSession, actor: CurrentActor
) -> OrgMemberOut:
    found = organizations.get_organization(session, actor, org)
    user = accounts.get_user(session, username)
    organizations.set_member(session, actor, found, user, body.role)
    return OrgMemberOut(username=user.username, display_name=user.display_name, role=body.role)


@router.delete(
    "/organizations/{org}/members/{username}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["organizations"],
)
def remove_org_member(org: str, username: str, session: DbSession, actor: CurrentActor) -> Response:
    found = organizations.get_organization(session, actor, org)
    organizations.remove_member(session, actor, found, accounts.get_user(session, username))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Spaces -----------------------------------------------------------------------------------


def find_space(session: Session, actor: Actor, org: str, space: str) -> Space:
    return spaces.get_space(
        session, actor, organizations.get_organization(session, actor, org), space
    )


@router.get("/organizations/{org}/spaces", tags=["spaces"])
def list_spaces(org: str, session: DbSession, actor: CurrentActor) -> list[SpaceOut]:
    found = organizations.get_organization(session, actor, org)
    return [SpaceOut.of(s, actor) for s in spaces.list_spaces(session, actor, found)]


@router.post("/organizations/{org}/spaces", status_code=status.HTTP_201_CREATED, tags=["spaces"])
def create_space(org: str, body: SpaceCreate, session: DbSession, actor: CurrentActor) -> SpaceOut:
    found = organizations.get_organization(session, actor, org)
    return SpaceOut.of(spaces.create_space(session, actor, found, **body.model_dump()), actor)


@router.get("/organizations/{org}/spaces/{space}", tags=["spaces"])
def get_space(org: str, space: str, session: DbSession, actor: CurrentActor) -> SpaceOut:
    return SpaceOut.of(find_space(session, actor, org, space), actor)


@router.patch("/organizations/{org}/spaces/{space}", tags=["spaces"])
def update_space(
    org: str, space: str, body: SpaceUpdate, session: DbSession, actor: CurrentActor
) -> SpaceOut:
    found = find_space(session, actor, org, space)
    spaces.update_space(session, actor, found, **body.model_dump(exclude_unset=True))
    return SpaceOut.of(found, actor)


@router.delete(
    "/organizations/{org}/spaces/{space}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["spaces"],
)
def delete_space(org: str, space: str, session: DbSession, actor: CurrentActor) -> Response:
    spaces.delete_space(session, actor, find_space(session, actor, org, space))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations/{org}/spaces/{space}/members", tags=["spaces"])
def list_space_members(
    org: str, space: str, session: DbSession, actor: CurrentActor
) -> list[SpaceMemberOut]:
    found = find_space(session, actor, org, space)
    return [
        SpaceMemberOut(username=u.username, display_name=u.display_name, role=role)
        for u, role in spaces.list_members(session, actor, found)
    ]


@router.put("/organizations/{org}/spaces/{space}/members/{username}", tags=["spaces"])
def set_space_member(
    org: str, space: str, username: str, body: SpaceRoleIn, session: DbSession, actor: CurrentActor
) -> SpaceMemberOut:
    found = find_space(session, actor, org, space)
    user = accounts.get_user(session, username)
    spaces.set_member(session, actor, found, user, body.role)
    return SpaceMemberOut(username=user.username, display_name=user.display_name, role=body.role)


@router.delete(
    "/organizations/{org}/spaces/{space}/members/{username}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["spaces"],
)
def remove_space_member(
    org: str, space: str, username: str, session: DbSession, actor: CurrentActor
) -> Response:
    found = find_space(session, actor, org, space)
    spaces.remove_member(session, actor, found, accounts.get_user(session, username))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Git --------------------------------------------------------------------------------------


class GitStatusOut(BaseModel):
    repo_url: str | None
    branch: str
    status: str = Field(description="never, ok, error or conflict")
    synced_at: datetime | None
    error: str | None
    remote_head: str | None
    conflicts: list[str]
    credentials: str | None = Field(description="https, ssh or null (admins only)")
    webhook_url: str


class GitCredentialsIn(BaseModel):
    kind: Literal["https", "ssh"]
    token: str = ""
    username: str = ""
    private_key: str = ""
    known_hosts: str = ""


class WebhookSecretOut(BaseModel):
    url: str
    secret: str


class ConflictChoices(BaseModel):
    choices: dict[str, Literal["local", "remote"]] = Field(
        description="For each conflicting file: the version to keep."
    )


def _webhook_url(request: Request, space: Space) -> str:
    public_url = request.app.state.settings.public_url
    return f"{public_url}/hooks/git/{space.address}"


@router.get("/organizations/{org}/spaces/{space}/git", tags=["git"])
def git_status(
    org: str, space: str, request: Request, session: DbSession, actor: CurrentActor, box: Box
) -> GitStatusOut:
    found = find_space(session, actor, org, space)
    manage = actor.access.is_org_admin(found.org_id)
    return GitStatusOut(
        repo_url=found.repo_url,
        branch=found.branch,
        status=found.sync_status,
        synced_at=found.synced_at,
        error=describe_error(found.sync_error, translator(api_locale(request)))
        if found.sync_error
        else None,
        remote_head=found.remote_head,
        conflicts=found.conflict_paths,
        credentials=git_sync.credential_kind(session, box, found) if manage else None,
        webhook_url=_webhook_url(request, found),
    )


@router.put(
    "/organizations/{org}/spaces/{space}/git/credentials",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["git"],
)
def set_git_credentials(
    org: str, space: str, body: GitCredentialsIn, session: DbSession, actor: CurrentActor, box: Box
) -> Response:
    found = find_space(session, actor, org, space)
    credentials = (
        GitCredentials.https(body.token, body.username)
        if body.kind == "https"
        else GitCredentials.ssh(body.private_key, body.known_hosts)
    )
    git_sync.set_credentials(session, actor, box, found, credentials)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/organizations/{org}/spaces/{space}/git/credentials",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["git"],
)
def clear_git_credentials(
    org: str, space: str, session: DbSession, actor: CurrentActor
) -> Response:
    git_sync.clear_credentials(session, actor, find_space(session, actor, org, space))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/organizations/{org}/spaces/{space}/git/sync",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["git"],
)
def request_git_sync(org: str, space: str, session: DbSession, actor: CurrentActor) -> Response:
    git_sync.request_sync(session, actor, find_space(session, actor, org, space))
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/organizations/{org}/spaces/{space}/git/webhook-secret", tags=["git"])
def rotate_webhook_secret(
    org: str, space: str, request: Request, session: DbSession, actor: CurrentActor, box: Box
) -> WebhookSecretOut:
    found = find_space(session, actor, org, space)
    secret = git_sync.rotate_webhook_secret(session, actor, box, found)
    return WebhookSecretOut(url=_webhook_url(request, found), secret=secret)


@router.post(
    "/organizations/{org}/spaces/{space}/git/conflicts/resolve",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["git"],
)
def resolve_git_conflicts(
    org: str, space: str, body: ConflictChoices, session: DbSession, actor: CurrentActor
) -> Response:
    found = find_space(session, actor, org, space)
    git_sync.request_resolution(session, actor, found, body.choices)
    return Response(status_code=status.HTTP_202_ACCEPTED)
