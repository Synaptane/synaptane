"""MCP calls per account and per client: "Connect my assistant" and connection test.

Recording a call must never slow the tool down: the write happens in a short, separate
transaction, at most once every `EVERY` seconds per client (calls between two writes are
counted in memory, then added).
"""

import threading
import time
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from synaptane.storage.db import Database
from synaptane.storage.models import AssistantUse

EVERY = 10.0  # seconds between two writes for the same client
_pending: dict[tuple[UUID, str], tuple[float, int]] = {}  # last write, pending calls
_lock = threading.Lock()


def record(database: Database, user_id: UUID, via: str) -> None:
    """Records an MCP tool call (one row per client, updated in place)."""
    key = (user_id, via[:200])
    now = time.monotonic()
    with _lock:
        written, calls = _pending.get(key, (0.0, 0))
        calls += 1
        if written and now - written < EVERY:
            _pending[key] = (written, calls)
            return
        _pending[key] = (now, 0)
    stamp = datetime.now(UTC)
    statement = insert(AssistantUse).values(user_id=user_id, via=key[1], last_at=stamp, calls=calls)
    with database.session() as session:
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["user_id", "via"],
                set_={"last_at": stamp, "calls": AssistantUse.calls + calls},
            )
        )


def recent(session: Session, user_id: UUID, since: datetime | None = None) -> list[AssistantUse]:
    """Clients used by the account, most recent first (since `since`)."""
    query = select(AssistantUse).where(AssistantUse.user_id == user_id)
    if since is not None:
        query = query.where(AssistantUse.last_at > since)
    return list(session.scalars(query.order_by(AssistantUse.last_at.desc()).limit(20)))
