"""Instance default language (`default_locale`): English unless configured otherwise.

It applies when the browser asks for no supported language, and to accounts created
without a browser (administrator, REST, CLI). The test suite runs in French
(`conftest.settings`); here, an instance with the default setting, in English.
"""

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.server import create_app
from synaptane.services import accounts, organizations
from synaptane.storage.db import Database
from tests.integration.conftest import PASSWORD, actor_of, make_user, token_for
from tests.integration.test_web import csrf, login

GERMAN = {"Accept-Language": "de-DE,de;q=0.9"}


@pytest.fixture
def english(settings: Settings, database: Database) -> Iterator[TestClient]:
    """Instance with the default setting (English)."""
    default = settings.model_copy(update={"default_locale": "en"})
    with TestClient(create_app(default, database), base_url="http://localhost") as client:
        yield client


def test_a_browser_in_an_unsupported_language_gets_the_instance_default(
    english: TestClient, client: TestClient
) -> None:
    assert "Sign in" in english.get("/login", headers=GERMAN).text
    missing = english.get("/nulle-part/ici/encore", headers=GERMAN)
    assert missing.status_code == 404 and 'lang="en"' in missing.text
    # A browser asking for a supported language gets it, whatever the setting.
    assert "Connexion" in english.get("/login", headers={"Accept-Language": "fr-FR"}).text
    # Instance set to French (`default_locale = "fr"`): same browser, French.
    assert "Connexion" in client.get("/login", headers=GERMAN).text


def test_accounts_created_without_a_browser_take_the_instance_default(
    english: TestClient, db: Session, database: Database
) -> None:
    make_user(db, "root", superadmin=True)
    db.commit()
    login(english, "root")
    token = csrf(english, "/admin/users")
    english.post(
        "/admin/users",
        data={"csrf": token, "username": "web", "display_name": "Web", "password": PASSWORD},
    )
    headers = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    created = english.post(
        "/api/v1/users", json={"username": "rest", "display_name": "Rest"}, headers=headers
    )
    assert created.status_code == 201 and created.json()["locale"] == "en"
    db.expire_all()
    assert accounts.get_user(db, "web").locale == "en"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [(GERMAN, "en"), ({"Accept-Language": "fr-CH, en;q=0.5"}, "fr")],
)
def test_an_invited_account_takes_the_language_of_its_browser(
    english: TestClient,
    db: Session,
    database: Database,
    headers: dict[str, str],
    expected: str,
) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    organizations.create_organization(db, root, slug="acme", name="Acme")
    db.commit()
    created = english.post(
        "/api/v1/organizations/acme/invitations",
        json={"email": "dana@example.com"},
        headers={"Authorization": f"Bearer {token_for(database, 'root')}"},
    )
    link = created.json()["link"]
    match = re.search(r"/invite/[\w-]+", link)
    assert match is not None
    english.post(
        match.group(0),
        data={"username": "dana", "display_name": "Dana", "password": PASSWORD},
        headers=headers,
    )
    db.expire_all()
    assert accounts.get_user(db, "dana").locale == expected


def test_an_unsupported_locale_is_refused_at_creation(db: Session) -> None:
    from synaptane.core.errors import SynaptaneError

    with pytest.raises(SynaptaneError, match="language"):
        accounts.create_user(
            db, None, username="zoe", display_name="Zoé", password=PASSWORD, locale="de"
        )
