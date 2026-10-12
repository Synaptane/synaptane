import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane import version_label
from synaptane.core.access import SpaceRole
from synaptane.services import organizations, spaces
from tests.integration.conftest import PASSWORD, actor_of, make_user


def login(client: TestClient, username: str, password: str = PASSWORD) -> None:
    response = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


def csrf(client: TestClient, path: str = "/") -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match is not None
    return match.group(1)


def test_anonymous_is_redirected_to_login(client: TestClient) -> None:
    response = client.get("/o/acme", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/o/acme"
    page = client.get("/login")
    assert "Content-Security-Policy" in page.headers
    assert "Connexion" in page.text


def test_bad_credentials(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    response = client.post("/login", data={"username": "alice", "password": "nope"})
    assert response.status_code == 401
    assert "Identifiants incorrects." in response.text


def test_login_rejects_cross_site_origin(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    response = client.post(
        "/login",
        data={"username": "alice", "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 401


def test_superadmin_creates_org_space_and_member(client: TestClient, db: Session) -> None:
    make_user(db, "root", superadmin=True)
    make_user(db, "alice")
    db.commit()
    login(client, "root")
    token = csrf(client, "/admin")
    assert f"Synaptane {version_label()}" in client.get("/admin").text  # what is running

    client.post("/admin/organizations", data={"csrf": token, "slug": "acme", "name": "Acme"})
    response = client.post(
        "/o/acme/-/spaces",
        data={"csrf": token, "slug": "infra", "name": "Infra", "branch": "main"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/o/acme/infra/-/git"  # next step: the repository
    client.post(
        "/o/acme/infra/-/members", data={"csrf": token, "username": "alice", "role": "reader"}
    )
    page = client.get("/o/acme/infra/-/members")
    assert "Espace créé." not in page.text  # the flash is shown only once
    assert "@alice" in page.text and "Lecteur" in page.text


def test_post_without_csrf_is_refused(client: TestClient, db: Session) -> None:
    make_user(db, "root", superadmin=True)
    db.commit()
    login(client, "root")
    response = client.post(
        "/admin/organizations", data={"csrf": "forged", "slug": "x", "name": "X"}
    )
    assert response.status_code == 403


def test_reader_sees_space_without_management_forms(client: TestClient, db: Session) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    alice = make_user(db, "alice")
    acme = organizations.create_organization(db, root, slug="acme", name="Acme")
    infra = spaces.create_space(db, root, acme, slug="infra", name="Infra")
    spaces.create_space(db, root, acme, slug="secret", name="Secret")
    spaces.set_member(db, root, infra, alice, SpaceRole.READER)
    db.commit()
    login(client, "alice")

    home = client.get("/").text
    assert "Infra" in home and "Secret" not in home
    page = client.get("/o/acme/infra/-/settings").text  # reader: the repository state
    assert "Adresse du dépôt" not in page and "/-/tidy-settings" not in page
    assert "Ajouter un membre" not in client.get("/o/acme/infra/-/members").text
    for hidden in ("/-/tidy-settings", "/-/semantic"):
        assert client.get(f"/o/acme/infra{hidden}").status_code == 404
    assert client.get("/o/acme/-/ai").status_code == 404
    assert client.get("/o/acme/secret").status_code == 404
    assert client.get("/o/acme/secret/-/settings").status_code == 404
    assert client.get("/admin").status_code == 403


def test_validation_error_is_flashed_and_rolled_back(client: TestClient, db: Session) -> None:
    make_user(db, "root", superadmin=True)
    db.commit()
    login(client, "root")
    token = csrf(client, "/admin")
    response = client.post("/admin/organizations", data={"csrf": token, "slug": "Bad", "name": "X"})
    assert "Identifiant invalide" in response.text
    assert "Aucune organisation" in response.text


def test_token_is_shown_once_and_language_switch(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    login(client, "alice")
    token = csrf(client, "/account")
    page = client.post("/account/tokens", data={"csrf": token, "name": "Claude Code", "days": "30"})
    assert "il ne sera plus affiché" in page.text
    assert re.search(r">syn_[\w-]+</code>", page.text)
    assert "claude mcp add" in page.text  # command ready to paste
    assert not re.search(r">syn_[\w-]+</code>", client.get("/account/assistants").text)

    client.post(
        "/account",
        data={"csrf": token, "display_name": "Alice", "locale": "en", "email": ""},
    )
    assert "My account" in client.get("/account").text


def test_language_is_saved_apart_from_the_profile(client: TestClient, db: Session) -> None:
    """Preferences: the language has its own form; the profile is saved without it."""
    make_user(db, "alice")
    db.commit()
    login(client, "alice")
    token = csrf(client, "/account")
    saved = client.post("/account/locale", data={"csrf": token, "locale": "en"})
    assert "Language saved." in saved.text and "My account" in saved.text
    renamed = client.post("/account", data={"csrf": token, "display_name": "Alice B."})
    assert "Alice B." in renamed.text and "My account" in renamed.text  # language unchanged
    refused = client.post("/account/locale", data={"csrf": token, "locale": "xx"})
    assert "Unsupported language." in refused.text


def test_logout_closes_the_session(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    login(client, "alice")
    client.post("/logout", data={"csrf": csrf(client)})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_repeated_failed_logins_are_throttled(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    for _ in range(10):
        bad = client.post("/login", data={"username": "alice", "password": "faux"})
        assert bad.status_code == 401
    blocked = client.post("/login", data={"username": "alice", "password": PASSWORD})
    assert blocked.status_code == 429 and "Trop de tentatives" in blocked.text


def test_new_secrets_are_shown_once_and_never_from_the_url(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    login(client, "alice")
    token = csrf(client, "/account")
    created = client.post("/account/tokens", data={"csrf": token, "name": "scripts"})
    shown = re.search(r">(syn_[\w-]+)</code>", created.text)
    assert created.url.path == "/account/assistants" and shown  # redirect: F5 has no effect
    assert shown.group(1) not in client.get("/account/assistants").text  # only once
    forged = client.get("/o/acme", params={"new_webhook_secret": "whsec_FAUX"})
    assert "whsec_FAUX" not in forged.text


def test_saturated_pool_answers_503_quickly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from synaptane.services import accounts

    def busy(*args: object) -> None:
        raise PoolTimeout("QueuePool limit reached")

    monkeypatch.setattr(accounts, "authenticate", busy)
    response = client.post("/login", data={"username": "alice", "password": "x"})
    assert response.status_code == 503 and "réessayez" in response.text


def test_readiness_checks_the_database(client: TestClient) -> None:
    assert client.get("/readyz").json() == {"status": "ok"}


def test_irreversible_deletion_needs_the_identifier_typed(client: TestClient, db: Session) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    acme = organizations.create_organization(db, root, slug="acme", name="Acme")
    spaces.create_space(db, root, acme, slug="infra", name="Infra")
    db.commit()
    login(client, "root")
    token = csrf(client, "/o/acme/infra/-/settings")
    refused = client.post("/o/acme/infra/-/delete", data={"csrf": token, "confirm": "oups"})
    assert "tapez exactement" in refused.text
    assert client.get("/o/acme/infra").status_code == 200
    gone = client.post("/o/acme/infra/-/delete", data={"csrf": token, "confirm": "infra"})
    assert gone.url.path == "/o/acme" and client.get("/o/acme/infra").status_code == 404


def test_unknown_pages_and_admin_sections(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    missing = client.get("/nimporte/quoi")
    assert missing.status_code == 404 and "Page introuvable" in missing.text
    login(client, "alice")
    for path in ("/admin", "/admin/users", "/admin/sso", "/admin/users/alice"):
        assert client.get(path).status_code == 403
    assert client.get("/account/security").status_code == 200
