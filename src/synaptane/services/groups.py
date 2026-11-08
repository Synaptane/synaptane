"""Groups of members: an organization's administrators give a role on several of its spaces at
once to some of its members (human resources, management, a project team…).

A person's effective role on a space is the highest of the role given in the space, the roles
of their groups on it and the organization's default role (`core.access.effective_role`,
computed once per request in `load_actor`). A group's role applies to a restricted space too:
that is how a restricted space is opened to a team. Organization administrators keep every
right, and the limits of a token or a connection still apply afterwards.

Only members of the organization join its groups, and a group only has roles on the spaces of
its organization: the database enforces both (composite foreign keys, see migration 0031).
Leaving the organization removes a person from its groups; deleting a group, or a space,
removes the access it gave.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.core import names
from synaptane.core.access import Action, SpaceRole, require
from synaptane.core.errors import conflict, invalid, not_found
from synaptane.services import Actor, audit
from synaptane.storage.models import (
    Group,
    GroupMember,
    GroupSpaceRole,
    Organization,
    OrgMember,
    Space,
    User,
)


@dataclass(frozen=True)
class GroupSummary:
    group: Group
    members: int
    spaces: int


@dataclass(frozen=True)
class SpaceGroup:
    """A group with a role on a space, and its members."""

    group: Group
    role: SpaceRole
    members: list[User]


def list_groups(session: Session, actor: Actor, org: Organization) -> list[GroupSummary]:
    """The organization's groups, by name, with their number of members and of spaces."""
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    members = (
        select(GroupMember.group_id, func.count().label("n"))
        .where(GroupMember.org_id == org.id)
        .group_by(GroupMember.group_id)
        .subquery()
    )
    roles = (
        select(GroupSpaceRole.group_id, func.count().label("n"))
        .where(GroupSpaceRole.org_id == org.id)
        .group_by(GroupSpaceRole.group_id)
        .subquery()
    )
    rows = session.execute(
        select(Group, func.coalesce(members.c.n, 0), func.coalesce(roles.c.n, 0))
        .outerjoin(members, members.c.group_id == Group.id)
        .outerjoin(roles, roles.c.group_id == Group.id)
        .where(Group.org_id == org.id)
        .order_by(func.lower(Group.name))
    )
    return [GroupSummary(group, int(m), int(s)) for group, m, s in rows]


def get_group(session: Session, actor: Actor, org: Organization, group_id: str | UUID) -> Group:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    try:
        wanted = group_id if isinstance(group_id, UUID) else UUID(str(group_id))
    except ValueError:
        raise not_found("Group not found.") from None
    group = session.get(Group, wanted)
    if group is None or group.org_id != org.id:
        raise not_found("Group not found.")
    return group


def _clean_name(session: Session, org_id: UUID, name: str, group: Group | None = None) -> str:
    """1 to 120 characters, unique in the organization whatever the case ("RH" and "rh")."""
    name = names.display_name(name)
    query = select(Group.id).where(Group.org_id == org_id, func.lower(Group.name) == name.lower())
    if group is not None:
        query = query.where(Group.id != group.id)
    if session.scalar(query.limit(1)):
        raise conflict("The group “{name}” already exists in this organization.", name=name)
    return name


def create_group(
    session: Session, actor: Actor, org: Organization, *, name: str, description: str = ""
) -> Group:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    group = Group(
        org_id=org.id,
        name=_clean_name(session, org.id, name),
        description=description.strip(),
    )
    session.add(group)
    session.flush()
    audit(session, actor, "group.created", org_id=org.id, group=group.name)
    return group


def update_group(
    session: Session,
    actor: Actor,
    group: Group,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Group:
    require(actor.access, Action.MANAGE_ORG, org_id=group.org_id)
    previous = group.name
    changed = []
    if name is not None:
        cleaned = _clean_name(session, group.org_id, name, group)
        if cleaned != group.name:
            group.name = cleaned
            changed.append("name")
    if description is not None and description.strip() != group.description:
        group.description = description.strip()
        changed.append("description")
    if changed:
        audit(
            session,
            actor,
            "group.updated",
            org_id=group.org_id,
            group=group.name,
            fields=" ".join(changed),
            previous=previous if "name" in changed else "",
        )
    return group


def delete_group(session: Session, actor: Actor, group: Group) -> None:
    """Its members lose the access it gave (unless they have it otherwise)."""
    require(actor.access, Action.MANAGE_ORG, org_id=group.org_id)
    audit(session, actor, "group.deleted", org_id=group.org_id, group=group.name)
    session.flush()  # pending changes of its members and roles first
    session.delete(group)  # members and roles: ON DELETE CASCADE
    session.flush()


def list_members(session: Session, actor: Actor, group: Group) -> list[User]:
    require(actor.access, Action.MANAGE_ORG, org_id=group.org_id)
    return list(
        session.scalars(
            select(User)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(GroupMember.group_id == group.id)
            .order_by(User.username)
        )
    )


def add_member(session: Session, actor: Actor, group: Group, user: User) -> bool:
    """Only a member of the organization joins its groups. False if already in the group."""
    require(actor.access, Action.MANAGE_ORG, org_id=group.org_id)
    if session.get(OrgMember, (group.org_id, user.id)) is None:
        raise invalid(
            "{name} is not a member of the organization: add them to it, or invite them, first.",
            name=user.display_name,
        )
    if session.get(GroupMember, (group.id, user.id)) is not None:
        return False
    session.add(GroupMember(group_id=group.id, org_id=group.org_id, user_id=user.id))
    audit(
        session,
        actor,
        "group.member_added",
        org_id=group.org_id,
        group=group.name,
        username=user.username,
    )
    return True


def remove_member(session: Session, actor: Actor, group: Group, user: User) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=group.org_id)
    member = session.get(GroupMember, (group.id, user.id))
    if member is None:
        raise not_found("This account is not a member of the group.")
    session.delete(member)
    session.flush()
    audit(
        session,
        actor,
        "group.member_removed",
        org_id=group.org_id,
        group=group.name,
        username=user.username,
    )


def list_spaces(session: Session, actor: Actor, group: Group) -> list[tuple[Space, SpaceRole]]:
    """The spaces the group has a role on, by name."""
    require(actor.access, Action.MANAGE_ORG, org_id=group.org_id)
    rows = session.execute(
        select(Space, GroupSpaceRole.role)
        .join(GroupSpaceRole, GroupSpaceRole.space_id == Space.id)
        .where(GroupSpaceRole.group_id == group.id)
        .order_by(Space.name)
    )
    return [(space, SpaceRole(role)) for space, role in rows]


def set_space_role(
    session: Session, actor: Actor, group: Group, space: Space, role: SpaceRole
) -> None:
    """Gives the group a role on a space of its organization, restricted or not."""
    require(actor.access, Action.MANAGE_ORG, org_id=group.org_id)
    if space.org_id != group.org_id:
        raise invalid("A group only has roles on the spaces of its organization.")
    found = session.get(GroupSpaceRole, (group.id, space.id))
    if found is not None and found.role == role.value:
        return
    if found is None:
        session.add(
            GroupSpaceRole(
                group_id=group.id, space_id=space.id, org_id=group.org_id, role=role.value
            )
        )
    else:
        found.role = role.value
    audit(session, actor, "group.space_role_set", space=space, group=group.name, role=role)


def remove_space_role(session: Session, actor: Actor, group: Group, space: Space) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=group.org_id)
    found = session.get(GroupSpaceRole, (group.id, space.id))
    if found is None:
        raise not_found("This group has no role on this space.")
    session.delete(found)
    session.flush()
    audit(session, actor, "group.space_role_removed", space=space, group=group.name)


def on_space(session: Session, actor: Actor, space: Space) -> list[SpaceGroup]:
    """The groups with a role on a space, and their members (two queries): what its members
    page shows to whoever reads the space."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    rows = session.execute(
        select(Group, GroupSpaceRole.role)
        .join(GroupSpaceRole, GroupSpaceRole.group_id == Group.id)
        .where(GroupSpaceRole.space_id == space.id)
        .order_by(func.lower(Group.name))
    ).all()
    people: dict[UUID, list[User]] = {}
    if rows:
        for group_id, user in session.execute(
            select(GroupMember.group_id, User)
            .join(User, User.id == GroupMember.user_id)
            .where(GroupMember.group_id.in_([group.id for group, _ in rows]))
            .order_by(User.username)
        ):
            people.setdefault(group_id, []).append(user)
    return [SpaceGroup(group, SpaceRole(role), people.get(group.id, [])) for group, role in rows]


def memberships(session: Session, actor: Actor, org: Organization) -> dict[UUID, list[str]]:
    """Names of the groups of each member of the organization (account id → names)."""
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    found: dict[UUID, list[str]] = {}
    for user_id, name in session.execute(
        select(GroupMember.user_id, Group.name)
        .join(Group, Group.id == GroupMember.group_id)
        .where(GroupMember.org_id == org.id)
        .order_by(func.lower(Group.name))
    ):
        found.setdefault(user_id, []).append(name)
    return found
