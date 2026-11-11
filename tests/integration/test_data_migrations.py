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
  of the organization only, spaces of the organization only), with cascading deletions;
- 0032: pending invitations keep their roles and join no group; an invitation only names
  groups of its organization, and deleting the group or the invitation removes the link;
- 0033: no capture by email is configured (nothing is read before the super admin sets it
  up), and what it remembers of an email outlives the space and the account it named;
- 0034: each routine keeps its row (state, runs), its name becomes its title, and it gets a
  file name unique in its space, waiting to be written to the repository;
- 0035: the nightly maintenance is on everywhere and starts from each space's last
  synchronized head (nothing is caught up, and the edit that triggers the next sync is not
  taken for the past); a space never synchronized waits for its first sync;
- 0037: every routine reads no external source, and going down removes the sources and their
  secrets, the routines staying as they were.

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


def test_invitation_groups_migration_keeps_them_within_the_organization(
    new_database: Callable[[], str],
) -> None:
    """0032 on an instance in service: the pending invitation is untouched and names no group;
    the composite foreign keys refuse a group of another organization, under either
    organization; deleting the group or the invitation removes the link; the migration goes
    down and up again."""
    url = new_database()
    config = alembic_config(url)
    command.upgrade(config, "0031")
    acme, globex, rh, globex_rh = uuid4(), uuid4(), uuid4(), uuid4()
    pending, other = uuid4(), uuid4()
    engine = create_engine(url)

    def run(sql: str, **values: object) -> None:
        with engine.begin() as connection:
            connection.execute(text(sql), values)

    def rows(sql: str) -> list[tuple[object, ...]]:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql))]

    invite = (
        "INSERT INTO invitations (id, token_hash, email, org_id, org_role, space_roles, "
        "created_at, expires_at) VALUES (:id, :hash, :email, :org, 'member', "
        "'{\"s\": \"reader\"}', now(), now() + interval '14 days')"
    )
    try:
        for org, slug in ((acme, "acme"), (globex, "globex")):
            run(
                "INSERT INTO organizations (id, slug, name, description, created_at) "
                "VALUES (:id, :slug, :slug, '', now())",
                id=org,
                slug=slug,
            )
        for group, org in ((rh, acme), (globex_rh, globex)):
            run(
                "INSERT INTO groups (id, org_id, name, created_at) VALUES (:id, :org, 'RH', now())",
                id=group,
                org=org,
            )
        run(invite, id=pending, hash="a" * 64, email="zoe@example.com", org=acme)
        command.upgrade(config, "0032")
        assert rows("SELECT email, org_role, space_roles::text FROM invitations") == [
            ("zoe@example.com", "member", '{"s": "reader"}')
        ]
        assert rows("SELECT * FROM invitation_groups") == []

        link = (
            "INSERT INTO invitation_groups (invitation_id, group_id, org_id) "
            "VALUES (:invitation, :group, :org)"
        )
        run(link, invitation=pending, group=rh, org=acme)
        for org in (acme, globex):  # Globex's group, under either organization
            with pytest.raises(IntegrityError):
                run(link, invitation=pending, group=globex_rh, org=org)
        with pytest.raises(IntegrityError):  # twice the same group
            run(link, invitation=pending, group=rh, org=acme)

        # Cascades: the group, the invitation, the organization.
        run(invite, id=other, hash="b" * 64, email="ada@example.com", org=acme)
        run(link, invitation=other, group=rh, org=acme)
        run("DELETE FROM groups WHERE id = :rh", rh=rh)
        assert rows("SELECT * FROM invitation_groups") == []
        run(
            "INSERT INTO groups (id, org_id, name, created_at) VALUES (:id, :org, 'RH', now())",
            id=rh,
            org=acme,
        )
        run(link, invitation=pending, group=rh, org=acme)
        run(link, invitation=other, group=rh, org=acme)
        run("DELETE FROM invitations WHERE id = :pending", pending=pending)
        assert rows("SELECT invitation_id FROM invitation_groups") == [(other,)]
        run("DELETE FROM organizations WHERE id = :acme", acme=acme)
        assert rows("SELECT * FROM invitation_groups") == []

        command.downgrade(config, "0031")
        assert (
            rows(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name = 'invitation_groups'"
            )
            == []
        )
        assert rows("SELECT conname FROM pg_constraint WHERE conname = 'uq_invitations_id'") == []
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_email_capture_migration_starts_empty_and_keeps_no_content(
    new_database: Callable[[], str],
) -> None:
    """0033 on an instance in service: no capture configured (nothing is read until the super
    admin sets it up); a single row of settings, within its bounds; a capture outlives the
    space and the account it named (the link is emptied, the line kept for the health
    page); the migration goes down and up again."""
    url = new_database()
    config = alembic_config(url)
    command.upgrade(config, "0032")
    org, space, user = uuid4(), uuid4(), uuid4()
    engine = create_engine(url)

    def run(sql: str, **values: object) -> None:
        with engine.begin() as connection:
            connection.execute(text(sql), values)

    def rows(sql: str) -> list[tuple[object, ...]]:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql))]

    settings = (
        "INSERT INTO capture_settings (id, host, port, security, username, folder, address, "
        "authserv_ids, unnamed_results, spf_domains, interval, replies, updated_at) VALUES "
        "(:id, 'imap.example.com', 993, :security, 'brain', 'INBOX', 'brain@example.com', "
        "'mx.example.com', false, '', :interval, :replies, now())"
    )
    try:
        run(
            "INSERT INTO organizations (id, slug, name, description, created_at) "
            "VALUES (:id, 'acme', 'Acme', '', now())",
            id=org,
        )
        run(
            "INSERT INTO spaces (id, org_id, slug, name, description, branch, created_at, "
            "sync_status, conflict_paths, tidy_enabled, tidy_time, tidy_timezone) VALUES "
            "(:id, :org, 'infra', 'Infra', '', 'main', now(), 'never', '[]', true, '02:00', "
            "'Europe/Paris')",
            id=space,
            org=org,
        )
        run(
            "INSERT INTO users (id, username, display_name, email, password_hash, is_superadmin, "
            "is_active, locale, created_at, must_change_password) VALUES (:id, 'ada', 'Ada', "
            "'ada@example.com', NULL, false, true, 'fr', now(), false)",
            id=user,
        )
        command.upgrade(config, "0033")
        assert rows("SELECT * FROM capture_settings") == []
        assert rows("SELECT * FROM email_captures") == []

        run(settings, id=1, security="ssl", interval=2, replies="refusals")
        for values in (
            {"id": 2, "security": "ssl", "interval": 2, "replies": "off"},  # a second row
            {"id": 1, "security": "tls", "interval": 2, "replies": "off"},
        ):
            with pytest.raises(IntegrityError):
                run(settings, **values)
        for column, value in (("interval", 0), ("interval", 61), ("replies", "maybe")):
            with pytest.raises(IntegrityError):
                run(f"UPDATE capture_settings SET {column} = :value", value=value)

        capture = (
            "INSERT INTO email_captures (id, key, at, status, reason, sender_domain, target, "
            "space_id, user_id) VALUES (:id, :key, now(), :status, '', 'example.com', "
            "'acme.infra', :space, :user)"
        )
        run(capture, id=uuid4(), key="a" * 64, status="accepted", space=space, user=user)
        with pytest.raises(IntegrityError):  # the same email twice
            run(capture, id=uuid4(), key="a" * 64, status="accepted", space=space, user=user)
        with pytest.raises(IntegrityError):
            run(capture, id=uuid4(), key="b" * 64, status="maybe", space=space, user=user)
        run("DELETE FROM spaces WHERE id = :space", space=space)
        run("DELETE FROM users WHERE id = :user", user=user)
        assert rows("SELECT status, target, space_id, user_id FROM email_captures") == [
            ("accepted", "acme.infra", None, None)
        ]

        command.downgrade(config, "0032")
        assert (
            rows(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ('capture_settings', 'email_captures')"
            )
            == []
        )
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_maintenance_migration_starts_from_the_last_synchronized_head(
    new_database: Callable[[], str],
) -> None:
    url = new_database()
    config = alembic_config(url)
    command.upgrade(config, "0033")
    org, synced, never = uuid4(), uuid4(), uuid4()
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
            for space, slug, head in ((synced, "rh", "a" * 40), (never, "dev", None)):
                connection.execute(
                    text(
                        "INSERT INTO spaces (id, org_id, slug, name, description, branch, "
                        "created_at, sync_status, conflict_paths, tidy_enabled, tidy_time, "
                        "tidy_timezone, embeddings_enabled, remote_head) VALUES (:id, :org, "
                        ":slug, :slug, '', 'main', now(), 'ok', '[]', true, '02:00', 'UTC', "
                        "false, :head)"
                    ),
                    {"id": space, "org": org, "slug": slug, "head": head},
                )
        command.upgrade(config, "0035")
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT slug, tidy_maintain, maintain_watermark, maintain_attempts FROM spaces"
                )
            ).all()
        assert sorted(tuple(r) for r in rows) == [("dev", True, None, 0), ("rh", True, "a" * 40, 0)]
        command.downgrade(config, "0033")
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_routines_migration_gives_each_routine_a_file_name_and_keeps_its_state(
    new_database: Callable[[], str],
) -> None:
    """0034 on an instance in service: each routine keeps its row (state, runs) and its name
    as its title; it gets a valid file name, unique in its space, and waits to be written to
    the repository (`sha` NULL); the migration goes down and up again."""
    url = new_database()
    config = alembic_config(url)
    command.upgrade(config, "0033")
    org, infra, ventes = uuid4(), uuid4(), uuid4()
    first, second, other = uuid4(), uuid4(), uuid4()
    engine = create_engine(url)

    def run(sql: str, **values: object) -> None:
        with engine.begin() as connection:
            connection.execute(text(sql), values)

    def rows(sql: str) -> list[tuple[object, ...]]:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql))]

    try:
        run(
            "INSERT INTO organizations (id, slug, name, description, created_at) "
            "VALUES (:id, 'acme', 'Acme', '', now())",
            id=org,
        )
        for space, slug in ((infra, "infra"), (ventes, "ventes")):
            run(
                "INSERT INTO spaces (id, org_id, slug, name, description, branch, created_at, "
                "sync_status, conflict_paths, tidy_enabled, tidy_time, tidy_timezone) VALUES "
                "(:id, :org, :slug, :slug, '', 'main', now(), 'never', '[]', true, '02:00', "
                "'Europe/Paris')",
                id=space,
                org=org,
                slug=slug,
            )
        for routine, space, name, created in (
            (first, infra, "Contrôle de cohérence", "2026-10-01"),
            (second, infra, "contrôle de cohérence !", "2026-10-02"),  # same file name
            (other, ventes, "Contrôle de cohérence", "2026-10-03"),  # another space
        ):
            run(
                "INSERT INTO routines (id, space_id, name, template, instructions, frequency, "
                "weekday, at, scope, output_page, output_inbox, output_email, output_webhook, "
                "enabled, last_date, requested, created_at, updated_at) VALUES (:id, :space, "
                ":name, 'coherence', 'Relis.', 'weekly', 0, '06:00', '[]', 'r.md', true, '', "
                "false, true, '2026-11-02', 'schedule', :created, now())",
                id=routine,
                space=space,
                name=name,
                created=created,
            )
        run(
            "INSERT INTO routine_runs (id, routine_id, space_id, name, trigger, status, model, "
            "started_at, summary, report, outputs, input_tokens, output_tokens, cached_tokens, "
            "cost) VALUES (:id, :routine, :space, 'Contrôle de cohérence', 'schedule', 'done', "
            "'m', now(), '', '', '{}', 0, 0, 0, 0.25)",
            id=uuid4(),
            routine=first,
            space=infra,
        )
        command.upgrade(config, "0034")
        assert rows(
            "SELECT id, name, title, sha, error, warnings, last_date::text, requested "
            "FROM routines ORDER BY created_at"
        ) == [
            (
                first,
                "controle-de-coherence",
                "Contrôle de cohérence",
                None,
                None,
                [],
                "2026-11-02",
                "schedule",
            ),
            (
                second,
                "controle-de-coherence-2",
                "contrôle de cohérence !",
                None,
                None,
                [],
                "2026-11-02",
                "schedule",
            ),
            (
                other,
                "controle-de-coherence",
                "Contrôle de cohérence",
                None,
                None,
                [],
                "2026-11-02",
                "schedule",
            ),
        ]
        assert rows("SELECT routine_id FROM routine_runs") == [(first,)]

        command.downgrade(config, "0033")
        assert rows("SELECT name FROM routines ORDER BY created_at") == [
            ("Contrôle de cohérence",),
            ("contrôle de cohérence !",),
            ("Contrôle de cohérence",),
        ]
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_routines_migration_goes_down_with_long_twin_titles(
    new_database: Callable[[], str],
) -> None:
    """Titles need not be unique since 0034: going down, two routines of a space with the same
    long title (or a title that reads like another one with its file name) still get names
    unique in their space, within 120 characters. Going up, titles without a single Latin
    letter get `routine`, `routine-2`…"""
    url = new_database()
    config = alembic_config(url)
    command.upgrade(config, "0034")
    org, infra = uuid4(), uuid4()
    engine = create_engine(url)

    def run(sql: str, **values: object) -> None:
        with engine.begin() as connection:
            connection.execute(text(sql), values)

    def names() -> list[str]:
        with engine.connect() as connection:
            return [
                row[0]
                for row in connection.execute(text("SELECT name FROM routines ORDER BY created_at"))
            ]

    long = "Revue " + "très " * 21 + "longue"  # 117 characters
    try:
        run(
            "INSERT INTO organizations (id, slug, name, description, created_at) "
            "VALUES (:id, 'acme', 'Acme', '', now())",
            id=org,
        )
        run(
            "INSERT INTO spaces (id, org_id, slug, name, description, branch, created_at, "
            "sync_status, conflict_paths, tidy_enabled, tidy_time, tidy_timezone) VALUES "
            "(:id, :org, 'infra', 'infra', '', 'main', now(), 'never', '[]', true, '02:00', "
            "'Europe/Paris')",
            id=infra,
            org=org,
        )
        for index, (name, title) in enumerate(
            (
                ("revue-tres-tres-tres-longue", long),
                ("revue-tres-tres-tres-longue-2", long),
                ("revue-tres-tres-tres-longue-3", long),
                ("autre", "Veille (veille-2)"),
                ("veille", "Veille"),
                ("veille-2", "Veille"),
            )
        ):
            run(
                "INSERT INTO routines (id, space_id, name, title, description, template, "
                "instructions, frequency, weekday, at, scope, output_page, output_inbox, "
                "output_email, output_webhook, enabled, warnings, created_at, updated_at) "
                "VALUES (:id, :space, :name, :title, '', '', 'Relis.', 'weekly', 0, '06:00', "
                "'[]', 'r.md', true, '', false, true, '[]', :created, now())",
                id=uuid4(),
                space=infra,
                name=name,
                title=title,
                created=f"2026-10-0{index + 1}",
            )
        command.downgrade(config, "0033")
        found = names()
        assert len(set(found)) == 6 and all(len(n) <= 120 for n in found)
        assert found[0] == long and found[3:5] == ["Veille (veille-2)", "Veille"]

        run("DELETE FROM routines")
        for index, name in enumerate(("日報", "!!!", "週報")):
            run(
                "INSERT INTO routines (id, space_id, name, template, instructions, frequency, "
                "weekday, at, scope, output_page, output_inbox, output_email, output_webhook, "
                "enabled, created_at, updated_at) VALUES (:id, :space, :name, '', 'Relis.', "
                "'weekly', 0, '06:00', '[]', 'r.md', true, '', false, true, :created, now())",
                id=uuid4(),
                space=infra,
                name=name,
                created=f"2026-10-0{index + 1}",
            )
        command.upgrade(config, "0034")
        assert names() == ["routine", "routine-2", "routine-3"]
    finally:
        engine.dispose()


def test_sources_migration_adds_no_source_and_goes_down_cleanly(
    new_database: Callable[[], str],
) -> None:
    """0037 on an instance in service: the existing routines read no external source; going
    down removes the sources (and their secrets) and keeps the routines."""
    url = new_database()
    config = alembic_config(url)
    command.upgrade(config, "0036")
    org, space, routine = uuid4(), uuid4(), uuid4()
    engine = create_engine(url)

    def run(sql: str, **values: object) -> None:
        with engine.begin() as connection:
            connection.execute(text(sql), values)

    def rows(sql: str) -> list[tuple[object, ...]]:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql))]

    try:
        run(
            "INSERT INTO organizations (id, slug, name, description, created_at) "
            "VALUES (:id, 'acme', 'Acme', '', now())",
            id=org,
        )
        run(
            "INSERT INTO spaces (id, org_id, slug, name, description, branch, created_at, "
            "sync_status, conflict_paths, tidy_enabled, tidy_time, tidy_timezone) VALUES "
            "(:id, :org, 'infra', 'Infra', '', 'main', now(), 'never', '[]', true, '02:00', "
            "'Europe/Paris')",
            id=space,
            org=org,
        )
        run(
            "INSERT INTO routines (id, space_id, name, title, description, template, "
            "instructions, frequency, weekday, at, scope, output_page, output_inbox, "
            "output_email, output_webhook, enabled, warnings, created_at, updated_at) VALUES "
            "(:id, :space, 'etat', 'État', '', '', 'Relis.', 'daily', NULL, '06:00', '[]', "
            "'etat.md', false, '', false, true, '[]', now(), now())",
            id=routine,
            space=space,
        )
        command.upgrade(config, "0037")
        assert rows("SELECT name, sources FROM routines") == [("etat", [])]
        run(
            "INSERT INTO routine_sources (id, space_id, name, base_url, auth, header, secret, "
            "created_at, updated_at) VALUES (:id, :space, 'statut', "
            "'https://status.example.com/', 'bearer', '', 'chiffré', now(), now())",
            id=uuid4(),
            space=space,
        )
        with pytest.raises(IntegrityError):  # one name per space
            run(
                "INSERT INTO routine_sources (id, space_id, name, base_url, auth, header, "
                "created_at, updated_at) VALUES (:id, :space, 'statut', 'https://x.example/', "
                "'', '', now(), now())",
                id=uuid4(),
                space=space,
            )
        command.downgrade(config, "0036")
        assert rows("SELECT name FROM routines") == [("etat",)]
        assert rows("SELECT to_regclass('routine_sources')") == [(None,)]
        command.upgrade(config, "head")
        assert rows("SELECT count(*) FROM routine_sources") == [(0,)]
    finally:
        engine.dispose()
