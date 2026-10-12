"""Every migration can be undone and redone, and the resulting schema is exactly that of the
models (no drift)."""

from collections.abc import Callable

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from synaptane.storage.db import alembic_config, upgrade
from synaptane.storage.models import Base


def test_downgrade_to_base_then_upgrade_without_drift(new_database: Callable[[], str]) -> None:
    url = new_database()
    upgrade(url)
    config = alembic_config(url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    finally:
        engine.dispose()
