"""Before publishing the code: bounded SSO sign-up (automatic creation disabled by default,
allowed domains), rate-limited e-mails, login log without the typed text."""

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.services import accounts, invitations, organizations, sso
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import EmailCheck, SSOProvider, User, UserIdentity
from tests.integration.conftest import PASSWORD, actor_of, make_user, token_for
from tests.integration.test_invitations import Inbox, org_admin_setup, smtp  # noqa: F401 — fixture
from tests.integration.test_sso_webhooks import FakeIdP, add_provider, idp, sso_login  # noqa: F401
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

# --- SSO: automatic creation and domains ------------------------------------------------------


def test_a_new_provider_creates_no_account_by_default(
    client: TestClient,
    db: Session,
    database: Database,
    ctx: Workspace,
    idp: FakeIdP,  # noqa: F811
) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    provider = sso.save_provider(
        db,
        root,
        ctx.box,
        slug="corp",
        kind="oidc",
        name="Corp",
        client_id="client",
        client_secret="s3cret",
        issuer=idp.url,
    )
    db.commit()
    assert provider.auto_create is False and provider.allowed_domains == []
    refused = sso_login(
        client, idp, {"sub": "n-1", "email": "x@example.com", "email_verified": True}
    )
    assert refused.headers["location"] == "/login"
    assert "Aucun compte" in client.get("/login").text
    assert db.scalar(select(User).where(User.email == "x@example.com")) is None

    headers = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    body = {"kind": "github", "name": "GitHub", "client_id": "cid", "client_secret": "cs"}
    saved = client.put("/api/v1/sso-providers/github", json=body, headers=headers).json()
    assert saved["auto_create"] is False and saved["allowed_domains"] == []
    assert saved["open_to_anyone"] is False


def test_allowed_domains_bound_creation_and_linking(
    client: TestClient,
    db: Session,
    ctx: Workspace,
    idp: FakeIdP,  # noqa: F811
) -> None:
    outsider = make_user(db, "outsider")
    outsider.email, outsider.email_verified_at = "out@other.net", datetime.now(UTC)
    insider = make_user(db, "insider")
    insider.email, insider.email_verified_at = "in@example.org", datetime.now(UTC)
    db.commit()
    add_provider(
        db, ctx, idp, link_by_email=True, allowed_domains=["Example.com, @example.org", ""]
    )
    provider = db.get(SSOProvider, "corp")
    assert provider is not None and provider.allowed_domains == ["example.com", "example.org"]

    for sub, email in (
        ("u-1", "x@other.net"),
        ("u-2", "out@other.net"),
        ("u-3", "y@a.example.com"),
    ):
        refused = sso_login(client, idp, {"sub": sub, "email": email, "email_verified": True})
        assert refused.headers["location"] == "/login"
        assert "pas admise" in client.get("/login").text
    assert db.scalars(select(UserIdentity)).all() == []  # neither created nor linked (exact domain)
    assert db.scalar(select(User).where(User.email == "x@other.net")) is None

    created = sso_login(
        client, idp, {"sub": "u-4", "email": "new@example.com", "email_verified": True}
    )
    assert created.headers["location"] == "/account"
    client.cookies.clear()
    sso_login(client, idp, {"sub": "u-5", "email": "in@example.org", "email_verified": True})
    linked = db.scalars(select(UserIdentity).where(UserIdentity.subject == "u-5")).one()
    assert linked.user_id == insider.id


def test_google_hosted_domain_must_be_allowed_when_given(
    client: TestClient,
    db: Session,
    ctx: Workspace,
    idp: FakeIdP,  # noqa: F811
) -> None:
    add_provider(db, ctx, idp, allowed_domains=["example.com"])
    profile = {"sub": "g-1", "email": "eve@example.com", "email_verified": True}
    refused = sso_login(client, idp, {**profile, "hd": "partner.example.net"})
    assert refused.headers["location"] == "/login"
    assert db.scalars(select(User).where(User.email == "eve@example.com")).all() == []
    accepted = sso_login(client, idp, {**profile, "hd": "example.com"})
    assert accepted.headers["location"] == "/account"


def test_invalid_domains_are_refused(db: Session, ctx: Workspace) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    with pytest.raises(SynaptaneError, match="Invalid email domain"):
        sso.save_provider(
            db,
            root,
            ctx.box,
            slug="gh",
            kind="github",
            name="GitHub",
            client_id="cid",
            client_secret="cs",
            allowed_domains=["example.com", "pas_un_domaine"],
        )


def test_a_provider_open_to_anyone_is_flagged(
    client: TestClient, db: Session, database: Database, wiki: Wiki
) -> None:
    login(client, "root")
    form = {
        "csrf": csrf(client, "/admin/sso"),
        "slug": "github",
        "kind": "github",
        "name": "GitHub",
        "client_id": "cid",
        "client_secret": "cs",
        "auto_create": "true",
        "enabled": "true",
    }
    saved = client.post("/admin/sso", data=form)
    assert "mais ouvert à tous" in saved.text and 'data-test="sso-open"' in saved.text
    headers = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    [listed] = client.get("/api/v1/sso-providers", headers=headers).json()
    assert listed["open_to_anyone"] is True

    saved = client.post("/admin/sso", data={**form, "allowed_domains": "example.com"})
    assert "Fournisseur de connexion enregistré" in saved.text
    assert 'data-test="sso-open"' not in saved.text and "domaines autorisés" in saved.text
    [listed] = client.get("/api/v1/sso-providers", headers=headers).json()
    assert listed["open_to_anyone"] is False and listed["allowed_domains"] == ["example.com"]


# --- Sending limits ----------------------------------------------------------------------------


def test_verification_links_are_limited_per_account_and_hour(
    client: TestClient,
    db: Session,
    smtp: Inbox,  # noqa: F811
) -> None:
    user = make_user(db, "ines")
    user.email = "ines@example.com"  # to verify
    db.commit()
    login(client, "ines")
    token = csrf(client, "/account")
    for _ in range(accounts.EMAIL_CHECKS_PER_HOUR):
        sent = client.post("/account/email/verify", data={"csrf": token})
        assert "Lien de vérification envoyé" in sent.text
    refused = client.post("/account/email/verify", data={"csrf": token})
    assert "Trop de liens de vérification" in refused.text
    assert len(smtp.messages) == accounts.EMAIL_CHECKS_PER_HOUR

    # Changing address would send a link: refused outright, the address stays the same.
    form = {"csrf": token, "display_name": "Inès", "email": "autre@example.com"}
    changed = client.post("/account", data={**form, "current": PASSWORD})
    assert "Trop de liens de vérification" in changed.text
    db.expire_all()
    assert db.get(User, user.id).email == "ines@example.com"  # type: ignore[union-attr]
    assert len(smtp.messages) == accounts.EMAIL_CHECKS_PER_HOUR

    # An hour later, the limit is lifted.
    db.execute(update(EmailCheck).values(expires_at=EmailCheck.expires_at - timedelta(hours=1)))
    db.commit()
    again = client.post("/account/email/verify", data={"csrf": token})
    assert "Lien de vérification envoyé" in again.text


def test_invitations_are_capped_per_organization_and_hour(
    client: TestClient, db: Session, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(invitations, "PER_HOUR", 3)
    org_admin_setup(db)
    carla = actor_of(db, accounts.get_user(db, "carla"))
    acme = organizations.get_organization(db, carla, "acme")
    invitations.invite(db, carla, acme, email="a@example.com")
    # Re-inviting or cancelling deletes the pending invitation, not the sent e-mail: it counts.
    invitations.invite(db, carla, acme, email="a@example.com")
    [pending] = invitations.pending(db, carla, acme)
    invitations.revoke(db, carla, acme, pending.id)
    invitations.invite(db, carla, acme, email="a@example.com")
    with pytest.raises(SynaptaneError) as refused:
        invitations.invite(db, carla, acme, email="b@example.com")
    assert refused.value.code is ErrorCode.TOO_MANY
    db.commit()

    login(client, "carla")
    token = csrf(client, "/o/acme/-/members")
    sent = client.post("/o/acme/-/invitations", data={"csrf": token, "email": "c@example.com"})
    assert "invitations envoyées depuis une heure" in sent.text and "3 au plus" in sent.text
    headers = {"Authorization": f"Bearer {token_for(database, 'carla')}"}
    rest = client.post(
        "/api/v1/organizations/acme/invitations", json={"email": "d@example.com"}, headers=headers
    )
    assert rest.status_code == 429 and "Too many invitations" in rest.json()["message"]
    assert [i.email for i in invitations.pending(db, carla, acme)] == ["a@example.com"]


# --- Login log ---------------------------------------------------------------------------------


def test_failed_logins_never_log_the_typed_text(
    client: TestClient, db: Session, caplog: pytest.LogCaptureFixture
) -> None:
    alice = make_user(db, "alice")
    db.commit()
    typed = "Mon-Mot-De-Passe-42"  # typed in the username field
    with caplog.at_level(logging.INFO, logger="synaptane.web"):
        for login_ in (typed, "alice"):
            failed = client.post("/login", data={"username": login_, "password": "faux"})
            assert failed.status_code == 401
    assert typed.lower() not in caplog.text.lower()
    digest = hashlib.sha256(typed.lower().encode()).hexdigest()[:8]
    assert f"login:{digest}" in caplog.text and f"id:{alice.id}" in caplog.text
