"""PostgreSQL connection and Alembic migrations."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS = Path(__file__).parent / "migrations"


APP_NAME = "synaptane"  # application_name of the connections: see backup.restore


class Database:
    def __init__(self, url: str, name: str = APP_NAME) -> None:
        self.url = url
        # 10 + 10 connections; beyond that, wait 10 s, then the application answers 503.
        self.engine: Engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
            pool_timeout=10,
            connect_args={"application_name": name},
        )
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Committed on normal exit, rolled back on exception.

        A caller that catches a business error calls `session.rollback()`: nothing partial is
        then committed.
        """
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()


def _space_key(space_id: UUID) -> int:
    return int.from_bytes(space_id.bytes[:8], "big", signed=True)


# Sync lock: two-integer key. PostgreSQL keeps one-`bigint` keys and two-`int` keys in
# separate key spaces: it never interferes with the space lock.
SYNC_CLASS = 0x53594E43  # "SYNC"


@contextmanager
def _advisory(engine: Engine, args: str, params: dict[str, int]) -> Iterator[None]:
    """Session-level advisory lock, waited for without limit, with no transaction left open.

    The lock belongs to the PostgreSQL session, not to the transaction: commit right away.
    Otherwise the connection would stay "idle in transaction" for the whole block, and an
    `idle_in_transaction_session_timeout` set by an administrator would cut it, which would
    release the lock silently."""
    with engine.connect() as connection:
        connection.execute(text(f"SELECT pg_advisory_lock({args})"), params)
        connection.commit()
        try:
            yield
        finally:
            connection.execute(text(f"SELECT pg_advisory_unlock({args})"), params)
            connection.commit()


@contextmanager
def space_lock(engine: Engine, space_id: UUID) -> Iterator[None]:
    """Exclusive lock on a space's local clone, shared by the web app and the workers.

    Never any network access under this lock: an unresponsive forge would block every write
    to the space (see `git_sync`)."""
    with _advisory(engine, ":key", {"key": _space_key(space_id)}):
        yield


@contextmanager
def sync_lock(engine: Engine, space_id: UUID) -> Iterator[None]:
    """A single exchange with the remote repository at a time per space (sync or conflict
    resolution), network included. Blocks neither the web app nor the tidy-up: they only take
    the space lock, which the sync holds only for its local steps."""
    key = int.from_bytes(space_id.bytes[:4], "big", signed=True)
    with _advisory(engine, ":cls, :key", {"cls": SYNC_CLASS, "key": key}):
        yield


@contextmanager
def try_space_lock(engine: Engine, space_id: UUID, timeout: float) -> Iterator[bool]:
    """Like `space_lock`, but gives up after `timeout` seconds (yields False)."""
    key = _space_key(space_id)
    deadline = time.monotonic() + timeout
    with engine.connect() as connection:
        while True:
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            ).scalar_one()
            connection.commit()
            if acquired or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        try:
            yield bool(acquired)
        finally:
            if acquired:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def pending_migrations(database: Database) -> bool:
    script = ScriptDirectory.from_config(alembic_config(database.url))
    with database.engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_heads()
    return set(current) != set(script.get_heads())


def known_revision(url: str, revision: str) -> bool:
    """True if this version of the code knows the revision (otherwise: newer database)."""
    script = ScriptDirectory.from_config(alembic_config(url))
    try:
        return script.get_revision(revision) is not None
    except Exception:  # noqa: BLE001 — unknown or malformed revision
        return False
