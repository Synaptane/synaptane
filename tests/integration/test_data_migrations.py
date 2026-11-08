"""Data migrations on real rows.

`test_migrations.py` downgrades and upgrades everything on an empty database: the backfill
`UPDATE`s touch no row there. Here, we stop at revision 0011, insert in raw SQL what a
running instance would have, then upgrade to `head`. The backfills checked are security
rules (CHANGELOG):

- 0012: addresses already in place are considered verified, not accounts without an
  address;
- 0014: account linking by e-mail stays enabled for GitHub and Google only, providers that
  verify addresses seriously;
- 0022: existing providers keep their automatic account creation (only the default for new
  ones changes), with no domain restriction;
- 0029: existing organizations give no default role in their spaces, and no space is
  restricted: nobody gains access.

Same principle for every future migration that touches data.
"""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from synaptane.storage.db import alembic_config

LINK_BY_EMAIL = {"github": True, "google": True, "oidc": False, "gitlab": False}
AUTO_CREATE = {"github": True, "google": False, "oidc": True, "gitlab": False}


def clock(engine: Engine) -> datetime:
    with engine.connect() as connection:
        value = connection.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(value, datetime)
    return value


def test_data_migrations_on_an_instance_in_service(new_database: Callable[[], str]) -> None:
    url = new_database()
    config = alembic_config(url)
    command.upgrade(config, "0011")
    with_email, without_email = uuid4(), uuid4()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for user_id, username, email in (
                (with_email, "ada", "ada@example.com"),
                (without_email, "grace", None),
            ):
                connection.execute(
                    text(
                        "INSERT INTO users (id, username, display_name, email, password_hash, "
                        "is_superadmin, is_active, locale, created_at, must_change_password) "
                        "VALUES (:id, :username, :username, :email, NULL, false, true, 'fr', "
                        "now(), false)"
                    ),
                    {"id": user_id, "username": username, "email": email},
                )
            for kind in LINK_BY_EMAIL:
                connection.execute(
                    text(
                        "INSERT INTO sso_providers (slug, kind, name, issuer, client_id, "
                        "client_secret, auto_create, enabled, created_at) VALUES (:slug, "
                        ":kind, :kind, 'https://sso.example.com', 'client', :secret, :auto, "
                        "true, now())"
                    ),
                    {
                        "slug": f"sso-{kind}",
                        "kind": kind,
                        "secret": b"chiffre",
                        "auto": AUTO_CREATE[kind],
                    },
                )
        before = clock(engine)  # server clock: the one of the migration's `now()`
        command.upgrade(config, "head")
        after = clock(engine)

        with engine.connect() as connection:
            verified: dict[str, datetime | None] = dict(
                connection.execute(text("SELECT username, email_verified_at FROM users")).all()
            )
            links: dict[str, bool] = dict(
                connection.execute(text("SELECT kind, link_by_email FROM sso_providers")).all()
            )
            created: dict[str, bool] = dict(
                connection.execute(text("SELECT kind, auto_create FROM sso_providers")).all()
            )
            domains = connection.scalars(text("SELECT allowed_domains FROM sso_providers")).all()
        ada = verified["ada"]
        assert ada is not None and before <= ada <= after  # verified during the migration
        assert verified["grace"] is None  # no address: nothing to verify
        assert links == LINK_BY_EMAIL
        assert created == AUTO_CREATE and domains == [[]] * len(AUTO_CREATE)
    finally:
        engine.dispose()


def test_default_role_migration_keeps_access_unchanged(new_database: Callable[[], str]) -> None:
    """0029: existing organizations get no default role and existing spaces are not
    restricted (access stays what it was); the constraint refuses any other role; the
    migration goes down and up again."""
    url = new_database()
    config = alembic_config(url)
    command.upgrade(config, "0028")
    org, space = uuid4(), uuid4()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, description, created_at) "
                    "VALUES (:id, 'acme', 'Acme', '', now())"
                ),
                {"id": org},
            )
            connection.execute(
                text(
                    "INSERT INTO spaces (id, org_id, slug, name, description, branch, "
                    "created_at, sync_status, conflict_paths, tidy_enabled, tidy_time, "
                    "tidy_timezone, embeddings_enabled) VALUES (:id, :org, 'rh', 'RH', '', "
                    "'main', now(), 'never', '[]', true, '02:00', 'UTC', false)"
                ),
                {"id": space, "org": org},
            )
        command.upgrade(config, "0029")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT default_space_role FROM organizations")) == (
                "none"
            )
            assert connection.scalar(text("SELECT restricted FROM spaces")) is False
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text("UPDATE organizations SET default_space_role = 'admin'"))
        command.downgrade(config, "0028")
        with engine.connect() as connection:
            columns = connection.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name IN ('organizations', 'spaces')"
                )
            ).all()
        assert "default_space_role" not in columns and "restricted" not in columns
        command.upgrade(config, "head")
    finally:
        engine.dispose()
