"""REST API: SSO login providers (super-admin) and organization webhooks."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from synaptane.core.access import OrgRole, SpaceRole
from synaptane.core.errors import not_found
from synaptane.services import invitations, organizations, spaces, sso, webhooks
from synaptane.storage.models import SSOProvider, Webhook

from .routes import Box, CurrentActor, DbSession

router = APIRouter(prefix="/api/v1")


class SSOProviderOut(BaseModel):
    slug: str
    kind: str
    name: str
    issuer: str
    client_id: str
    auto_create: bool
    link_by_email: bool
    allowed_domains: list[str]
    enabled: bool
    callback_url: str
    open_to_anyone: bool = Field(
        description="Automatic creation without an allowed domain on GitHub, Google or "
        "gitlab.com: any account of this provider gets an account here"
    )

    @classmethod
    def of(cls, provider: SSOProvider, public_url: str) -> "SSOProviderOut":
        return cls(
            slug=provider.slug,
            kind=provider.kind,
            name=provider.name,
            issuer=provider.issuer,
            client_id=provider.client_id,
            auto_create=provider.auto_create,
            link_by_email=provider.link_by_email,
            allowed_domains=list(provider.allowed_domains or []),
            enabled=provider.enabled,
            callback_url=f"{public_url}/sso/{provider.slug}/callback",
            open_to_anyone=sso.open_to_anyone(provider),
        )


class SSOProviderIn(BaseModel):
    kind: str
    name: str
    client_id: str
    client_secret: str | None = Field(default=None, description="null: secret unchanged")
    issuer: str = ""
    auto_create: bool = Field(
        default=False,
        description="Create an account on the first login of an unknown address",
    )
    link_by_email: bool | None = Field(
        default=None,
        description="Link to an existing account with the same verified address; null: yes "
        "for GitHub and Google, no for GitLab and OIDC",
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="E-mail domains allowed for automatic creation and linking "
        "(exact domain); empty: no restriction",
    )
    enabled: bool = True


class WebhookOut(BaseModel):
    id: UUID
    url: str
    events: list[str]
    active: bool
    last_delivery_at: datetime | None
    last_status: str | None
    failures: int = Field(0, description="Consecutive delivery failures (circuit breaker)")

    @classmethod
    def of(cls, webhook: Webhook) -> "WebhookOut":
        return cls.model_validate(webhook, from_attributes=True)


class WebhookIn(BaseModel):
    url: str
    events: list[str] = Field(default_factory=lambda: list(webhooks.EVENTS))


class WebhookCreated(WebhookOut):
    secret: str = Field(description="Signing secret, shown only once")


def _public_url(request: Request) -> str:
    return str(request.app.state.settings.public_url)


@router.get("/sso-providers", tags=["sso"])
def list_sso(request: Request, session: DbSession, actor: CurrentActor) -> list[SSOProviderOut]:
    from synaptane.core.access import Action, require

    require(actor.access, Action.MANAGE_INSTANCE)
    return [SSOProviderOut.of(p, _public_url(request)) for p in sso.list_providers(session)]


@router.put("/sso-providers/{slug}", tags=["sso"])
def put_sso(
    slug: str,
    body: SSOProviderIn,
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    box: Box,
) -> SSOProviderOut:
    provider = sso.save_provider(session, actor, box, slug=slug, **body.model_dump())
    return SSOProviderOut.of(provider, _public_url(request))


@router.delete("/sso-providers/{slug}", status_code=status.HTTP_204_NO_CONTENT, tags=["sso"])
def delete_sso(slug: str, session: DbSession, actor: CurrentActor) -> Response:
    sso.delete_provider(session, actor, slug)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations/{org}/webhooks", tags=["webhooks"])
def list_hooks(org: str, session: DbSession, actor: CurrentActor) -> list[WebhookOut]:
    found = organizations.get_organization(session, actor, org)
    return [WebhookOut.of(w) for w in webhooks.list_webhooks(session, actor, found)]


@router.post(
    "/organizations/{org}/webhooks", status_code=status.HTTP_201_CREATED, tags=["webhooks"]
)
def create_hook(
    org: str, body: WebhookIn, request: Request, session: DbSession, actor: CurrentActor, box: Box
) -> WebhookCreated:
    found = organizations.get_organization(session, actor, org)
    webhook, secret = webhooks.create(
        session,
        actor,
        box,
        found,
        body.url,
        body.events,
        allow_private=request.app.state.settings.allow_private_webhooks,
    )
    return WebhookCreated(secret=secret, **WebhookOut.of(webhook).model_dump())


@router.delete(
    "/organizations/{org}/webhooks/{webhook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["webhooks"],
)
def delete_hook(org: str, webhook_id: UUID, session: DbSession, actor: CurrentActor) -> Response:
    webhooks.delete(session, actor, organizations.get_organization(session, actor, org), webhook_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/organizations/{org}/webhooks/{webhook_id}/test",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["webhooks"],
)
def test_hook(org: str, webhook_id: UUID, session: DbSession, actor: CurrentActor) -> Response:
    webhooks.send_test(
        session, actor, organizations.get_organization(session, actor, org), webhook_id
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/organizations/{org}/webhooks/{webhook_id}/reactivate",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["webhooks"],
)
def reactivate_hook(
    org: str, webhook_id: UUID, session: DbSession, actor: CurrentActor
) -> Response:
    """Bring back into service a webhook cut off by the circuit breaker (repeated failures)."""
    webhooks.reactivate(
        session, actor, organizations.get_organization(session, actor, org), webhook_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Invitations (organization admin) ---------------------------------------------------------


class InvitationIn(BaseModel):
    email: str
    org_role: OrgRole = OrgRole.MEMBER
    spaces: dict[str, SpaceRole] = Field(
        default_factory=dict, description="Space identifier (slug) → role"
    )


class InvitationOut(BaseModel):
    id: UUID | None
    email: str
    org_role: str
    expires_at: datetime | None
    link: str | None = Field(
        default=None, description="Link to pass on, shown only once (account to create)"
    )
    granted: bool = Field(default=False, description="Existing account: access granted right away")


@router.get("/organizations/{org}/invitations", tags=["invitations"])
def list_invitations(org: str, session: DbSession, actor: CurrentActor) -> list[InvitationOut]:
    found = organizations.get_organization(session, actor, org)
    return [
        InvitationOut(id=i.id, email=i.email, org_role=i.org_role, expires_at=i.expires_at)
        for i in invitations.pending(session, actor, found)
    ]


@router.post(
    "/organizations/{org}/invitations", status_code=status.HTTP_201_CREATED, tags=["invitations"]
)
def create_invitation(
    org: str, body: InvitationIn, request: Request, session: DbSession, actor: CurrentActor
) -> InvitationOut:
    found = organizations.get_organization(session, actor, org)
    by_slug = {s.slug: s for s in spaces.list_spaces(session, actor, found)}
    unknown = sorted(set(body.spaces) - set(by_slug))
    if unknown:
        raise not_found("Space not found: {spaces}.", spaces=", ".join(unknown))
    created = invitations.invite(
        session,
        actor,
        found,
        email=body.email,
        org_role=body.org_role,
        space_roles={str(by_slug[s].id): role.value for s, role in body.spaces.items()},
    )
    if created.invitation is None:
        return InvitationOut(
            id=None,
            email=body.email.strip().lower(),
            org_role=body.org_role,
            expires_at=None,
            granted=True,
        )
    return InvitationOut(
        id=created.invitation.id,
        email=created.invitation.email,
        org_role=created.invitation.org_role,
        expires_at=created.invitation.expires_at,
        link=f"{_public_url(request)}/invite/{created.token}",
    )


@router.delete(
    "/organizations/{org}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["invitations"],
)
def delete_invitation(
    org: str, invitation_id: UUID, session: DbSession, actor: CurrentActor
) -> Response:
    found = organizations.get_organization(session, actor, org)
    invitations.revoke(session, actor, found, invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
