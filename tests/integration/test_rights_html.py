"""Web permission checks, each from three angles.

A test "the reader does not see 'Add a member'" still passes the day the label changes, while
no longer checking anything; and a hidden button protects nothing if the route accepts the
`POST`. For each restricted action, we therefore check:

1. that the account without the right does not see the element, found by its `data-test`;
2. that the account with the right sees it, by the same `data-test` (otherwise 1 proves nothing);
3. that the `POST` from the account without the right is refused and changes nothing, then that
   the same `POST` from the authorized account succeeds (so the refused request was well formed).

Web form refusals do not always answer 403: most redirect (303) to the original page with the
`FORBIDDEN` error message. We accept both, but require that exact message: a refusal for
another reason (invalid field) would prove nothing about permissions.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole
from synaptane.services import content, organizations
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.htmlutil import data_test, select, select_one
from tests.integration.conftest import PASSWORD, make_user
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

FORBIDDEN = "Action non autorisée."


@pytest.fixture
def carol(db: Session, wiki: Wiki) -> Iterator[str]:
    """Admin of the Acme organization, without being super-admin: her role, not a global
    privilege, is what must reveal member management."""
    user = make_user(db, "carol")
    organizations.set_member(db, wiki.root, wiki.infra.organization, user, OrgRole.ADMIN)
    db.commit()
    yield "carol"


def as_user(client: TestClient, username: str) -> str:
    """Switch account and return a CSRF token for the new session."""
    client.cookies.clear()
    login(client, username)
    return csrf(client, "/account")


def snapshot(database: Database) -> dict[str, list[tuple[object, ...]]]:
    """What a refusal must not change: accounts, organizations, members, spaces,
    invitations. Read in a fresh session, to see what the server committed."""
    queries = {
        "users": "SELECT username, is_superadmin FROM users",
        "orgs": "SELECT slug, name FROM organizations",
        "org_members": "SELECT o.slug, u.username, m.role FROM org_members m"
        " JOIN users u ON u.id = m.user_id JOIN organizations o ON o.id = m.org_id",
        "space_members": "SELECT s.slug, u.username, m.role FROM space_members m"
        " JOIN users u ON u.id = m.user_id JOIN spaces s ON s.id = m.space_id",
        "spaces": "SELECT slug, repo_url, branch FROM spaces",
        "invitations": "SELECT email, org_role FROM invitations",
    }
    with database.session() as session:
        return {
            name: sorted(tuple(row) for row in session.execute(text(sql)))
            for name, sql in queries.items()
        }


def head(ctx: Workspace, wiki: Wiki) -> str | None:
    return ctx.local_repository(wiki.infra).head()


def post(client: TestClient, url: str, token: str, **data: str) -> Response:
    """A form `POST`, without following the redirect: its status code matters."""
    return client.post(url, data={"csrf": token, **data}, follow_redirects=False)


def assert_refused(client: TestClient, response: Response, message: str = FORBIDDEN) -> None:
    """Refusal for lack of permission: 403 or 404; otherwise the expected error message, on the
    rendered page (400) or on the one we are redirected to (303, "flash" message)."""
    if response.status_code in (403, 404):
        return
    if response.status_code == 303:
        shown = client.get(response.headers["location"])
        if shown.status_code in (403, 404):
            # Redirected to a page closed to them (organization members,
            # administration): the message waits for the next page.
            shown = client.get("/account")
        html = shown.text
    else:
        assert response.status_code == 400, (response.status_code, response.text[:300])
        html = response.text
    alerts = [alert.text for alert in select(html, '.notice-error[role="alert"]')]
    assert message in alerts, (response.status_code, alerts)


def assert_done(response: Response) -> None:
    assert response.status_code == 303, (response.status_code, response.text[:300])


# --- Space members ----------------------------------------------------------------------------


def test_space_member_forms_are_shown_to_org_admins_only(
    client: TestClient, wiki: Wiki, carol: str
) -> None:
    for username in ("bob", "alice"):  # reader, contributor: the list, without a form
        as_user(client, username)
        page = client.get("/o/acme/infra/-/members")
        assert page.status_code == 200
        assert "@bob" in select_one(page.text, "table").text  # it is indeed the members page
        assert select(page.text, data_test("member-add")) == []
        assert select(page.text, data_test("member-role")) == []

    as_user(client, carol)
    html = client.get("/o/acme/infra/-/members").text
    add = select_one(html, data_test("member-add"))
    assert add.action == "/o/acme/infra/-/members" and add.get("method") == "post"
    assert select_one(add, 'input[name="username"]') and select_one(add, 'select[name="role"]')
    assert len(select(html, data_test("member-role"))) == 2  # Alice and Bob


def test_reader_cannot_change_space_members_server_side(
    client: TestClient, database: Database, wiki: Wiki, carol: str
) -> None:
    before = snapshot(database)
    for username in ("bob", "alice"):
        token = as_user(client, username)
        attempts = [
            ("/o/acme/infra/-/members", {"username": username, "role": "contributor"}),
            ("/o/acme/infra/-/members", {"username": "carol", "role": "contributor"}),
            ("/o/acme/infra/-/members/alice/remove", {}),
        ]
        for url, data in attempts:
            assert_refused(client, post(client, url, token, **data))
    assert snapshot(database) == before

    token = as_user(client, carol)  # the same request, by someone who has the right
    assert_done(post(client, "/o/acme/infra/-/members", token, username="bob", role="contributor"))
    assert ("infra", "bob", "contributor") in snapshot(database)["space_members"]


# --- Organization members and invitations -----------------------------------------------------


def test_org_members_page_is_for_org_admins_only(
    client: TestClient, wiki: Wiki, carol: str
) -> None:
    as_user(client, "bob")  # plain member of Acme (through his space)
    assert client.get("/o/acme/-/members").status_code == 404
    assert select(client.get("/o/acme").text, data_test("member-add")) == []

    as_user(client, carol)
    page = client.get("/o/acme/-/members").text
    assert select_one(page, data_test("member-add")).action == "/o/acme/-/members"
    assert select_one(page, data_test("invite-form")).action == "/o/acme/-/invitations"
    assert len(select(page, data_test("member-role"))) == 3  # Alice, Bob, Carol


def test_member_cannot_change_org_members_or_invite_server_side(
    client: TestClient, database: Database, wiki: Wiki, carol: str
) -> None:
    before = snapshot(database)
    token = as_user(client, "bob")
    attempts = [
        ("/o/acme/-/members", {"username": "bob", "role": "admin"}),  # promotion
        ("/o/acme/-/members", {"username": "carol", "role": "member"}),  # demotion
        ("/o/acme/-/members/alice/remove", {}),
        ("/o/acme/-/invitations", {"email": "dora@example.com", "org_role": "admin"}),
    ]
    for url, data in attempts:
        assert_refused(client, post(client, url, token, **data))
    assert snapshot(database) == before

    token = as_user(client, carol)
    assert_done(post(client, "/o/acme/-/members", token, username="bob", role="admin"))
    invited = post(
        client, "/o/acme/-/invitations", token, email="dora@example.com", org_role="member"
    )
    assert_done(invited)
    after = snapshot(database)
    assert ("acme", "bob", "admin") in after["org_members"]
    assert after["invitations"] == [("dora@example.com", "member")]


# --- Page creation and editing ----------------------------------------------------------------


def test_create_and_edit_buttons_are_shown_to_contributors_only(
    client: TestClient, wiki: Wiki
) -> None:
    as_user(client, "bob")
    absent = client.get("/o/acme/infra/p/guides/absent.md")
    assert absent.status_code == 404
    assert select(absent.text, data_test("page-create")) == []
    page = client.get("/o/acme/infra/p/serveurs/proxmox.md").text
    assert select_one(page, "h1").text == "Proxmox"  # the page is indeed displayed
    assert select(page, data_test("page-edit")) == []

    as_user(client, "alice")
    absent = client.get("/o/acme/infra/p/guides/absent.md")
    create = select_one(absent.text, data_test("page-create"))
    assert create.href == "/o/acme/infra/new?folder=guides&name=absent.md"
    page = client.get("/o/acme/infra/p/serveurs/proxmox.md").text
    assert select_one(page, data_test("page-edit")).href == "/o/acme/infra/edit/serveurs/proxmox.md"


def test_reader_cannot_create_or_edit_pages_server_side(
    client: TestClient, ctx: Workspace, wiki: Wiki
) -> None:
    original = content.read_file(wiki.root, ctx, wiki.infra, "serveurs/proxmox.md")
    before = head(ctx, wiki)
    token = as_user(client, "bob")
    created = post(
        client, "/o/acme/infra/-/new", token, folder="guides", name="absent.md", text="# Pirate\n"
    )
    edited = post(
        client,
        "/o/acme/infra/edit/serveurs/proxmox.md",
        token,
        text="# Écrasé\n",
        base_sha=original.sha,
    )
    for response in (created, edited):
        assert_refused(client, response)
    assert head(ctx, wiki) == before  # no commit
    assert client.get("/o/acme/infra/p/guides/absent.md").status_code == 404
    page = content.read_file(wiki.root, ctx, wiki.infra, "serveurs/proxmox.md")
    assert page.text == original.text

    token = as_user(client, "alice")
    assert_done(
        post(client, "/o/acme/infra/-/new", token, folder="guides", name="absent.md", text="# G\n")
    )
    assert head(ctx, wiki) != before
    assert client.get("/o/acme/infra/p/guides/absent.md").status_code == 200


# --- Administration ---------------------------------------------------------------------------


def test_administration_link_is_shown_to_superadmins_only(
    client: TestClient, wiki: Wiki, carol: str
) -> None:
    for username in ("alice", carol):  # even an organization admin: no link
        as_user(client, username)
        for path in ("/", "/account"):
            page = client.get(path)
            assert page.status_code == 200
            assert select(page.text, data_test("admin-link")) == []
            assert select(page.text, 'a[href="/admin"]') == []

    as_user(client, "root")
    for path in ("/", "/account"):
        assert select_one(client.get(path).text, data_test("admin-link")).href == "/admin"


def test_administration_routes_refuse_other_accounts(
    client: TestClient, database: Database, wiki: Wiki, carol: str
) -> None:
    before = snapshot(database)
    for username in ("alice", carol):
        token = as_user(client, username)
        for path in ("/admin", "/admin/users", "/admin/users/bob", "/admin/sso"):
            assert client.get(path).status_code == 403, path
        attempts = [
            ("/admin/organizations", {"slug": "pirate", "name": "Pirate"}),
            (
                "/admin/users",
                {"username": "mallory", "display_name": "M", "password": PASSWORD},
            ),
            (
                "/admin/users/bob",
                {"display_name": "Bob", "email": "", "is_superadmin": "true"},
            ),
        ]
        for url, data in attempts:
            assert_refused(client, post(client, url, token, **data))
    assert snapshot(database) == before

    token = as_user(client, "root")
    assert_done(post(client, "/admin/organizations", token, slug="neuve", name="N"))
    assert ("neuve", "N") in snapshot(database)["orgs"]


# --- Submitting to the inbox ------------------------------------------------------------------


def test_deposit_button_is_shown_to_contributors_only(client: TestClient, wiki: Wiki) -> None:
    as_user(client, "bob")
    for path in ("/", "/o/acme/infra"):
        assert select(client.get(path).text, data_test("deposit-link")) == []

    as_user(client, "alice")
    home = client.get("/").text
    # Sidebar and home page; `next` returns to the original page after submitting.
    links = [a.href for a in select(home, data_test("deposit-link"))]
    assert len(links) == 2 and all((href or "").startswith("/deposit") for href in links), links
    space = client.get("/o/acme/infra").text
    href = select_one(space, data_test("deposit-link")).href or ""
    assert href.startswith("/deposit?space=acme/infra")


def test_reader_cannot_deposit_server_side(client: TestClient, ctx: Workspace, wiki: Wiki) -> None:
    before = head(ctx, wiki)
    token = as_user(client, "bob")
    # The general form only offers spaces one can submit to: Bob has none, he gets a 400
    # and the "no space to submit to" page, without a form.
    anywhere = post(client, "/deposit", token, space="acme/infra", title="Intrus", body="Texte")
    assert anywhere.status_code == 400
    assert select(anywhere.text, 'form[action="/deposit"]') == []
    # The space inbox refuses too (400); the reader has no form there, hence no message:
    # the refusal is silent, but there is no commit.
    inbox = post(client, "/o/acme/infra/-/inbox", token, title="Intrus", body="Texte")
    assert inbox.status_code == 400
    assert select(inbox.text, 'form[action="/o/acme/infra/-/inbox"]') == []
    assert head(ctx, wiki) == before

    token = as_user(client, "alice")
    assert_done(
        post(client, "/deposit", token, space="acme/infra", title="Switch", body="24 ports")
    )
    assert head(ctx, wiki) != before


# --- Git repository URL -----------------------------------------------------------------------


def test_repository_form_is_shown_to_org_admins_only(
    client: TestClient, wiki: Wiki, carol: str
) -> None:
    for username in ("bob", "alice"):
        as_user(client, username)
        for path in ("/o/acme/infra/-/settings", "/o/acme/infra/-/git"):
            page = client.get(path)
            assert page.status_code == 200
            assert select(page.text, data_test("repo-form")) == []
            assert select(page.text, '[action$="/-/repository"]') == []

    as_user(client, carol)
    form = select_one(client.get("/o/acme/infra/-/git").text, data_test("repo-form"))
    assert form.action == "/o/acme/infra/-/repository"
    assert select_one(form, 'input[name="repo_url"]').get("value") == wiki.infra.repo_url


def test_reader_cannot_change_the_repository_server_side(
    client: TestClient, database: Database, wiki: Wiki, carol: str
) -> None:
    before = snapshot(database)
    for username in ("bob", "alice"):
        token = as_user(client, username)
        attempts = [
            ("/o/acme/infra/-/repository", {"repo_url": "https://evil.example/x.git"}),
            ("/o/acme/infra/-/update", {"name": "Piraté", "repo_url": "https://evil.example/"}),
        ]
        for url, data in attempts:
            assert_refused(client, post(client, url, token, **data))
    assert snapshot(database) == before

    token = as_user(client, carol)
    repo_url = wiki.infra.repo_url
    assert repo_url is not None
    assert_done(
        post(client, "/o/acme/infra/-/repository", token, repo_url=repo_url, branch="autre")
    )
    assert ("infra", repo_url, "autre") in snapshot(database)["spaces"]
