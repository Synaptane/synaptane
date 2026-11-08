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

from synaptane.core.access import Action, Limits, OrgRole, SpaceRole, can
from synaptane.core.errors import SynaptaneError
from synaptane.services import (
    accounts,
    git_sync,
    invitations,
    load_actor,
    organizations,
    spaces,
)
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
    # Changing only the default role or the restriction has its own event, and no generic
    # "settings changed" one besides (it would be an empty line in the audit log).
    updates = db.scalars(
        select(AuditEvent.action).where(AuditEvent.action.in_(["org.updated", "space.updated"]))
    ).all()
    assert updates == []


def test_empty_updates_are_logged_as_before(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    carol = {"Authorization": f"Bearer {token_for(database, 'carol')}"}
    assert client.patch("/api/v1/organizations/acme", json={}, headers=carol).status_code == 200
    space = client.patch("/api/v1/organizations/acme/spaces/infra", json={}, headers=carol)
    assert space.status_code == 200
    by_carol = AuditEvent.actor_id == accounts.get_user(db, "carol").id
    actions = sorted(
        db.scalars(select(AuditEvent.action).where(by_carol, AuditEvent.action != "token.created"))
    )
    assert actions == ["org.updated", "space.updated"]  # nothing changed: no other event


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


# --- Adding someone who is not a member of the organization -------------------------------------


def test_adding_an_outsider_to_one_space_opens_the_others_and_says_so(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    """Adding an account to a space makes it a member of the organization: with a default
    role, it then reaches every space that is not restricted. The model stays so (no guest
    yet); the web form and the confirmation say it."""
    configure(db, team, "reader")
    spaces.update_space(db, team.wiki.root, team.wiki.other, restricted=True)
    db.commit()
    carol = {"Authorization": f"Bearer {token_for(database, 'carol')}"}
    put = client.put(
        "/api/v1/organizations/acme/spaces/autre/members/eve",
        json={"role": "reader"},
        headers=carol,
    )
    assert put.status_code == 200, put.text
    db.expire_all()
    # Eve (Globex) now belongs to Acme: added to "autre", reader of "infra" by default.
    assert role(db, "eve", team.wiki.other) == "reader"
    assert role(db, "eve", team.wiki.infra) == "reader"
    assert role(db, "eve", team.ventes) == "contributor"  # her own organization, unchanged

    token = as_user(client, "carol")
    html = client.get("/o/acme/infra/-/members").text
    warning = select_one(html, data_test("member-add-help")).text
    assert "n&#39;est pas encore membre" in html and "en tant que lecteur" in warning
    make_user(db, "zack")  # an account in no organization
    db.commit()
    added = post(client, "/o/acme/infra/-/members", token, username="zack", role="contributor")
    assert_done(added)
    flash = client.get(added.headers["location"]).text
    assert "Zack n&#39;était pas membre de l&#39;organisation" in flash
    assert "lit désormais chaque espace non restreint" in flash
    assert role(db, "zack", team.wiki.infra) == "contributor"
    assert role(db, "zack", team.wiki.other) is None  # restricted
    # Already a member: the usual message.
    again = post(client, "/o/acme/infra/-/members", token, username="dora", role="reader")
    shown = client.get(again.headers["location"]).text
    assert "Membre enregistré." in shown and "n&#39;était pas membre" not in shown


def test_no_warning_without_default_role(db: Session, client: TestClient, team: Team) -> None:
    token = as_user(client, "carol")
    assert (
        select_html(client.get("/o/acme/infra/-/members").text, data_test("member-add-help")) == []
    )
    make_user(db, "zack")
    db.commit()
    added = post(client, "/o/acme/infra/-/members", token, username="zack", role="reader")
    shown = client.get(added.headers["location"]).text
    assert "Membre enregistré." in shown and "n&#39;était pas membre" not in shown
    assert role(db, "zack", team.wiki.other) is None


def test_removing_a_member_says_the_default_access_remains(
    db: Session, client: TestClient, team: Team
) -> None:
    configure(db, team, "contributor")
    as_user(client, "carol")
    html = client.get("/o/acme/infra/-/members").text
    removals = select_html(html, 'form[action="/o/acme/infra/-/members/bob/remove"]')
    assert len(removals) == 1
    confirm = removals[0].get("data-confirm") or ""
    assert confirm.startswith("Retirer Bob\u202f?") and "en tant que contributeur" in confirm
    spaces.remove_member(db, team.wiki.root, team.wiki.infra, accounts.get_user(db, "bob"))
    db.commit()
    assert role(db, "bob", team.wiki.infra) == "contributor"  # as announced
    # Restricted: nothing is kept, nothing is announced.
    spaces.update_space(db, team.wiki.root, team.wiki.infra, restricted=True)
    db.commit()
    html = client.get("/o/acme/infra/-/members").text
    [removal] = select_html(html, 'form[action="/o/acme/infra/-/members/alice/remove"]')
    assert removal.get("data-confirm") == "Retirer Alice\u202f?"


# --- Invitations --------------------------------------------------------------------------------


def test_invitation_form_offers_the_default_role_as_a_floor(
    db: Session, client: TestClient, team: Team
) -> None:
    configure(db, team, "reader")
    spaces.update_space(db, team.wiki.root, team.wiki.other, restricted=True)
    db.commit()
    as_user(client, "carol")
    form = select_one(client.get("/o/acme/-/members").text, data_test("invite-form"))

    def options(space: Space) -> list[tuple[str, str]]:
        menu = select_one(form, f"#space-role-{space.id}")
        return [(o.get("value") or "", o.text) for o in select_html(menu, "option")]

    infra, other = team.wiki.infra, team.wiki.other
    assert options(infra) == [
        ("", "Lecteur (par défaut)"),
        (f"{infra.id}:contributor", "Contributeur"),
    ]
    assert options(other) == [  # restricted: as before
        ("", "Pas d'accès"),
        (f"{other.id}:reader", "Lecteur"),
        (f"{other.id}:contributor", "Contributeur"),
    ]
    configure(db, team, "contributor")
    form = select_one(client.get("/o/acme/-/members").text, data_test("invite-form"))
    assert options(infra) == [("", "Contributeur (par défaut)")]


def test_invitation_page_shows_the_real_access(db: Session, client: TestClient, team: Team) -> None:
    """No role asked in a space that is not restricted: no error, the default role applies,
    and the invitation page says so before the account is created."""
    configure(db, team, "reader")
    spaces.update_space(db, team.wiki.root, team.wiki.other, restricted=True)
    db.commit()
    token = as_user(client, "carol")
    response = post(
        client,
        "/o/acme/-/invitations",
        token,
        email="zoe@example.com",
        org_role="member",
        space_role="",  # "Reader (by default)" left as is
    )
    assert_done(response)
    revealed = client.get(response.headers["location"]).text
    link = revealed.split("/invite/", 1)[1].split("<", 1)[0].split('"', 1)[0]
    client.cookies.clear()
    page = client.get(f"/invite/{link}")
    access = select_one(page.text, data_test("invitation-access")).text
    assert "Infra : lecteur, par défaut" in access and "Autre" not in access
    created = client.post(
        f"/invite/{link}",
        data={"display_name": "Zoé", "username": "zoe", "password": "mot-de-passe-solide-42"},
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text[:300]
    db.expire_all()
    assert role(db, "zoe", team.wiki.infra) == "reader"
    assert role(db, "zoe", team.wiki.other) is None


def test_invitation_with_a_role_below_the_default_keeps_the_default(
    db: Session, team: Team
) -> None:
    """Through the service (REST): a reader role asked where the default role is
    "contributor" is kept as given, the effective role stays the higher one."""
    configure(db, team, "contributor")
    user = make_user(db, "zoe")
    user.email, user.email_verified_at = "zoe@example.com", datetime.now(UTC)
    db.commit()
    carol = load_actor(db, accounts.get_user(db, "carol"))
    invitations.invite(
        db,
        carol,
        team.wiki.infra.organization,
        email="zoe@example.com",
        space_roles={str(team.wiki.infra.id): "reader"},
    )
    db.commit()
    assert role(db, "zoe", team.wiki.infra) == "contributor"
    assert role(db, "zoe", team.wiki.other) == "contributor"


# --- Spaces created restricted ------------------------------------------------------------------


def test_a_space_can_be_created_restricted(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    """An HR space is never open to everyone, not even between its creation and the first
    change of its settings."""
    configure(db, team, "reader")
    carol = {"Authorization": f"Bearer {token_for(database, 'carol')}"}
    created = client.post(
        "/api/v1/organizations/acme/spaces",
        json={"slug": "rh", "name": "RH", "restricted": True},
        headers=carol,
    )
    assert created.status_code == 201, created.text
    assert created.json()["restricted"] is True
    token = as_user(client, "carol")
    form = select_one(client.get("/o/acme/-/new-space").text, data_test("new-space-restricted"))
    assert form.get("name") == "restricted"
    assert_done(
        post(
            client,
            "/o/acme/-/spaces",
            token,
            slug="direction",
            name="Direction",
            restricted="true",
            repo_url="https://forge.example.com/acme/direction.git",
        )
    )
    assert_done(
        post(
            client,
            "/o/acme/-/spaces",
            token,
            slug="ouvert",
            name="Ouvert",
            repo_url="https://forge.example.com/acme/ouvert.git",
        )
    )
    db.expire_all()
    acme = team.wiki.infra.organization
    rh, direction, opened = (
        spaces.get_space(db, team.wiki.root, acme, slug) for slug in ("rh", "direction", "ouvert")
    )
    assert (rh.restricted, direction.restricted, opened.restricted) == (True, True, False)
    assert role(db, "dora", rh) is None and role(db, "dora", direction) is None
    assert role(db, "dora", opened) == "reader"  # a space created afterwards follows the default
    logged = db.scalars(
        select(AuditEvent.space_id).where(AuditEvent.action == "space.restricted")
    ).all()
    assert sorted(map(str, logged)) == sorted([str(rh.id), str(direction.id)])
    # By default, as before.
    plain = client.post(
        "/api/v1/organizations/acme/spaces", json={"slug": "s", "name": "S"}, headers=carol
    )
    assert plain.status_code == 201 and plain.json()["restricted"] is False


# --- Other checks (independent verification) ----------------------------------------------------


def test_restricted_tokens_of_an_admin_change_no_setting(
    db: Session, client: TestClient, team: Team
) -> None:
    carol = load_actor(db, accounts.get_user(db, "carol"))
    tokens = [
        accounts.create_api_token(db, carol, "infra", space_ids=[team.wiki.infra.id])[0],
        accounts.create_api_token(db, carol, "lecture", read_only=True)[0],
        accounts.create_api_token(db, carol, "inbox", inbox_only=True)[0],
    ]
    db.commit()
    for token in tokens:
        headers = {"Authorization": f"Bearer {token}"}
        org = client.patch(
            "/api/v1/organizations/acme", json={"default_space_role": "reader"}, headers=headers
        )
        assert org.status_code == 403, org.text
        space = client.patch(
            "/api/v1/organizations/acme/spaces/infra", json={"restricted": True}, headers=headers
        )
        assert space.status_code == 403, space.text
    db.expire_all()
    assert team.wiki.infra.organization.default_space_role == "none"
    assert not team.wiki.infra.restricted


def test_forged_requests_and_other_organizations_change_nothing(
    db: Session, client: TestClient, team: Team
) -> None:
    as_user(client, "carol")
    for url, data in (
        ("/o/acme/-/default-role", {"default_space_role": "contributor"}),
        ("/o/acme/infra/-/access", {"restricted": "true"}),
    ):
        forged = client.post(url, data={"csrf": "faux", **data}, follow_redirects=False)
        assert forged.status_code in (303, 400, 403)
    token = as_user(client, "carol")  # Acme admin, invisible Globex
    assert (
        post(client, "/o/globex/-/default-role", token, default_space_role="none").status_code
        == 404
    )
    assert post(client, "/o/globex/ventes/-/access", token, restricted="true").status_code == 404
    db.expire_all()
    assert team.wiki.infra.organization.default_space_role == "none"
    assert team.ventes.organization.default_space_role == "contributor"
    assert not team.ventes.restricted and not team.wiki.infra.restricted


def test_open_sessions_and_old_tokens_follow_changes(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    """Permissions are loaded on every request: a signed-in session and a token created
    before the change follow it at once, both ways."""
    full = {"Authorization": f"Bearer {token_for(database, 'dora')}"}
    as_user(client, "dora")
    assert client.get("/o/acme/autre/p/secret.md").status_code == 404
    assert client.get("/api/v1/spaces", headers=full).json() == []
    configure(db, team, "reader")
    assert client.get("/o/acme/autre/p/secret.md").status_code == 200
    assert {s["slug"] for s in client.get("/api/v1/spaces", headers=full).json()} == {
        "infra",
        "autre",
    }
    spaces.update_space(db, team.wiki.root, team.wiki.other, restricted=True)
    db.commit()
    assert client.get("/o/acme/autre/p/secret.md").status_code == 404


def test_each_organization_keeps_its_own_default_role(db: Session, team: Team) -> None:
    configure(db, team, "reader")
    root, acme = team.wiki.root, team.wiki.infra.organization
    new = spaces.create_space(db, root, acme, slug="neuf", name="Neuf")
    dora = accounts.get_user(db, "dora")
    organizations.set_member(db, root, team.ventes.organization, dora, OrgRole.MEMBER)
    db.commit()
    assert role(db, "dora", new) == "reader"
    assert role(db, "dora", team.wiki.infra) == "reader"
    assert role(db, "dora", team.ventes) == "contributor"
    limited = load_actor(db, dora, limits=Limits(read_only=True))
    assert set(limited.access.space_roles.values()) == {SpaceRole.READER}
    assert not limited.access.is_org_admin(acme.id)


def test_members_read_the_setting_of_their_organization_only(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    configure(db, team, "reader")
    dora = {"Authorization": f"Bearer {token_for(database, 'dora')}"}
    acme = client.get("/api/v1/organizations/acme", headers=dora).json()
    assert acme["default_space_role"] == "reader"
    assert client.get("/api/v1/organizations/globex", headers=dora).status_code == 404
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    created = client.post(
        "/api/v1/organizations", json={"slug": "initech", "name": "Initech"}, headers=root
    )
    assert created.status_code == 201 and created.json()["default_space_role"] == "none"
