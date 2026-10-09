"""Organizations and their members."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from synaptane.core import names
from synaptane.core.access import Action, OrgRole, can, require
from synaptane.core.errors import conflict, invalid, not_found
from synaptane.services import Actor, audit
from synaptane.storage.models import Organization, OrgMember, Space, SpaceMember, User


def list_organizations(session: Session, actor: Actor) -> list[Organization]:
    query = select(Organization).order_by(Organization.name)
    if not actor.access.is_superadmin:
        query = query.where(Organization.id.in_(actor.access.org_roles))
    return list(session.scalars(query))


def get_organization(session: Session, actor: Actor, slug: str) -> Organization:
    org = session.scalar(select(Organization).where(Organization.slug == slug))
    # Same answer for "missing" and "invisible": organizations are not revealed.
    if org is None or not can(actor.access, Action.VIEW_ORG, org_id=org.id):
        raise not_found("Organization not found.")
    return org


def create_organization(
    session: Session,
    actor: Actor,
    *,
    slug: str,
    name: str,
    description: str = "",
    join: bool = False,
) -> Organization:
    """`join`: the creator becomes an administrator of the organization. Otherwise, their
    assistants (MCP) see nothing there: the super-admin only has their actual memberships."""
    require(actor.access, Action.MANAGE_INSTANCE)
    slug = names.slug(slug)
    if session.scalar(select(Organization).where(Organization.slug == slug)):
        raise conflict("The organization “{slug}” already exists.", slug=slug)
    org = Organization(slug=slug, name=names.display_name(name), description=description.strip())
    session.add(org)
    session.flush()
    audit(session, actor, "org.created", org_id=org.id, slug=slug)
    if join:
        set_member(session, actor, org, actor.user, OrgRole.ADMIN)
    return org


def update_organization(
    session: Session,
    actor: Actor,
    org: Organization,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Organization:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    if name is not None:
        org.name = names.display_name(name)
    if description is not None:
        org.description = description.strip()
    audit(session, actor, "org.updated", org_id=org.id)
    return org


def delete_organization(session: Session, actor: Actor, org: Organization) -> None:
    require(actor.access, Action.MANAGE_INSTANCE)
    if session.scalar(select(Space.id).where(Space.org_id == org.id).limit(1)):
        raise invalid("Delete the organization's spaces first.")
    audit(session, actor, "org.deleted", slug=org.slug)
    session.delete(org)


def list_members(session: Session, actor: Actor, org: Organization) -> list[tuple[User, OrgRole]]:
    require(actor.access, Action.VIEW_ORG, org_id=org.id)
    rows = session.execute(
        select(User, OrgMember.role)
        .join(OrgMember, OrgMember.user_id == User.id)
        .where(OrgMember.org_id == org.id)
        .order_by(User.username)
    )
    return [(user, OrgRole(role)) for user, role in rows]


def set_member(
    session: Session, actor: Actor, org: Organization, user: User, role: OrgRole
) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    member = session.get(OrgMember, (org.id, user.id))
    if member is None:
        session.add(OrgMember(org_id=org.id, user_id=user.id, role=role.value))
    else:
        member.role = role.value
    audit(session, actor, "org.member_set", org_id=org.id, username=user.username, role=role)


def ensure_member(session: Session, org: Organization, user: User) -> None:
    """A space member is always a member of that space's organization."""
    if session.get(OrgMember, (org.id, user.id)) is None:
        session.add(OrgMember(org_id=org.id, user_id=user.id, role=OrgRole.MEMBER.value))


def remove_member(session: Session, actor: Actor, org: Organization, user: User) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    member = session.get(OrgMember, (org.id, user.id))
    if member is None:
        raise not_found("This account is not a member of the organization.")
    space_ids = select(Space.id).where(Space.org_id == org.id)
    session.execute(
        delete(SpaceMember).where(
            SpaceMember.user_id == user.id, SpaceMember.space_id.in_(space_ids)
        )
    )
    session.delete(member)
    audit(session, actor, "org.member_removed", org_id=org.id, username=user.username)
