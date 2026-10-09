"""OAuth 2.1 authorization server for MCP (see docs/concepts.md, Interfaces).

The MCP SDK provides the routes (discovery, RFC 7591 dynamic registration, `/authorize`,
`/token` with PKCE, `/revoke`). This module provides storage and the link to accounts:
- `/authorize` redirects to Synaptane's consent page (`/oauth/consent`);
- an authorization grants access to the spaces chosen at consent: all of them (future ones
  included), or a list; for a member of several organizations, the page offers by default
  only the spaces of the first one (`ui.html`, `access_limits`);
- one-hour access token; 30-day refresh token, renewed on every use (sliding expiry);
- personal tokens (`syn_…`) are accepted too, for Claude Code and scripts
  (`load_access_token`).
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    IdentityAssertionParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy import delete, exists, select, update

from synaptane.core.access import Limits
from synaptane.core.security import token_hash
from synaptane.services import accounts
from synaptane.storage.db import Database
from synaptane.storage.models import ApiToken, OAuthClient, OAuthCode, OAuthRequest, User
from synaptane.storage.models import OAuthToken as TokenRow

SCOPE = "synaptane"
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=5)
REQUEST_TTL = timedelta(minutes=10)
REUSE_GRACE = timedelta(seconds=30)
UNUSED_CLIENT_TTL = timedelta(hours=24)  # registered but never authorized: deleted afterwards
# Known publishers: a client claiming to be one of them must redirect to its domains.
PUBLISHERS = {
    "Anthropic": (("claude", "anthropic"), ("claude.ai", "claude.com", "anthropic.com")),
    "OpenAI": (("chatgpt", "openai"), ("chatgpt.com", "openai.com")),
    "Mistral": (("mistral", "le chat"), ("mistral.ai",)),
    "Google": (("gemini", "google"), ("google.com",)),
    "GitHub": (("copilot", "github"), ("github.com", "microsoft.com")),
    "Cursor": (("cursor",), ("cursor.com", "cursor.sh")),
}
log = logging.getLogger("synaptane.oauth")


def _now() -> datetime:
    return datetime.now(UTC)


def _allowed_redirect(uri: str) -> bool:
    """HTTPS, or HTTP on the local machine (desktop clients, development tools)."""
    from urllib.parse import urlsplit

    parts = urlsplit(uri)
    if parts.fragment:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


class SynaptaneOAuthProvider:
    def __init__(self, database: Database, public_url: str) -> None:
        self.database = database
        self.public_url = public_url
        self.resource = f"{public_url}/mcp"

    # --- Clients ------------------------------------------------------------------------------

    def _get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.database.session() as session:
            row = session.get(OAuthClient, client_id)
            return OAuthClientInformationFull.model_validate(row.info) if row else None

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await anyio.to_thread.run_sync(self._get_client, client_id)

    def _register(self, info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in info.redirect_uris or []]
        if not uris or not all(_allowed_redirect(u) for u in uris):
            raise RegistrationError(
                "invalid_redirect_uri", "Redirect URIs must use HTTPS (or HTTP on localhost)."
            )
        with self.database.session() as session:
            session.add(OAuthClient(client_id=info.client_id, info=info.model_dump(mode="json")))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await anyio.to_thread.run_sync(self._register, client_info)

    # --- Authorization ------------------------------------------------------------------------

    def _authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        with self.database.session() as session:
            session.execute(delete(OAuthRequest).where(OAuthRequest.expires_at < _now()))
            session.add(
                OAuthRequest(
                    id=request_id,
                    client_id=client.client_id,
                    params=params.model_dump(mode="json"),
                    expires_at=_now() + REQUEST_TTL,
                )
            )
        return f"{self.public_url}/oauth/consent?request={request_id}"

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        return await anyio.to_thread.run_sync(self._authorize, client, params)

    def _load_code(self, client_id: str, code: str) -> AuthorizationCode | None:
        with self.database.session() as session:
            row = session.get(OAuthCode, token_hash(code))
            if row is None or row.client_id != client_id or row.expires_at < _now():
                return None
            params = AuthorizationParams.model_validate(row.params)
            return AuthorizationCode(
                code=code,
                scopes=params.scopes or [SCOPE],
                expires_at=row.expires_at.timestamp(),
                client_id=row.client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource or self.resource,
                subject=str(row.user_id),
            )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return await anyio.to_thread.run_sync(self._load_code, client.client_id, authorization_code)

    def _issue(
        self,
        client_id: str,
        user_id: UUID,
        scopes: list[str],
        resource: str | None,
        family: UUID,
        grant: tuple[bool, list[str] | None] = (False, None),
    ) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = _now()
        with self.database.session() as session:
            for value, kind, ttl in (
                (access, "access", ACCESS_TTL),
                (refresh, "refresh", REFRESH_TTL),
            ):
                session.add(
                    TokenRow(
                        token_hash=token_hash(value),
                        kind=kind,
                        family=family,
                        client_id=client_id,
                        user_id=user_id,
                        scopes=scopes,
                        resource=resource,
                        read_only=grant[0],
                        space_ids=grant[1],
                        expires_at=now + ttl,
                    )
                )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=int(ACCESS_TTL.total_seconds()),
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _exchange_code(self, code: AuthorizationCode) -> OAuthToken:
        with self.database.session() as session:
            row = session.get(OAuthCode, token_hash(code.code))
            if row is None:
                raise TokenError("invalid_grant", "Code already used or expired.")
            user_id, grant = row.user_id, (row.read_only, row.space_ids)
            session.delete(row)  # single use
        return self._issue(code.client_id, user_id, code.scopes, code.resource, uuid4(), grant)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return await anyio.to_thread.run_sync(self._exchange_code, authorization_code)

    # --- Tokens -------------------------------------------------------------------------------

    def _load_token(self, value: str, kind: str) -> TokenRow | None:
        with self.database.session() as session:
            row = session.get(TokenRow, token_hash(value))
            if row is None or row.kind != kind or row.revoked_at or row.expires_at < _now():
                return None
            user = session.get(User, row.user_id)
            if user is None or not user.is_active:
                return None
            session.expunge(row)
            return row

    def _reused(self, value: str) -> bool:
        """Refresh token already replaced, presented again: stolen, or replayed.

        The whole family is then revoked (the legitimate client will have to reconnect). A
        short grace period covers two simultaneous refreshes from the same client.
        """
        with self.database.session() as session:
            row = session.get(TokenRow, token_hash(value))
            if row is None or row.kind != "refresh" or row.revoked_at is None:
                return False
            if row.revoked_at > _now() - REUSE_GRACE:
                return True
            revoke_family(session, row.family)
            log.warning("Refresh token replayed: family %s revoked.", row.family)
            return True

    def _load_refresh(self, client_id: str, value: str) -> RefreshToken | None:
        if self._reused(value):
            return None
        row = self._load_token(value, "refresh")
        if row is None or row.client_id != client_id:
            return None
        return RefreshToken(
            token=value,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=int(row.expires_at.timestamp()),
            resource=row.resource,
            subject=str(row.user_id),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return await anyio.to_thread.run_sync(self._load_refresh, client.client_id, refresh_token)

    def _exchange_refresh(self, token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self.database.session() as session:
            row = session.get(TokenRow, token_hash(token.token))
            assert row is not None
            row.revoked_at = _now()  # rotation: the old token will not be used again
            family, user_id, grant = row.family, row.user_id, (row.read_only, row.space_ids)
        return self._issue(
            token.client_id, user_id, scopes or token.scopes, token.resource, family, grant
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return await anyio.to_thread.run_sync(self._exchange_refresh, refresh_token, scopes)

    def _load_access(self, value: str) -> AccessToken | None:
        row = self._load_token(value, "access")
        if row is None:
            return None
        return AccessToken(
            token=value,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=int(row.expires_at.timestamp()),
            resource=row.resource,
            subject=str(row.user_id),
        )

    def _personal(self, token: str) -> AccessToken | None:
        """Personal token `syn_…` (Claude Code, scripts): same rights as its owner."""
        with self.database.session() as session:
            found = accounts.resolve_api_token(session, token)
            if found is None:
                return None
            user = found[0]
            return AccessToken(
                token=token,
                client_id="personal-token",
                scopes=[SCOPE],
                resource=self.resource,
                subject=str(user.id),
            )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Used by the SDK to authenticate every call to /mcp."""
        if token.startswith("syn_"):
            return await anyio.to_thread.run_sync(self._personal, token)
        return await anyio.to_thread.run_sync(self._load_access, token)

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        """Enterprise identity assertions (SEP-990): not supported."""
        raise TokenError("unsupported_grant_type", "Grant type not supported.")

    def _revoke(self, value: str) -> None:
        with self.database.session() as session:
            row = session.get(TokenRow, token_hash(value))
            if row is not None:
                revoke_family(session, row.family)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await anyio.to_thread.run_sync(self._revoke, token.token)


def token_identity(session: Any, client_id: str, token: str) -> tuple[str, Limits]:
    """Readable name of the client (OAuth application or personal token) and the token's
    restrictions.

    Through MCP, the super-admin only has their actual memberships: their assistant does not
    see the whole instance."""
    if client_id == "personal-token":
        row = session.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(token)))
        if row is None:
            return "MCP", Limits(members_only=True)
        limits = accounts.token_limits(row.read_only, row.space_ids, members_only=True)
        return f"MCP · {row.name}", limits
    found = session.get(TokenRow, token_hash(token))
    limits = (
        accounts.token_limits(found.read_only, found.space_ids, members_only=True)
        if found is not None
        else Limits(members_only=True)
    )
    client = session.get(OAuthClient, client_id)
    name = str(client.info.get("client_name") or "") if client is not None else ""
    return (f"MCP · {name[:80]}" if name else "MCP"), limits


def revoke_family(session: Any, family: UUID) -> None:
    session.execute(
        update(TokenRow)
        .where(TokenRow.family == family, TokenRow.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


def purge_unused_clients(session: Any) -> int:
    """Delete clients registered more than a day ago and never authorized: registration is
    open to anyone, the table must not grow forever."""
    used = exists().where(TokenRow.client_id == OAuthClient.client_id)
    coded = exists().where(OAuthCode.client_id == OAuthClient.client_id)
    result = session.execute(
        delete(OAuthClient).where(
            OAuthClient.created_at < _now() - UNUSED_CLIENT_TTL, ~used, ~coded
        )
    )
    return int(result.rowcount or 0)


def lookalike(client_name: str, redirect_uri: object) -> str | None:
    """Publisher the client name claims to be, while the redirect goes somewhere other than
    its domains. Redirects to the local machine (desktop applications) are not flagged."""
    from urllib.parse import urlsplit

    host = (urlsplit(str(redirect_uri)).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return None
    name = client_name.lower()
    for publisher, (words, domains) in PUBLISHERS.items():
        if any(word in name for word in words) and not any(
            host == domain or host.endswith("." + domain) for domain in domains
        ):
            return publisher
    return None


# --- Consent (web pages) ---------------------------------------------------------------------


def pending_request(
    database: Database, request_id: str
) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
    with database.session() as session:
        row = session.get(OAuthRequest, request_id)
        if row is None or row.expires_at < _now():
            return None
        client = session.get(OAuthClient, row.client_id)
        if client is None:
            return None
        return (
            OAuthClientInformationFull.model_validate(client.info),
            AuthorizationParams.model_validate(row.params),
        )


def decide(
    session: Any,
    request_id: str,
    user: User | None,
    *,
    read_only: bool = False,
    space_ids: list[str] | None = None,
) -> str | None:
    """Accept (user) or deny (None) a request. Return the client's redirect URL.

    `read_only` and `space_ids` (None: all spaces, future ones included) restrict the tokens
    issued for this connection, refreshes included."""
    from mcp.server.auth.provider import construct_redirect_uri

    row = session.get(OAuthRequest, request_id)
    if row is None or row.expires_at < _now():
        return None
    params = AuthorizationParams.model_validate(row.params)
    session.delete(row)
    redirect = str(params.redirect_uri)
    if user is None:
        return construct_redirect_uri(redirect, error="access_denied", state=params.state)
    code = secrets.token_urlsafe(32)
    session.add(
        OAuthCode(
            code_hash=token_hash(code),
            client_id=row.client_id,
            user_id=user.id,
            params=row.params,
            read_only=read_only,
            space_ids=space_ids,
            expires_at=_now() + CODE_TTL,
        )
    )
    return construct_redirect_uri(redirect, code=code, state=params.state)


def connected_apps(session: Any, user_id: UUID) -> list[tuple[UUID, str, datetime]]:
    """Applications authorized by the user: (family, client name, last issuance)."""
    rows = session.execute(
        select(TokenRow.family, OAuthClient.info, TokenRow.created_at)
        .join(OAuthClient, OAuthClient.client_id == TokenRow.client_id)
        .where(
            TokenRow.user_id == user_id,
            TokenRow.kind == "refresh",
            TokenRow.revoked_at.is_(None),
            TokenRow.expires_at > _now(),
        )
        .order_by(TokenRow.created_at.desc())
    )
    apps: dict[UUID, tuple[UUID, str, datetime]] = {}
    for family, info, created in rows:
        apps.setdefault(family, (family, str(info.get("client_name") or "Application"), created))
    return list(apps.values())


def revoke_user_family(session: Any, user_id: UUID, family: UUID) -> None:
    session.execute(
        update(TokenRow)
        .where(
            TokenRow.family == family, TokenRow.user_id == user_id, TokenRow.revoked_at.is_(None)
        )
        .values(revoked_at=_now())
    )


def redirect_target(uri: AnyUrl | str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(str(uri)).netloc
