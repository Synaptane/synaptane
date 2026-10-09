"""Invitations: an organization admin lets someone in without the super-admin.

The invitation carries the role in the organization and, optionally, roles in its spaces. If
the address is the verified address of an existing account, access is granted at once.
Otherwise, a single-use link (14 days) lets the person create their account; they choose
their password, and the address counts as verified since the link was received there.

Each invitation sends an e-mail to an address chosen by the admin: at most `PER_HOUR` per
organization per hour.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.core.access import Action, OrgRole, SpaceRole, require
from synaptane.core.errors import conflict, invalid, not_found, too_many
from synaptane.core.security import new_token, token_hash
from synaptane.services import Actor, accounts, audit, organizations, spaces
from synaptane.storage.models import AuditEvent, Invitation, Organization, Space, User

TTL = timedelta(days=14)
PER_HOUR = 30
# Counted in the audit log, not in the invitations: re-inviting or revoking deletes the
# invitation row, not the e-mail already sent.
SENT = ("org.invitation_created", "org.member_invited")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Created:
    invitation: Invitation | None  # None: existing account, access granted directly
    token: str | None
    existing: User | None


def _apply(
    session: Session,
    actor: Actor,
    org: Organization,
    user: User,
    org_role: OrgRole,
    space_roles: dict[str, str],
) -> None:
    organizations.set_member(session, actor, org, user, org_role)
    for space_id, role in space_roles.items():
        space = session.get(Space, UUID(space_id))
        if space is not None and space.org_id == org.id:
            spaces.set_member(session, actor, space, user, SpaceRole(role))


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
) -> Created:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    email = email.strip().lower()
    if "@" not in email or len(email) > 320 or " " in email:
        raise invalid("Invalid email address.")
    space_roles = dict(space_roles or {})
    for space_id, role in space_roles.items():
        space = session.get(Space, UUID(space_id))
        if space is None or space.org_id != org.id or role not in (r.value for r in SpaceRole):
            raise invalid("Invalid space or role.")
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
            raise conflict("This account is disabled: a super-admin must reactivate it.")
        _apply(session, actor, org, existing, org_role, space_roles)
        audit(session, actor, "org.member_invited", org_id=org.id, username=existing.username)
        return Created(None, None, existing)
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
    audit(session, actor, "org.invitation_created", org_id=org.id, email=email)
    return Created(invitation, token, None)


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
    # link proves the address, all of them are honoured.
    others = session.scalars(
        select(Invitation).where(
            Invitation.email == invitation.email,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > _now(),
        )
    )
    for pending_invitation in {invitation, *others}:
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


def describe(session: Session, invitation: Invitation) -> tuple[str, list[tuple[str, str]]]:
    """Who invites, and which spaces with which role (for the invitation page)."""
    inviter = session.get(User, invitation.invited_by) if invitation.invited_by else None
    granted = []
    for space_id, role in invitation.space_roles.items():
        space = session.get(Space, UUID(space_id))
        if space is not None:
            granted.append((space.name, role))
    return (inviter.display_name if inviter else ""), granted
