"""Git access tokens that expire (docs/admin/git.md): alerts to the organization admins at 30,
7 and 1 day before then on the day (once each), the Health page, the Git page, the sync error
after the expiry, and the expiry read from the provider when an admin saves a token by hand.

The providers' APIs are simulated (`httpx2.MockTransport`, the `forge` fixture of the
repository creation tests): no test reaches the network. The token never appears in an alert,
an email, the audit log, a log line, a page or an answer.
"""

import html
import json
import logging
import threading
import time
import types
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.core.access import SpaceRole
from synaptane.core.security import SecretBox
from synaptane.git.credentials import GitCredentials
from synaptane.git.repo import GitError, Repository, describe_error, explain
from synaptane.server import create_app
from synaptane.services import (
    accounts,
    alerts,
    forges,
    git_expiry,
    git_sync,
    instance,
    jobs,
    mail,
    organizations,
    spaces,
)
from synaptane.services.git_sync import GIT_CREDENTIALS
from synaptane.services.secrets import get_space_secret, put_space_secret
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Job, Secret, Space, User
from synaptane.web.i18n import format_day, translator
from synaptane.worker import runner
from tests.integration.conftest import actor_of, token_for
from tests.integration.test_alert_mails import PUBLIC_URL, admins, by_recipient, smtp  # noqa: F401
from tests.integration.test_create_repository import (  # noqa: F401 — shared fixtures
    Answer,
    Forge,
    acme,
    box_of,
    forge,
    resolving,
)
from tests.integration.test_invitations import Inbox
from tests.integration.test_space_settings_web import flash
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

SECRET = "glpat-SyncTokenNeverInAnAlert"
EXPIRES = date(2027, 11, 8)
NBSP = " "


def noon(day: date) -> datetime:
    """Midday UTC of a day: the moment the worker's check runs, in these tests."""
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


def give_token(
    db: Session, box: SecretBox, space: Space, expires: date | None, token: str = SECRET
) -> None:
    credentials = GitCredentials.https(token, expiry=expires)
    put_space_secret(db, box, space.id, GIT_CREDENTIALS, credentials.to_json(), None)
    db.commit()


def alerts_of(db: Session) -> list[tuple[str, dict[str, Any]]]:
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.action.startswith("alert.")).order_by(AuditEvent.at)
    )
    return [(e.action, e.details) for e in events]


def assert_no_secret(db: Session, *texts: str) -> None:
    for event in db.scalars(select(AuditEvent)):
        assert SECRET not in json.dumps(event.details)
    assert all(SECRET not in text for text in texts)


# --- Alerts at each step -----------------------------------------------------------------------


def test_one_alert_at_each_step_never_twice(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    days = [40, 31, 30, 30, 29, 20, 8, 7, 7, 3, 2, 1, 1, 0, 0, -1, -30]
    sent = [git_expiry.check(database, ctx, now=noon(EXPIRES - timedelta(days=d))) for d in days]
    assert sent == [0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0]
    found = alerts_of(db)
    assert [(action, details["once"]) for action, details in found] == [
        ("alert.git_token_expiring", "2027-11-08/30"),
        ("alert.git_token_expiring", "2027-11-08/7"),
        ("alert.git_token_expiring", "2027-11-08/1"),
        ("alert.git_token_expired", "2027-11-08/0"),
    ]
    # Details in English (the log), the date written out; never the token.
    assert [details.get("when") for _, details in found] == [
        "in 30 days",
        "in 7 days",
        "tomorrow",
        None,
    ]
    assert {details["date"] for _, details in found} == {"2027-11-08"}  # written out when shown
    assert_no_secret(db)


def test_a_late_worker_alerts_only_the_step_reached(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Off for weeks (or a token saved 5 days before its expiry): the 7-day step only."""
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    assert git_expiry.check(database, ctx, now=noon(EXPIRES - timedelta(days=5))) == 1
    assert [d["once"] for _, d in alerts_of(db)] == ["2027-11-08/7"]
    assert [d["when"] for _, d in alerts_of(db)] == ["in 5 days"]


def test_a_renewed_token_starts_over(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    assert git_expiry.check(database, ctx, now=noon(EXPIRES)) == 1  # expired
    renewed = EXPIRES + timedelta(days=365)
    give_token(db, ctx.box, wiki.infra, renewed)
    assert git_expiry.check(database, ctx, now=noon(EXPIRES + timedelta(days=1))) == 0  # far away
    assert git_expiry.check(database, ctx, now=noon(renewed - timedelta(days=30))) == 1
    assert [d["once"] for _, d in alerts_of(db)] == ["2027-11-08/0", "2028-11-07/30"]


def test_each_space_has_its_own_steps(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    give_token(db, ctx.box, wiki.other, EXPIRES)
    assert git_expiry.check(database, ctx, now=noon(EXPIRES - timedelta(days=7))) == 2
    events = db.scalars(select(AuditEvent).where(AuditEvent.action.startswith("alert.")))
    assert {e.space_id for e in events} == {wiki.infra.id, wiki.other.id}


def test_quiet_without_a_known_expiry(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    give_token(db, ctx.box, wiki.infra, None)  # entered by hand, the provider said nothing
    ssh = GitCredentials(kind="ssh", private_key="key", known_hosts="github.com ssh-ed25519 A")
    put_space_secret(db, ctx.box, wiki.other.id, GIT_CREDENTIALS, ssh.to_json(), None)
    db.commit()
    assert git_expiry.expiries(db, ctx.box, within=None) == []
    assert git_expiry.check(database, ctx, now=noon(EXPIRES)) == 0
    assert alerts_of(db) == []


def test_an_unreadable_secret_is_skipped(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Master key replaced: the Health page says so in its own line; nothing breaks here."""
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    give_token(db, ctx.box, wiki.other, EXPIRES)
    other = SecretBox(SecretBox.generate_key())
    put_space_secret(db, other, wiki.other.id, GIT_CREDENTIALS, "{}", None)
    db.commit()
    found = git_expiry.expiries(db, ctx.box, now=noon(EXPIRES))
    assert [e.space.id for e in found] == [wiki.infra.id]


def test_a_space_without_repository_is_left_out(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.repo_url = None
    db.commit()
    assert git_expiry.expiries(db, ctx.box, now=noon(EXPIRES)) == []


def test_the_alerts_reach_the_org_admins_in_their_language(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    admins: dict[str, User],  # noqa: F811
    smtp: Inbox,  # noqa: F811
) -> None:
    """The organization's admins (ada in French, grace in English), as for every space alert;
    not Alice (contributor, with an address)."""
    ws = replace(ctx, public_url=PUBLIC_URL)
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    assert git_expiry.check(database, ws, now=noon(EXPIRES - timedelta(days=7))) == 1
    mails = by_recipient(smtp)
    assert set(mails) == {"ada@example.com", "grace@example.com"}
    link = f"{PUBLIC_URL}/o/acme/infra/-/git"
    french_subject, french_body = mails["ada@example.com"]
    english_subject, english_body = mails["grace@example.com"]
    assert french_subject == "Le jeton d'accès Git de « Infra » expire dans 7 jours"
    assert english_subject == "Git access token of “Infra” expires in 7 days"
    assert "expire le 8 novembre 2027 (dans 7 jours)" in french_body
    assert "expires on November 8, 2027 (in 7 days)" in english_body
    assert link in french_body and link in english_body
    assert "Settings → Access tokens" in french_body and "Contents" in english_body

    smtp.messages.clear()
    assert git_expiry.check(database, ws, now=noon(EXPIRES - timedelta(days=1))) == 1
    mails = by_recipient(smtp)
    assert mails["ada@example.com"][0] == "Le jeton d'accès Git de « Infra » expire demain"
    assert mails["grace@example.com"][0] == "Git access token of “Infra” expires tomorrow"

    smtp.messages.clear()
    assert git_expiry.check(database, ws, now=noon(EXPIRES)) == 1
    mails = by_recipient(smtp)
    assert mails["ada@example.com"][0] == "Le jeton d'accès Git de « Infra » a expiré"
    assert mails["grace@example.com"][0] == "Git access token of “Infra” expired"
    assert "a expiré le 8 novembre 2027" in mails["ada@example.com"][1]
    assert "expired on November 8, 2027" in mails["grace@example.com"][1]

    smtp.messages.clear()
    assert git_expiry.check(database, ws, now=noon(EXPIRES + timedelta(days=2))) == 0
    assert smtp.messages == []
    assert_no_secret(db, *(m.as_string() for m in smtp.messages))


def test_no_secret_in_any_mail(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    admins: dict[str, User],  # noqa: F811
    smtp: Inbox,  # noqa: F811
) -> None:
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    for days in (30, 7, 1, 0):
        git_expiry.check(database, ctx, now=noon(EXPIRES - timedelta(days=days)))
    assert len(smtp.messages) == 8  # 4 steps, 2 admins
    assert_no_secret(db, *(message.as_string() for message in smtp.messages))


def test_the_journal_shows_the_alert(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, settings: Settings
) -> None:
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    git_expiry.check(database, ctx, now=noon(EXPIRES - timedelta(days=7)))
    with TestClient(create_app(settings, database), base_url="http://localhost") as client:
        login(client, "root")
        journal = html.unescape(client.get("/o/acme/-/journal").text)
        health = html.unescape(client.get("/admin/health").text)
    assert "Un jeton d'accès Git expire bientôt" in journal
    assert "Un jeton d'accès Git expire bientôt" in health  # system alerts
    # The expiry date, in both, written out in the reader's language.
    assert f"Date d'expiration{NBSP}: 8 novembre 2027" in journal
    assert f"Date d'expiration{NBSP}: 8 novembre 2027" in health
    assert SECRET not in journal + health


def test_the_worker_checks_every_hour(
    settings: Settings,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[Workspace] = []

    def check(database: Database, ws: Workspace, now: datetime | None = None) -> int:
        calls.append(ws)
        raise RuntimeError("broken")

    def broken(*args: object) -> None:
        raise RuntimeError("lasting failure")

    monkeypatch.setattr(git_expiry, "check", check)
    # A machine booted a few seconds ago: the monotonic clock is near 0, the first
    # maintenance still makes its hourly checks.
    clock = [5.0]
    fake_time = types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time, sleep=time.sleep)
    monkeypatch.setattr("synaptane.worker.runner.time", fake_time)  # the worker's clock only
    # The checks run before it fail too: each one on its own, they do not skip it.
    monkeypatch.setattr(alerts, "check_backlogs", broken)
    monkeypatch.setattr(runner.Worker, "advance_idle_watermarks", broken)
    worker = runner.Worker(settings, database, ctx)
    with caplog.at_level(logging.WARNING):
        worker.maintenance()  # a failure here must not stop the maintenance
    assert calls == [ctx]
    assert "Git token expiry not checked" in caplog.text
    assert "Inbox backlog not checked" in caplog.text
    assert "Change watermarks not moved" in caplog.text
    clock[0] += 61
    worker.maintenance()  # a minute later: not again within the hour
    assert calls == [ctx]
    clock[0] += 3600
    worker.maintenance()  # an hour later: again
    assert calls == [ctx, ctx]


# --- Health page -------------------------------------------------------------------------------


def test_health_lists_the_tokens_expiring_or_expired(
    settings: Settings, database: Database, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    today = git_expiry.today()
    title = "Git access tokens"

    def check() -> instance.Check:
        found = instance.report(db, database, ctx, settings).checks
        return next(c for c in found if c.title == title)

    assert check().state == instance.OK
    assert check().message.template == "None expires within 30 days."
    give_token(db, ctx.box, wiki.infra, today + timedelta(days=45))
    assert check().state == instance.OK  # too far to tell
    give_token(db, ctx.box, wiki.infra, today + timedelta(days=10))
    found = check()
    assert found.state == instance.WARN and found.guide == "git"
    assert [(t.space.id, t.days) for t in found.tokens] == [(wiki.infra.id, 10)]
    assert found.message.params == {"count": 1}
    give_token(db, ctx.box, wiki.other, today - timedelta(days=2))
    found = check()
    assert found.state == instance.ERROR
    assert [(t.space.id, t.days, t.expired) for t in found.tokens] == [
        (wiki.other.id, -2, True),  # the most urgent first
        (wiki.infra.id, 10, False),
    ]
    assert found.message.params == {"count": 1}
    assert instance.report(db, database, ctx, settings).state == instance.ERROR


def test_health_page_links_each_space_to_its_git_page(
    settings: Settings, database: Database, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    today = git_expiry.today()
    give_token(db, ctx.box, wiki.infra, today + timedelta(days=10))
    give_token(db, ctx.box, wiki.other, today - timedelta(days=2))
    alice = accounts.get_user(db, "alice")
    alice.locale = "en"
    db.commit()
    with TestClient(create_app(settings, database), base_url="http://localhost") as client:
        login(client, "root")
        page = html.unescape(client.get("/admin/health").text)
        client.cookies.clear()
        login(client, "alice")
        refused = client.get("/admin/health")
    assert refused.status_code == 403
    assert "Jetons d'accès Git" in page and 'data-test="health-git-tokens"' in page
    assert '<a href="/o/acme/infra/-/git">acme/infra</a>' in page
    assert '<a href="/o/acme/autre/-/git">acme/autre</a>' in page
    assert f"expiré le {format_fr(today - timedelta(days=2))}" in page
    assert f"expire le {format_fr(today + timedelta(days=10))} (dans 10 jours)" in page
    assert "Jetons expirés" in page and "Settings → Access tokens" in page
    assert "docs/admin/git.fr.md" in page and "Renouveler un jeton d'accès Git" in page
    assert page.index("acme/autre/-/git") < page.index("acme/infra/-/git")
    assert SECRET not in page


def test_health_page_in_english_tomorrow(
    settings: Settings, database: Database, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    root = accounts.get_user(db, "root")
    root.locale = "en"
    give_token(db, ctx.box, wiki.infra, git_expiry.today() + timedelta(days=1))
    with TestClient(create_app(settings, database), base_url="http://localhost") as client:
        login(client, "root")
        page = html.unescape(client.get("/admin/health").text)
    assert "Git access tokens" in page and "(tomorrow)" in page
    assert "How to renew a Git access token" in page and "docs/admin/git.md" in page


def format_fr(day: date) -> str:
    return format_day(day, "fr")


# --- Git page ----------------------------------------------------------------------------------


@pytest.fixture
def hosted(db: Session, wiki: Wiki) -> dict[str, Space]:
    """Two Acme spaces on HTTPS forges (never synchronized here)."""
    org = organizations.get_organization(db, wiki.root, "acme")
    found = {
        host: spaces.create_space(
            db, wiki.root, org, slug=host, name=host, repo_url=f"https://{host}.com/acme/x.git"
        )
        for host in ("github", "gitlab")
    }
    db.commit()
    return found


def test_git_page_says_the_token_expired(
    client: TestClient, db: Session, wiki: Wiki, hosted: dict[str, Space]
) -> None:
    box = box_of(client)
    give_token(db, box, hosted["github"], git_expiry.today() - timedelta(days=1))
    give_token(db, box, hosted["gitlab"], git_expiry.today() + timedelta(days=5))
    login(client, "root")
    page = html.unescape(client.get("/o/acme/github/-/git").text)
    assert (
        'class="notice notice-error" data-test="git-token-expiry" data-soon="true" '
        'data-expired="true"' in page
    )
    assert (
        "Le jeton d'accès a expiré le" in page and "GitHub" + NBSP + ": jeton fine-grained" in page
    )
    assert "prévenus 30, 7 et 1 jour avant" in page
    page = html.unescape(client.get("/o/acme/gitlab/-/git").text)
    assert (
        'class="notice notice-warning" data-test="git-token-expiry" data-soon="true" '
        'data-expired="false"' in page
    )
    assert "Le jeton d'accès expire le" in page and "portées read_repository" in page
    assert SECRET not in page


def test_git_page_shows_no_expiry_to_a_reader(client: TestClient, db: Session, wiki: Wiki) -> None:
    give_token(db, box_of(client), wiki.infra, git_expiry.today() - timedelta(days=1))
    login(client, "bob")
    page = html.unescape(client.get("/o/acme/infra/-/git").text)
    assert 'data-test="git-token-expiry"' not in page


# --- The sync says the token expired -----------------------------------------------------------


def refuse_fetch(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    def fetch(self: Repository) -> None:
        raise GitError(explain("fetch", output))

    monkeypatch.setattr(Repository, "fetch", fetch)


DENIED = "remote: HTTP Basic: Access denied\nfatal: Authentication failed for 'https://gitlab'"


def sync_error(database: Database, ctx: Workspace, space: Space) -> str:
    with pytest.raises(GitError):
        git_sync.run_sync(database, ctx, space.id)
    with database.session() as session:
        found = session.get(Space, space.id)
        assert found is not None and found.sync_status == "error"
        return found.sync_error or ""


def test_a_refused_sync_after_the_expiry_says_the_token_expired(
    settings: Settings,
    database: Database,
    db: Session,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired = git_expiry.today() - timedelta(days=3)
    give_token(db, ctx.box, wiki.infra, expired)
    refuse_fetch(monkeypatch, DENIED)
    error = sync_error(database, ctx, wiki.infra)
    assert error.startswith(f"The access token expired on {expired.isoformat()}: create a new")
    assert "HTTP Basic: Access denied" in error and SECRET not in error
    french = describe_error(error, translator("fr"), "fr")
    assert french.startswith(f"Le jeton d'accès a expiré le {format_fr(expired)}")
    # The space's page and the API say it too, in their reader's language.
    with TestClient(create_app(settings, database), base_url="http://localhost") as client:
        login(client, "root")
        page = html.unescape(client.get("/o/acme/infra/-/git").text)
        headers = {
            "Authorization": f"Bearer {token_for(database, 'root')}",
            "Accept-Language": "en",
        }
        status = client.get("/api/v1/organizations/acme/spaces/infra/git", headers=headers).json()
    assert f"Le jeton d'accès a expiré le {format_fr(expired)}" in page
    assert status["error"].startswith("The access token expired on ")
    assert status["credentials_expire"] == expired.isoformat()
    # The sync alert (persistent error) carries the same explanation.
    events = [d for a, d in alerts_of(db) if a == "alert.sync_error"]
    assert events and events[0]["error"].startswith("The access token expired on ")


def test_a_refusal_before_the_expiry_keeps_its_message(
    database: Database, db: Session, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    give_token(db, ctx.box, wiki.infra, git_expiry.today() + timedelta(days=3))
    refuse_fetch(monkeypatch, DENIED)
    assert sync_error(database, ctx, wiki.infra).startswith(
        "The repository requires authentication"
    )


def test_another_failure_after_the_expiry_keeps_its_message(
    database: Database, db: Session, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    give_token(db, ctx.box, wiki.infra, git_expiry.today() - timedelta(days=3))
    refuse_fetch(monkeypatch, "remote: Repository not found.\nfatal: repository not found")
    assert sync_error(database, ctx, wiki.infra).startswith("Repository not found")


# --- Expiry read when an admin saves a token by hand -------------------------------------------


@pytest.fixture
def forges_space(db: Session, acme: None) -> dict[str, Space]:  # noqa: F811
    """Acme spaces on github.com, gitlab.com, a self-hosted GitLab and over SSH."""
    actor = actor_of(db, accounts.get_user(db, "root"))
    org = organizations.get_organization(db, actor, "acme")  # the `acme` fixture's
    urls = {
        "github": "https://github.com/acme/infra.git",
        "gitlab": "https://gitlab.com/acme/infra.git",
        "selfhosted": "https://gitlab.example.com:8443/acme/infra.git",
        "ssh": "git@gitlab.com:acme/infra.git",
    }
    found = {
        slug: spaces.create_space(db, actor, org, slug=slug, name=slug, repo_url=url)
        for slug, url in urls.items()
    }
    db.commit()
    return found


MANUAL = "glpat-TokenEnteredByHand0123"


def save(client: TestClient, slug: str, username: str = "carla", **data: str) -> str:
    """Saves MANUAL on the space's Git page: the flash message shown after."""
    login(client, username)
    fields = {"csrf": csrf(client, f"/o/acme/{slug}/-/git"), "token": MANUAL, **data}
    response = client.post(f"/o/acme/{slug}/-/git/https", data=fields, follow_redirects=False)
    assert response.status_code == 303
    assert MANUAL not in json.dumps(dict(response.headers))
    message = flash(client.get(f"/o/acme/{slug}/-/git").text)
    assert message is not None
    return message[1]


def saved(db: Session, box: SecretBox, space: Space) -> GitCredentials:
    db.expire_all()
    raw = get_space_secret(db, box, space.id, GIT_CREDENTIALS)
    assert raw is not None
    return GitCredentials.from_json(raw)


def gitlab_self(forge: Forge, answer: Answer) -> None:  # noqa: F811
    forge.routes[("GET", "/api/v4/personal_access_tokens/self")] = answer


def test_a_gitlab_token_saved_by_hand_gets_its_expiry(
    client: TestClient,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    gitlab_self(forge, httpx2.Response(200, json={"expires_at": "2027-03-01", "scopes": ["api"]}))
    with caplog.at_level(logging.DEBUG):
        message = save(client, "gitlab")
    assert message == "Accès au dépôt enregistré. Le jeton expire le 1er mars 2027."
    assert saved(db, box_of(client), forges_space["gitlab"]).expires == "2027-03-01"
    assert saved(db, box_of(client), forges_space["gitlab"]).token == MANUAL
    [request] = forge.seen
    assert str(request.url) == "https://gitlab.com/api/v4/personal_access_tokens/self"
    assert request.headers["private-token"] == MANUAL  # to the repository's own host only
    page = html.unescape(client.get("/o/acme/gitlab/-/git").text)
    assert "Le jeton d'accès expire le 1er mars 2027" in page
    assert MANUAL not in page and MANUAL not in caplog.text
    for event in db.scalars(select(AuditEvent)):
        assert MANUAL not in json.dumps(event.details)
    actions = [e.action for e in db.scalars(select(AuditEvent))]
    assert "space.git_credentials_set" in actions


def test_a_github_token_saved_by_hand_gets_its_expiry(
    client: TestClient,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
) -> None:
    forge.routes[("GET", "/rate_limit")] = httpx2.Response(
        200,
        json={"resources": {}},
        headers={"GitHub-Authentication-Token-Expiration": "2027-03-01 07:00:00 UTC"},
    )
    message = save(client, "github")
    assert message.endswith("Le jeton expire le 1er mars 2027.")
    assert (
        saved(db, box_of(client), forges_space["github"]).expires == "2027-03-01T07:00:00+00:00"
    )  # the moment
    [request] = forge.seen
    assert str(request.url) == "https://api.github.com/rate_limit"
    assert request.headers["authorization"] == f"Bearer {MANUAL}"


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(200, json={"expires_at": None}),  # no expiry
        httpx2.Response(401, json={"message": "401 Unauthorized"}),
        httpx2.Response(403, json={"error": "insufficient_scope"}),  # no API scope
        httpx2.Response(404, text="not a GitLab"),
        httpx2.Response(200, text="<html>"),
        httpx2.Response(302, headers={"location": "https://elsewhere.example/"}),
    ],
)
def test_without_an_expiry_the_token_is_saved_all_the_same(
    client: TestClient,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
    answer: httpx2.Response,
) -> None:
    gitlab_self(forge, answer)
    assert save(client, "gitlab") == "Accès au dépôt enregistré."
    credentials = saved(db, box_of(client), forges_space["gitlab"])
    assert credentials.token == MANUAL and credentials.expires == ""
    assert [r.url.host for r in forge.seen] == ["gitlab.com"]  # no redirect followed


def test_a_github_token_without_expiry(
    client: TestClient,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
) -> None:
    forge.routes[("GET", "/rate_limit")] = httpx2.Response(200, json={})
    assert save(client, "github") == "Accès au dépôt enregistré."
    assert saved(db, box_of(client), forges_space["github"]).expires == ""


def test_an_unreachable_or_slow_provider_does_not_block_the_save(
    client: TestClient,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host")

    gitlab_self(forge, unreachable)
    assert save(client, "gitlab") == "Accès au dépôt enregistré."

    def slow(request: httpx2.Request) -> httpx2.Response:
        time.sleep(0.2)
        return httpx2.Response(200, json={"expires_at": "2027-03-01"})

    monkeypatch.setattr(forges, "EXPIRY_SECONDS", 0.05)
    gitlab_self(forge, slow)
    assert save(client, "gitlab") == "Accès au dépôt enregistré."
    assert saved(db, box_of(client), forges_space["gitlab"]).expires == ""


def test_a_self_hosted_gitlab_at_a_public_address_is_pinned(
    client: TestClient,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gitlab_self(forge, httpx2.Response(200, json={"expires_at": "2027-03-01"}))
    asked = resolving(monkeypatch, "140.82.112.3")
    assert save(client, "selfhosted").endswith("Le jeton expire le 1er mars 2027.")
    assert set(asked) == {"gitlab.example.com"}
    [request] = forge.seen
    assert request.url.host == "140.82.112.3"  # the address checked
    assert request.headers["host"] == "gitlab.example.com:8443"
    assert request.url.raw_path == b"/api/v4/personal_access_tokens/self"


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"])
def test_a_self_hosted_gitlab_on_an_internal_address_is_not_asked(
    client: TestClient,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    gitlab_self(forge, httpx2.Response(200, json={"expires_at": "2027-03-01"}))
    resolving(monkeypatch, address)
    assert save(client, "selfhosted") == "Accès au dépôt enregistré."
    assert forge.seen == []
    assert saved(db, box_of(client), forges_space["selfhosted"]).token == MANUAL


def test_the_super_admin_reaches_a_gitlab_of_the_local_network(
    client: TestClient,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
) -> None:
    """No name resolution first (the `forge` fixture refuses any): connected as is."""
    gitlab_self(forge, httpx2.Response(200, json={"expires_at": "2027-03-01"}))
    assert save(client, "selfhosted", "root").endswith("Le jeton expire le 1er mars 2027.")
    assert [r.url.host for r in forge.seen] == ["gitlab.example.com"]


def contributor(db: Session, space: Space, username: str = "bob") -> None:
    root = actor_of(db, accounts.get_user(db, "root"))
    spaces.set_member(db, root, space, accounts.get_user(db, username), SpaceRole.CONTRIBUTOR)
    db.commit()


def test_only_an_organization_admin_triggers_a_call(
    client: TestClient,
    database: Database,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
) -> None:
    """A contributor of the space (not admin) is refused before any call; so is a request
    without the form's token."""
    gitlab_self(forge, httpx2.Response(200, json={"expires_at": "2027-03-01"}))
    contributor(db, forges_space["gitlab"])
    login(client, "bob")
    fields = {"csrf": csrf(client, "/o/acme/gitlab/-/git"), "token": MANUAL}
    response = client.post("/o/acme/gitlab/-/git/https", data=fields, follow_redirects=False)
    assert response.status_code == 303  # back to the page, refused
    refused = flash(client.get("/o/acme/gitlab/-/git").text)
    assert refused is not None and refused[0] == "error"
    bearer = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    response = client.put(
        "/api/v1/organizations/acme/spaces/gitlab/git/credentials",
        json={"kind": "https", "token": MANUAL},
        headers=bearer,
    )
    assert response.status_code == 403 and MANUAL not in response.text
    client.cookies.clear()
    login(client, "carla")
    response = client.post(
        "/o/acme/gitlab/-/git/https",
        data={"csrf": "wrong", "token": MANUAL},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert forge.seen == []
    assert db.scalar(select(Secret).where(Secret.space_id == forges_space["gitlab"].id)) is None


def test_a_token_for_an_ssh_address_is_refused_before_any_call(
    client: TestClient,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
) -> None:
    message = save(client, "ssh")
    assert message.startswith("Ces identifiants ne correspondent pas")
    assert forge.seen == []


def test_no_database_transaction_held_during_the_call(
    client: TestClient,
    db: Session,
    database: Database,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin's request gives its connection back while the provider answers."""
    busy: list[int] = []

    def watching(request: httpx2.Request) -> httpx2.Response:
        with database.engine.connect() as connection:
            busy.append(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND state = 'idle in transaction'"
                    )
                )
                or 0
            )
        return httpx2.Response(200, json={"expires_at": "2027-03-01"})

    gitlab_self(forge, watching)
    save(client, "gitlab")
    assert busy == [0]


def test_api_saves_the_expiry_and_tells_it_to_admins(
    client: TestClient,
    database: Database,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
) -> None:
    gitlab_self(forge, httpx2.Response(200, json={"expires_at": "2027-03-01"}))
    admin = {"Authorization": f"Bearer {token_for(database, 'carla')}"}
    url = "/api/v1/organizations/acme/spaces/gitlab/git"
    response = client.put(
        f"{url}/credentials", json={"kind": "https", "token": MANUAL}, headers=admin
    )
    assert response.status_code == 204
    assert saved(db, box_of(client), forges_space["gitlab"]).expires == "2027-03-01"
    status = client.get(url, headers=admin).json()
    assert status["credentials"] == "https" and status["credentials_expire"] == "2027-03-01"
    contributor(db, forges_space["gitlab"])
    member = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    seen = client.get(url, headers=member).json()  # the state, never the access
    assert seen["credentials"] is None and seen["credentials_expire"] is None
    assert MANUAL not in response.text + json.dumps(status)


# --- Second review: delivery, concurrency, readers, GitHub's hour, odd tokens and hosts --------


def test_a_step_whose_mails_all_failed_is_sent_again(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    admins: dict[str, User],  # noqa: F811
    smtp: Inbox,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mail server down at the 7-day step: the next hourly check sends the emails, without a
    second audit event; then nothing more."""
    give_token(db, ctx.box, wiki.infra, EXPIRES)
    real_send = mail.send

    def down(*args: object, **kwargs: object) -> None:
        raise mail.MailError("connection refused")

    monkeypatch.setattr(mail, "send", down)
    moment = noon(EXPIRES - timedelta(days=7))
    assert git_expiry.check(database, ctx, now=moment) == 1  # recorded, not delivered
    assert smtp.messages == []
    [(_, details)] = alerts_of(db)
    assert details[alerts.UNDELIVERED] == "true"
    assert git_expiry.check(database, ctx, now=moment + timedelta(hours=1)) == 0  # still down
    monkeypatch.setattr(mail, "send", real_send)
    assert git_expiry.check(database, ctx, now=moment + timedelta(hours=2)) == 1
    assert set(by_recipient(smtp)) == {"ada@example.com", "grace@example.com"}
    db.expire_all()
    [(_, details)] = alerts_of(db)  # still a single event
    assert alerts.UNDELIVERED not in details and details["once"] == "2027-11-08/7"
    assert git_expiry.check(database, ctx, now=moment + timedelta(hours=3)) == 0
    assert len(smtp.messages) == 2


def test_two_workers_send_an_alert_once(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """The second transaction waits for the first one's lock, then sees its alert."""
    space_id = wiki.infra.id
    first = database.session().__enter__()
    space = first.get(Space, space_id)
    assert space is not None
    assert alerts.notify(first, ctx.box, space, "git_token_expired", once="k", date="d")
    results: list[bool] = []

    def second() -> None:
        with database.session() as session:
            found = session.get(Space, space_id)
            assert found is not None
            results.append(
                alerts.notify(session, ctx.box, found, "git_token_expired", once="k", date="d")
            )

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive()  # waiting for the lock
    first.commit()
    first.close()
    thread.join(10)
    assert results == [False]
    assert len(alerts_of(db)) == 1


def test_a_reader_sees_the_general_authentication_message(
    settings: Settings,
    database: Database,
    db: Session,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token's expiry date is for admins, like the rest of the repository access."""
    give_token(db, ctx.box, wiki.infra, git_expiry.today() - timedelta(days=3))
    refuse_fetch(monkeypatch, DENIED)
    sync_error(database, ctx, wiki.infra)
    with TestClient(create_app(settings, database), base_url="http://localhost") as client:
        login(client, "bob")
        page = html.unescape(client.get("/o/acme/infra/-/git").text)
        headers = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
        url = "/api/v1/organizations/acme/spaces/infra/git"
        status = client.get(url, headers={**headers, "Accept-Language": "en"}).json()
    assert "Le dépôt demande une authentification" in page and "a expiré" not in page
    assert status["error"].startswith("The repository requires authentication")
    assert status["credentials_expire"] is None


def test_a_github_token_expires_at_its_hour(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """GitHub gives the moment: its last day before the hour, "expires today" (the last step
    before), then expired once the hour has passed."""
    moment = datetime(2027, 11, 8, 7, tzinfo=UTC)
    give_token(db, ctx.box, wiki.infra, moment)
    assert git_expiry.check(database, ctx, now=moment - timedelta(hours=2)) == 1
    [(action, details)] = alerts_of(db)
    assert action == "alert.git_token_expiring" and details["when"] == "today"
    assert git_expiry.check(database, ctx, now=moment - timedelta(minutes=1)) == 0
    assert git_expiry.check(database, ctx, now=moment) == 1
    assert [a for a, _ in alerts_of(db)][-1] == "alert.git_token_expired"


def test_health_and_git_page_say_today_before_the_hour(
    client: TestClient, db: Session, wiki: Wiki, hosted: dict[str, Space]
) -> None:
    last = datetime.combine(git_expiry.today(), datetime.max.time(), UTC).replace(microsecond=0)
    give_token(db, box_of(client), hosted["github"], last)
    login(client, "root")
    page = html.unescape(client.get("/o/acme/github/-/git").text)
    assert "Le jeton d'accès expire aujourd'hui" in page and 'data-expired="false"' in page
    health = html.unescape(client.get("/admin/health").text)
    assert f"expire aujourd'hui, {format_fr(git_expiry.today())}" in health
    assert "status-error" not in health.split("health-git-tokens")[1].split("</ul>")[0]


@pytest.mark.parametrize("token", ["glpat-abc\u200bdef", "ghp_abc\tdef", "glpat-\u00a0x"])
@pytest.mark.parametrize("slug", ["gitlab", "github"])
def test_a_token_with_an_invisible_character_is_refused_without_error(
    client: TestClient,
    database: Database,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
    token: str,
    slug: str,
) -> None:
    message = save(client, slug, token=token)
    assert message.startswith("Jeton d'accès invalide")
    bearer = {"Authorization": f"Bearer {token_for(database, 'carla')}"}
    response = client.put(
        f"/api/v1/organizations/acme/spaces/{slug}/git/credentials",
        json={"kind": "https", "token": token},
        headers=bearer,
    )
    assert response.status_code == 400 and token not in response.text
    assert forge.seen == []
    assert db.scalar(select(Secret).where(Secret.space_id == forges_space[slug].id)) is None


@pytest.mark.parametrize("username", ["carla", "root"])
@pytest.mark.parametrize(
    "url", ["https://[2001:4860::1]/acme/x.git", "https://140.82.112.3/acme/x.git"]
)
def test_a_repository_at_an_ip_address_is_saved_without_asking(
    client: TestClient,
    database: Database,
    db: Session,
    acme: None,  # noqa: F811
    forge: Forge,  # noqa: F811
    username: str,
    url: str,
) -> None:
    actor = actor_of(db, accounts.get_user(db, "root"))
    org = organizations.get_organization(db, actor, "acme")
    space = spaces.create_space(db, actor, org, slug="ip", name="IP", repo_url=url)
    db.commit()
    assert save(client, "ip", username) == "Accès au dépôt enregistré."
    bearer = {"Authorization": f"Bearer {token_for(database, username)}"}
    response = client.put(
        "/api/v1/organizations/acme/spaces/ip/git/credentials",
        json={"kind": "https", "token": MANUAL},
        headers=bearer,
    )
    assert response.status_code == 204
    assert forge.seen == []
    assert saved(db, box_of(client), space).token == MANUAL


@pytest.mark.parametrize("username", ["carla", "root"])
def test_an_internationalized_host_is_never_asked(
    client: TestClient,
    database: Database,
    db: Session,
    acme: None,  # noqa: F811
    forge: Forge,  # noqa: F811
    username: str,
) -> None:
    """faß.de is fass.de for one IDNA version, xn--fa-hia.de for another: the token is saved,
    never sent to a guessed domain."""
    gitlab_self(forge, httpx2.Response(200, json={"expires_at": "2027-03-01"}))
    actor = actor_of(db, accounts.get_user(db, "root"))
    org = organizations.get_organization(db, actor, "acme")
    space = spaces.create_space(
        db, actor, org, slug="idn", name="IDN", repo_url="https://faß.de/acme/x.git"
    )
    db.commit()
    assert save(client, "idn", username) == "Accès au dépôt enregistré."
    assert forge.seen == []
    assert saved(db, box_of(client), space).token == MANUAL


@pytest.mark.parametrize("token", ["mot de passe", "pässwörd"])
def test_a_basic_password_with_spaces_or_accents_is_saved(
    client: TestClient,
    database: Database,
    db: Session,
    forges_space: dict[str, Space],
    forge: Forge,  # noqa: F811
    token: str,
) -> None:
    """Gitea, Bitbucket Server: a password in the token field. Saved as it is; one that is
    not plain ASCII is not sent to ask for an expiry (no HTTP header can carry it)."""
    gitlab_self(forge, httpx2.Response(404))
    assert save(client, "selfhosted", "root", token=token) == "Accès au dépôt enregistré."
    assert saved(db, box_of(client), forges_space["selfhosted"]).token == token
    assert len(forge.seen) == (1 if token.isascii() else 0)


def test_abandoned_jobs_come_in_a_fixed_order(db: Session, wiki: Wiki) -> None:
    """`alerts.check_abandoned_jobs` takes its locks in this order: two workers agree on it."""
    for space in (wiki.other, wiki.infra, wiki.other):
        db.add(
            Job(
                kind="index",
                space_id=space.id,
                status="failed",
                finished_at=noon(git_expiry.today()),
                error=f"{jobs.ABANDONED}: no network",
            )
        )
    db.commit()
    found = [job.space_id for job in jobs.abandoned(db, noon(git_expiry.today()) - timedelta(1))]
    assert found == sorted(found, key=str)
