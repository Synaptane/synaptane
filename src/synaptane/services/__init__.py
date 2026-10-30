"""Use cases: each function checks permissions (`core.access`), acts on the database and logs.

The web UI, the REST API and the MCP server call these functions; they hold no business rules.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import Access, Limits, OrgRole, SpaceRole
from synaptane.storage.models import AuditEvent, OrgMember, Space, SpaceMember, User


@dataclass(frozen=True)
class Actor:
    """Authenticated account and its permissions, loaded once per request."""

    user: User
    access: Access
    via: str = ""  # client used: "web", name of the OAuth application or of the token
    restricted: bool = False  # restricted token: no account management, no administration


def load_actor(session: Session, user: User, via: str = "", limits: Limits | None = None) -> Actor:
    org_roles = {
        m.org_id: OrgRole(m.role)
        for m in session.scalars(select(OrgMember).where(OrgMember.user_id == user.id))
    }
    space_roles = {
        m.space_id: SpaceRole(m.role)
        for m in session.scalars(select(SpaceMember).where(SpaceMember.user_id == user.id))
    }
    limits = limits or Limits()
    if limits.restricted:
        org_roles, space_roles = _narrow(session, org_roles, space_roles, limits)
    access = Access(
        user_id=user.id,
        is_superadmin=user.is_superadmin and not (limits.restricted or limits.members_only),
        org_roles=org_roles,
        space_roles=space_roles,
        inbox_only=limits.inbox_only,
    )
    return Actor(user=user, access=access, via=via, restricted=limits.restricted)


def _narrow(
    session: Session,
    org_roles: dict[UUID, OrgRole],
    space_roles: dict[UUID, SpaceRole],
    limits: Limits,
) -> tuple[dict[UUID, OrgRole], dict[UUID, SpaceRole]]:
    """Permissions of a restricted token: explicit space roles, filtered, no administration.

    An organization admin contributes to all its spaces without being a member: these implicit
    accesses become explicit before the admin role is removed.
    """
    roles = dict(space_roles)
    admin_orgs = [org for org, role in org_roles.items() if role is OrgRole.ADMIN]
    if admin_orgs:
        for space_id in session.scalars(select(Space.id).where(Space.org_id.in_(admin_orgs))):
            roles[space_id] = SpaceRole.CONTRIBUTOR
    if limits.space_ids is not None:
        roles = {s: r for s, r in roles.items() if s in limits.space_ids}
    if limits.read_only:
        roles = dict.fromkeys(roles, SpaceRole.READER)
    owners = (
        set(session.scalars(select(Space.org_id).where(Space.id.in_(roles)))) if roles else set()
    )
    if limits.space_ids is None:
        owners |= set(org_roles)
    return dict.fromkeys(owners, OrgRole.MEMBER), roles


def audit(
    session: Session,
    actor: Actor | None,
    action: str,
    *,
    org_id: object = None,
    space: Space | None = None,
    **details: object,
) -> None:
    session.add(
        AuditEvent(
            actor_id=actor.user.id if actor else None,
            action=action,
            org_id=space.org_id if space else org_id,
            space_id=space.id if space else None,
            details={k: str(v) for k, v in details.items()},
        )
    )
