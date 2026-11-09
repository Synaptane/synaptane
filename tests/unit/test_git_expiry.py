"""Expiry of the Git access tokens: alert steps, the sync error that says the token expired,
and the expiry dates read from the providers' answers (docs/admin/git.md)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime

import httpx2
import pytest

from synaptane.core.errors import SynaptaneError
from synaptane.git.credentials import GitCredentials
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
    ("days", "expired", "expected"),
    [
        (365, False, None),
        (31, False, None),
        (30, False, 30),
        (8, False, 30),
        (7, False, 7),
        (2, False, 7),
        (1, False, 1),
        (0, False, 1),  # GitHub, its last day before the hour: still the last step before
        (0, True, 0),
        (-40, True, 0),
    ],
)
def test_alert_steps(days: int, expired: bool, expected: int | None) -> None:
    assert git_expiry.step(days, expired) == expected


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_a_gitlab_day_counts_as_expired_from_its_first_moment() -> None:
    expires_at = GitCredentials.https("t", expiry=date(2027, 11, 8)).expires_at
    assert expires_at == at("2027-11-08T00:00:00")
    assert git_expiry.state(expires_at, at("2027-11-07T23:59:00")) == git_expiry.State(1, False)
    assert git_expiry.state(expires_at, at("2027-11-08T00:00:00")) == git_expiry.State(0, True)
    assert git_expiry.state(expires_at, at("2027-10-09T08:00:00")) == git_expiry.State(30, False)


def test_a_github_moment_expires_at_its_hour() -> None:
    credentials = GitCredentials.https("t", expiry=at("2027-11-08T07:00:00"))
    assert credentials.expires == "2027-11-08T07:00:00+00:00"
    assert credentials.expiry == date(2027, 11, 8)
    stored = GitCredentials.from_json(credentials.to_json())
    assert stored.expires_at == at("2027-11-08T07:00:00")
    assert git_expiry.state(stored.expires_at, at("2027-11-08T06:59:00")) == git_expiry.State(
        0, False
    )  # "expires today"
    assert git_expiry.state(stored.expires_at, at("2027-11-08T07:00:00")).expired


@pytest.mark.parametrize("value", ["", "soon", "2027-13-01", "2027-11-08Tnoon"])
def test_an_unreadable_expiry_is_unknown(value: str) -> None:
    credentials = GitCredentials(kind="https", token="t", expires=value)
    assert credentials.expires_at is None and credentials.expiry is None


@pytest.mark.parametrize(
    "token", ["glpat-abc\u200bdef", "glpat-\u00a0x", "glpat-\tx", "glpat-\x7fx", "a\u2028b"]
)
def test_a_token_with_an_invisible_character_is_refused(token: str) -> None:
    with pytest.raises(SynaptaneError) as raised:
        GitCredentials.https(token)
    assert raised.value.message.startswith("Invalid access token")


@pytest.mark.parametrize("token", ["mot de passe", "pässwörd", "  glpat-x  "])
def test_a_basic_password_may_hold_spaces_and_accents(token: str) -> None:
    """The field also takes a Basic password (Gitea, Bitbucket Server); outer spaces go."""
    assert GitCredentials.https(token).token == token.strip()


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
        ("2027-03-01 07:00:00 UTC", datetime(2027, 3, 1, 7, tzinfo=UTC)),
        ("2027-03-01 00:30:00 +0100", datetime(2027, 2, 28, 23, 30, tzinfo=UTC)),
        ("2027-03-01 23:30:00 -0800", datetime(2027, 3, 2, 7, 30, tzinfo=UTC)),
        ("", None),
        ("tomorrow", None),
        ("2027-03-01", None),
    ],
)
def test_github_expiry_header(header: str, expected: datetime | None) -> None:
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


@pytest.mark.parametrize(
    "url",
    [
        "https://[2001:4860::1]/acme/infra.git",  # an IP address: never asked
        "https://140.82.112.3/acme/infra.git",
    ],
)
def test_no_call_for_an_ip_address_or_an_unusable_name(
    no_call: list[httpx2.Request], url: str
) -> None:
    assert forges.token_expiry(url, "glpat-secret", allow_private=True) is None
    assert no_call == []


@pytest.mark.parametrize(
    "url",
    [
        "https://faß.de/a.git",  # fass.de for Python's IDNA 2003, xn--fa-hia.de for Git's
        "https://gïtlab.example/acme/infra.git",
        "https://xn--fa-hia.de.évil/a.git",
    ],
)
def test_an_internationalized_name_is_never_asked(no_call: list[httpx2.Request], url: str) -> None:
    """Two IDNA versions can name two different domains: the token never goes to a guess."""
    assert forges.token_expiry(url, "glpat-x", allow_private=True) is None
    assert no_call == []


def test_a_password_that_is_not_ascii_is_never_sent(no_call: list[httpx2.Request]) -> None:
    """It could not go in an HTTP header; `save_https_token` does not even ask."""
    assert forges.token_expiry("https://gitlab.com/a.git", "pässwörd") is None


def test_any_unexpected_error_is_swallowed_without_its_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("glpat-secret leaked in a message")

    monkeypatch.setattr(forges, "TRANSPORT", httpx2.MockTransport(handle))
    url = "https://gitlab.com/acme/infra.git"
    assert forges.token_expiry(url, "glpat-secret") is None
    assert "RuntimeError" in caplog.text and "glpat-secret" not in caplog.text


def test_a_day_is_written_out_in_the_readers_language() -> None:
    """Alert details keep a day in ISO form; interface texts and emails write it out."""
    from synaptane.core.errors import Label
    from synaptane.services import mail

    day = date(2027, 11, 1)
    assert (
        translator("fr")("Expiry date: {value}", value=day)
        == "Date d'expiration : 1er novembre 2027"
    )
    assert (
        translator("en")(Label("Expiry date: {value}", value=day))
        == "Expiry date: November 1, 2027"
    )
    assert mail.localized(day, "fr") == "1er novembre 2027"
    assert mail.localized(day, "en") == "November 1, 2027"
