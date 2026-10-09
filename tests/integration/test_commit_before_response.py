"""Regression: the transaction must be committed before the response is sent.

With `scope="request"` (FastAPI's default), a browser immediately following the login
redirect sometimes arrived before its session was saved.
"""

import asyncio
from collections.abc import MutableMapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.server import create_app
from synaptane.storage.db import Database
from synaptane.storage.models import WebSession
from tests.integration.conftest import PASSWORD, make_user

Message = MutableMapping[str, Any]


def test_session_is_committed_when_the_response_starts(
    settings: Settings, database: Database, db: Session
) -> None:
    make_user(db, "alice")
    db.commit()
    app = create_app(settings, database)
    body = f"username=alice&password={PASSWORD}".encode()
    seen: list[int] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            with database.session() as session:  # another connection: sees only committed data
                seen.append(session.scalar(select(func.count()).select_from(WebSession)) or 0)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"localhost"),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 80),
    }

    async def run() -> None:
        await app(scope, receive, send)

    asyncio.run(run())
    assert seen == [1]
