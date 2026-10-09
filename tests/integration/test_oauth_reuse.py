"""Replay of an already replaced refresh token: the whole family is revoked."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from synaptane.core.security import token_hash
from synaptane.mcp.oauth import SynaptaneOAuthProvider, _now
from synaptane.storage.db import Database
from synaptane.storage.models import OAuthClient
from synaptane.storage.models import OAuthToken as TokenRow
from tests.integration.conftest import make_user


def test_stolen_refresh_token_revokes_the_family(db: Session, database: Database) -> None:
    alice = make_user(db, "alice")
    db.add(OAuthClient(client_id="claude", info={"client_id": "claude", "redirect_uris": []}))
    db.commit()
    provider = SynaptaneOAuthProvider(database, "http://localhost")
    first = provider._issue("claude", alice.id, ["synaptane"], None, uuid4())
    assert first.refresh_token
    refresh = provider._load_refresh("claude", first.refresh_token)
    assert refresh is not None
    second = provider._exchange_refresh(refresh, [])
    assert second.refresh_token

    # Immediate replay (two simultaneous refreshes): refused, the family survives.
    assert provider._load_refresh("claude", first.refresh_token) is None
    assert provider._load_access(second.access_token) is not None

    # Later replay: stolen token. Everything falls, including the legitimate client's tokens.
    with database.session() as session:
        session.execute(
            update(TokenRow)
            .where(TokenRow.token_hash == token_hash(first.refresh_token))
            .values(revoked_at=_now() - timedelta(minutes=5))
        )
    assert provider._load_refresh("claude", first.refresh_token) is None
    assert provider._load_access(second.access_token) is None
    assert provider._load_refresh("claude", second.refresh_token) is None
