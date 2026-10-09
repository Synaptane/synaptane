"""Sign-in through an external provider (see docs/admin/sso.md): GitLab, Google, generic
OIDC, GitHub.

"Authorization code" flow with PKCE, single-use `state` and `nonce`. The identity is read
server-side (`userinfo` endpoint, or GitHub API) with the token obtained thanks to the client
secret.

Matching: identity already known, otherwise an existing account with the same **verified**
e-mail, otherwise creation of an account (if the provider allows it; disabled by default).
Automatic linking and creation only accept the provider's allowed e-mail domains, if it has
any. SSO grants no rights: an admin then adds the account to organizations and spaces.
"""

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx2
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from synaptane.core import names
from synaptane.core.access import Action, require
from synaptane.core.errors import conflict, forbidden, invalid, not_found
from synaptane.core.security import SecretBox, token_hash
from synaptane.services import Actor, accounts, audit
from synaptane.storage.models import SSOProvider, SSOState, User, UserIdentity

KINDS = ("gitlab", "google", "github", "oidc")
DEFAULT_ISSUERS = {"gitlab": "https://gitlab.com", "google": "https://accounts.google.com"}
# Providers where anyone can open an account: without an allowed domain, automatic creation
# there opens the instance to the whole Internet.
PUBLIC_ISSUERS = ("https://accounts.google.com", "https://gitlab.com")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$")
STATE_TTL = timedelta(minutes=10)
TIMEOUT = httpx2.Timeout(20.0, connect=10.0)
GITHUB = {
    "authorize": "https://github.com/login/oauth/authorize",
    "token": "https://github.com/login/oauth/access_token",
    "user": "https://api.github.com/user",
    "emails": "https://api.github.com/user/emails",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _context(slug: str) -> str:
    return f"sso:{slug}:client-secret"


# --- Administration (super-admin) -------------------------------------------------------------


def parse_domains(values: Iterable[str]) -> list[str]:
    """E-mail domains as entered (separated by commas, spaces or line breaks), normalized:
    lowercase, no leading "@", no duplicates."""
    found: list[str] = []
    for value in values:
        for part in re.split(r"[\s,;]+", value):
            domain = part.strip().lower().removeprefix("@").rstrip(".")
            if not domain:
                continue
            if not DOMAIN.match(domain):
                raise invalid("Invalid email domain: {domain}", domain=part.strip())
            if domain not in found:
                found.append(domain)
    return found


def open_to_anyone(provider: SSOProvider) -> bool:
    """True if anyone holding a GitHub, Google or gitlab.com account gets an account here:
    automatic creation without an allowed domain (to be flagged in the admin)."""
    public = provider.kind == "github" or provider.issuer in PUBLIC_ISSUERS
    return public and provider.auto_create and not provider.allowed_domains


def domain_allowed(provider: SSOProvider, email: str, hosted_domain: str = "") -> bool:
    """Address accepted for automatic linking or creation. Google also gives the account's
    Workspace domain (`hd`): if provided, it must be allowed too."""
    allowed = provider.allowed_domains or []
    if not allowed:
        return True
    if email.rpartition("@")[2].lower() not in allowed:
        return False
    return not hosted_domain or hosted_domain.lower() in allowed


def list_providers(session: Session, enabled_only: bool = False) -> list[SSOProvider]:
    query = select(SSOProvider).order_by(SSOProvider.name)
    if enabled_only:
        query = query.where(SSOProvider.enabled.is_(True))
    return list(session.scalars(query))


def save_provider(
    session: Session,
    actor: Actor,
    box: SecretBox,
    *,
    slug: str,
    kind: str,
    name: str,
    client_id: str,
    client_secret: str | None,
    issuer: str = "",
    auto_create: bool = False,
    enabled: bool = True,
    link_by_email: bool | None = None,
    allowed_domains: Iterable[str] = (),
) -> SSOProvider:
    """Creates or updates a provider. `client_secret=None` keeps the stored secret.
    `link_by_email` None: yes for GitHub and Google (addresses verified seriously), no for a
    generic OIDC provider or GitLab (often self-hosted). `allowed_domains`: the only e-mail
    domains accepted for automatic linking and creation (empty: all)."""
    require(actor.access, Action.MANAGE_INSTANCE)
    slug = names.slug(slug)
    if kind not in KINDS:
        raise invalid("Unknown provider type.")
    issuer = (issuer.strip() or DEFAULT_ISSUERS.get(kind, "")).rstrip("/")
    local = urlsplit(issuer).hostname in ("localhost", "127.0.0.1")  # development and tests
    if kind != "github" and not (issuer.startswith("https://") or local):
        raise invalid("Enter the HTTPS URL of the OIDC issuer.")
    if not client_id.strip():
        raise invalid("Enter the client ID.")
    domains = parse_domains(allowed_domains)
    provider = session.get(SSOProvider, slug)
    if provider is None:
        if not client_secret:
            raise invalid("Enter the client secret.")
        provider = SSOProvider(slug=slug, client_secret=b"")
        session.add(provider)
    provider.kind, provider.name, provider.issuer = kind, names.display_name(name), issuer
    provider.client_id, provider.auto_create, provider.enabled = (
        client_id.strip(),
        auto_create,
        enabled,
    )
    provider.link_by_email = (
        link_by_email if link_by_email is not None else kind in ("github", "google")
    )
    provider.allowed_domains = domains
    if client_secret:
        provider.client_secret = box.encrypt(client_secret.strip(), _context(slug))
    audit(
        session,
        actor,
        "sso.provider_saved",
        provider=slug,
        kind=kind,
        auto_create=auto_create,
        domains=" ".join(domains),
    )
    session.flush()
    return provider


def delete_provider(session: Session, actor: Actor, slug: str) -> None:
    require(actor.access, Action.MANAGE_INSTANCE)
    provider = session.get(SSOProvider, slug)
    if provider is None:
        raise not_found("Provider not found.")
    session.delete(provider)
    audit(session, actor, "sso.provider_deleted", provider=slug)


def identities(session: Session, user_id: Any) -> list[tuple[UserIdentity, SSOProvider]]:
    rows = session.execute(
        select(UserIdentity, SSOProvider)
        .join(SSOProvider, SSOProvider.slug == UserIdentity.provider)
        .where(UserIdentity.user_id == user_id)
    )
    return [(identity, provider) for identity, provider in rows]


# --- OIDC discovery ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoints:
    authorize: str
    token: str
    userinfo: str


_discovered: dict[str, Endpoints] = {}


def endpoints(provider: SSOProvider) -> Endpoints:
    if provider.kind == "github":
        return Endpoints(GITHUB["authorize"], GITHUB["token"], GITHUB["user"])
    cached = _discovered.get(provider.issuer)
    if cached:
        return cached
    try:
        response = httpx2.get(
            f"{provider.issuer}/.well-known/openid-configuration", timeout=TIMEOUT
        )
        response.raise_for_status()
        data = response.json()
        found = Endpoints(
            data["authorization_endpoint"], data["token_endpoint"], data["userinfo_endpoint"]
        )
    except (httpx2.HTTPError, KeyError, ValueError):
        raise invalid("Sign-in provider unreachable or misconfigured.") from None
    _discovered[provider.issuer] = found
    return found


# --- Sign-in ----------------------------------------------------------------------------------


def start(
    session: Session,
    provider_slug: str,
    redirect_uri: str,
    next_url: str,
    link_user: UUID | None = None,
) -> str:
    """The provider's authorization URL, with a `state` stored for the return.
    `link_user`: linking requested from "My account" by this signed-in account."""
    provider = session.get(SSOProvider, provider_slug)
    if provider is None or not provider.enabled:
        raise not_found("Sign-in provider not found.")
    state, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(3))
    session.execute(delete(SSOState).where(SSOState.expires_at < _now()))
    session.add(
        SSOState(
            state_hash=token_hash(state),
            provider=provider.slug,
            verifier=verifier,
            nonce=nonce,
            next_url=next_url,
            link_user_id=link_user,
            expires_at=_now() + STATE_TTL,
        )
    )
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge.rstrip(b"=").decode(),
        "code_challenge_method": "S256",
    }
    if provider.kind == "github":
        params["scope"] = "read:user user:email"
    else:
        params["scope"], params["nonce"] = "openid email profile", nonce
    return f"{endpoints(provider).authorize}?{urlencode(params)}"


@dataclass(frozen=True)
class Profile:
    subject: str
    email: str
    email_verified: bool
    name: str
    login: str
    hosted_domain: str = ""  # Google: the account's Workspace domain (`hd`), absent otherwise


def _exchange(
    provider: SSOProvider, box: SecretBox, code: str, verifier: str, redirect_uri: str
) -> tuple[str, str]:
    """(access token, id_token); id_token empty for GitHub."""
    secret = box.decrypt(provider.client_secret, _context(provider.slug))
    try:
        response = httpx2.post(
            endpoints(provider).token,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": provider.client_id,
                "client_secret": secret,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        data = response.json()
    except (httpx2.HTTPError, ValueError):
        raise invalid("The sign-in provider is not responding.") from None
    token = data.get("access_token")
    if response.status_code >= 400 or not token:
        raise invalid("Sign-in refused by the provider (invalid or expired code).")
    return str(token), str(data.get("id_token") or "")


def check_id_token(provider: SSOProvider, id_token: str, nonce: str, subject: str) -> None:
    """Checks the id_token claims: issuer, audience, expiry, nonce, subject.

    The signature is not checked: the token comes straight from the provider's endpoint, over
    an authenticated TLS connection (OpenID Connect Core, §3.1.3.7, item 6).
    """
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        assert isinstance(claims, dict)
    except (IndexError, ValueError, AssertionError):
        raise forbidden("Invalid provider response (id_token).") from None
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    problems = [
        str(claims.get("iss", "")).rstrip("/") != provider.issuer,
        provider.client_id not in audiences,
        float(claims.get("exp", 0)) < _now().timestamp() - 60,
        claims.get("nonce") != nonce,
        str(claims.get("sub", "")) != subject,
    ]
    if any(problems):
        raise forbidden("Provider response refused (invalid id_token): please try again.")


def _profile(provider: SSOProvider, token: str) -> Profile:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        if provider.kind == "github":
            user = httpx2.get(GITHUB["user"], headers=headers, timeout=TIMEOUT).json()
            emails = httpx2.get(GITHUB["emails"], headers=headers, timeout=TIMEOUT).json()
            primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
            return Profile(
                subject=str(user["id"]),
                email=str(primary["email"]) if primary else "",
                email_verified=primary is not None,
                name=str(user.get("name") or user.get("login") or ""),
                login=str(user.get("login") or ""),
            )
        info = httpx2.get(endpoints(provider).userinfo, headers=headers, timeout=TIMEOUT).json()
    except (httpx2.HTTPError, ValueError, KeyError, TypeError):
        raise invalid("Profile unavailable from the sign-in provider.") from None
    if not info.get("sub"):
        raise invalid("Incomplete profile returned by the provider.")
    return Profile(
        subject=str(info["sub"]),
        email=str(info.get("email") or ""),
        email_verified=bool(info.get("email_verified")),
        name=str(info.get("name") or info.get("preferred_username") or ""),
        login=str(info.get("preferred_username") or info.get("nickname") or ""),
        hosted_domain=str(info.get("hd") or ""),
    )


def _new_username(session: Session, profile: Profile) -> str:
    base = profile.login or profile.email.split("@")[0] or "user"
    base = re.sub(r"[^a-z0-9._-]+", "-", names.ascii_text(base)).strip(".-_")[:50]
    base = base or "user"
    candidate, n = base, 1
    while session.scalar(select(User.id).where(User.username == candidate)):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def complete(
    session: Session,
    box: SecretBox,
    provider_slug: str,
    code: str,
    state: str,
    redirect_uri: str,
    locale: str,
) -> tuple[User, str, bool]:
    """Completes the sign-in: returns the account, the page to go back to and whether it was
    a linking requested from "My account" (no new session in that case). `locale`: language
    of an account created on this occasion (that of the visitor's page)."""
    pending = session.get(SSOState, token_hash(state))
    if pending is None or pending.provider != provider_slug or pending.expires_at < _now():
        raise forbidden("Sign-in expired or tampered with: please start again.")
    session.delete(pending)  # single use
    provider = session.get(SSOProvider, provider_slug)
    if provider is None or not provider.enabled:
        raise not_found("Sign-in provider not found.")
    token, id_token = _exchange(provider, box, code, pending.verifier, redirect_uri)
    profile = _profile(provider, token)
    if provider.kind != "github":  # OIDC: the id_token is mandatory and bound to this `nonce`
        check_id_token(provider, id_token, pending.nonce, profile.subject)
    identity = session.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == provider.slug, UserIdentity.subject == profile.subject
        )
    )
    if pending.link_user_id is not None:  # linking from "My account"
        user = session.get(User, pending.link_user_id)
        if user is None or not user.is_active:
            raise forbidden("This account is disabled.")
        if identity is not None and identity.user_id != user.id:
            raise conflict("This provider account is already linked to another Synaptane account.")
        if identity is None:
            identity = UserIdentity(
                provider=provider.slug,
                subject=profile.subject,
                user_id=user.id,
                email=(profile.email or "").lower(),
            )
            session.add(identity)
            audit(session, None, "sso.linked", provider=provider.slug, username=user.username)
    elif identity is not None:
        user = session.get(User, identity.user_id)
    else:
        if not profile.email or not profile.email_verified:
            raise forbidden("Your email address must be verified with the provider.")
        email = profile.email.lower()
        if not domain_allowed(provider, email, profile.hosted_domain):
            raise forbidden(
                "This address is not allowed to sign in with this provider: ask an "
                "administrator for an account."
            )
        # Automatic linking only to an address verified in Synaptane: an address merely
        # declared on an account must not capture someone's identity.
        user = session.scalar(
            select(User).where(User.email == email, User.email_verified_at.is_not(None))
        )
        if user is not None and not provider.link_by_email:
            raise forbidden(
                "A Synaptane account already exists for this email: sign in with your "
                "password, then link this provider from My account → Security."
            )
        if user is None:
            accounts.claim_email(session, email)  # removed from an account that did not verify it
            if not provider.auto_create:
                raise forbidden(
                    "No Synaptane account matches this address: ask an administrator for one."
                )
            user = User(
                username=_new_username(session, profile),
                display_name=names.display_name(profile.name or profile.login or email),
                email=email,
                email_verified_at=_now(),  # vouched for by the provider
                password_hash=None,
                locale=locale,
            )
            session.add(user)
            session.flush()
            audit(
                session, None, "user.created_by_sso", provider=provider.slug, username=user.username
            )
        identity = UserIdentity(
            provider=provider.slug, subject=profile.subject, user_id=user.id, email=email
        )
        session.add(identity)
    if user is None or not user.is_active:
        raise forbidden("This account is disabled.")
    identity.last_login_at = _now()
    return user, pending.next_url, pending.link_user_id is not None


def unlink(session: Session, actor: Actor, identity_id: Any) -> None:
    identity = session.get(UserIdentity, identity_id)
    if identity is None or identity.user_id != actor.user.id:
        raise not_found("Identity not found.")
    others = len(identities(session, actor.user.id)) - 1
    if not actor.user.password_hash and others == 0:
        raise conflict("Not possible: this is your only way to sign in. Set a password first.")
    session.delete(identity)
    audit(session, actor, "sso.unlinked", provider=identity.provider)
