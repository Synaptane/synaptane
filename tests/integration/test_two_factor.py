"""Two-factor authentication: turned on in My account, asked after the password, recovery
codes, required for super admins, turned off by a super admin for a lost phone."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.core import totp
from synaptane.server import create_app
from synaptane.services import accounts
from synaptane.storage.db import Database
from tests.integration.conftest import PASSWORD, make_user
from tests.integration.test_web import csrf, login


def browser(settings: Settings, database: Database, **changes: object) -> TestClient:
    app = create_app(settings.model_copy(update=changes), database)
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def other(settings: Settings, database: Database) -> Iterator[TestClient]:
    """A second browser."""
    with browser(settings, database) as client:
        yield client


def next_code(secret: str) -> str:
    """The code of the next period: still accepted, and not the one just used."""
    return totp.code_at(secret, totp.step_now() + 1)


def turn_on(client: TestClient) -> tuple[str, list[str]]:
    """Sets up two-factor authentication in My account: the secret and the recovery codes."""
    page = client.get("/account/2fa/setup").text
    assert "<svg" in page and 'class="qr-code"' in page
    key = re.search(r'id="totp-key">([A-Z2-7 ]+)<', page)
    sealed = re.search(r'name="sealed" value="([^"]+)"', page)
    assert key is not None and sealed is not None
    secret = key.group(1).replace(" ", "")
    token = csrf(client, "/account/2fa/setup")
    wrong = client.post(
        "/account/2fa/enable", data={"csrf": token, "sealed": sealed.group(1), "code": "000000"}
    )
    assert wrong.status_code == 400 and "Code incorrect" in wrong.text
    done = client.post(
        "/account/2fa/enable",
        data={
            "csrf": token,
            "sealed": sealed.group(1),
            "code": totp.code_at(secret, totp.step_now()),
        },
    )
    assert "Double authentification activée." in done.text
    codes = re.findall(r"<li><code>([a-z2-9]{5}-[a-z2-9]{5})</code></li>", done.text)
    assert len(codes) == 10
    assert "Vos codes de secours" not in client.get("/account/security").text  # shown once
    return secret, codes


def code_step(client: TestClient, username: str, code: str) -> int:
    client.post("/login", data={"username": username, "password": PASSWORD})
    return client.post("/login/code", data={"code": code}, follow_redirects=False).status_code


def test_turned_on_then_asked_after_the_password(
    client: TestClient, other: TestClient, db: Session
) -> None:
    make_user(db, "alice")
    db.commit()
    login(client, "alice")
    login(other, "alice")
    secret, codes = turn_on(client)
    assert other.get("/", follow_redirects=False).status_code == 303  # other session closed
    assert "10 codes de secours restants." in client.get("/account/security").text

    response = other.post(
        "/login",
        data={"username": "alice", "password": PASSWORD, "next": "/account"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/login/code"
    assert other.get("/", follow_redirects=False).status_code == 303  # not signed in yet
    assert "Code de votre application" in other.get("/login/code").text
    assert other.post("/login/code", data={"code": "123456"}).status_code == 401
    signed = other.post("/login/code", data={"code": next_code(secret)}, follow_redirects=False)
    assert signed.status_code == 303 and signed.headers["location"] == "/account"
    assert other.get("/account").status_code == 200
    assert other.get("/login/code", follow_redirects=False).headers["location"] == "/login"

    other.cookies.clear()
    assert code_step(other, "alice", codes[0].upper()) == 303  # a recovery code, once
    other.cookies.clear()
    assert code_step(other, "alice", codes[0]) == 401
    assert "9 codes de secours restants." in client.get("/account/security").text


def test_a_code_cannot_be_guessed(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    login(client, "alice")
    secret, _ = turn_on(client)
    client.cookies.clear()
    client.post("/login", data={"username": "alice", "password": PASSWORD})
    statuses = [client.post("/login/code", data={"code": "000000"}).status_code for _ in range(5)]
    assert statuses == [401] * 5
    blocked = client.post("/login/code", data={"code": next_code(secret)})
    assert blocked.status_code == 429  # even the right one, for 15 minutes


def test_a_password_reset_still_asks_for_the_code(client: TestClient, db: Session) -> None:
    alice = make_user(db, "alice")
    alice.email, alice.email_verified_at = "alice@example.com", datetime.now(UTC)
    db.commit()
    login(client, "alice")
    turn_on(client)
    client.cookies.clear()
    found = accounts.request_password_reset(db, "alice@example.com")
    assert found is not None
    db.commit()
    response = client.post(
        f"/reset/{found[1]}",
        data={"password": "un tout nouveau mot de passe"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/login/code"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_new_recovery_codes_and_turning_it_off(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    login(client, "alice")
    secret, old = turn_on(client)
    token = csrf(client, "/account/security")
    renewed = client.post("/account/2fa/recovery", data={"csrf": token, "code": next_code(secret)})
    new = re.findall(r"<li><code>([a-z2-9]{5}-[a-z2-9]{5})</code></li>", renewed.text)
    assert len(new) == 10 and not set(new) & set(old)
    assert (
        "Code incorrect"
        in client.post("/account/2fa/disable", data={"csrf": token, "code": old[0]}).text
    )  # the old codes stopped working
    off = client.post("/account/2fa/disable", data={"csrf": token, "code": new[0]})
    assert "Double authentification désactivée." in off.text
    client.cookies.clear()
    login(client, "alice")  # the password alone again


def test_required_for_super_admins_and_reset_by_another(
    settings: Settings, database: Database, db: Session
) -> None:
    make_user(db, "root", superadmin=True)
    make_user(db, "alice")
    db.commit()
    with (
        browser(settings, database, superadmin_2fa=True) as root,
        browser(settings, database, superadmin_2fa=True) as alice,
    ):
        login(root, "root")
        response = root.get("/admin", follow_redirects=False)
        assert response.headers["location"] == "/account/2fa/setup"
        forced = root.get("/account/2fa/setup").text
        assert "Les super-admins doivent confirmer" in forced and "Se déconnecter" in forced
        assert root.get("/account/security", follow_redirects=False).status_code == 303
        secret, _ = turn_on(root)
        assert root.get("/admin").status_code == 200
        assert (
            "Désactiver la double authentification</button>"
            not in root.get("/account/security").text
        )  # super admins keep it
        token = csrf(root, "/account/security")
        refused = root.post("/account/2fa/disable", data={"csrf": token, "code": next_code(secret)})
        assert "doivent garder" in refused.text

        login(alice, "alice")  # not required for her
        assert alice.get("/", follow_redirects=False).status_code == 200
        turn_on(alice)
        reset = root.post("/admin/users/alice/2fa/reset", data={"csrf": token})
        assert "Double authentification désactivée." in reset.text
        alice.cookies.clear()
        login(alice, "alice")
        assert alice.get("/", follow_redirects=False).status_code == 200
