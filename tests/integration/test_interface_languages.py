"""Interface texts written in English, translated into French (`locales/fr.json`): REST
errors follow `Accept-Language`, texts computed or stored by the services (Git explanation,
connection test) are shown in each reader's language."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.ai.base import AIError
from synaptane.git.repo import explain
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Space, User
from synaptane.web import tidy_pages
from tests.integration.conftest import token_for
from tests.integration.test_tidy import configure_ai
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

FRENCH = {"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}


def set_locale(db: Session, username: str, locale: str) -> None:
    user = db.query(User).filter_by(username=username).one()
    user.locale = locale
    db.commit()


def test_rest_errors_are_in_english_unless_the_caller_asks_for_french(
    client: TestClient, database: Database, wiki: Wiki
) -> None:
    """The `error` code never changes; the message is English by default (whatever the
    instance's language: French in the tests), French with `Accept-Language: fr`."""
    token = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    english = client.get("/api/v1/organizations/nope", headers=token)
    french = client.get("/api/v1/organizations/nope", headers={**token, **FRENCH})
    assert english.status_code == french.status_code == 404
    assert english.json() == {"error": "not_found", "message": "Organization not found."}
    assert french.json() == {"error": "not_found", "message": "Organisation introuvable."}

    # A parameter that is itself an interface text ("Title") is translated too.
    inbox = "/api/v1/organizations/acme/spaces/infra/inbox"
    body = {"title": " ", "body": "x"}
    assert client.post(inbox, json=body, headers=token).json()["message"] == (
        "Title: between 1 and 120 characters."
    )
    assert client.post(inbox, json=body, headers={**token, **FRENCH}).json()["message"] == (
        "Titre : entre 1 et 120 caractères."
    )
    assert client.get("/api/v1/me", headers=FRENCH).json() == {
        "error": "unauthenticated",
        "message": "Jeton absent, invalide ou expiré.",
    }


@pytest.mark.parametrize(
    ("stored", "english", "french"),
    [
        # Written now (English), and written in French before the switch.
        (
            explain("fetch", "remote: Repository not found."),
            "Repository not found: check the address",
            "Dépôt introuvable : vérifiez l&#39;adresse",
        ),
        (
            "Hôte introuvable : vérifiez l'adresse du dépôt. (git fetch: Could not resolve host)",
            "Host not found: check the repository address. (git fetch: Could not resolve host)",
            "Hôte introuvable : vérifiez l&#39;adresse du dépôt. (git fetch: Could not",
        ),
    ],
)
def test_a_git_explanation_is_shown_in_the_readers_language(
    client: TestClient, db: Session, wiki: Wiki, stored: str, english: str, french: str
) -> None:
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.sync_status, space.sync_error = "error", stored
    db.commit()
    login(client, "root")
    assert french in client.get("/o/acme/infra/-/git").text
    set_locale(db, "root", "en")
    page = client.get("/o/acme/infra/-/git").text
    assert english in page and "Dépôt introuvable" not in page and "Hôte introuvable" not in page


def test_a_connection_test_result_is_stored_then_shown_in_each_language(
    client: TestClient,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message of a failed test: in the flash, then on the AI page of any reader, in
    their language (stored with its values, not as text)."""
    configure_ai(db, ctx, wiki)

    def refused(config: object) -> str:
        raise AIError("Request refused ({status}): {detail}", status=400, detail="bad model")

    monkeypatch.setattr(tidy_pages, "check_connection", refused)
    login(client, "root")
    tested = client.post("/o/acme/-/ai/test", data={"csrf": csrf(client, "/o/acme/-/ai")})
    assert "Requête refusée (400) : bad model" in tested.text
    set_locale(db, "root", "en")
    assert "Request refused (400): bad model" in client.get("/o/acme/-/ai").text
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    assert client.get("/api/v1/organizations/acme/ai", headers=root).json()["test_message"] == (
        "Request refused (400): bad model"
    )
