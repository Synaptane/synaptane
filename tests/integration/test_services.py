from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole, SpaceRole
from synaptane.core.errors import SynaptaneError
from synaptane.services import Actor, accounts, organizations, spaces
from synaptane.services.workspace import Workspace
from synaptane.storage.models import AuditEvent, Organization, OrgMember, Space
from tests.integration.conftest import PASSWORD, actor_of, make_user


@dataclass
class World:
    root: Actor
    acme: Organization
    perso: Organization
    infra: Space
    brand: Space
    notes: Space


@pytest.fixture
def world(db: Session) -> World:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    acme = organizations.create_organization(db, root, slug="acme", name="Acme")
    perso = organizations.create_organization(db, root, slug="perso", name="Perso")
    return World(
        root=root,
        acme=acme,
        perso=perso,
        infra=spaces.create_space(db, root, acme, slug="infra", name="Infra"),
        brand=spaces.create_space(db, root, acme, slug="brand", name="Marque"),
        notes=spaces.create_space(db, root, perso, slug="notes", name="Notes"),
    )


def test_user_sees_only_spaces_with_a_role_across_organizations(db: Session, world: World) -> None:
    alice = make_user(db, "alice")
    spaces.set_member(db, world.root, world.infra, alice, SpaceRole.CONTRIBUTOR)
    spaces.set_member(db, world.root, world.notes, alice, SpaceRole.READER)
    actor = actor_of(db, alice)

    visible = {f"{s.organization.slug}/{s.slug}" for s in spaces.list_all_visible_spaces(db, actor)}
    assert visible == {"acme/infra", "perso/notes"}
    assert {o.slug for o in organizations.list_organizations(db, actor)} == {"acme", "perso"}
    # Being a member of a space makes one a plain member of its organization.
    member = db.get(OrgMember, (world.acme.id, alice.id))
    assert member is not None and member.role == "member"
    with pytest.raises(SynaptaneError) as error:
        spaces.get_space(db, actor, world.acme, "brand")
    assert error.value.code == "not_found"


def test_org_admin_manages_own_org_only(db: Session, world: World) -> None:
    bob = make_user(db, "bob")
    organizations.set_member(db, world.root, world.acme, bob, OrgRole.ADMIN)
    actor = actor_of(db, bob)

    space = spaces.create_space(db, actor, world.acme, slug="rh", name="RH")
    assert spaces.role_in(actor, space) is SpaceRole.CONTRIBUTOR
    assert len(spaces.list_spaces(db, actor, world.acme)) == 3
    with pytest.raises(SynaptaneError):
        organizations.get_organization(db, actor, "perso")
    with pytest.raises(SynaptaneError) as error:
        organizations.create_organization(db, actor, slug="new", name="New")
    assert error.value.code == "forbidden"


def test_removing_org_member_removes_space_roles(db: Session, world: World) -> None:
    alice = make_user(db, "alice")
    spaces.set_member(db, world.root, world.infra, alice, SpaceRole.READER)
    organizations.remove_member(db, world.root, world.acme, alice)
    assert spaces.list_all_visible_spaces(db, actor_of(db, alice)) == []


def test_conflicts_and_validation(db: Session, world: World) -> None:
    with pytest.raises(SynaptaneError) as error:
        spaces.create_space(db, world.root, world.acme, slug="infra", name="Doublon")
    assert error.value.code == "conflict"
    # The same slug in another organization is allowed.
    spaces.create_space(db, world.root, world.perso, slug="infra", name="Infra perso")
    for url in ("ftp://example.com/x.git", "https://user:token@github.com/a/b.git"):
        with pytest.raises(SynaptaneError):
            spaces.create_space(db, world.root, world.acme, slug="x", name="X", repo_url=url)
    with pytest.raises(SynaptaneError):
        organizations.delete_organization(db, world.root, world.acme)


def test_password_change_revokes_tokens_and_sessions(db: Session) -> None:
    alice = make_user(db, "alice")
    actor = actor_of(db, alice)
    token, _ = accounts.create_api_token(db, actor, "cli")
    session_token, _ = accounts.open_web_session(db, alice, 30)
    found = accounts.resolve_api_token(db, token)
    assert found is not None and found[0] == alice

    with pytest.raises(SynaptaneError):
        accounts.set_password(db, actor, alice, "un nouveau mot de passe", current="faux")
    accounts.set_password(db, actor, alice, "un nouveau mot de passe", current=PASSWORD)
    assert accounts.resolve_api_token(db, token) is None
    assert accounts.resolve_web_session(db, session_token) is None
    assert accounts.authenticate(db, "alice", "un nouveau mot de passe") == alice


def test_inactive_user_cannot_authenticate(db: Session) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    alice = make_user(db, "alice")
    accounts.update_user(db, root, alice, is_active=False)
    assert accounts.authenticate(db, "alice", PASSWORD) is None
    with pytest.raises(SynaptaneError):
        accounts.update_user(db, root, root.user, is_superadmin=False)


def test_actions_are_audited(db: Session, world: World) -> None:
    actions = set(db.scalars(select(AuditEvent.action)))
    assert {"user.created", "org.created", "space.created"} <= actions


def test_doctor_detects_a_replaced_master_key(db: Session, ctx: Workspace) -> None:
    from synaptane.core.security import SecretBox
    from synaptane.services.secrets import put_org_secret, readable

    root = actor_of(db, make_user(db, "root", superadmin=True))
    org = organizations.create_organization(db, root, slug="acme", name="Acme")
    assert readable(db, ctx.box) is None  # no secret
    put_org_secret(db, ctx.box, org.id, "ai", "sk-test", root.user.id)
    db.flush()
    assert readable(db, ctx.box) is True
    assert readable(db, SecretBox(SecretBox.generate_key())) is False
