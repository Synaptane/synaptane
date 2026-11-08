"""Default role of an organization's members in its spaces, and restricted spaces.

The default role gives every member of the organization a role on each of its spaces without
being added to it; the effective role is the higher of that and the role given in the space.
A restricted space ignores it. Organization admins keep their rights; nobody ever reaches the
spaces of an organization they do not belong to. Checked from every interface: services,
web, REST, MCP, routine emails.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import Action, OrgRole, can
from synaptane.core.errors import SynaptaneError
from synaptane.services import accounts, git_sync, load_actor, organizations, spaces
from synaptane.services.extraction import update_index
from synaptane.services.routine_agent import recipients
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Space
from tests.htmlutil import data_test, select_one
from tests.htmlutil import select as select_html
from tests.integration.conftest import make_user, token_for
from tests.integration.gitutil import Remote
from tests.integration.test_mcp import Live, live, mcp_calls  # noqa: F401 — fixture
from tests.integration.test_rights_html import as_user, assert_done, assert_refused, post
from tests.integration.wikidata import Wiki

DEFAULTS = ("none", "reader", "contributor")
RANK = {None: 0, "reader": 1, "contributor": 2}


@dataclass
class Team:
    wiki: Wiki
    ventes: Space  # space of Globex, an organization whose default role is "contributor"


@pytest.fixture
def team(db: Session, database: Database, ctx: Workspace, tmp_path: Path, wiki: Wiki) -> Team:
    """Acme: Alice (contributor on infra) and Bob (reader on infra) are members through their
    space; Dora is a member added to no space; Carol administers Acme. Globex: Eve, a member
    of Globex only, and its space "ventes"."""
    acme = wiki.infra.organization
    organizations.set_member(db, wiki.root, acme, make_user(db, "dora"), OrgRole.MEMBER)
    organizations.set_member(db, wiki.root, acme, make_user(db, "carol"), OrgRole.ADMIN)
    globex = organizations.create_organization(db, wiki.root, slug="globex", name="Globex")
    remote = Remote(tmp_path / "globex")
    remote.push({"content/plan.md": "# Plan\n\nFusion confidentielle chez Globex.\n"})
    ventes = spaces.create_space(
        db, wiki.root, globex, slug="ventes", name="Ventes", repo_url=remote.url
    )
    organizations.set_member(db, wiki.root, globex, make_user(db, "eve"), OrgRole.MEMBER)
    organizations.update_organization(db, wiki.root, globex, default_space_role="contributor")
    db.commit()
    git_sync.run_sync(database, ctx, ventes.id)
    update_index(ctx, ventes.id, ctx.local_repository(ventes))
    return Team(wiki, ventes)


def configure(db: Session, team: Team, default: str, *, restricted: bool = False) -> None:
    """Acme's default role; `restricted` applies to both its spaces."""
    root = team.wiki.root
    organizations.update_organization(
        db, root, team.wiki.infra.organization, default_space_role=default
    )
    for space in (team.wiki.infra, team.wiki.other):
        spaces.update_space(db, root, space, restricted=restricted)
    db.commit()


def role(db: Session, username: str, space: Space) -> str | None:
    """Effective role, read from the permissions loaded for a request."""
    access = load_actor(db, accounts.get_user(db, username)).access
    where = {"org_id": space.org_id, "space_id": space.id}
    if not can(access, Action.READ_SPACE, **where):
        return None
    return "contributor" if can(access, Action.CONTRIBUTE_SPACE, **where) else "reader"


def expected(given: str | None, default: str, restricted: bool) -> str | None:
    if restricted or default == "none":
        return given
    return max(given, default, key=lambda role: RANK[role])


# --- Access matrix ----------------------------------------------------------------------------


@pytest.mark.parametrize("restricted", [False, True], ids=["normal", "restricted"])
@pytest.mark.parametrize("default", DEFAULTS)
def test_access_matrix(db: Session, team: Team, default: str, restricted: bool) -> None:
    configure(db, team, default, restricted=restricted)
    infra, other, ventes = team.wiki.infra, team.wiki.other, team.ventes
    given = {  # role added in the space: (infra, autre)
        "dora": (None, None),
        "bob": ("reader", None),
        "alice": ("contributor", None),
    }
    for username, (on_infra, on_other) in given.items():
        assert role(db, username, infra) == expected(on_infra, default, restricted), username
        assert role(db, username, other) == expected(on_other, default, restricted), username
        assert role(db, username, ventes) is None, username  # never another organization
    # The organization admin contributes everywhere, restricted or not.
    assert role(db, "carol", infra) == role(db, "carol", other) == "contributor"
    assert role(db, "carol", ventes) is None
    # Globex's default role ("contributor") reaches Eve in Globex only.
    assert role(db, "eve", ventes) == "contributor"
    assert role(db, "eve", infra) is None and role(db, "eve", other) is None
    # The same answer from the services every interface calls.
    dora = load_actor(db, accounts.get_user(db, "dora"))
    visible = {s.address for s in spaces.list_all_visible_spaces(db, dora)}
    reaches = expected(None, default, restricted) is not None
    assert visible == ({"acme/infra", "acme/autre"} if reaches else set())
    assert {s.slug for s in spaces.list_spaces(db, dora, other.organization)} == (
        {"infra", "autre"} if reaches else set()
    )
    if not reaches:
        with pytest.raises(SynaptaneError) as error:
            spaces.get_space(db, dora, other.organization, "autre")
        assert error.value.code == "not_found"
    assert spaces.role_in(dora, other) == expected(None, default, restricted)


def test_a_restricted_space_keeps_its_explicit_members(db: Session, team: Team) -> None:
    """Restricting one space leaves the other under the default role."""
    configure(db, team, "reader")
    spaces.update_space(db, team.wiki.root, team.wiki.other, restricted=True)
    db.commit()
    assert role(db, "dora", team.wiki.infra) == "reader"
    assert role(db, "dora", team.wiki.other) is None
    assert role(db, "bob", team.wiki.infra) == "reader"
    assert role(db, "alice", team.wiki.infra) == "contributor"


def test_leaving_the_organization_removes_the_default_access(db: Session, team: Team) -> None:
    configure(db, team, "contributor")
    acme = team.wiki.infra.organization
    organizations.remove_member(db, team.wiki.root, acme, accounts.get_user(db, "dora"))
    db.commit()
    assert role(db, "dora", team.wiki.infra) is None


def test_token_limits_still_apply_to_the_default_role(
    db: Session, database: Database, team: Team, client: TestClient
) -> None:
    """A token limited to some spaces, to reading or to the inbox stays limited when its
    access comes from the default role."""
    configure(db, team, "contributor")
    dora = load_actor(db, accounts.get_user(db, "dora"))
    infra_only, _ = accounts.create_api_token(db, dora, "infra", space_ids=[team.wiki.infra.id])
    read_only, _ = accounts.create_api_token(db, dora, "lecture", read_only=True)
    inbox_only, _ = accounts.create_api_token(db, dora, "inbox", inbox_only=True)
    db.commit()
    base = "/api/v1/organizations/acme/spaces"

    def call(token: str, method: str, url: str, **kwargs: object) -> int:
        headers = {"Authorization": f"Bearer {token}"}
        return client.request(method, url, headers=headers, **kwargs).status_code  # type: ignore[arg-type]

    mine = client.get("/api/v1/spaces", headers={"Authorization": f"Bearer {infra_only}"})
    assert [(s["slug"], s["role"]) for s in mine.json()] == [("infra", "contributor")]
    assert call(infra_only, "GET", f"{base}/autre/files/secret.md") == 404
    page = {"json": {"content": "# Note\n"}}
    assert call(read_only, "GET", f"{base}/autre/files/secret.md") == 200
    assert call(read_only, "PUT", f"{base}/autre/files/lecture.md", **page) == 403
    assert call(inbox_only, "PUT", f"{base}/autre/files/inbox.md", **page) == 403
    note = {"json": {"title": "Note", "content": "Par l'inbox."}}
    assert call(inbox_only, "POST", f"{base}/autre/inbox", **note) == 201
    # A token chosen for a space reached by default: refused once the space is restricted.
    spaces.update_space(db, team.wiki.root, team.wiki.infra, restricted=True)
    db.commit()
    assert call(infra_only, "GET", f"{base}/infra/files/README.md") == 404


# --- Web --------------------------------------------------------------------------------------


def test_web_reading_and_search_follow_the_default_role(
    db: Session, client: TestClient, team: Team
) -> None:
    configure(db, team, "reader")
    as_user(client, "dora")
    page = client.get("/o/acme/autre/p/secret.md")
    assert page.status_code == 200 and "Sauvegarde confidentielle" in page.text
    assert "secret.md" in client.get("/search", params={"q": "confidentielle"}).text
    assert "/o/globex/" not in client.get("/search", params={"q": "fusion"}).text
    edit = client.get("/o/acme/autre/edit/secret.md")
    assert edit.status_code == 403  # a reader by default does not edit

    spaces.update_space(db, team.wiki.root, team.wiki.other, restricted=True)
    db.commit()
    assert client.get("/o/acme/autre/p/secret.md").status_code == 404
    assert "secret.md" not in client.get("/search", params={"q": "confidentielle"}).text
    assert "/o/acme/autre" not in client.get("/").text  # nor in the navigation

    as_user(client, "eve")  # another organization: nothing of Acme
    assert client.get("/o/acme/infra/p/README.md").status_code == 404
    assert "/o/acme/" not in client.get("/search", params={"q": "sauvegarde"}).text
    assert "/o/globex/ventes/p/plan.md" in client.get("/search", params={"q": "fusion"}).text


def test_web_contributor_by_default_edits(
    db: Session, client: TestClient, team: Team, ctx: Workspace
) -> None:
    configure(db, team, "contributor")
    token = as_user(client, "dora")
    assert client.get("/o/acme/autre/edit/secret.md").status_code == 200
    created = client.post(
        "/o/acme/autre/-/new",
        data={"csrf": token, "folder": "guides", "name": "nouveau", "text": "# Nouveau\n"},
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text[:300]
    assert ctx.local_repository(team.wiki.other).blob("content/guides/nouveau.md")


def test_members_page_shows_who_accesses_by_default(
    db: Session, client: TestClient, team: Team
) -> None:
    configure(db, team, "contributor")
    as_user(client, "carol")
    html = client.get("/o/acme/infra/-/members").text
    through = select_one(html, data_test("through-org")).text
    assert "@dora" in through and "Contributeur, par défaut" in through
    assert "@carol" in through and "Administrateur de l&#39;organisation" in html
    assert "@bob" not in through  # added to the space: in the table above
    [note] = select_html(html, data_test("member-note"))
    assert note.text == "Contributeur, par défaut"  # Bob, reader there, contributor by default
    assert "en tant que contributeur" in select_one(html, data_test("space-default-access")).text
    form = select_one(html, data_test("space-restricted"))
    assert form.action == "/o/acme/infra/-/access"

    spaces.update_space(db, team.wiki.root, team.wiki.infra, restricted=True)
    db.commit()
    html = client.get("/o/acme/infra/-/members").text
    through = select_one(html, data_test("through-org")).text
    assert "@dora" not in through and "@carol" in through
    assert "Restreint" in select_one(html, data_test("space-default-access")).text
    assert select_html(html, data_test("member-note")) == []
    assert "Restreint" in client.get("/o/acme").text  # the badge on the space's card

    as_user(client, "bob")  # a reader sees who has access, without the form
    html = client.get("/o/acme/infra/-/members").text
    assert select_html(html, data_test("space-restricted")) == []
    assert select_one(html, data_test("through-org"))


def test_org_settings_show_the_default_role(db: Session, client: TestClient, team: Team) -> None:
    configure(db, team, "reader")
    spaces.update_space(db, team.wiki.root, team.wiki.other, restricted=True)
    db.commit()
    as_user(client, "carol")
    html = client.get("/o/acme/-/settings").text
    form = select_one(html, data_test("default-role-form"))
    assert form.action == "/o/acme/-/default-role"
    assert select_one(form, 'option[value="reader"]').get("selected") is not None
    assert "Espace restreint : Autre." in select_one(html, data_test("restricted-spaces")).text
    members = client.get("/o/acme/-/members").text
    assert "chaque espace non restreint en tant que lecteur" in members


def test_only_admins_change_the_default_role_and_restriction(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    def state() -> tuple[str, bool]:
        """Read again from the database: what the server committed."""
        db.expire_all()
        return team.wiki.infra.organization.default_space_role, team.wiki.infra.restricted

    for username in ("dora", "alice", "eve"):
        token = as_user(client, username)
        refused = post(client, "/o/acme/-/default-role", token, default_space_role="contributor")
        if username == "eve":
            assert refused.status_code == 404  # an organization she does not see
        else:
            assert_refused(client, refused)
        assert_refused(client, post(client, "/o/acme/infra/-/access", token, restricted="true"))
    for username in ("dora", "alice"):
        headers = {"Authorization": f"Bearer {token_for(database, username)}"}
        org = client.patch(
            "/api/v1/organizations/acme", json={"default_space_role": "reader"}, headers=headers
        )
        assert org.status_code == 403
        space = client.patch(
            "/api/v1/organizations/acme/spaces/infra", json={"restricted": True}, headers=headers
        )
        assert space.status_code == (404 if username == "dora" else 403)  # Dora: no access
    assert state() == ("none", False)

    token = as_user(client, "carol")
    assert_done(post(client, "/o/acme/-/default-role", token, default_space_role="contributor"))
    assert_done(post(client, "/o/acme/infra/-/access", token, restricted="true"))
    bad = post(client, "/o/acme/-/default-role", token, default_space_role="admin")
    assert_refused(client, bad, "Rôle par défaut inconnu : none, reader ou contributor.")
    assert state() == ("contributor", True)
    assert_done(post(client, "/o/acme/infra/-/access", token))  # unticked: no longer restricted
    assert state() == ("contributor", False)


def test_rest_api_reads_and_sets_the_default_role(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    carol = {"Authorization": f"Bearer {token_for(database, 'carol')}"}
    dora = {"Authorization": f"Bearer {token_for(database, 'dora')}"}
    assert client.get("/api/v1/spaces", headers=dora).json() == []
    org = client.patch(
        "/api/v1/organizations/acme", json={"default_space_role": "contributor"}, headers=carol
    )
    assert org.status_code == 200 and org.json()["default_space_role"] == "contributor"
    invalid = client.patch(
        "/api/v1/organizations/acme", json={"default_space_role": "admin"}, headers=carol
    )
    assert invalid.status_code in (400, 422)
    mine = client.get("/api/v1/spaces", headers=dora).json()
    assert [(s["slug"], s["role"], s["restricted"]) for s in mine] == [
        ("autre", "contributor", False),
        ("infra", "contributor", False),
    ]
    base = "/api/v1/organizations/acme/spaces/autre"
    written = client.put(f"{base}/files/dora.md", json={"content": "# Dora\n"}, headers=dora)
    assert written.status_code == 200, written.text
    found = client.get("/api/v1/search", params={"q": "confidentielle"}, headers=dora).json()
    assert {hit["space"] for hit in found} == {"acme/autre"}  # never Globex's "ventes"
    # Explicit members only in /members: the default role is not a membership.
    listed = client.get(f"{base}/members", headers=dora).json()
    assert listed == []

    restricted = client.patch(base, json={"restricted": True}, headers=carol)
    assert restricted.status_code == 200 and restricted.json()["restricted"] is True
    assert client.get(base, headers=dora).status_code == 404
    assert client.get(f"{base}/files/secret.md", headers=dora).status_code == 404
    found = client.get("/api/v1/search", params={"q": "confidentielle"}, headers=dora).json()
    assert found == []
    assert client.get(base, headers=carol).json()["role"] == "contributor"  # admin: as before


def test_changes_are_logged(db: Session, team: Team) -> None:
    configure(db, team, "reader", restricted=True)
    configure(db, team, "reader", restricted=True)  # unchanged: nothing more
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.action.in_(["org.default_space_role", "space.restricted"]))
        .order_by(AuditEvent.id)
    ).all()
    acme = team.wiki.infra.organization
    logged = [(e.action, e.org_id, e.space_id, e.details) for e in events]
    assert (
        "org.default_space_role",
        team.ventes.org_id,
        None,
        {"role": "contributor"},
    ) in logged  # Globex, set by the fixture
    assert ("org.default_space_role", acme.id, None, {"role": "reader"}) in logged
    assert ("space.restricted", acme.id, team.wiki.infra.id, {"restricted": "True"}) in logged
    assert ("space.restricted", acme.id, team.wiki.other.id, {"restricted": "True"}) in logged
    assert len(logged) == 4
    # Changing only these settings logs nothing else.
    assert not db.scalars(
        select(AuditEvent).where(AuditEvent.action.in_(["org.updated", "space.updated"]))
    ).all()


def test_routine_emails_reach_members_by_default(db: Session, team: Team) -> None:
    """A routine that writes to "the members" also writes to those who access by default,
    never to a member of another organization, and no longer once the space is restricted."""
    for username in ("dora", "bob", "eve"):
        user = accounts.get_user(db, username)
        user.email, user.email_verified_at = f"{username}@example.com", datetime.now(UTC)
    db.commit()
    other = team.wiki.other
    assert [u.username for u in recipients(db, other, "members")] == []
    configure(db, team, "reader")
    assert [u.username for u in recipients(db, other, "members")] == ["bob", "dora"]
    spaces.update_space(db, team.wiki.root, other, restricted=True)
    db.commit()
    assert [u.username for u in recipients(db, other, "members")] == []


# --- MCP --------------------------------------------------------------------------------------


def test_mcp_follows_the_default_role(db: Session, live: Live) -> None:  # noqa: F811
    """`list_spaces`, search, reading and writing through MCP, as the role changes."""
    root = load_actor(db, accounts.get_user(db, "root"))
    acme = organizations.get_organization(db, root, "acme")
    infra = spaces.get_space(db, root, acme, "infra")
    organizations.set_member(db, root, acme, make_user(db, "dora"), OrgRole.MEMBER)
    db.commit()
    dora = token_for(live.database, "dora")

    [(_, listed)] = mcp_calls(live, dora, ("list_spaces", {}), writer=False)
    assert listed.startswith("No space")

    organizations.update_organization(db, root, acme, default_space_role="reader")
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
    assert results[3][0]  # a reader by default does not write

    organizations.update_organization(db, root, acme, default_space_role="contributor")
    db.commit()
    [(failed, written)] = mcp_calls(
        live,
        dora,
        ("write_page", {"space": "acme/infra", "path": "dora.md", "content": "# Dora\n"}),
    )
    assert not failed, written

    spaces.update_space(db, root, infra, restricted=True)
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
