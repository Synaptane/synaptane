"""Invitations: an organization admin lets someone in without the super-admin.

The invitation carries the role in the organization and, optionally, roles in its spaces and
groups of the organization to join. If the address is the verified address of an existing
account, access is granted at once.
Otherwise, a single-use link (14 days) lets the person create their account; they choose
their password, and the address counts as verified since the link was received there.

Each invitation sends an email to an address chosen by the admin: at most `PER_HOUR` per
organization per hour.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.core.access import (
    Action,
    OrgRole,
    SpaceRole,
    effective_role,
    highest_role,
    require,
)
from synaptane.core.errors import conflict, invalid, not_found, too_many
from synaptane.core.security import new_token, token_hash
from synaptane.services import Actor, accounts, audit, groups, organizations, spaces
from synaptane.storage.models import (
    AuditEvent,
    Group,
    GroupSpaceRole,
    Invitation,
    InvitationGroup,
    Organization,
    Space,
    User,
)

TTL = timedelta(days=14)
PER_HOUR = 30
# Counted in the audit log, not in the invitations: re-inviting or revoking deletes the
# invitation row, not the email already sent.
SENT = ("org.invitation_created", "org.member_invited")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Created:
    invitation: Invitation | None  # None: existing account, access granted directly
    token: str | None
    existing: User | None
    groups: list[Group] = field(default_factory=list)  # to join, by name


def _apply(
    session: Session,
    actor: Actor,
    org: Organization,
    user: User,
    org_role: OrgRole,
    space_roles: dict[str, str],
    joined: Iterable[Group] = (),
) -> None:
    """Member of the organization first (only its members join its groups), then the roles in
    its spaces and the groups. Already in a group: nothing changes, nothing is logged."""
    organizations.set_member(session, actor, org, user, org_role)
    for space_id, role in space_roles.items():
        space = session.get(Space, UUID(space_id))
        if space is not None and space.org_id == org.id:
            spaces.set_member(session, actor, space, user, SpaceRole(role))
    session.flush()  # the membership, which the groups' foreign key refers to
    for group in joined:
        if group.org_id == org.id:
            groups.add_member(session, actor, group, user)


def _chosen_groups(
    session: Session, org: Organization, group_ids: Iterable[str | UUID]
) -> list[Group]:
    """The groups chosen for an invitation, by name: all of the organization (a group of
    another one, or deleted in the meantime, is refused)."""
    wanted: set[UUID] = set()
    for value in group_ids:
        try:
            wanted.add(value if isinstance(value, UUID) else UUID(str(value).strip()))
        except ValueError:
            raise not_found("Group not found in this organization.") from None
    if not wanted:
        return []
    found = list(
        session.scalars(
            select(Group)
            .where(Group.id.in_(wanted), Group.org_id == org.id)
            .order_by(func.lower(Group.name))
        )
    )
    if len(found) != len(wanted):
        raise not_found("Group not found in this organization.")
    return found


def _names(joined: Iterable[Group]) -> dict[str, Any]:
    """The audit detail of the groups, their names at the time: none without groups (as
    before groups existed)."""
    names = ", ".join(group.name for group in joined)
    return {"groups": names} if names else {}


def invited_groups(session: Session, invitation_ids: Iterable[UUID]) -> dict[UUID, list[Group]]:
    """The groups that still exist of each invitation, by name (one query): a deleted group
    has left its invitations (ON DELETE CASCADE), and a renamed one shows its new name."""
    ids = list(invitation_ids)
    found: dict[UUID, list[Group]] = {}
    if not ids:
        return found
    for invitation_id, group in session.execute(
        select(InvitationGroup.invitation_id, Group)
        .join(Group, Group.id == InvitationGroup.group_id)
        .where(InvitationGroup.invitation_id.in_(ids))
        .order_by(func.lower(Group.name))
    ):
        found.setdefault(invitation_id, []).append(group)
    return found


def sent_last_hour(session: Session, org: Organization) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.org_id == org.id,
                AuditEvent.action.in_(SENT),
                AuditEvent.at > _now() - timedelta(hours=1),
            )
        )
        or 0
    )


def invite(
    session: Session,
    actor: Actor,
    org: Organization,
    *,
    email: str,
    org_role: OrgRole = OrgRole.MEMBER,
    space_roles: dict[str, str] | None = None,
    group_ids: Iterable[str | UUID] = (),
) -> Created:
    """`group_ids`: groups of the organization the person joins, right away (existing account)
    or on acceptance."""
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    email = email.strip().lower()
    if "@" not in email or len(email) > 320 or " " in email:
        raise invalid("Invalid email address.")
    space_roles = dict(space_roles or {})
    for space_id, role in space_roles.items():
        space = session.get(Space, UUID(space_id))
        if space is None or space.org_id != org.id or role not in (r.value for r in SpaceRole):
            raise invalid("Invalid space or role.")
    joined = _chosen_groups(session, org, group_ids)
    if sent_last_hour(session, org) >= PER_HOUR:
        raise too_many(
            "Too many invitations sent in the last hour for this organization ({limit} at "
            "most): try again later.",
            limit=PER_HOUR,
        )
    # Only a verified address designates an account: a merely declared address grants no
    # access (otherwise, putting it on one's account would be enough to receive someone
    # else's invitation).
    existing = session.scalar(
        select(User).where(User.email == email, User.email_verified_at.is_not(None))
    )
    if existing is not None:
        if not existing.is_active:
            raise conflict("This account is disabled: a super admin must reactivate it.")
        _apply(session, actor, org, existing, org_role, space_roles, joined)
        audit(
            session,
            actor,
            "org.member_invited",
            org_id=org.id,
            username=existing.username,
            role=org_role,
            **_names(joined),
        )
        return Created(None, None, existing, joined)
    # A single pending invitation per address and organization: re-inviting renews it.
    for previous in session.scalars(
        select(Invitation).where(
            Invitation.org_id == org.id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
        )
    ):
        session.delete(previous)
    token = new_token("inv_")
    invitation = Invitation(
        token_hash=token_hash(token),
        email=email,
        org_id=org.id,
        org_role=org_role.value,
        space_roles=space_roles,
        invited_by=actor.user.id,
        expires_at=_now() + TTL,
    )
    session.add(invitation)
    session.flush()
    session.add_all(
        InvitationGroup(invitation_id=invitation.id, group_id=group.id, org_id=org.id)
        for group in joined
    )
    session.flush()
    audit(
        session,
        actor,
        "org.invitation_created",
        org_id=org.id,
        email=email,
        role=org_role,
        **_names(joined),
    )
    return Created(invitation, token, None, joined)


def pending(session: Session, actor: Actor, org: Organization) -> list[Invitation]:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    return list(
        session.scalars(
            select(Invitation)
            .where(
                Invitation.org_id == org.id,
                Invitation.accepted_at.is_(None),
                Invitation.expires_at > _now(),
            )
            .order_by(Invitation.created_at.desc())
        )
    )


def revoke(session: Session, actor: Actor, org: Organization, invitation_id: UUID) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    invitation = session.get(Invitation, invitation_id)
    if invitation is None or invitation.org_id != org.id:
        raise not_found("Invitation not found.")
    session.delete(invitation)
    audit(session, actor, "org.invitation_revoked", org_id=org.id, email=invitation.email)


def find(session: Session, token: str) -> Invitation:
    invitation = session.scalar(
        select(Invitation).where(Invitation.token_hash == token_hash(token))
    )
    if invitation is None or invitation.accepted_at is not None or invitation.expires_at < _now():
        raise invalid("This invitation is no longer valid: ask for a new one.")
    return invitation


def accept(
    session: Session, token: str, *, username: str, display_name: str, password: str, locale: str
) -> User:
    """Creates the invited account (in the language of its invitation page) and grants its
    access."""
    invitation = find(session, token)
    if session.scalar(
        select(User).where(User.email == invitation.email, User.email_verified_at.is_not(None))
    ):
        raise conflict("An account already exists for this address: sign in.")
    user = accounts.create_user(
        session,
        None,
        username=username,
        display_name=display_name,
        password=password,
        email=invitation.email,
        locale=locale,
    )
    # This invitation, and the others pending for the same address (other organizations): the
    # link proves the address, all of them are honored.
    others = session.scalars(
        select(Invitation).where(
            Invitation.email == invitation.email,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > _now(),
        )
    )
    honored = {invitation, *others}
    joined = invited_groups(session, (i.id for i in honored))
    for pending_invitation in honored:
        org = session.get(Organization, pending_invitation.org_id)
        if org is None:
            continue
        _apply(
            session,
            _system_actor(org),
            org,
            user,
            OrgRole(pending_invitation.org_role),
            dict(pending_invitation.space_roles),
            joined.get(pending_invitation.id, []),
        )
        pending_invitation.accepted_at, pending_invitation.accepted_by = _now(), user.id
        audit(session, None, "org.invitation_accepted", org_id=org.id, username=user.username)
    return user


def _system_actor(org: Organization) -> Actor:
    """The invitation, signed by an admin, stands as authorization for that access."""
    from synaptane.core.access import Access

    return Actor(
        user=User(username="synaptane"),
        access=Access(user_id=UUID(int=0), org_roles={org.id: OrgRole.ADMIN}),
    )


@dataclass(frozen=True)
class Granted:
    """The access an invitation gives to one space."""

    space: str  # name
    role: SpaceRole
    # Where it comes from: "given" (in the invitation), "group", "default" (the organization's
    # default role) or "admin" (an administrator contributes everywhere).
    source: str
    groups: tuple[str, ...] = ()  # source "group": the groups that give this role, by name


@dataclass(frozen=True)
class Description:
    inviter: str  # display name, "" if their account is gone
    granted: list[Granted]  # by space name
    groups: list[str]  # the groups to join, by name


def describe(session: Session, invitation: Invitation) -> Description:
    """Who invites, and the access the invitation actually gives (for the invitation page):
    for each space, the highest of the role given in the invitation, of the roles of its
    groups and of the organization's default role (which a restricted space ignores; a group
    opens it), with where it comes from; an administrator contributes everywhere."""
    inviter = session.get(User, invitation.invited_by) if invitation.invited_by else None
    org = session.get(Organization, invitation.org_id)
    joined = invited_groups(session, [invitation.id]).get(invitation.id, [])
    granted: list[Granted] = []
    if org is not None:
        through: dict[UUID, list[tuple[str, SpaceRole]]] = {}
        if joined:
            names = {group.id: group.name for group in joined}
            for group_id, space_id, value in session.execute(
                select(GroupSpaceRole.group_id, GroupSpaceRole.space_id, GroupSpaceRole.role).where(
                    GroupSpaceRole.group_id.in_(names)
                )
            ):
                through.setdefault(space_id, []).append((names[group_id], SpaceRole(value)))
        given = invitation.space_roles
        for space in session.scalars(
            select(Space).where(Space.org_id == org.id).order_by(Space.name)
        ):
            if invitation.org_role == OrgRole.ADMIN.value:
                granted.append(Granted(space.name, SpaceRole.CONTRIBUTOR, "admin"))
                continue
            explicit = SpaceRole(given[str(space.id)]) if str(space.id) in given else None
            by_groups = sorted(through.get(space.id, []), key=lambda item: item[0].lower())
            group = highest_role(role for _, role in by_groups)
            role = effective_role(explicit, org.default_space_role, space.restricted, group)
            if role is None:
                continue
            if role == explicit:
                granted.append(Granted(space.name, role, "given"))
            elif role == group:
                via = tuple(name for name, found in by_groups if found == role)
                granted.append(Granted(space.name, role, "group", via))
            else:
                granted.append(Granted(space.name, role, "default"))
    return Description(
        inviter.display_name if inviter else "", granted, [group.name for group in joined]
    )
