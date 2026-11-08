"""Groups of members: a role on several spaces at once, beyond the organization's default role.

A person's effective role on a space is the highest of the role given in the space, the roles
of their groups on it and the default role (which a restricted space ignores; a group opens
it). Organization admins keep every right, a token's limits still apply, and nothing ever
crosses organizations. Checked from every interface: services, web, search (semantic too),
REST, MCP, routine emails, review reminders.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole, SpaceRole
from synaptane.core.errors import SynaptaneError
from synaptane.services import (
    accounts,
    content,
    freshness,
    groups,
    load_actor,
    organizations,
    spaces,
)
from synaptane.services import search as search_service
from synaptane.services.routine_agent import recipients
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import (
    AuditEvent,
    Group,
    GroupMember,
    GroupSpaceRole,
    OrgMember,
    Space,
)
from tests.htmlutil import data_test, select_one
from tests.htmlutil import select as select_html
from tests.integration.conftest import make_user, token_for
from tests.integration.teamdata import DEFAULTS, RANK, Team, configure, role
from tests.integration.test_alert_mails import received, smtp  # noqa: F401 — fixture
from tests.integration.test_invitations import Inbox
from tests.integration.test_mcp import Live, live, mcp_calls  # noqa: F401 — fixture
from tests.integration.test_rights_html import (
    as_user,
    assert_done,
    assert_refused,
    post,
    snapshot,
)
from tests.integration.test_semantic import ConceptEmbedder, enable, index


def make_group(
    db: Session,
    team: Team,
    name: str = "RH",
    *,
    members: tuple[str, ...] = ("dora",),
    roles: dict[str, str] | None = None,
) -> Group:
    """A group of Acme, created by the super admin: `roles`, space slug → role."""
    root = team.wiki.root
    acme = team.wiki.infra.organization
    group = groups.create_group(db, root, acme, name=name)
    for username in members:
        groups.add_member(db, root, group, accounts.get_user(db, username))
    for slug, given in (roles or {}).items():
        space = spaces.get_space(db, root, acme, slug)
        groups.set_space_role(db, root, group, space, SpaceRole(given))
    db.commit()
    return group


def expected(given: str | None, group: str | None, default: str, restricted: bool) -> str | None:
    candidates = [given, group]
    if not restricted and default != "none":
        candidates.append(default)
    return max(candidates, key=lambda r: RANK[r])


def bearer(database: Database, username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(database, username)}"}


# --- Access matrix ----------------------------------------------------------------------------


@pytest.mark.parametrize("group_role", ["reader", "contributor"])
@pytest.mark.parametrize("restricted", [False, True], ids=["normal", "restricted"])
@pytest.mark.parametrize("default", DEFAULTS)
def test_access_matrix_with_a_group(
    db: Session, team: Team, default: str, restricted: bool, group_role: str
) -> None:
    """Dora (added nowhere) and Bob (reader of infra) in a group with a role on both Acme
    spaces; Alice (contributor of infra) outside it."""
    configure(db, team, default, restricted=restricted)
    make_group(db, team, members=("dora", "bob"), roles={"infra": group_role, "autre": group_role})
    infra, other, ventes = team.wiki.infra, team.wiki.other, team.ventes
    given = {
        "dora": (None, None, group_role),
        "bob": ("reader", None, group_role),
        "alice": ("contributor", None, None),
    }
    for username, (on_infra, on_other, through) in given.items():
        assert role(db, username, infra) == expected(on_infra, through, default, restricted)
        assert role(db, username, other) == expected(on_other, through, default, restricted)
        assert role(db, username, ventes) is None  # never another organization
    assert role(db, "carol", infra) == role(db, "carol", other) == "contributor"
    assert role(db, "eve", infra) is None and role(db, "eve", ventes) == "contributor"
    # The same answer from the services every interface calls: a group always opens.
    dora = load_actor(db, accounts.get_user(db, "dora"))
    assert {s.address for s in spaces.list_all_visible_spaces(db, dora)} == {
        "acme/infra",
        "acme/autre",
    }
    assert spaces.role_in(dora, other) == expected(None, group_role, default, restricted)


def test_a_group_opens_a_restricted_space_to_its_members_only(db: Session, team: Team) -> None:
    configure(db, team, "reader", restricted=True)
    make_group(db, team, "Direction", members=("dora",), roles={"autre": "reader"})
    assert role(db, "dora", team.wiki.other) == "reader"
    assert role(db, "dora", team.wiki.infra) is None  # restricted, and not given to the group
    assert role(db, "bob", team.wiki.other) is None  # outside the group
    assert role(db, "bob", team.wiki.infra) == "reader"  # his own role, as before


def test_the_highest_of_several_groups(db: Session, team: Team) -> None:
    configure(db, team, "none", restricted=True)
    make_group(db, team, "Lecteurs", members=("dora",), roles={"autre": "reader"})
    writers = make_group(db, team, "Auteurs", members=("dora",), roles={"autre": "contributor"})
    assert role(db, "dora", team.wiki.other) == "contributor"
    groups.remove_member(db, team.wiki.root, writers, accounts.get_user(db, "dora"))
    db.commit()
    assert role(db, "dora", team.wiki.other) == "reader"


def test_a_group_never_lowers_a_role(db: Session, team: Team) -> None:
    make_group(db, team, members=("alice",), roles={"infra": "reader"})
    assert role(db, "alice", team.wiki.infra) == "contributor"


def test_loading_the_permissions_takes_no_query_per_space(db: Session, team: Team) -> None:
    """One query for the roles given in the spaces and through the groups, however many."""

    @contextmanager
    def counted() -> Iterator[list[str]]:
        statements: list[str] = []

        def count(*args: object) -> None:
            statements.append(str(args[2]))

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", count)
        try:
            yield statements
        finally:
            event.remove(engine, "before_cursor_execute", count)

    configure(db, team, "reader")
    dora = accounts.get_user(db, "dora")
    with counted() as before:
        load_actor(db, dora)
    acme, root = team.wiki.infra.organization, team.wiki.root
    for number in range(6):
        spaces.create_space(db, root, acme, slug=f"s{number}", name=f"S{number}", restricted=True)
    db.commit()
    for number in range(3):
        make_group(
            db,
            team,
            f"G{number}",
            members=("dora",),
            roles={f"s{number}": "reader", f"s{number + 3}": "contributor", "autre": "reader"},
        )
    db.expire_all()
    dora = accounts.get_user(db, "dora")
    with counted() as after:
        actor = load_actor(db, dora)
    assert len(after) == len(before), after
    assert len(actor.access.space_roles) == 2 + 6


# --- Rules of membership ------------------------------------------------------------------------


def test_only_members_of_the_organization_join_its_groups(db: Session, team: Team) -> None:
    group = make_group(db, team, members=())
    eve = accounts.get_user(db, "eve")  # Globex only
    with pytest.raises(SynaptaneError) as error:
        groups.add_member(db, team.wiki.root, group, eve)
    assert error.value.code == "invalid"
    assert "n'est pas membre" in error.value.template or "not a member" in error.value.message
    # The database refuses it too.
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(GroupMember(group_id=group.id, org_id=group.org_id, user_id=eve.id))
        db.flush()
    db.rollback()
    assert groups.add_member(db, team.wiki.root, group, accounts.get_user(db, "dora")) is True
    assert groups.add_member(db, team.wiki.root, group, accounts.get_user(db, "dora")) is False


def test_a_group_has_roles_only_on_the_spaces_of_its_organization(db: Session, team: Team) -> None:
    root = team.wiki.root
    globex = team.ventes.organization
    foreign = groups.create_group(db, root, globex, name="Commerciaux")
    db.commit()
    with pytest.raises(SynaptaneError) as error:
        groups.set_space_role(db, root, foreign, team.wiki.infra, SpaceRole.READER)
    assert error.value.code == "invalid"
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(
            GroupSpaceRole(
                group_id=foreign.id, space_id=team.wiki.infra.id, org_id=globex.id, role="reader"
            )
        )
        db.flush()
    db.rollback()
    # Nor through Acme's address: the group is not found there.
    with pytest.raises(SynaptaneError) as missing:
        groups.get_group(db, root, team.wiki.infra.organization, foreign.id)
    assert missing.value.code == "not_found"


def test_group_names_are_unique_in_the_organization(db: Session, team: Team) -> None:
    root, acme = team.wiki.root, team.wiki.infra.organization
    group = groups.create_group(db, root, acme, name="  Ressources   humaines ")
    assert group.name == "Ressources humaines"
    for taken in ("Ressources humaines", "ressources HUMAINES"):
        with pytest.raises(SynaptaneError) as error:
            groups.create_group(db, root, acme, name=taken)
        assert error.value.code == "conflict"
    other = groups.create_group(db, root, acme, name="Direction")
    with pytest.raises(SynaptaneError):
        groups.update_group(db, root, other, name="RESSOURCES humaines")
    groups.update_group(db, root, group, name="ressources humaines")  # its own name: allowed
    assert group.name == "ressources humaines"
    for empty in ("", "   ", "x" * 121):
        with pytest.raises(SynaptaneError) as invalid:
            groups.create_group(db, root, acme, name=empty)
        assert invalid.value.code == "invalid"
    # Another organization may use the same name.
    assert groups.create_group(db, root, team.ventes.organization, name="Direction").id
    with pytest.raises(SynaptaneError) as unknown:
        groups.get_group(db, root, acme, "not-a-uuid")
    assert unknown.value.code == "not_found"


# --- Cascades -----------------------------------------------------------------------------------


def test_leaving_the_organization_leaves_its_groups(db: Session, team: Team) -> None:
    configure(db, team, "none", restricted=True)
    group = make_group(db, team, members=("dora", "bob"), roles={"autre": "contributor"})
    acme = team.wiki.infra.organization
    organizations.remove_member(db, team.wiki.root, acme, accounts.get_user(db, "dora"))
    db.commit()
    assert role(db, "dora", team.wiki.other) is None
    members = [u.username for u in groups.list_members(db, team.wiki.root, group)]
    assert members == ["bob"]
    # Back in the organization: not back in the group.
    organizations.set_member(
        db, team.wiki.root, acme, accounts.get_user(db, "dora"), OrgRole.MEMBER
    )
    db.commit()
    assert role(db, "dora", team.wiki.other) is None
    # The database does it as well, whoever deletes the membership.
    db.execute(
        text("DELETE FROM org_members WHERE user_id = :user"),
        {"user": accounts.get_user(db, "bob").id},
    )
    db.commit()
    assert db.scalar(select(func.count()).select_from(GroupMember)) == 0


def test_deleting_a_group_removes_the_access_it_gave(db: Session, team: Team) -> None:
    configure(db, team, "none", restricted=True)
    group = make_group(db, team, members=("dora",), roles={"autre": "contributor"})
    groups.delete_group(db, team.wiki.root, group)
    db.commit()
    assert role(db, "dora", team.wiki.other) is None
    assert db.scalar(select(func.count()).select_from(GroupSpaceRole)) == 0
    assert db.scalar(select(func.count()).select_from(GroupMember)) == 0
    assert db.get(OrgMember, (team.wiki.infra.org_id, accounts.get_user(db, "dora").id))


def test_deleting_a_space_removes_the_roles_of_groups_on_it(db: Session, team: Team) -> None:
    group = make_group(db, team, members=("dora",), roles={"autre": "reader", "infra": "reader"})
    spaces.delete_space(db, team.wiki.root, team.wiki.other)
    db.commit()
    db.expire_all()
    left = groups.list_spaces(db, team.wiki.root, db.get(Group, group.id))  # type: ignore[arg-type]
    assert [(s.slug, r) for s, r in left] == [("infra", SpaceRole.READER)]
    assert [u.username for u in groups.list_members(db, team.wiki.root, group)] == ["dora"]


def test_deleting_the_organization_deletes_its_groups(db: Session, team: Team) -> None:
    root, globex = team.wiki.root, team.ventes.organization
    sales = groups.create_group(db, root, globex, name="Commerciaux")
    groups.add_member(db, root, sales, accounts.get_user(db, "eve"))
    groups.set_space_role(db, root, sales, team.ventes, SpaceRole.READER)
    db.commit()
    spaces.delete_space(db, root, team.ventes)
    organizations.delete_organization(db, root, globex)
    db.commit()
    assert db.scalar(select(func.count()).select_from(Group)) == 0
    assert db.scalar(select(func.count()).select_from(GroupMember)) == 0


# --- Limits of tokens and connections ----------------------------------------------------------


def test_token_limits_still_apply_to_group_roles(
    db: Session, client: TestClient, team: Team
) -> None:
    configure(db, team, "none", restricted=True)
    make_group(db, team, members=("dora",), roles={"autre": "contributor", "infra": "reader"})
    dora = load_actor(db, accounts.get_user(db, "dora"))
    infra_only, _ = accounts.create_api_token(db, dora, "infra", space_ids=[team.wiki.infra.id])
    read_only, _ = accounts.create_api_token(db, dora, "lecture", read_only=True)
    inbox_only, _ = accounts.create_api_token(db, dora, "inbox", inbox_only=True)
    full, _ = accounts.create_api_token(db, dora, "tout")
    db.commit()
    base = "/api/v1/organizations/acme/spaces"

    def call(token: str, method: str, url: str, **kwargs: object) -> int:
        headers = {"Authorization": f"Bearer {token}"}
        return client.request(method, url, headers=headers, **kwargs).status_code  # type: ignore[arg-type]

    mine = client.get("/api/v1/spaces", headers={"Authorization": f"Bearer {infra_only}"})
    assert [(s["slug"], s["role"]) for s in mine.json()] == [("infra", "reader")]
    assert call(infra_only, "GET", f"{base}/autre/files/secret.md") == 404
    page = {"json": {"content": "# Note\n"}}
    assert call(full, "PUT", f"{base}/autre/files/dora.md", **page) == 200
    assert call(read_only, "GET", f"{base}/autre/files/secret.md") == 200
    assert call(read_only, "PUT", f"{base}/autre/files/lecture.md", **page) == 403
    assert call(inbox_only, "PUT", f"{base}/autre/files/inbox.md", **page) == 403
    note = {"json": {"title": "Note", "content": "Par l'inbox."}}
    assert call(inbox_only, "POST", f"{base}/autre/inbox", **note) == 201
    # A token chosen for a space opened by a group: refused once the group no longer opens it.
    group = db.scalar(select(Group))
    assert group is not None
    groups.remove_space_role(db, team.wiki.root, group, team.wiki.infra)
    db.commit()
    assert call(infra_only, "GET", f"{base}/infra/files/README.md") == 404
    # Choosing spaces for a token: those of the groups are the account's own.
    dora = load_actor(db, accounts.get_user(db, "dora"))
    assert accounts.checked_spaces(db, dora, [team.wiki.other.id]) == [str(team.wiki.other.id)]
    with pytest.raises(SynaptaneError):
        accounts.checked_spaces(db, dora, [team.wiki.infra.id])


# --- Web, search ------------------------------------------------------------------------------


def test_web_reading_search_and_editing_follow_groups(
    db: Session, client: TestClient, team: Team
) -> None:
    configure(db, team, "reader", restricted=True)
    group = make_group(db, team, members=("dora",), roles={"autre": "contributor"})
    as_user(client, "dora")
    page = client.get("/o/acme/autre/p/secret.md")
    assert page.status_code == 200 and "Sauvegarde confidentielle" in page.text
    assert "secret.md" in client.get("/search", params={"q": "confidentielle"}).text
    assert client.get("/o/acme/autre/edit/secret.md").status_code == 200
    assert "/o/acme/autre" in client.get("/").text  # in the navigation
    assert client.get("/o/acme/infra/p/README.md").status_code == 404  # not given to the group
    groups.remove_member(db, team.wiki.root, group, accounts.get_user(db, "dora"))
    db.commit()
    assert client.get("/o/acme/autre/p/secret.md").status_code == 404
    assert "secret.md" not in client.get("/search", params={"q": "confidentielle"}).text


def test_semantic_search_follows_groups(
    db: Session, database: Database, ctx: Workspace, team: Team
) -> None:
    wiki = team.wiki
    enable(db, ctx, wiki)
    embedder = ConceptEmbedder()
    index(database, ctx, wiki, embedder)
    configure(db, team, "reader", restricted=True)

    def found(username: str) -> list[tuple[str, str]]:
        actor = load_actor(db, accounts.get_user(db, username))
        hits = search_service.search(db, actor, ctx, "backup", embedder=embedder)
        return [(h.address, h.match) for h in hits]

    assert found("dora") == []
    group = make_group(db, team, members=("dora",), roles={"infra": "reader"})
    assert ("acme/infra", "meaning") in found("dora")  # no word in common: by meaning
    groups.delete_group(db, wiki.root, group)
    db.commit()
    assert found("dora") == []


# --- REST API ---------------------------------------------------------------------------------


def test_rest_api_manages_groups(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    carol = bearer(database, "carol")
    base = "/api/v1/organizations/acme/groups"
    assert client.get(base, headers=carol).json() == []
    created = client.post(
        base, json={"name": "RH", "description": "Ressources humaines"}, headers=carol
    )
    assert created.status_code == 201, created.text
    group = created.json()
    assert group["name"] == "RH" and group["members"] == [] and group["spaces"] == []
    url = f"{base}/{group['id']}"
    assert client.post(base, json={"name": "rh"}, headers=carol).status_code == 409
    added = client.put(f"{url}/members/dora", headers=carol)
    assert added.json() == {"username": "dora", "display_name": "Dora"}
    assert client.put(f"{url}/members/dora", headers=carol).status_code == 200  # idempotent
    outsider = client.put(f"{url}/members/eve", headers=carol)
    assert outsider.status_code == 400 and outsider.json()["error"] == "invalid"
    assert client.put(f"{url}/members/nobody", headers=carol).status_code == 404
    given = client.put(f"{url}/spaces/autre", json={"role": "contributor"}, headers=carol)
    assert given.json() == {
        "space": "autre",
        "name": "Autre",
        "restricted": False,
        "role": "contributor",
    }
    bad_role = client.put(f"{url}/spaces/autre", json={"role": "admin"}, headers=carol)
    assert bad_role.status_code == 422
    assert client.put(f"{url}/spaces/ventes", json={"role": "reader"}, headers=carol).status_code
    assert client.get(f"{url}/members", headers=carol).json() == [
        {"username": "dora", "display_name": "Dora"}
    ]
    assert [s["space"] for s in client.get(f"{url}/spaces", headers=carol).json()] == ["autre"]
    renamed = client.patch(url, json={"name": "Ressources humaines"}, headers=carol)
    assert renamed.json()["name"] == "Ressources humaines"
    assert renamed.json()["description"] == "Ressources humaines"
    whole = client.get(url, headers=carol).json()
    assert whole["members"] == [{"username": "dora", "display_name": "Dora"}]
    assert whole["spaces"][0]["role"] == "contributor"
    assert [g["id"] for g in client.get(base, headers=carol).json()] == [group["id"]]

    # Whoever reads the space sees the groups that access it, and through the group, Dora
    # reaches it; the members of the space keep their form.
    dora = bearer(database, "dora")
    space = "/api/v1/organizations/acme/spaces/autre"
    listed = client.get(f"{space}/groups", headers=dora).json()
    assert listed == [
        {
            "id": group["id"],
            "name": "Ressources humaines",
            "role": "contributor",
            "members": [{"username": "dora", "display_name": "Dora"}],
        }
    ]
    assert client.get(f"{space}/members", headers=dora).json() == []
    infra = client.get("/api/v1/organizations/acme/spaces/infra/members", headers=carol).json()
    assert {tuple(sorted(m)) for m in infra} == {("display_name", "role", "username")}
    mine = client.get("/api/v1/spaces", headers=dora).json()
    assert [(s["slug"], s["role"]) for s in mine] == [("autre", "contributor")]
    written = client.put(f"{space}/files/dora.md", json={"content": "# Dora\n"}, headers=dora)
    assert written.status_code == 200, written.text

    assert client.delete(f"{url}/spaces/autre", headers=carol).status_code == 204
    assert client.delete(f"{url}/spaces/autre", headers=carol).status_code == 404
    assert client.get(space, headers=dora).status_code == 404
    assert client.delete(f"{url}/members/dora", headers=carol).status_code == 204
    assert client.delete(f"{url}/members/dora", headers=carol).status_code == 404
    assert client.delete(url, headers=carol).status_code == 204
    assert client.get(url, headers=carol).status_code == 404


def test_rest_api_refuses_who_does_not_administer(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    group = make_group(db, team, members=("dora",), roles={"infra": "reader"})
    base = "/api/v1/organizations/acme/groups"
    url = f"{base}/{group.id}"
    before = snapshot(database)
    for username, code in (("dora", 403), ("alice", 403), ("eve", 404)):
        headers = bearer(database, username)
        assert client.get(base, headers=headers).status_code == code, username
        assert client.post(base, json={"name": "X"}, headers=headers).status_code == code
        assert client.get(url, headers=headers).status_code == code
        assert client.patch(url, json={"name": "Y"}, headers=headers).status_code == code
        assert client.put(f"{url}/members/alice", headers=headers).status_code == code
        given = client.put(f"{url}/spaces/autre", json={"role": "reader"}, headers=headers)
        assert given.status_code in (code, 404)
        assert client.delete(url, headers=headers).status_code == code
    # The restricted tokens of an admin administer nothing.
    carol = load_actor(db, accounts.get_user(db, "carol"))
    tokens = [
        accounts.create_api_token(db, carol, "infra", space_ids=[team.wiki.infra.id])[0],
        accounts.create_api_token(db, carol, "lecture", read_only=True)[0],
        accounts.create_api_token(db, carol, "inbox", inbox_only=True)[0],
    ]
    db.commit()
    for token in tokens:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.post(base, json={"name": "X"}, headers=headers).status_code == 403
        assert client.delete(url, headers=headers).status_code == 403
    # Another organization's group, or a malformed identifier: not found.
    root = team.wiki.root
    foreign = groups.create_group(db, root, team.ventes.organization, name="Commerciaux")
    db.commit()
    carol_api = bearer(database, "carol")
    assert client.get(f"{base}/{foreign.id}", headers=carol_api).status_code == 404
    assert client.get(f"{base}/pas-un-uuid", headers=carol_api).status_code == 404
    assert client.get("/api/v1/organizations/globex/groups", headers=carol_api).status_code == 404
    db.expire_all()
    after = snapshot(database)
    after["groups"].remove(("Commerciaux", ""))
    assert after == before


# --- MCP --------------------------------------------------------------------------------------


def test_mcp_follows_groups(db: Session, live: Live) -> None:  # noqa: F811
    """`list_spaces`, search, reading and writing through MCP, as the group changes."""
    root = load_actor(db, accounts.get_user(db, "root"))
    acme = organizations.get_organization(db, root, "acme")
    infra = spaces.get_space(db, root, acme, "infra")
    organizations.set_member(db, root, acme, make_user(db, "dora"), OrgRole.MEMBER)
    organizations.update_organization(db, root, acme, default_space_role="contributor")
    spaces.update_space(db, root, infra, restricted=True)
    db.commit()
    dora = token_for(live.database, "dora")
    [(_, listed)] = mcp_calls(live, dora, ("list_spaces", {}), writer=False)
    assert listed.startswith("No space")

    group = groups.create_group(db, root, acme, name="Infra")
    groups.add_member(db, root, group, accounts.get_user(db, "dora"))
    groups.set_space_role(db, root, group, infra, SpaceRole.READER)
    db.commit()
    results = mcp_calls(
        live,
        dora,
        ("list_spaces", {}),
        ("search", {"query": "sauvegarde"}),
        ("read", {"space": "acme/infra", "path": "README.md"}),
        ("write_page", {"space": "acme/infra", "path": "dora.md", "content": "# Dora\n"}),
        writer=False,
    )
    assert '"role": "reader"' in results[0][1].replace("\n", "")
    assert "acme/infra/README.md" in results[1][1]
    assert "sauvegarde nocturne" in results[2][1]
    assert results[3][0]  # a reader through the group does not write

    groups.set_space_role(db, root, group, infra, SpaceRole.CONTRIBUTOR)
    db.commit()
    [(failed, written)] = mcp_calls(
        live,
        dora,
        ("write_page", {"space": "acme/infra", "path": "dora.md", "content": "# Dora\n"}),
    )
    assert not failed, written

    groups.delete_group(db, root, group)
    db.commit()
    results = mcp_calls(
        live,
        dora,
        ("list_spaces", {}),
        ("search", {"query": "sauvegarde"}),
        ("read", {"space": "acme/infra", "path": "README.md"}),
        writer=False,
    )
    assert results[0][1].startswith("No space")
    assert "acme/infra" not in results[1][1]
    assert results[2][0]


# --- Emails -----------------------------------------------------------------------------------


def test_routine_emails_reach_group_members(db: Session, team: Team) -> None:
    for username in ("dora", "bob", "eve"):
        user = accounts.get_user(db, username)
        user.email, user.email_verified_at = f"{username}@example.com", datetime.now(UTC)
    db.commit()
    configure(db, team, "reader", restricted=True)
    other = team.wiki.other
    assert [u.username for u in recipients(db, other, "members")] == []
    group = make_group(db, team, members=("dora",), roles={"autre": "reader"})
    assert [u.username for u in recipients(db, other, "members")] == ["dora"]
    assert [u.username for u in recipients(db, other, "admins")] == []  # admins only
    groups.delete_group(db, team.wiki.root, group)
    db.commit()
    assert [u.username for u in recipients(db, other, "members")] == []


def test_review_reminders_reach_group_members(
    smtp: Inbox,  # noqa: F811
    db: Session,
    database: Database,
    ctx: Workspace,
    team: Team,
) -> None:
    """An owner who reads the space through a group only is reminded of their pages."""
    wiki = team.wiki
    verified = (date.today() - timedelta(days=400)).isoformat()
    page = f"---\nowner: dora\nverified: {verified}\nreview_every: 1y\n---\n# Vieille\n\nX.\n"
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "vieille.md", page.encode())
    dora = accounts.get_user(db, "dora")
    dora.email, dora.email_verified_at = "dora@example.com", datetime.now(UTC)
    spaces.update_space(db, wiki.root, wiki.infra, restricted=True)
    db.commit()
    monday = datetime.now(ZoneInfo("Europe/Paris")).replace(hour=10, minute=0)
    monday -= timedelta(days=monday.weekday())
    assert freshness.send_reminders(database, ctx, now=monday) == 0  # no access: no reminder
    make_group(db, team, members=("dora",), roles={"infra": "reader"})
    assert freshness.send_reminders(database, ctx, now=monday) == 1
    [message] = smtp.messages
    to, _, body = received(message)
    assert to == "dora@example.com" and "Infra · vieille.md" in body


def test_page_owners_offered_include_group_members(
    db: Session, client: TestClient, team: Team
) -> None:
    make_group(db, team, members=("dora",), roles={"infra": "reader"})
    as_user(client, "alice")
    html = client.get("/o/acme/infra/p/README.md").text
    offered = [o.get("value") for o in select_html(html, "#verify-people option")]
    assert "dora" in offered and "bob" in offered and "eve" not in offered


# --- Web pages --------------------------------------------------------------------------------


def test_the_groups_tab_is_for_admins_only(db: Session, client: TestClient, team: Team) -> None:
    group = make_group(db, team)
    for username, code in (("dora", 404), ("alice", 404), ("eve", 404)):
        token = as_user(client, username)
        assert client.get("/o/acme/-/groups").status_code == code
        assert client.get(f"/o/acme/-/groups/{group.id}").status_code == code
        assert_refused(client, post(client, "/o/acme/-/groups", token, name="X"))
        assert_refused(
            client, post(client, f"/o/acme/-/groups/{group.id}/members", token, username="alice")
        )
        assert_refused(client, post(client, f"/o/acme/-/groups/{group.id}/delete", token))
        assert_refused(
            client,
            post(client, "/o/acme/autre/-/groups", token, group=str(group.id), role="reader"),
        )
    as_user(client, "dora")
    assert "/o/acme/-/groups" not in client.get("/o/acme").text
    as_user(client, "carol")
    assert 'href="/o/acme/-/groups"' in client.get("/o/acme").text


def test_an_admin_manages_a_group_from_the_web(db: Session, client: TestClient, team: Team) -> None:
    configure(db, team, "reader")
    spaces.update_space(db, team.wiki.root, team.wiki.other, restricted=True)
    db.commit()
    token = as_user(client, "carol")
    assert "Aucun groupe pour l&#39;instant." in client.get("/o/acme/-/groups").text
    created = post(client, "/o/acme/-/groups", token, name="RH", description="Paie")
    assert_done(created)
    url = created.headers["location"]
    page = client.get(url).text
    assert "Groupe créé. Ajoutez ses membres" in page
    assert select_one(page, data_test("group-member-add")).action == f"{url}/members"
    offered = [o.get("value") for o in select_html(page, "#org-people option")]
    assert "dora" in offered and "eve" not in offered  # members of the organization only

    def flash(response: object) -> str:
        assert_done(response)  # type: ignore[arg-type]
        return client.get(response.headers["location"]).text  # type: ignore[attr-defined]

    assert "Dora fait désormais partie du groupe" in flash(
        post(client, f"{url}/members", token, username="dora")
    )
    assert "Dora fait déjà partie du groupe." in flash(
        post(client, f"{url}/members", token, username="dora")
    )
    outsider = post(client, f"{url}/members", token, username="eve")
    assert_refused(
        client,
        outsider,
        "Eve n'est pas membre de l'organisation : ajoutez d'abord cette personne à "
        "l'organisation, ou invitez-la.",
    )
    shown = flash(post(client, f"{url}/spaces", token, space="autre", role="contributor"))
    assert "Autre est restreint\u00a0: le groupe l&#39;ouvre à ses membres." in shown
    covered = post(client, f"{url}/spaces", token, space="infra", role="reader")
    warning = flash(covered)
    assert 'data-flash="warning"' in warning and "le groupe n&#39;y ajoute rien" in warning
    assert role(db, "dora", team.wiki.other) == "contributor"
    page = client.get(url).text
    rows = select_one(page, data_test("group-spaces")).text
    assert "Autre" in rows and "Restreint" in rows and "Infra" in rows
    assert "rôle par défaut donne déjà à chaque membre un accès lecteur" in page

    assert "Groupe enregistré." in flash(
        post(client, f"{url}/update", token, name="Ressources humaines", description="")
    )
    assert "Ressources humaines" in client.get("/o/acme/-/groups").text
    assert "Accès retiré." in flash(post(client, f"{url}/spaces/infra/remove", token))
    removal = select_html(client.get(url).text, f'form[action="{url}/members/dora/remove"]')
    assert "perd l'accès qu'il donne" in (removal[0].get("data-confirm") or "")
    assert "Dora ne fait plus partie du groupe." in flash(
        post(client, f"{url}/members/dora/remove", token)
    )
    assert role(db, "dora", team.wiki.other) is None
    deleted = flash(post(client, f"{url}/delete", token))
    assert "Groupe Ressources humaines supprimé" in deleted
    assert client.get(url).status_code == 404


def test_space_members_page_says_which_group_gives_access(
    db: Session, client: TestClient, team: Team
) -> None:
    configure(db, team, "none")
    group = make_group(db, team, members=("dora", "bob"), roles={"infra": "contributor"})
    make_group(db, team, "Lecteurs", members=("dora",), roles={"infra": "reader"})
    as_user(client, "carol")
    html = client.get("/o/acme/infra/-/members").text
    by_group = select_one(html, data_test("through-groups")).text
    assert "RH" in by_group and "Dora" in by_group and "Bob" in by_group
    assert "Contributeur" in by_group and "Lecteurs" in by_group
    through = select_one(html, data_test("through-org")).text
    assert "@dora" in through and "Contributeur, par le groupe RH" in through
    [note] = select_html(html, data_test("member-note"))
    assert note.text == "Contributeur, par le groupe RH"  # Bob, reader there
    form = select_one(html, data_test("space-group-add"))
    assert form.action == "/o/acme/infra/-/groups"
    assert select_html(html, f'form[action="/o/acme/infra/-/groups/{group.id}/remove"]')

    token = as_user(client, "carol")
    saved = post(client, "/o/acme/autre/-/groups", token, group=str(group.id), role="reader")
    assert "Accès enregistré." in client.get(saved.headers["location"]).text
    assert role(db, "dora", team.wiki.other) == "reader"
    removed = post(client, f"/o/acme/autre/-/groups/{group.id}/remove", token)
    assert_done(removed)
    assert role(db, "dora", team.wiki.other) is None
    foreign = groups.create_group(db, team.wiki.root, team.ventes.organization, name="G")
    db.commit()
    assert_refused(
        client,
        post(client, "/o/acme/autre/-/groups", token, group=str(foreign.id), role="reader"),
        "Groupe introuvable.",
    )

    as_user(client, "bob")  # a reader sees the groups, without forms nor links
    html = client.get("/o/acme/infra/-/members").text
    assert "RH" in select_one(html, data_test("through-groups")).text
    assert select_html(html, data_test("space-group-add")) == []
    assert select_html(html, f'a[href="/o/acme/-/groups/{group.id}"]') == []


def test_several_groups_and_the_default_role_are_told_apart(
    db: Session, client: TestClient, team: Team
) -> None:
    configure(db, team, "reader")
    make_group(db, team, "RH", members=("dora",), roles={"infra": "contributor"})
    make_group(db, team, "Direction", members=("dora",), roles={"infra": "contributor"})
    make_group(db, team, "Paie", members=("alice",), roles={"infra": "reader"})
    as_user(client, "carol")
    through = select_one(client.get("/o/acme/infra/-/members").text, data_test("through-org"))
    rows = {row.text for row in select_html(through, "tr")}
    assert any("@dora" in r and "Contributeur, par les groupes Direction, RH" in r for r in rows)
    assert any("@carol" in r and "Administrateur de l'organisation" in r for r in rows)
    # Alice is added to the space: in the table above, where her group gives nothing more.
    assert not any("@alice" in r for r in rows)


def test_org_members_page_lists_the_groups_of_each_member(
    db: Session, client: TestClient, team: Team
) -> None:
    make_group(db, team, "RH", members=("dora",))
    make_group(db, team, "Direction", members=("dora", "bob"))
    as_user(client, "carol")
    html = client.get("/o/acme/-/members").text
    notes = [n.text for n in select_html(html, data_test("member-note"))]
    assert sorted(notes) == ["Groupe : Direction", "Groupes : Direction, RH"]  # text: spaces
    [removal] = select_html(html, 'form[action="/o/acme/-/members/dora/remove"]')
    assert (removal.get("data-confirm") or "").endswith(
        "Cette personne quitte aussi les groupes Direction, RH."
    )


def test_forged_and_cross_organization_requests_change_nothing(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    group = make_group(db, team, members=("dora",))
    foreign = groups.create_group(db, team.wiki.root, team.ventes.organization, name="Globex")
    db.commit()
    before = snapshot(database)
    as_user(client, "carol")
    for url, data in (
        ("/o/acme/-/groups", {"name": "Forgé"}),
        (f"/o/acme/-/groups/{group.id}/delete", {}),
        (f"/o/acme/-/groups/{group.id}/members/dora/remove", {}),
        ("/o/acme/infra/-/groups", {"group": str(group.id), "role": "reader"}),
    ):
        forged = client.post(url, data={"csrf": "faux", **data}, follow_redirects=False)
        assert forged.status_code in (303, 400, 403)
    token = as_user(client, "carol")  # Acme admin, invisible Globex
    assert post(client, "/o/globex/-/groups", token, name="X").status_code == 404
    assert post(client, f"/o/globex/-/groups/{foreign.id}/delete", token).status_code == 404
    assert post(client, f"/o/acme/-/groups/{foreign.id}/delete", token).status_code == 404
    assert client.get("/o/acme/-/groups/pas-un-uuid").status_code == 404
    # A space of another organization through Acme's group page: refused.
    assert_refused(
        client,
        post(client, f"/o/acme/-/groups/{group.id}/spaces", token, space="ventes", role="reader"),
        "Espace introuvable.",
    )
    assert snapshot(database) == before


# --- Audit log --------------------------------------------------------------------------------


def test_group_changes_are_logged(db: Session, client: TestClient, team: Team) -> None:
    group = make_group(db, team, members=("dora",), roles={"autre": "reader"})
    root, dora = team.wiki.root, accounts.get_user(db, "dora")
    groups.set_space_role(db, root, group, team.wiki.other, SpaceRole.READER)  # unchanged
    groups.add_member(db, root, group, dora)  # already in
    groups.update_group(db, root, group, name="RH", description="")  # nothing changes
    groups.set_space_role(db, root, group, team.wiki.other, SpaceRole.CONTRIBUTOR)
    groups.update_group(db, root, group, name="Ressources humaines", description="Paie")
    groups.remove_space_role(db, root, group, team.wiki.other)
    groups.remove_member(db, root, group, dora)
    groups.delete_group(db, root, group)
    db.commit()
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.action.like("group.%")).order_by(AuditEvent.at)
    ).all()
    acme, other = team.wiki.infra.org_id, team.wiki.other.id
    assert [(e.action, e.org_id, e.space_id, e.details) for e in events] == [
        ("group.created", acme, None, {"group": "RH"}),
        ("group.member_added", acme, None, {"group": "RH", "username": "dora"}),
        ("group.space_role_set", acme, other, {"group": "RH", "role": "reader"}),
        ("group.space_role_set", acme, other, {"group": "RH", "role": "contributor"}),
        (
            "group.updated",
            acme,
            None,
            {"group": "Ressources humaines", "fields": "name description", "previous": "RH"},
        ),
        ("group.space_role_removed", acme, other, {"group": "Ressources humaines"}),
        (
            "group.member_removed",
            acme,
            None,
            {"group": "Ressources humaines", "username": "dora"},
        ),
        ("group.deleted", acme, None, {"group": "Ressources humaines"}),
    ]
    as_user(client, "carol")
    journal = client.get("/o/acme/-/journal").text
    for shown in ("Groupe créé", "Groupe Ressources humaines", "Auparavant", "Ajout à un groupe"):
        assert shown in journal, shown
    members = client.get("/o/acme/-/journal", params={"family": "members"}).text
    assert "Rôle d&#39;un groupe dans l&#39;espace défini" in members


# --- English ----------------------------------------------------------------------------------


def test_the_pages_in_english(db: Session, client: TestClient, team: Team) -> None:
    make_group(db, team, members=("dora", "bob"), roles={"infra": "contributor"})
    carol = accounts.get_user(db, "carol")
    carol.locale = "en"
    db.commit()
    as_user(client, "carol")
    html = client.get("/o/acme/infra/-/members").text
    assert "By a group" in html
    assert "Contributor, through the group RH" in select_one(html, data_test("through-org")).text
    assert ">Groups<" in client.get("/o/acme/-/groups").text


def _spaces_of(db: Session, username: str) -> set[str]:
    actor = load_actor(db, accounts.get_user(db, username))
    return {s.slug for s in db.scalars(select(Space).where(Space.id.in_(actor.access.space_roles)))}


def test_open_sessions_follow_group_changes_at_once(
    db: Session, client: TestClient, team: Team
) -> None:
    """Permissions are loaded on every request: a signed-in session follows a group's change
    both ways, without signing in again."""
    configure(db, team, "none", restricted=True)
    as_user(client, "dora")
    assert client.get("/o/acme/autre/p/secret.md").status_code == 404
    group = make_group(db, team, members=("dora",), roles={"autre": "reader"})
    assert client.get("/o/acme/autre/p/secret.md").status_code == 200
    assert _spaces_of(db, "dora") == {"autre"}
    groups.remove_space_role(db, team.wiki.root, group, team.wiki.other)
    db.commit()
    assert client.get("/o/acme/autre/p/secret.md").status_code == 404
