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
  restricted: nobody gains access;
- 0031: groups start empty, and the database keeps them within their organization (members
  of the organization only, spaces of the organization only), with cascading deletions.

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


def test_groups_migration_keeps_groups_within_their_organization(
    new_database: Callable[[], str],
) -> None:
    """0031 on an instance in service: existing rows untouched, no group; the composite
    foreign keys refuse a member outside the organization and a space of another one; deleting
    a membership, a space, a group or an organization removes what depends on it; the
    migration goes down and up again."""
    url = new_database()
    config = alembic_config(url)
    command.upgrade(config, "0030")
    acme, globex, infra, ventes = uuid4(), uuid4(), uuid4(), uuid4()
    ada, grace = uuid4(), uuid4()
    engine = create_engine(url)

    def run(sql: str, **values: object) -> None:
        with engine.begin() as connection:
            connection.execute(text(sql), values)

    def count(table: str) -> int:
        with engine.connect() as connection:
            value = connection.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
        assert isinstance(value, int)
        return value

    try:
        for org, slug in ((acme, "acme"), (globex, "globex")):
            run(
                "INSERT INTO organizations (id, slug, name, description, created_at) "
                "VALUES (:id, :slug, :slug, '', now())",
                id=org,
                slug=slug,
            )
        for space, org, slug in ((infra, acme, "infra"), (ventes, globex, "ventes")):
            run(
                "INSERT INTO spaces (id, org_id, slug, name, description, branch, created_at, "
                "sync_status, conflict_paths, tidy_enabled, tidy_time, tidy_timezone, "
                "embeddings_enabled) VALUES (:id, :org, :slug, :slug, '', 'main', now(), "
                "'never', '[]', true, '02:00', 'UTC', false)",
                id=space,
                org=org,
                slug=slug,
            )
        for user, username in ((ada, "ada"), (grace, "grace")):
            run(
                "INSERT INTO users (id, username, display_name, password_hash, is_superadmin, "
                "is_active, locale, created_at, must_change_password) VALUES (:id, :username, "
                ":username, NULL, false, true, 'fr', now(), false)",
                id=user,
                username=username,
            )
        run(
            "INSERT INTO org_members (org_id, user_id, role, created_at) "
            "VALUES (:acme, :ada, 'member', now()), (:globex, :grace, 'member', now())",
            acme=acme,
            ada=ada,
            globex=globex,
            grace=grace,
        )
        run(
            "INSERT INTO space_members (space_id, user_id, role, created_at) "
            "VALUES (:infra, :ada, 'reader', now())",
            infra=infra,
            ada=ada,
        )
        command.upgrade(config, "0031")
        assert count("groups") == count("group_members") == count("group_space_roles") == 0
        assert count("space_members") == 1 and count("spaces") == 2

        group = uuid4()
        run(
            "INSERT INTO groups (id, org_id, name, created_at) VALUES (:id, :org, 'RH', now())",
            id=group,
            org=acme,
        )
        for taken in ("RH", "rh"):  # same name in the organization, whatever the case
            with pytest.raises(IntegrityError):
                run(
                    "INSERT INTO groups (id, org_id, name, created_at) "
                    "VALUES (:id, :org, :name, now())",
                    id=uuid4(),
                    org=acme,
                    name=taken,
                )
        run(  # the same name in another organization: allowed
            "INSERT INTO groups (id, org_id, name, created_at) VALUES (:id, :org, 'RH', now())",
            id=uuid4(),
            org=globex,
        )
        add_member = (
            "INSERT INTO group_members (group_id, org_id, user_id, created_at) "
            "VALUES (:group, :org, :user, now())"
        )
        run(add_member, group=group, org=acme, user=ada)
        with pytest.raises(IntegrityError):  # Grace is not a member of Acme
            run(add_member, group=group, org=acme, user=grace)
        with pytest.raises(IntegrityError):  # the group does not belong to Globex
            run(add_member, group=group, org=globex, user=grace)
        give = (
            "INSERT INTO group_space_roles (group_id, space_id, org_id, role, created_at) "
            "VALUES (:group, :space, :org, :role, now())"
        )
        run(give, group=group, space=infra, org=acme, role="contributor")
        for space, org, role in ((ventes, acme, "reader"), (ventes, globex, "reader")):
            with pytest.raises(IntegrityError):  # a space of another organization
                run(give, group=group, space=space, org=org, role=role)
        with pytest.raises(IntegrityError):  # unknown role
            run("UPDATE group_space_roles SET role = 'admin'")

        # Cascades: the membership, the space, the group, the organization.
        run("DELETE FROM org_members WHERE user_id = :ada", ada=ada)
        assert count("group_members") == 0
        run("DELETE FROM spaces WHERE id = :infra", infra=infra)
        assert count("group_space_roles") == 0
        run("DELETE FROM groups WHERE id = :group", group=group)
        run("DELETE FROM spaces WHERE id = :ventes", ventes=ventes)
        run("DELETE FROM organizations WHERE id = :globex", globex=globex)
        assert count("groups") == 0

        command.downgrade(config, "0030")
        with engine.connect() as connection:
            tables = connection.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name LIKE 'group%'"
                )
            ).all()
            constraints = connection.scalars(
                text("SELECT conname FROM pg_constraint WHERE conname = 'uq_spaces_id'")
            ).all()
        assert tables == [] and constraints == []
        command.upgrade(config, "head")
    finally:
        engine.dispose()
