"""Web sessions: they end after a period without use, and each person sees theirs and can
close them."""

import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.server import create_app
from synaptane.services import accounts
from synaptane.storage.db import Database
from synaptane.storage.models import WebSession
from tests.integration.conftest import actor_of, make_user
from tests.integration.test_web import csrf, login

FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0"
PHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.1 Mobile/15E148 Safari/604.1"
)


@pytest.fixture
def other(settings: Settings, database: Database) -> Iterator[TestClient]:
    """A second browser."""
    with TestClient(create_app(settings, database), base_url="http://localhost") as client:
        yield client


def test_a_session_unused_for_too_long_ends(db: Session) -> None:
    alice = make_user(db, "alice")
    token, web_session = accounts.open_web_session(db, alice, 30, FIREFOX, "203.0.113.7")
    assert web_session.client == FIREFOX and web_session.address == "203.0.113.7"

    web_session.last_seen_at -= timedelta(days=2)
    found = accounts.resolve_web_session(db, token, idle_days=7)
    assert found is not None and found[1].last_seen_at > web_session.created_at  # seen now

    web_session.last_seen_at -= timedelta(days=8)
    assert accounts.resolve_web_session(db, token, idle_days=0) is not None  # no idle limit
    web_session.last_seen_at -= timedelta(days=8)
    assert accounts.resolve_web_session(db, token, idle_days=7) is None
    assert db.scalar(select(WebSession).where(WebSession.user_id == alice.id)) is None


def test_my_sessions_are_listed_and_can_be_closed(
    client: TestClient, other: TestClient, db: Session
) -> None:
    make_user(db, "alice")
    db.commit()
    client.headers["User-Agent"] = FIREFOX
    other.headers["User-Agent"] = PHONE
    login(client, "alice")
    login(other, "alice")

    page = client.get("/account/security").text
    assert page.count('data-test="session"') == 2
    assert "Firefox · Linux" in page and "Safari · iOS" in page and "Cette session" in page
    ids = re.findall(r'action="/account/sessions/([0-9a-f-]+)/close"', page)
    assert len(ids) == 1  # this session has no "Sign out" of its own: the menu has one

    response = client.post(
        f"/account/sessions/{ids[0]}/close", data={"csrf": csrf(client, "/account/security")}
    )
    assert "Session fermée." in response.text
    assert other.get("/", follow_redirects=False).headers["location"].startswith("/login")
    assert client.get("/", follow_redirects=False).status_code == 200


def test_signing_out_of_all_other_sessions_keeps_this_one(
    client: TestClient, other: TestClient, db: Session
) -> None:
    alice = make_user(db, "alice")
    bob = make_user(db, "bob")
    _, bobs = accounts.open_web_session(db, bob, 30)
    db.commit()
    login(client, "alice")
    login(other, "alice")

    token = csrf(client, "/account/security")
    refused = client.post(f"/account/sessions/{bobs.id}/close", data={"csrf": token})
    assert "existe plus." in refused.text  # not Bob's to close
    response = client.post("/account/sessions/close-others", data={"csrf": token})
    assert "Autres sessions fermées." in response.text
    assert other.get("/", follow_redirects=False).status_code == 303
    assert accounts.list_web_sessions(db, actor_of(db, bob)) != []
    assert len(accounts.list_web_sessions(db, actor_of(db, alice))) == 1
