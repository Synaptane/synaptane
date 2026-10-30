"""Single access rule: who can do what on the instance, an organization or a space.

Every interface (web, REST, MCP) and every service goes through `can` / `require`.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from synaptane.core.errors import forbidden


class OrgRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class SpaceRole(StrEnum):
    READER = "reader"
    CONTRIBUTOR = "contributor"


class Action(StrEnum):
    # Instance
    MANAGE_INSTANCE = "manage_instance"  # accounts, organizations, organization admins
    # Organization
    VIEW_ORG = "view_org"
    MANAGE_ORG = "manage_org"  # spaces, members, AI keys, webhooks
    # Space
    READ_SPACE = "read_space"  # read, search
    DEPOSIT_SPACE = "deposit_space"  # drop, correct, move or remove notes in the inbox
    CONTRIBUTE_SPACE = "contribute_space"  # direct changes, manual tidy-up (and the inbox)


@dataclass(frozen=True)
class Access:
    """Snapshot of an account's permissions, loaded once per request."""

    user_id: UUID
    is_superadmin: bool = False
    org_roles: dict[UUID, OrgRole] = field(default_factory=dict)
    space_roles: dict[UUID, SpaceRole] = field(default_factory=dict)
    inbox_only: bool = False  # connection limited to the inbox: a contributor drops notes only

    def is_org_admin(self, org_id: UUID) -> bool:
        return self.is_superadmin or self.org_roles.get(org_id) is OrgRole.ADMIN


@dataclass(frozen=True)
class Limits:
    """Restrictions of a token (personal or OAuth), chosen when it is created.

    `space_ids` None: all the account's spaces, including future ones. A restricted token
    (read-only or chosen spaces) administers nothing: no instance, no organization, no
    account (it cannot create a token broader than itself).
    """

    read_only: bool = False
    space_ids: frozenset[UUID] | None = None
    members_only: bool = False  # MCP: the super admin does not read the whole instance
    # Reading, and dropping notes in the inbox, but no direct change to pages: what an
    # assistant writes then goes through the nightly tidy-up.
    inbox_only: bool = False

    @property
    def restricted(self) -> bool:
        return self.read_only or self.inbox_only or self.space_ids is not None


def can(
    access: Access, action: Action, *, org_id: UUID | None = None, space_id: UUID | None = None
) -> bool:
    """For a space action, `org_id` is the organization that owns the space."""
    if access.is_superadmin:
        return True  # the super admin administers and reads everything (docs/concepts.md)
    match action:
        case Action.MANAGE_INSTANCE:
            return False
        case Action.VIEW_ORG:
            return org_id is not None and org_id in access.org_roles
        case Action.MANAGE_ORG:
            return org_id is not None and access.is_org_admin(org_id)
        case Action.READ_SPACE | Action.DEPOSIT_SPACE | Action.CONTRIBUTE_SPACE:
            if org_id is None or space_id is None:
                return False
            if action is Action.CONTRIBUTE_SPACE and access.inbox_only:
                return False
            if access.is_org_admin(org_id):
                return True
            role = access.space_roles.get(space_id)
            if action is Action.READ_SPACE:
                return role is not None
            return role is SpaceRole.CONTRIBUTOR


def require(
    access: Access, action: Action, *, org_id: UUID | None = None, space_id: UUID | None = None
) -> None:
    if not can(access, action, org_id=org_id, space_id=space_id):
        raise forbidden()
