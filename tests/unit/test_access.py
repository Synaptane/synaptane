from uuid import uuid4

import pytest

from synaptane.core.access import Access, Action, OrgRole, SpaceRole, can, require
from synaptane.core.errors import SynaptaneError

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
