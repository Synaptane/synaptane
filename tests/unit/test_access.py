from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint

from synaptane.core.access import (
    DEFAULT_ROLES,
    NO_DEFAULT_ROLE,
    Access,
    Action,
    OrgRole,
    SpaceRole,
    can,
    effective_role,
    higher_role,
    highest_role,
    require,
)
from synaptane.core.errors import SynaptaneError
from synaptane.storage.models import Organization

ORG, OTHER_ORG = uuid4(), uuid4()
SPACE, OTHER_SPACE = uuid4(), uuid4()


def access(**kwargs: object) -> Access:
    return Access(user_id=uuid4(), **kwargs)  # type: ignore[arg-type]


def test_superadmin_can_do_everything_everywhere() -> None:
    admin = access(is_superadmin=True)
    for action in Action:
        assert can(admin, action, org_id=OTHER_ORG, space_id=OTHER_SPACE)


def test_org_admin_manages_and_contributes_only_in_own_org() -> None:
    org_admin = access(org_roles={ORG: OrgRole.ADMIN})
    assert can(org_admin, Action.MANAGE_ORG, org_id=ORG)
    assert can(org_admin, Action.CONTRIBUTE_SPACE, org_id=ORG, space_id=SPACE)
    assert not can(org_admin, Action.MANAGE_INSTANCE)
    assert not can(org_admin, Action.VIEW_ORG, org_id=OTHER_ORG)
    assert not can(org_admin, Action.READ_SPACE, org_id=OTHER_ORG, space_id=OTHER_SPACE)


def test_org_member_sees_org_but_only_spaces_with_a_role() -> None:
    member = access(org_roles={ORG: OrgRole.MEMBER}, space_roles={SPACE: SpaceRole.READER})
    assert can(member, Action.VIEW_ORG, org_id=ORG)
    assert not can(member, Action.MANAGE_ORG, org_id=ORG)
    assert can(member, Action.READ_SPACE, org_id=ORG, space_id=SPACE)
    assert not can(member, Action.READ_SPACE, org_id=ORG, space_id=OTHER_SPACE)


@pytest.mark.parametrize(
    ("role", "read", "contribute"),
    [(SpaceRole.READER, True, False), (SpaceRole.CONTRIBUTOR, True, True), (None, False, False)],
)
def test_space_roles(role: SpaceRole | None, read: bool, contribute: bool) -> None:
    roles = {SPACE: role} if role else {}
    user = access(org_roles={ORG: OrgRole.MEMBER}, space_roles=roles)
    assert can(user, Action.READ_SPACE, org_id=ORG, space_id=SPACE) is read
    assert can(user, Action.CONTRIBUTE_SPACE, org_id=ORG, space_id=SPACE) is contribute


def test_space_action_without_target_is_denied() -> None:
    user = access(space_roles={SPACE: SpaceRole.CONTRIBUTOR})
    assert not can(user, Action.READ_SPACE, space_id=SPACE)
    assert not can(user, Action.MANAGE_ORG)


def test_require_raises_forbidden() -> None:
    with pytest.raises(SynaptaneError) as error:
        require(access(), Action.MANAGE_INSTANCE)
    assert error.value.code == "forbidden"


def test_inbox_only_connection_deposits_but_does_not_contribute() -> None:
    """Reading and the inbox: a contributor's connection drops notes, changes no page; a
    reader drops nothing, limit or not."""
    where = {"org_id": ORG, "space_id": SPACE}
    full = access(space_roles={SPACE: SpaceRole.CONTRIBUTOR})
    assert can(full, Action.DEPOSIT_SPACE, **where) and can(full, Action.CONTRIBUTE_SPACE, **where)
    limited = access(space_roles={SPACE: SpaceRole.CONTRIBUTOR}, inbox_only=True)
    assert can(limited, Action.READ_SPACE, **where)
    assert can(limited, Action.DEPOSIT_SPACE, **where)
    assert not can(limited, Action.CONTRIBUTE_SPACE, **where)
    reader = access(space_roles={SPACE: SpaceRole.READER})
    assert not can(reader, Action.DEPOSIT_SPACE, **where)


R, C = SpaceRole.READER, SpaceRole.CONTRIBUTOR


@pytest.mark.parametrize(
    ("first", "second", "higher"),
    [(None, None, None), (R, None, R), (None, C, C), (R, C, C), (C, R, C), (R, R, R)],
)
def test_higher_role(
    first: SpaceRole | None, second: SpaceRole | None, higher: SpaceRole | None
) -> None:
    assert higher_role(first, second) is higher


@pytest.mark.parametrize(
    ("explicit", "org_default", "restricted", "effective"),
    [
        # No default role: only the role given in the space (behavior before 1.12).
        (None, "none", False, None),
        (R, "none", False, R),
        (C, "none", False, C),
        # The higher of the given role and the default role.
        (None, "reader", False, R),
        (R, "reader", False, R),
        (C, "reader", False, C),
        (None, "contributor", False, C),
        (R, "contributor", False, C),
        (C, "contributor", False, C),
        # A restricted space ignores the default role.
        (None, "reader", True, None),
        (None, "contributor", True, None),
        (R, "contributor", True, R),
        (C, "reader", True, C),
    ],
)
def test_effective_role(
    explicit: SpaceRole | None, org_default: str, restricted: bool, effective: SpaceRole | None
) -> None:
    assert effective_role(explicit, org_default, restricted) is effective


RANK = {None: 0, R: 1, C: 2}


@pytest.mark.parametrize("restricted", [False, True], ids=["open", "restricted"])
@pytest.mark.parametrize("org_default", DEFAULT_ROLES)
@pytest.mark.parametrize("group", [None, R, C], ids=["no-group", "group-R", "group-C"])
@pytest.mark.parametrize("explicit", [None, R, C], ids=["none", "given-R", "given-C"])
def test_effective_role_every_combination(
    explicit: SpaceRole | None, group: SpaceRole | None, org_default: str, restricted: bool
) -> None:
    """The highest of the role given in the space, the groups' role, and the default role
    (ignored by a restricted space, unlike a group's role)."""
    candidates = [explicit, group]
    if not restricted and org_default != NO_DEFAULT_ROLE:
        candidates.append(SpaceRole(org_default))
    expected = max(candidates, key=lambda role: RANK[role])
    assert effective_role(explicit, org_default, restricted, group) is expected


@pytest.mark.parametrize(
    ("explicit", "group", "org_default", "restricted", "effective"),
    [
        # A group opens a restricted space (human resources, management)…
        (None, R, "none", True, R),
        (None, C, "contributor", True, C),
        # …and raises a role given in it, never lowers it.
        (R, C, "none", True, C),
        (C, R, "none", False, C),
        # On a space that is not restricted, the default role can already give more.
        (None, R, "contributor", False, C),
        (None, C, "reader", False, C),
        # Without a group, as before.
        (None, None, "reader", True, None),
    ],
)
def test_effective_role_with_a_group(
    explicit: SpaceRole | None,
    group: SpaceRole | None,
    org_default: str,
    restricted: bool,
    effective: SpaceRole | None,
) -> None:
    assert effective_role(explicit, org_default, restricted, group) is effective


def test_highest_role() -> None:
    assert highest_role([]) is None
    assert highest_role([None, None]) is None
    assert highest_role([R, None, R]) is R
    assert highest_role(iter([R, C, R])) is C


@pytest.mark.parametrize("restricted", [False, True])
@pytest.mark.parametrize("org_default", DEFAULT_ROLES)
def test_an_org_admin_contributes_whatever_the_groups(org_default: str, restricted: bool) -> None:
    """An organization admin needs no group nor role: `can` lets them contribute; a group's
    role recorded for them changes nothing."""
    where = {"org_id": ORG, "space_id": SPACE}
    for given in (None, R):
        roles = {SPACE: role} if (role := effective_role(given, org_default, restricted, R)) else {}
        admin = access(org_roles={ORG: OrgRole.ADMIN}, space_roles=roles)
        assert can(admin, Action.CONTRIBUTE_SPACE, **where)
        assert can(admin, Action.MANAGE_ORG, org_id=ORG)
        assert not can(admin, Action.READ_SPACE, org_id=OTHER_ORG, space_id=OTHER_SPACE)


def test_default_roles_match_the_database_constraint() -> None:
    [check] = [
        c
        for c in Organization.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(c, CheckConstraint) and c.name == "ck_organizations_default_space_role"
    ]
    listed = ", ".join(f"'{role}'" for role in DEFAULT_ROLES)
    assert str(check.sqltext) == f"default_space_role IN ({listed})"
    assert DEFAULT_ROLES[0] == NO_DEFAULT_ROLE
