"""Expiry of the Git access tokens: alert steps, the sync error that says the token expired,
and the expiry dates read from the providers' answers (docs/admin/git.md)."""

from collections.abc import Iterator
from datetime import date

import httpx2
import pytest

from synaptane.git.repo import (
    AUTHENTICATION_REQUIRED,
    TOKEN_EXPIRED,
    describe_error,
    expired_error,
    explain,
)
from synaptane.services import forges, git_expiry
from synaptane.web.i18n import translator

REFUSED = explain("fetch", "remote: HTTP Basic: Access denied\nfatal: Authentication failed")


@pytest.mark.parametrize(
    ("days", "expected"),
    [(365, None), (31, None), (30, 30), (8, 30), (7, 7), (2, 7), (1, 1), (0, 0), (-40, 0)],
)
def test_alert_steps(days: int, expected: int | None) -> None:
    assert git_expiry.step(days) == expected


def test_days_left_counts_the_expiry_day_as_expired() -> None:
    expires = date(2027, 11, 8)
    assert git_expiry.days_left(expires, date(2027, 11, 7)) == 1
    assert git_expiry.days_left(expires, date(2027, 11, 8)) == 0
    assert git_expiry.days_left(expires, date(2027, 10, 9)) == 30


def test_an_authentication_refusal_says_the_token_expired() -> None:
    assert REFUSED.startswith(AUTHENTICATION_REQUIRED)
    stored = expired_error(REFUSED, date(2027, 11, 1))
    assert stored.startswith(TOKEN_EXPIRED.format(date="2027-11-01"))
    assert stored.endswith("Access denied\nfatal: Authentication failed)")
    french = describe_error(stored, translator("fr"), "fr")
    assert french.startswith("Le jeton d'accès a expiré le 1er novembre 2027\u00a0: créez-en")
    assert "(git fetch: remote: HTTP Basic" in french
    english = describe_error(stored, translator("en"), "en")
    assert english.startswith("The access token expired on November 1, 2027: create a new one")
    # Without a language, the date is written out in English.
    assert describe_error(stored, translator("en")) == english


def test_other_errors_are_kept_as_they_are() -> None:
    missing = explain("fetch", "fatal: repository 'https://gitlab.com/x.git/' not found")
    assert expired_error(missing, date(2027, 11, 1)) == missing
    assert expired_error("git fetch: early EOF", date(2027, 11, 1)) == "git fetch: early EOF"


def test_an_impossible_date_is_shown_as_stored() -> None:
    stored = TOKEN_EXPIRED.format(date="2027-13-45") + " (git fetch: denied)"
    shown = describe_error(stored, translator("fr"), "fr")
    assert shown.startswith("Le jeton d'accès a expiré le 2027-13-45\u00a0:")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("2027-03-01 07:00:00 UTC", date(2027, 3, 1)),
        ("2027-03-01 00:30:00 +0100", date(2027, 2, 28)),  # the day in UTC
        ("2027-03-01 23:30:00 -0800", date(2027, 3, 2)),
        ("", None),
        ("tomorrow", None),
        ("2027-03-01", None),
    ],
)
def test_github_expiry_header(header: str, expected: date | None) -> None:
    assert forges._github_expiry(header) == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"expires_at": "2027-03-01"}, date(2027, 3, 1)),
        ({"expires_at": "2027-03-01T00:00:00.000Z"}, date(2027, 3, 1)),
        ({"expires_at": None}, None),  # a token without expiry
        ({"expires_at": "soon"}, None),
        ({}, None),
        (["2027-03-01"], None),
        (None, None),
    ],
)
def test_gitlab_expiry_answer(answer: object, expected: date | None) -> None:
    assert forges._gitlab_expiry(answer) == expected


@pytest.fixture
def no_call(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(500)

    monkeypatch.setattr(forges, "TRANSPORT", httpx2.MockTransport(handle))
    yield seen


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.example.com/acme/infra.git",  # never over plain HTTP
        "git@gitlab.com:acme/infra.git",
        "ssh://git@gitlab.com/acme/infra.git",
        "https://gitlab.example.com:99999/acme/infra.git",
        "https:///acme/infra.git",
        "",
    ],
)
def test_no_call_for_an_address_that_is_not_https(no_call: list[httpx2.Request], url: str) -> None:
    assert forges.token_expiry(url, "glpat-secret") is None
    assert no_call == []
