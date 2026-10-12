"""Real PostgreSQL: a throwaway database per test session, emptied between tests.

Start the development database: `docker compose -f compose.dev.yaml up -d`.
Another instance: `SYNAPTANE_TEST_DATABASE_URL=postgresql+psycopg://…/postgres`.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.core.security import SecretBox
from synaptane.server import create_app
from synaptane.services import Actor, accounts, load_actor
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database, upgrade
from synaptane.storage.models import Base, User
from tests.integration.gitutil import Remote, workspace_for

ADMIN_URL = os.environ.get(
    "SYNAPTANE_TEST_DATABASE_URL",
    "postgresql+psycopg://synaptane:synaptane@127.0.0.1:5433/synaptane",
)
PASSWORD = "mot-de-passe-de-test"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    name = f"synaptane_test_{uuid4().hex[:10]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        pytest.fail(
            "PostgreSQL de test injoignable. Lancez `docker compose -f compose.dev.yaml up -d` "
            "ou définissez SYNAPTANE_TEST_DATABASE_URL.",
            pytrace=False,
        )
    url = make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    upgrade(url)
    yield url
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def new_database() -> Iterator[Callable[[], str]]:
    """Factory of empty databases, dropped at the end of the test."""
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    names: list[str] = []

    def make() -> str:
        name = f"synaptane_restore_{uuid4().hex[:10]}"
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        names.append(name)
        return make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)

    yield make
    with admin.connect() as connection:
        for name in names:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def database(database_url: str) -> Iterator[Database]:
    database = Database(database_url)
    yield database
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with database.engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    database.dispose()


@pytest.fixture
def db(database: Database) -> Iterator[Session]:
    with database.session() as session:
        yield session


@pytest.fixture
def settings(database_url: str, tmp_path: Path) -> Settings:
    settings = Settings(
        public_url="http://localhost",
        database_url=database_url,
        data_dir=tmp_path / "data",
        allow_local_repos=True,
        # The tests check the interface texts in its source language, French.
        default_locale="fr",
    )
    settings.data_dir.mkdir()
    settings.key_file.write_bytes(SecretBox.generate_key())
    return settings


@pytest.fixture
def client(settings: Settings, database: Database) -> Iterator[TestClient]:
    with TestClient(create_app(settings, database), base_url="http://localhost") as client:
        yield client


def make_user(session: Session, username: str, *, superadmin: bool = False) -> User:
    return accounts.create_user(
        session,
        None,
        username=username,
        display_name=username.capitalize(),
        password=PASSWORD,
        is_superadmin=superadmin,
        locale="fr",
    )


def actor_of(session: Session, user: User) -> Actor:
    return load_actor(session, user)


def token_for(database: Database, username: str) -> str:
    with database.session() as session:
        actor = load_actor(session, accounts.get_user(session, username))
        value, _ = accounts.create_api_token(session, actor, "tests")
    return value


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    return Remote(tmp_path / "remote")


@pytest.fixture
def ctx(settings: Settings) -> Workspace:
    return workspace_for(settings)


from tests.integration.wikidata import wiki  # noqa: E402,F401 — shared fixture
