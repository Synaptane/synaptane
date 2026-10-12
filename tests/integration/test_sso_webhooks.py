import base64
import hashlib
import hmac
import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from synaptane.core.errors import SynaptaneError
from synaptane.services import content, deposits, sso, webhooks
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, SSOProvider, User, UserIdentity, Webhook
from tests.integration.conftest import actor_of, make_user, token_for
from tests.integration.test_web import csrf, login
from tests.integration.waiting import wait_until_started
from tests.integration.wikidata import Wiki


def fake_jwt(claims: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


@dataclass
class FakeIdP:
    """Test identity provider: OIDC (discovery, token with PKCE, userinfo) and GitHub."""

    url: str = ""
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    received: list[tuple[dict[str, str], bytes]] = field(default_factory=list)
    status: int = 200

    def app(self) -> Starlette:
        async def discovery(request: Request) -> Response:
            return JSONResponse(
                {
                    "authorization_endpoint": f"{self.url}/authorize",
                    "token_endpoint": f"{self.url}/token",
                    "userinfo_endpoint": f"{self.url}/userinfo",
                }
            )

        async def token(request: Request) -> Response:
            form = await request.form()
            grant = self.codes.pop(str(form.get("code")), None)
            verifier = str(form.get("code_verifier", ""))
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if (
                grant is None
                or form.get("client_secret") != "s3cret"
                or challenge != grant["challenge"]
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            value = f"at-{len(self.tokens)}"
            self.tokens[value] = grant["profile"]
            claims = {
                "iss": self.url,
                "aud": grant.get("aud", "client"),
                "exp": time.time() + 300,
                "nonce": grant.get("nonce"),
                "sub": str(grant["profile"]["sub"]),
            }
            return JSONResponse(
                {"access_token": value, "token_type": "bearer", "id_token": fake_jwt(claims)}
            )

        def bearer(request: Request) -> dict[str, Any]:
            return self.tokens[request.headers["authorization"].removeprefix("Bearer ")]

        async def userinfo(request: Request) -> Response:
            return JSONResponse(bearer(request))

        async def github_user(request: Request) -> Response:
            profile = bearer(request)
            return JSONResponse(
                {"id": profile["sub"], "login": profile["login"], "name": profile["name"]}
            )

        async def github_emails(request: Request) -> Response:
            profile = bearer(request)
            return JSONResponse(
                [
                    {"email": "old@example.com", "primary": False, "verified": True},
                    {
                        "email": profile["email"],
                        "primary": True,
                        "verified": profile["email_verified"],
                    },
                ]
            )

        async def receiver(request: Request) -> Response:
            self.received.append((dict(request.headers), await request.body()))
            return Response(status_code=self.status)

        return Starlette(
            routes=[
                Route("/.well-known/openid-configuration", discovery),
                Route("/token", token, methods=["POST"]),
                Route("/userinfo", userinfo),
                Route("/github/user", github_user),
                Route("/github/emails", github_emails),
                Route("/hook", receiver, methods=["POST"]),
            ]
        )


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIdP]:
    fake = FakeIdP()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    fake.url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(fake.app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until_started(server, thread, "d'identité factice")
    monkeypatch.setattr(sso, "_discovered", {})
    monkeypatch.setitem(sso.GITHUB, "user", f"{fake.url}/github/user")
    monkeypatch.setitem(sso.GITHUB, "emails", f"{fake.url}/github/emails")
    monkeypatch.setitem(sso.GITHUB, "token", f"{fake.url}/token")
    monkeypatch.setitem(sso.GITHUB, "authorize", f"{fake.url}/authorize")
    yield fake
    server.should_exit = True
    thread.join(timeout=5)


def add_provider(
    db: Session, ctx: Workspace, idp: FakeIdP, slug: str = "corp", kind: str = "oidc", **kw: Any
) -> None:
    root = actor_of(db, make_user(db, f"root-{slug}", superadmin=True))
    kw.setdefault("auto_create", True)  # disabled by default: these tests create accounts
    sso.save_provider(
        db,
        root,
        ctx.box,
        slug=slug,
        kind=kind,
        name="Corp",
        client_id="client",
        client_secret="s3cret",
        issuer="" if kind == "github" else idp.url,
        **kw,
    )
    db.commit()


def sso_login(
    client: TestClient,
    idp: FakeIdP,
    profile: dict[str, Any],
    slug: str = "corp",
    **grant: Any,
) -> Any:
    start = client.get(f"/sso/{slug}/start", params={"next": "/account"}, follow_redirects=False)
    query = parse_qs(urlsplit(start.headers["location"]).query)
    code = f"code-{len(idp.codes)}-{profile['sub']}"
    idp.codes[code] = {
        "challenge": query["code_challenge"][0],
        "profile": profile,
        "nonce": query.get("nonce", [None])[0],
        **grant,
    }
    return client.get(
        f"/sso/{slug}/callback",
        params={"code": code, "state": query["state"][0]},
        follow_redirects=False,
    )


ALICE = {
    "sub": "u-1",
    "email": "Alice@Example.com",
    "email_verified": True,
    "name": "Alice Martin",
    "preferred_username": "alice.m",
}


def test_oidc_login_creates_then_reuses_the_account(
    client: TestClient, db: Session, ctx: Workspace, idp: FakeIdP
) -> None:
    add_provider(db, ctx, idp)
    assert "Se connecter avec Corp" in client.get("/login").text
    first = sso_login(client, idp, ALICE)
    assert first.status_code == 303 and first.headers["location"] == "/account"
    assert "Alice Martin" in client.get("/account").text
    users = db.scalars(select(User).where(User.email == "alice@example.com")).all()
    assert [u.username for u in users] == ["alice.m"] and users[0].password_hash is None
    client.cookies.clear()
    sso_login(client, idp, ALICE)
    assert len(db.scalars(select(UserIdentity)).all()) == 1  # same identity, no duplicate


def test_existing_local_account_is_linked_by_verified_email(
    client: TestClient, db: Session, ctx: Workspace, idp: FakeIdP
) -> None:
    bob = make_user(db, "bob")
    bob.email, bob.email_verified_at = "bob@example.com", datetime.now(UTC)
    mallory = make_user(db, "mallory")
    mallory.email = "carol@example.com"  # declared, never verified
    db.commit()
    add_provider(db, ctx, idp, link_by_email=True)
    sso_login(
        client, idp, {"sub": "u-2", "email": "bob@example.com", "email_verified": True, "name": "B"}
    )
    identity = db.scalars(select(UserIdentity)).one()
    assert identity.user_id == bob.id
    # A merely declared address does not capture the identity of its real owner.
    client.cookies.clear()
    sso_login(
        client,
        idp,
        {"sub": "u-5", "email": "carol@example.com", "email_verified": True, "name": "C"},
    )
    carol = db.scalars(select(UserIdentity).where(UserIdentity.subject == "u-5")).one()
    assert carol.user_id != mallory.id
    db.expire_all()
    assert db.get(User, mallory.id).email is None  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("profile", "options", "message"),
    [
        ({"sub": "u-3", "email": "x@example.com", "email_verified": False}, {}, "vérifiée"),
        (
            {"sub": "u-4", "email": "nobody@example.com", "email_verified": True},
            {"auto_create": False},
            "Aucun compte",
        ),
    ],
)
def test_refused_logins(
    client: TestClient,
    db: Session,
    ctx: Workspace,
    idp: FakeIdP,
    profile: dict[str, Any],
    options: dict[str, Any],
    message: str,
) -> None:
    add_provider(db, ctx, idp, **options)
    response = sso_login(client, idp, profile)
    assert response.headers["location"] == "/login"
    assert message in client.get("/login").text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_forged_or_replayed_state_is_refused(
    client: TestClient, db: Session, ctx: Workspace, idp: FakeIdP
) -> None:
    add_provider(db, ctx, idp)
    forged = client.get(
        "/sso/corp/callback", params={"code": "x", "state": "forged"}, follow_redirects=False
    )
    assert forged.headers["location"] == "/login"
    start = client.get("/sso/corp/start", follow_redirects=False)
    query = parse_qs(urlsplit(start.headers["location"]).query)
    idp.codes["c"] = {"challenge": query["code_challenge"][0], "profile": ALICE}
    client.get("/sso/corp/callback", params={"code": "c", "state": query["state"][0]})
    client.cookies.clear()
    idp.codes["c2"] = {"challenge": query["code_challenge"][0], "profile": ALICE}
    replay = client.get(
        "/sso/corp/callback",
        params={"code": "c2", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert replay.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_github_uses_primary_verified_email(
    client: TestClient, db: Session, ctx: Workspace, idp: FakeIdP
) -> None:
    add_provider(db, ctx, idp, slug="github", kind="github")
    sso_login(
        client,
        idp,
        {
            "sub": 42,
            "email": "carol@example.com",
            "email_verified": True,
            "name": "Carol",
            "login": "carol",
        },
        slug="github",
    )
    user = db.scalars(select(User).where(User.username == "carol")).one()
    assert user.email == "carol@example.com"


def test_unlink_requires_another_way_in(
    client: TestClient, db: Session, ctx: Workspace, idp: FakeIdP
) -> None:
    add_provider(db, ctx, idp)
    sso_login(client, idp, ALICE)
    page = client.get("/account/security").text
    identity = page.split("/account/identities/")[1].split("/")[0]
    token = csrf(client, "/account/security")
    assert (
        "seul moyen de connexion"
        in client.post(f"/account/identities/{identity}/unlink", data={"csrf": token}).text
    )
    client.post(
        "/account/password",
        data={"csrf": token, "current": "", "password": "un mot de passe solide"},
    )
    login(client, "alice.m", "un mot de passe solide")
    token = csrf(client, "/account/security")
    assert (
        "Identité déliée"
        in client.post(f"/account/identities/{identity}/unlink", data={"csrf": token}).text
    )


def finish(client: TestClient, idp: FakeIdP, start: Any, profile: dict[str, Any]) -> Any:
    query = parse_qs(urlsplit(start.headers["location"]).query)
    code = f"code-{len(idp.codes)}-{profile['sub']}"
    idp.codes[code] = {
        "challenge": query["code_challenge"][0],
        "profile": profile,
        "nonce": query.get("nonce", [None])[0],
    }
    return client.get(
        "/sso/corp/callback",
        params={"code": code, "state": query["state"][0]},
        follow_redirects=False,
    )


def test_without_email_linking_the_owner_links_from_their_account(
    client: TestClient, db: Session, ctx: Workspace, idp: FakeIdP
) -> None:
    """A generic OIDC provider does not capture an existing account by its address; the
    owner logs in, then links the provider from My account."""
    bob = make_user(db, "bob")
    bob.email, bob.email_verified_at = "bob@example.com", datetime.now(UTC)
    db.commit()
    add_provider(db, ctx, idp)  # OIDC: linking by address closed by default
    profile = {"sub": "u-9", "email": "bob@example.com", "email_verified": True, "name": "B"}
    refused = sso_login(client, idp, profile)
    assert refused.headers["location"] == "/login"
    assert "liez ce fournisseur depuis Mon compte" in client.get("/login").text
    assert db.scalars(select(UserIdentity)).all() == []

    login(client, "bob")
    token = csrf(client, "/account/security")
    start = client.post(
        "/account/identities/link/corp", data={"csrf": token}, follow_redirects=False
    )
    linked = finish(client, idp, start, profile)
    assert linked.headers["location"] == "/account/security"
    assert "Compte du fournisseur lié" in client.get("/account/security").text
    assert db.scalars(select(UserIdentity)).one().user_id == bob.id
    client.cookies.clear()
    assert sso_login(client, idp, profile).headers["location"] == "/account"  # logged in


def test_a_link_cannot_land_in_another_session(
    client: TestClient, db: Session, ctx: Workspace, idp: FakeIdP
) -> None:
    make_user(db, "bob")
    db.commit()
    add_provider(db, ctx, idp)
    login(client, "bob")
    start = client.post(
        "/account/identities/link/corp",
        data={"csrf": csrf(client, "/account/security")},
        follow_redirects=False,
    )
    client.cookies.delete("synaptane_session")  # session closed before the callback
    finish(client, idp, start, ALICE)
    assert db.scalars(select(UserIdentity)).all() == []


# --- Webhooks ---------------------------------------------------------------------------------


def deliver_all(database: Database, ctx: Workspace, *, allow_private: bool = True) -> list[str]:
    errors = []
    with database.session() as session:
        pending = [j.payload for j in session.scalars(select(Job).where(Job.kind == "webhook"))]
    for payload in pending:
        try:
            webhooks.deliver(database, ctx.box, payload, allow_private=allow_private)
        except webhooks.DeliveryError as error:
            errors.append(f"{error}|{error.transient}")
    return errors


def test_events_are_signed_and_delivered(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, idp: FakeIdP
) -> None:
    org = wiki.infra.organization
    with pytest.raises(SynaptaneError):
        webhooks.create(
            db, wiki.root, ctx.box, org, f"{idp.url}/hook", ["content.changed"]
        )  # http refused
    hook, secret = webhooks.create(
        db,
        wiki.root,
        ctx.box,
        org,
        f"{idp.url}/hook",
        ["content.changed", "inbox.item_added"],
        allow_private=True,
    )
    assert secret.startswith("whsec_") and secret.encode() not in hook.secret
    db.commit()
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# A\n", source="mcp")
    deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title="Note", body="x", source="web"
    )
    db.commit()
    assert deliver_all(database, ctx) == []
    events = []
    for headers, body in idp.received:
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert headers["x-synaptane-signature"] == expected
        payload = json.loads(body)
        events.append(
            (
                payload["event"],
                payload["space"],
                payload["data"].get("paths") or payload["data"].get("title"),
            )
        )
    assert events == [
        ("content.changed", "acme/infra", ["a.md"]),
        ("inbox.item_added", "acme/infra", "Note"),
    ]


def test_private_addresses_are_blocked_and_errors_recorded(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, idp: FakeIdP
) -> None:
    org = wiki.infra.organization
    webhooks.create(
        db, wiki.root, ctx.box, org, f"{idp.url}/hook", ["inbox.item_added"], allow_private=True
    )
    db.commit()
    deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title="Note", body="x", source="web"
    )
    db.commit()
    assert deliver_all(database, ctx, allow_private=False) == [
        "Internal address refused: 127.0.0.1|False"
    ]
    assert idp.received == []
    idp.status = 503
    assert deliver_all(database, ctx) == ["Response 503|True"]
    with database.session() as session:
        assert session.scalars(select(Webhook)).one().last_status == "503"


def test_a_failing_webhook_is_cut_off_then_reactivated(
    client: TestClient,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    idp: FakeIdP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Circuit breaker. After N consecutive failures spread over a long enough time, the
    webhook is disabled and the admins notified; an admin re-enables it from the page."""
    from datetime import timedelta

    from synaptane.services import mail
    from synaptane.storage.models import AuditEvent

    monkeypatch.setattr(webhooks, "BREAKER_FAILURES", 3)
    hook, _ = webhooks.create(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        f"{idp.url}/hook",
        ["inbox.item_added"],
        allow_private=True,
    )
    db.commit()
    hook_id = hook.id

    def ping() -> list[str]:
        webhooks.send_test(db, wiki.root, wiki.infra.organization, hook_id)
        db.commit()
        errors = deliver_all(database, ctx)
        with database.session() as session:
            session.execute(delete(Job))
        return errors

    def state() -> tuple[bool, int, datetime | None]:
        db.expire_all()
        found = db.get(Webhook, hook_id)
        assert found is not None
        return found.active, found.failures, found.failing_since

    idp.status = 503
    assert ping() == ["Response 503|True"]
    idp.status = 200
    assert ping() == []
    assert state() == (True, 0, None)  # a success resets the counter

    idp.status = 503
    for _ in range(4):  # close together: a short outage disables nothing
        assert ping() == ["Response 503|True"]
    active, failures, since = state()
    assert active and failures == 4 and since is not None

    found = db.get(Webhook, hook_id)
    assert found is not None
    found.failing_since = since - timedelta(hours=2)  # lasting outage
    db.commit()
    assert ping() == ["Webhook disabled after 5 consecutive failures (503).|False"]
    received = len(idp.received)
    assert ping() == [] and len(idp.received) == received  # disabled: no more calls
    assert not state()[0]
    alert = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "alert.webhook_disabled")
    ).one()
    assert alert.org_id == wiki.infra.org_id and alert.details["url"] == f"{idp.url}/hook"
    for locale in ("fr", "en"):  # e-mail template: all values are provided
        subject, body = mail.compose(
            "alert_webhook_disabled", locale, org="Acme", link="https://x/o/acme", **alert.details
        )
        assert "Acme" in subject and f"{idp.url}/hook" in body and "/o/acme/-/webhooks" in body

    login(client, "root")
    page = client.get("/o/acme/-/webhooks").text
    assert "Désactivé après 5 échecs consécutifs" in page and "/reactivate" in page
    token = csrf(client, "/o/acme/-/webhooks")
    done = client.post(f"/o/acme/-/webhooks/{hook_id}/reactivate", data={"csrf": token})
    assert "Webhook réactivé." in done.text and "/reactivate" not in done.text
    db.expire_all()
    found = db.get(Webhook, hook_id)
    assert found is not None and found.active and found.failures == 0
    idp.status = 200
    assert ping() == [] and len(idp.received) == received + 1


def test_tidy_and_conflict_events(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    from synaptane.services import git_sync
    from tests.integration.gitutil import ALICE as GIT_ALICE

    webhooks.create(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        "https://hooks.example.com/x",
        list(webhooks.EVENTS),
    )
    db.commit()
    repo = ctx.local_repository(wiki.infra)
    repo.commit_changes({"content/README.md": b"# Local\n"}, GIT_ALICE, "local")
    wiki.remote.push({"content/README.md": "# Distant\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    with database.session() as session:
        bodies: list[Any] = [
            j.payload["body"] for j in session.scalars(select(Job).where(Job.kind == "webhook"))
        ]
    assert [(b["event"], b["data"].get("paths")) for b in bodies] == [
        ("sync.conflict", ["content/README.md"])
    ]


def test_integration_rest(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    body = {"kind": "google", "name": "Google", "client_id": "cid", "client_secret": "cs"}
    saved = client.put("/api/v1/sso-providers/google", json=body, headers=root).json()
    assert saved["issuer"] == "https://accounts.google.com" and saved["callback_url"].endswith(
        "/sso/google/callback"
    )
    assert client.put("/api/v1/sso-providers/google", json=body, headers=alice).status_code == 403
    # A full replacement, except the secret: `null` keeps it, an absent option takes its default.
    full = {**body, "auto_create": True, "allowed_domains": ["example.com"], "client_secret": None}
    updated = client.put("/api/v1/sso-providers/google", json=full, headers=root).json()
    assert updated["auto_create"] and updated["allowed_domains"] == ["example.com"]
    provider = db.get(SSOProvider, "google")
    assert provider is not None and provider.client_secret  # kept
    reset = client.put(
        "/api/v1/sso-providers/google", json={**body, "client_secret": None}, headers=root
    )
    assert reset.json()["auto_create"] is False and reset.json()["allowed_domains"] == []
    created = client.post(
        "/api/v1/organizations/acme/webhooks",
        json={"url": "https://hooks.example.com/x"},
        headers=root,
    )
    assert created.status_code == 201 and created.json()["secret"].startswith("whsec_")
    listed = client.get("/api/v1/organizations/acme/webhooks", headers=root).json()
    assert "secret" not in listed[0] and listed[0]["events"] == sorted(webhooks.EVENTS)
    assert listed[0]["failures"] == 0
    reactivate = f"/api/v1/organizations/acme/webhooks/{listed[0]['id']}/reactivate"
    assert client.post(reactivate, headers=alice).status_code == 403
    assert client.post(reactivate, headers=root).status_code == 204
    assert client.get("/api/v1/organizations/acme/webhooks", headers=alice).status_code == 403
    assert (
        client.post(
            f"/api/v1/organizations/acme/webhooks/{listed[0]['id']}/test", headers=root
        ).status_code
        == 202
    )
    assert (
        client.delete(
            f"/api/v1/organizations/acme/webhooks/{listed[0]['id']}", headers=root
        ).status_code
        == 204
    )


def test_tidy_completed_event(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> None:
    from synaptane.services import tidy

    webhooks.create(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        "https://hooks.example.com/x",
        ["tidy.completed"],
    )
    db.commit()
    tidy.run(database, ctx, wiki.infra.id, trigger="manual")
    with database.session() as session:
        [job] = session.scalars(select(Job).where(Job.kind == "webhook")).all()
    body: Any = job.payload["body"]
    assert body["event"] == "tidy.completed" and body["data"]["status"] == "failed"


def test_web_admin_sso_and_org_webhooks(client: TestClient, db: Session, wiki: Wiki) -> None:
    login(client, "root")
    token = csrf(client, "/admin")
    saved = client.post(
        "/admin/sso",
        data={
            "csrf": token,
            "slug": "gitlab",
            "kind": "gitlab",
            "name": "GitLab",
            "client_id": "cid",
            "client_secret": "cs",
            "auto_create": "true",
            "enabled": "true",
            "allowed_domains": "example.com",
        },
    )
    assert (
        "Fournisseur de connexion enregistré" in saved.text and "/sso/gitlab/callback" in saved.text
    )
    # Editing a provider fills the form with its settings, never with its secret.
    form = client.get("/admin/sso?edit=gitlab").text
    assert "Modifier le fournisseur GitLab" in form and 'value="gitlab" readonly' in form
    assert 'name="client_id" required value="cid"' in form and 'value="example.com"' in form
    assert '<option value="gitlab" selected>' in form
    assert 'name="auto_create" value="true" checked' in form
    assert 'name="link_by_email" value="true">' in form  # GitLab: off
    assert 'name="enabled" value="true" checked' in form and 'value="cs"' not in form
    assert "Ajouter ou modifier un fournisseur" in client.get("/admin/sso?edit=inconnu").text
    created = client.post(
        "/o/acme/-/webhooks",
        data={"csrf": token, "url": "https://hooks.example.com/x", "events": ["sync.conflict"]},
    )
    secret = created.text.split("whsec_")[1].split("<")[0]
    assert len(secret) > 20
    assert "whsec_" not in client.get("/o/acme/-/webhooks").text  # shown only once
    hook = db.scalars(select(Webhook)).one()
    assert hook.events == ["sync.conflict"]
    client.post(f"/o/acme/-/webhooks/{hook.id}/delete", data={"csrf": token})
    db.expire_all()
    assert db.scalars(select(Webhook)).all() == []


def test_delivery_connects_to_the_verified_address(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["93.184.216.34", "127.0.0.1"])  # 2nd resolution: rebinding to the host

    def resolve(host: str, port: object, **kwargs: object) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    url, host, extensions = webhooks.pinned_request("https://hooks.example.com:8443/x?y=1")
    assert url == "https://93.184.216.34:8443/x?y=1"
    assert host == {"Host": "hooks.example.com:8443"}
    assert extensions == {"sni_hostname": "hooks.example.com"}


@pytest.mark.parametrize(
    "grant", [{"nonce": "rejoué"}, {"aud": "autre-client"}], ids=["nonce", "audience"]
)
def test_id_token_must_match_this_login(
    client: TestClient, db: Session, ctx: Workspace, idp: FakeIdP, grant: dict[str, Any]
) -> None:
    add_provider(db, ctx, idp)
    response = sso_login(client, idp, ALICE, **grant)
    assert response.headers["location"] == "/login"
    assert "id_token" in client.get("/login").text
    assert db.scalars(select(UserIdentity)).all() == []


def test_a_callback_from_another_browser_is_refused(
    client: TestClient, db: Session, ctx: Workspace, idp: FakeIdP
) -> None:
    """The provider callback opened by the victim (attacker's link) fails."""
    add_provider(db, ctx, idp)
    start = client.get("/sso/corp/start", follow_redirects=False)
    query = parse_qs(urlsplit(start.headers["location"]).query)
    code = "code-x"
    idp.codes[code] = {
        "challenge": query["code_challenge"][0],
        "profile": ALICE,
        "nonce": query.get("nonce", [None])[0],
    }
    client.cookies.clear()  # other browser: not the cookie set at the start
    back = client.get("/sso/corp/callback", params={"code": code, "state": query["state"][0]})
    assert "falsifiée" in back.text and "synaptane_session" not in client.cookies
