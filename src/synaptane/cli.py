"""The `synaptane` command-line interface."""

import argparse
import getpass
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from synaptane import backup_cli, version_label
from synaptane.config import ConfigError, Settings, load_settings
from synaptane.core.errors import BackupError, SynaptaneError
from synaptane.core.security import SecretBox

if TYPE_CHECKING:
    from synaptane.ai.agents_client import State
    from synaptane.services.backup import BackupInfo

Command = Callable[[argparse.Namespace], int]


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(args.config)


def cmd_init(args: argparse.Namespace) -> int:
    """Write the configuration and generate the master key if they do not exist."""
    config: Path = args.config or Path(os.environ.get("SYNAPTANE_CONFIG", "synaptane.toml"))
    if config.exists():
        print(f"The configuration already exists: {config}")
    elif not args.database_url and os.environ.get("SYNAPTANE_DATABASE_URL"):
        print("Configuration read from the SYNAPTANE_* environment variables.")
    else:
        if not args.database_url:
            print("--database-url is required to create the configuration.", file=sys.stderr)
            return 2
        lines = [
            f'public_url = "{args.public_url}"',
            f'database_url = "{args.database_url}"',
            f'data_dir = "{args.data_dir}"',
        ]
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        config.chmod(0o600)
        print(f"Configuration written: {config}")
    settings = load_settings(config)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.repos_dir.mkdir(exist_ok=True)
    try:
        fd = os.open(settings.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # also when two containers start at the same time
        print(f"The master key already exists: {settings.key_file}")
        return 0
    with os.fdopen(fd, "wb") as key:
        key.write(SecretBox.generate_key())
    print(f"Master key generated: {settings.key_file} (back it up outside the database)")
    return 0


def cmd_db_upgrade(args: argparse.Namespace) -> int:
    from synaptane.core import logs
    from synaptane.storage.db import Database, upgrade

    logs.configure()  # alembic logs each migration applied
    url = _settings(args).database_url
    upgrade(url)
    database = Database(url)
    try:
        with database.engine.connect() as connection:
            from sqlalchemy import text

            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        database.dispose()
    print(f"Database up to date (revision {revision}).")
    return 0


def cmd_admin_create(args: argparse.Namespace) -> int:
    from synaptane.services import accounts
    from synaptane.storage.db import Database

    password = os.environ.get("SYNAPTANE_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    settings = _settings(args)
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            accounts.create_user(
                session,
                None,
                username=args.username,
                display_name=args.display_name or args.username,
                password=password,
                email=args.email,
                is_superadmin=True,
                locale=settings.default_locale,
            )
    finally:
        database.dispose()
    print(f'Super admin "{args.username}" created.')
    return 0


def cmd_admin_reset_2fa(args: argparse.Namespace) -> int:
    """Turns off two-factor authentication of an account (lost phone and recovery codes)."""
    from synaptane.services import accounts, two_factor
    from synaptane.storage.db import Database

    settings = _settings(args)
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            two_factor.reset(session, None, accounts.get_user(session, args.username))
    finally:
        database.dispose()
    print(f'Two-factor authentication turned off for "{args.username}".')
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Demo data (examples/acme-wiki): account, organization, indexed space."""
    from synaptane.server import load_secret_box
    from synaptane.services import demo
    from synaptane.services.workspace import Workspace
    from synaptane.storage.db import Database

    settings = _settings(args)
    password = args.password or os.environ.get("SYNAPTANE_DEMO_PASSWORD") or None
    database = Database(settings.database_url)
    try:
        demo.check_ready(settings, database)  # before reading the master key
        ws = Workspace.from_settings(settings, load_secret_box(settings))
        result = demo.run(settings, database, ws, password=password, source=args.source)
    except demo.DemoError as error:
        print(f"Demo not set up: {error}", file=sys.stderr)
        return 1
    finally:
        database.dispose()
    print(demo.summary(settings, result))
    return 0


def _schema_ready(settings: Settings) -> bool:
    """Refuse to start on a database behind on migrations: pages reading a missing column
    would return 500 errors. An unreachable database does not prevent starting (pages then
    answer "retry")."""
    from synaptane.storage.db import Database, pending_migrations

    database = Database(settings.database_url)
    try:
        pending = pending_migrations(database)
    except Exception as error:  # noqa: BLE001 — database not ready yet: start anyway
        print(f"Database unreachable at startup: {type(error).__name__}", file=sys.stderr)
        return True
    finally:
        database.dispose()
    if pending:
        print(
            "The database is not up to date: run `synaptane db upgrade` before starting "
            "(with Docker Compose, the `migrate` service does it).",
            file=sys.stderr,
        )
    return not pending


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from synaptane.core import logs
    from synaptane.server import create_app

    log_config = logs.configure()
    if not _schema_ready(_settings(args)):
        return 1
    uvicorn.run(
        create_app(_settings(args)),
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
        log_config=log_config,
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from synaptane.core import logs
    from synaptane.server import load_secret_box
    from synaptane.services.workspace import Workspace
    from synaptane.storage.db import Database
    from synaptane.worker.runner import Worker

    logs.configure()
    settings = _settings(args)
    if not _schema_ready(settings):
        return 1
    database = Database(settings.database_url, "synaptane-worker")
    try:
        Worker(
            settings, database, Workspace.from_settings(settings, load_secret_box(settings))
        ).run()
    finally:
        database.dispose()
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    """Clear the extracted text and request a new sync and indexing of the spaces."""
    from sqlalchemy import select

    from synaptane.server import load_secret_box
    from synaptane.services import jobs, semantic
    from synaptane.services.extraction import clear_index
    from synaptane.services.workspace import Workspace
    from synaptane.storage.db import Database
    from synaptane.storage.models import Organization, Space

    settings = _settings(args)
    ws = Workspace.from_settings(settings, load_secret_box(settings))
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            query = select(Space).join(Space.organization)
            if args.space:
                org, _, slug = args.space.partition("/")
                query = query.where(Organization.slug == org, Space.slug == slug)
            found = list(session.scalars(query))
            for space in found:
                clear_index(ws, space.id)
                semantic.clear(session, space.id)
                jobs.enqueue(session, "sync", space.id)
                jobs.enqueue(session, "index", space.id)
    finally:
        database.dispose()
    if not found:
        print("No space found.", file=sys.stderr)
        return 1
    print(f"{len(found)} space(s) to reindex: the worker takes care of it.")
    return 0


OFFSITE_INCOMPLETE = 3  # good (and written) archive, incomplete off-site copy


def backup_wait(settings: Settings) -> float:
    """Seconds a backup waits for the one running: the longest a backup holds the lock (its
    dump and its read-back, each bounded by `backup_timeout_minutes`; off-site copies are made
    outside the lock)."""
    return 2 * settings.backup_timeout_minutes * 60


def cmd_backup(args: argparse.Namespace) -> int:
    """Archive, read back, then off-site copy (`--offsite`).

    Exit codes: 0 all is well; 1 no usable backup (nothing is written to standard output);
    3 archive verified and written, but at least one off-site destination failed. A cron job
    can therefore keep the local archive on code 3, and alert.
    """
    import tempfile

    from synaptane.services import backup, backup_schedule, instance, offsite

    settings = _settings(args)
    if args.offsite:
        offsite.require_ready(settings)  # refused before any write
    if args.output == "-" and sys.stdout.isatty():
        print("Redirect the output to a file: … backup --output - > file", file=sys.stderr)
        return 2
    wait = backup_wait(settings)
    minutes = round(wait / 60)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # In the data folder (mode 700), not in /tmp: in a container, /tmp is in memory.
    with tempfile.TemporaryDirectory(prefix="backup-", dir=settings.data_dir) as scratch:
        with backup.exclusive(
            settings,
            wait,
            lambda: print(
                "Another backup is running (the worker's nightly one, a cron job or a "
                f"deployment): waiting for it to finish, {minutes} minutes at most…",
                file=sys.stderr,
            ),
        ) as acquired:
            if not acquired:
                print(
                    f"Another backup is still running after {minutes} minutes: nothing was "
                    "done. Try again later (synaptane backup check).",
                    file=sys.stderr,
                )
                return 1
            path, info, kept = _write_backup(settings, args, Path(scratch))
        # Lock released: the off-site copy of this archive does not hold up other backups.
        target = "standard output" if args.output == "-" else str(kept or "no local copy")
        print(
            f"Backup verified: {target} ({info.users} account(s), {info.spaces} space(s)).",
            file=sys.stderr,
        )
        if info.key and not args.offsite:
            print(
                "It contains the master key in plain text: store it somewhere safe, encrypted "
                "(see --offsite).",
                file=sys.stderr,
            )
        if not args.offsite:
            return 0
        code = _send_offsite(settings, path, Path(scratch))
        if code == 0 and info.repos and info.key:
            instance.mark(settings, backup_schedule.COMPLETE_DONE)
        return code


def _write_backup(
    settings: Settings, args: argparse.Namespace, scratch: Path
) -> "tuple[Path, BackupInfo, Path | None]":
    """The verified archive: in `scratch`, or kept (file or folder), always written to a
    hidden `.part` file renamed once read back. Marked as done only once it has also reached
    standard output."""
    from synaptane.services import backup, backup_schedule, instance

    options = {"with_repos": not args.without_repos, "with_key": not args.without_key}
    name = backup.default_name()
    kept: Path | None = None  # local archive kept (file or folder)
    if args.output != "-" and (args.output or not args.offsite):
        # Without --output, the archive goes to $SYNAPTANE_BACKUP_DIR, except with --offsite
        # alone: the day's copy goes off-site.
        kept = Path(args.output or settings.backup_dir or ".")
        if kept.is_dir():
            # Two backups in the same second get the same name: the second one gets a suffix.
            folder, kept = kept, kept / name
            stem = name.removesuffix(".tar.gz")
            count = 2
            while kept.exists():
                kept = folder / f"{stem}-{count}.tar.gz"
                count += 1
        if kept.exists():
            raise FileExistsError(f"Already exists: {kept}")
    path = kept or scratch / name
    part = path.with_name(f".{path.name}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            info = backup.backup(settings, out, **options)
        backup.verify(settings, part)
        part.rename(path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if args.output == "-":
        with path.open("rb") as source:
            shutil.copyfileobj(source, sys.stdout.buffer)
        sys.stdout.buffer.flush()  # a broken stream raises here: nothing is marked
        if os.environ.get(backup_schedule.HOST_MARKS) == "1":
            # deploy/host/backup.sh: the archive is good only once checked and moved on the
            # host; the script marks it then (verified, complete, and the cron job's).
            return path, info, kept
    instance.mark(settings, instance.BACKUP_DONE)
    in_folder = (
        kept is not None
        and settings.backup_dir is not None
        and kept.parent.resolve() == settings.backup_dir.resolve()
    )
    if info.repos and info.key and in_folder:
        # A full backup kept here: the backup of the day for the worker, if recent. Sent to
        # standard output, it is only known to be kept by the one who receives it.
        instance.mark(settings, backup_schedule.COMPLETE_DONE)
    return path, info, kept


def _send_offsite(settings: Settings, archive: Path, scratch: Path) -> int:
    from synaptane.services import instance, offsite

    try:
        outcomes = offsite.copy(settings, archive, scratch)
    except BackupError as error:
        print(f"Off-site copy failed: {error}", file=sys.stderr)
        return OFFSITE_INCOMPLETE
    for outcome in outcomes:
        state = "ok" if outcome.ok else f"FAILED — {outcome.error}"
        where = offsite.target(outcome.remote, f"{archive.name}{offsite.SUFFIX}")
        print(f"Off-site: {where}: {state}", file=sys.stderr)
    failed = [outcome.remote for outcome in outcomes if not outcome.ok]
    if failed:
        print(f"Incomplete off-site copy: {', '.join(failed)}.", file=sys.stderr)
        return OFFSITE_INCOMPLETE
    instance.mark(settings, instance.OFFSITE_DONE)
    print(f"Encrypted copy sent to {len(outcomes)} destination(s).", file=sys.stderr)
    return 0


NO_DESTINATION = (
    "No destination: run `synaptane backup setup` (or `synaptane offsite add NAME:PATH`), "
    "or set SYNAPTANE_BACKUP_REMOTES."
)


def cmd_offsite_test(args: argparse.Namespace) -> int:
    """Write, read back and delete a probe file on each destination."""
    from synaptane.services import offsite

    settings = _settings(args)
    if not offsite.remotes(settings):
        print(NO_DESTINATION, file=sys.stderr)
        return 1
    if shutil.which("rclone") is None:
        print("rclone not found.", file=sys.stderr)
        return 1
    ok = True
    for remote in offsite.remotes(settings):
        outcome = offsite.check(settings, remote)
        ok &= outcome.ok
        state = "ok" if outcome.ok else "FAILED"
        print(f"[{state}] {remote}" + (f" — {outcome.error}" if outcome.error else ""))
    if not offsite.has_recipients(settings):
        ok = False
        print("[FAILED] age recipients — none: `backup --offsite` will be refused.")
    elif shutil.which("age") is None:
        ok = False
        print("[FAILED] age — not found: backups cannot be encrypted.")
    return 0 if ok else 1


def cmd_offsite_list(args: argparse.Namespace) -> int:
    """Encrypted archives present on each destination."""
    from synaptane.services import offsite

    settings = _settings(args)
    if not offsite.remotes(settings):
        print(NO_DESTINATION, file=sys.stderr)
        return 1
    ok = True
    for remote in offsite.remotes(settings):
        print(f"{remote}")
        try:
            files = offsite.listing(settings, remote)
        except BackupError as error:
            ok = False
            print(f"  FAILED — {error}")
            continue
        for file in files:
            print(f"  {file.name}  {file.size / 1_000_000:.1f} MB  {file.modified}")
        if not files:
            print("  (no archive)")
    return 0 if ok else 1


def cmd_restore(args: argparse.Namespace) -> int:
    from synaptane.services import backup

    settings = _settings(args)
    if args.archive == "-":
        info = backup.restore(settings, sys.stdin.buffer, force=args.force)
    else:
        with open(args.archive, "rb") as source:
            info = backup.restore(settings, source, force=args.force)
    print(
        f"Backup of {info.created_at} (Synaptane {info.version}, commit {info.commit}) "
        "restored: "
        f"{info.spaces} space(s)"
        + ("" if info.repos else ", repositories to clone again from the remotes")
        + ". The worker syncs and reindexes again."
    )
    return 0


def _doctor_offsite(settings: Settings, check: Callable[..., None]) -> None:
    """Off-site backup: always a "warning", never a failure (optional)."""
    from synaptane.services import backup_schedule, offsite

    if backup_schedule.time_invalid(settings):
        check(
            "Automatic backup time",
            False,
            f'"{settings.backup_time}" is not a time: 04:00 is used (SYNAPTANE_BACKUP_TIME, '
            "HH:MM or off)",
            optional=True,
        )
    if backup_schedule.enabled(settings):
        check(f"Automatic backup: every day at {backup_schedule.time_text(settings)}", True)
    else:
        check(
            "Automatic backup",
            False,
            "off: `synaptane backup check` says what is missing",
            optional=True,
        )
    if not offsite.remotes(settings):
        check(
            "Off-site backup",
            False,
            "not configured: run `synaptane backup setup` (docs/admin/backup.md)",
            optional=True,
        )
        return
    count = len(offsite.remotes(settings))
    check(f"Off-site backup: {count} destination(s)", True)
    check(
        "age recipients",
        offsite.has_recipients(settings),
        "no public key: `backup --offsite` will refuse to copy (`synaptane offsite key`)",
        optional=True,
    )
    for tool, role in (("age", "encryption"), ("rclone", "copy")):
        installed = shutil.which(tool) is not None
        check(f"{tool} installed ({role})", installed, f"install {tool}", optional=True)
    undefined = offsite.undefined_remotes(settings)
    if undefined is not None:
        check(
            "Destinations declared in rclone",
            not undefined,
            f"missing from {settings.rclone_config}: {', '.join(undefined)} "
            "(docker compose exec -it app rclone config)",
            optional=True,
        )


def cmd_doctor(args: argparse.Namespace) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from synaptane import extract
    from synaptane.storage.db import Database, pending_migrations

    settings = _settings(args)
    ok = True

    def check(label: str, passed: bool, hint: str = "", *, optional: bool = False) -> None:
        nonlocal ok
        ok &= passed or optional
        state = "ok" if passed else "warning" if optional else "FAILED"
        print(f"[{state}] {label}" + (f" — {hint}" if not passed else ""))

    print(f"Synaptane {version_label()}")
    check("Configuration", True)
    key = settings.key_file
    key_ok = key.exists() and key.stat().st_size == SecretBox.KEY_BYTES
    check("Master key", key_ok, f"{key} missing or invalid: run `synaptane init`")
    if key_ok:
        private = not stat.S_IMODE(key.stat().st_mode) & 0o077
        check("Master key permissions", private, f"chmod 600 {key}")
    check("Repositories folder", settings.repos_dir.is_dir(), "run `synaptane init`")
    check("Git installed", shutil.which("git") is not None, "install git")
    check("ripgrep installed", shutil.which("rg") is not None, "install ripgrep (search)")
    check(
        "Tesseract installed",
        shutil.which("tesseract") is not None,
        "install tesseract-ocr (text of scanned PDFs and images)",
        optional=True,
    )
    if shutil.which("tesseract"):
        languages = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30
        ).stdout.split()
        wanted = settings.ocr_languages.split("+")
        absent = [lang for lang in wanted if lang not in languages]
        check(
            "Tesseract languages",
            not absent,
            f"missing: {', '.join(absent)} (tesseract-ocr-… packages)",
            optional=True,
        )
    check(
        "catdoc installed",
        extract.legacy_available(),
        "install catdoc (text of the old Word and Excel files .doc and .xls)",
        optional=True,
    )
    check(
        "PostgreSQL client (backups)",
        shutil.which("pg_dump") is not None and shutil.which("pg_restore") is not None,
        "install postgresql-client for `synaptane backup` and `restore`",
        optional=True,
    )
    _doctor_offsite(settings, check)
    check(
        "Public URL over HTTPS",
        settings.secure_cookies,
        f"{settings.public_url}: required in production (cookies, OAuth, Claude.ai)",
        optional=True,
    )
    writable = os.access(settings.data_dir, os.W_OK)
    check("Data folder writable", writable, str(settings.data_dir))
    _doctor_subscriptions(settings, check)
    database = Database(settings.database_url)
    try:
        pending = pending_migrations(database)
        check("Database reachable", True)
        check("Migrations up to date", not pending, "run `synaptane db upgrade`")
        if key_ok and not pending:
            from synaptane.services.secrets import readable

            with database.session() as session:
                state = readable(session, SecretBox(key.read_bytes()))
            check(
                "Secrets decryptable with the current key",
                state is not False,
                "the master key is not the one that encrypted the secrets: put the original "
                "key (backup) back in " + str(key),
            )
    except SQLAlchemyError as error:
        check("Database reachable", False, type(error).__name__)
    finally:
        database.dispose()
    return 0 if ok else 1


def _doctor_subscriptions(settings: Settings, check: Callable[..., None]) -> None:
    """Subscription CLIs: where they run (agents container or worker), installed, isolated
    (contract), signed in outside the shared volume."""
    from synaptane.ai import agents_client, cli_agents

    # In the worker, the agents container is asked directly; elsewhere (web container, which
    # does not reach it), what the worker last reported.
    if cli_agents.agents_url():
        state: agents_client.State | None = agents_client.probe_state()
    else:
        state = agents_client.read_state(settings.data_dir)
    if state is not None and state.container:
        _doctor_agents_container(state, check)
    else:
        _doctor_local_clis(check)
    legacy = cli_agents.legacy_logins(settings.data_dir)
    check(
        "Subscription sign-ins outside the data volume",
        not legacy,
        f"{', '.join(map(str, legacy))}: visible from the web container. Sign the CLI in again "
        f"(`{cli_agents.login_command('claude_code', container=bool(state and state.container))}`"
        f" or `{cli_agents.login_command('codex', container=bool(state and state.container))}`),"
        f" then delete the old folder (`{cli_agents.legacy_cleanup(settings.data_dir)}`)",
        optional=True,
    )


SUBSCRIPTION_LABELS = {
    "claude": "Claude subscription (claude)",
    "codex": "ChatGPT subscription (codex)",
}


def _doctor_local_clis(check: Callable[..., None]) -> None:
    """The CLIs run in the worker: their contract is checked here, in the same image."""
    from synaptane.ai import cli_contract

    reports = cli_contract.check_all()
    for report in reports:
        label = SUBSCRIPTION_LABELS.get(report.binary, report.label)
        if not report.installed:
            check(label, False, "optional: CLI not installed", optional=True)
            continue
        check(
            f"{label}: agent isolation ({report.version})",
            not report.problems,
            "; ".join(report.problems) + " — details: `synaptane check-clis`",
            optional=True,
        )
    if any(report.installed for report in reports):
        check(
            "Subscription CLIs apart from the worker",
            False,
            "they run in the worker, which can read the data folder (master key, "
            "repositories): run them in the agents container of compose.yaml "
            "(SYNAPTANE_AGENTS_URL, docs/admin/ai.md)",
            optional=True,
        )


def _doctor_agents_container(state: "State", check: Callable[..., None]) -> None:
    from datetime import UTC, datetime

    from synaptane.ai import agents_client

    seen = state.checked_at.isoformat(timespec="minutes") if state.checked_at else "?"
    check(
        f"Agents container (subscription CLIs, {state.url})",
        state.reachable,
        f"unreachable: {state.error} (seen {seen}; `docker compose ps agents`, "
        "`docker compose logs agents`)",
        optional=True,
    )
    if state.checked_at and datetime.now(UTC) - state.checked_at > agents_client.STALE:
        check(
            "Agents container: recent report from the worker",
            False,
            f"last report {seen}: is the worker running?",
            optional=True,
        )
    if not state.reachable:
        return
    check(
        "Agents container isolated",
        not state.exposures,
        "it can reach: " + ", ".join(state.exposures) + " (compose.yaml, service agents)",
        optional=True,
    )
    if state.contract_pending:
        check("Agents container: isolation contract", False, "not checked yet", optional=True)
    for cli in state.clis:
        label = SUBSCRIPTION_LABELS.get(cli.binary, cli.label)
        if not cli.installed:
            check(label, False, "optional: CLI not installed in the image", optional=True)
            continue
        check(
            f"{label}: agent isolation ({cli.version}, agents container)",
            not cli.problems,
            "; ".join(cli.problems)
            + " — details: `docker compose exec agents synaptane check-clis`",
            optional=True,
        )


def cmd_agents_runner(args: argparse.Namespace) -> int:
    """Agents container: runs the subscription CLIs for the worker (docs/admin/ai.md)."""
    from synaptane.ai import agents_client, agents_runner
    from synaptane.ai.base import AIError
    from synaptane.core import logs

    logs.configure()
    try:
        key_file = agents_client.key_file()
    except AIError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    max_runs = str(args.max_runs if args.max_runs is not None else "")
    max_runs = max_runs or os.environ.get("SYNAPTANE_AGENTS_MAX_RUNS", "").strip() or "2"
    if not max_runs.isdigit() or int(max_runs) < 1:
        print(f"Error: CLIs at a time (--max-runs): {max_runs!r}, 1 at least.", file=sys.stderr)
        return 2
    agents_runner.serve(args.host, args.port, key_file, int(max_runs))
    return 0


def cmd_check_clis(args: argparse.Namespace) -> int:
    """Contract of the subscription CLIs (docs/admin/ai.md): no sign-in, no model call."""
    from synaptane.ai import cli_contract

    failed = False
    for report in cli_contract.check_all():
        if not report.installed:
            failed |= not args.installed_only
            state = "warning" if args.installed_only else "FAILED"
            print(f"[{state}] {report.label}: not installed")
            continue
        failed |= bool(report.problems)
        print(f"[{'ok' if report.ok else 'FAILED'}] {report.label} {report.version}")
        for problem in report.problems:
            print(f"  - {problem}")
    if failed:
        print(
            "Contract broken or not verified: a subscription's agent could have tools other "
            "than Synaptane's. See docs/admin/ai.md (Security).",
            file=sys.stderr,
        )
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synaptane",
        description="A team second brain stored in Git.",
    )
    parser.add_argument("--version", action="version", version=f"synaptane {version_label()}")
    parser.add_argument(
        "--config",
        type=Path,
        help="configuration file (default: $SYNAPTANE_CONFIG or ./synaptane.toml)",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    init = commands.add_parser("init", help="create the configuration and the master key")
    init.add_argument("--database-url", help="postgresql+psycopg://user:password@host/database")
    init.add_argument("--data-dir", default="./data", help="data folder (default ./data)")
    init.add_argument("--public-url", default="http://127.0.0.1:8000")
    init.set_defaults(handler=cmd_init)

    db = commands.add_parser("db", help="database").add_subparsers(
        dest="db_command", metavar="<action>", required=True
    )
    db.add_parser("upgrade", help="apply the migrations").set_defaults(handler=cmd_db_upgrade)

    admin = commands.add_parser("admin", help="administration accounts").add_subparsers(
        dest="admin_command", metavar="<action>", required=True
    )
    create = admin.add_parser("create", help="create a super admin")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name")
    create.add_argument("--email")
    create.set_defaults(handler=cmd_admin_create)
    reset_2fa = admin.add_parser(
        "reset-2fa", help="turn off an account's two-factor authentication (lost phone)"
    )
    reset_2fa.add_argument("--username", required=True)
    reset_2fa.set_defaults(handler=cmd_admin_reset_2fa)

    demo = commands.add_parser(
        "demo",
        help="load the demo data (Acme's fictional wiki): demo account, acme/it space; "
        "requires SYNAPTANE_ALLOW_LOCAL_REPOS=true",
    )
    demo.add_argument(
        "--password",
        help="password of the demo account when created (else $SYNAPTANE_DEMO_PASSWORD, or "
        "generated and shown once)",
    )
    demo.add_argument("--source", type=Path, help="dataset folder (default: examples/acme-wiki)")
    demo.set_defaults(handler=cmd_demo)

    serve = commands.add_parser("serve", help="start the web, REST and MCP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--forwarded-allow-ips", default="127.0.0.1", help="trusted proxies (X-Forwarded-*)"
    )
    serve.set_defaults(handler=cmd_serve)

    commands.add_parser("worker", help="run the background jobs (Git sync…)").set_defaults(
        handler=cmd_worker
    )
    reindex = commands.add_parser("reindex", help="rebuild the search index from Git")
    reindex.add_argument("--space", help="a single space, as org/space")
    reindex.set_defaults(handler=cmd_reindex)
    commands.add_parser("doctor", help="check the installation").set_defaults(handler=cmd_doctor)
    clis = commands.add_parser(
        "check-clis",
        help="check that the subscription CLIs (claude, codex) give the agent only Synaptane's "
        "tools",
    )
    clis.add_argument(
        "--installed-only",
        action="store_true",
        help="do not fail for a missing CLI (by default both are required, as in an image "
        "built with AGENT_CLIS=true; the published image has neither)",
    )
    clis.set_defaults(handler=cmd_check_clis)
    runner = commands.add_parser(
        "agents-runner",
        help="agents container: run the subscription CLIs for the worker, apart from it "
        "(key: $SYNAPTANE_AGENTS_KEY_FILE)",
    )
    runner.add_argument("--host", default="127.0.0.1")
    runner.add_argument("--port", type=int, default=8700)
    runner.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="CLIs running at the same time, at most (default 2, $SYNAPTANE_AGENTS_MAX_RUNS)",
    )
    runner.set_defaults(handler=cmd_agents_runner)

    save = commands.add_parser(
        "backup",
        help="back up the database, master key and repositories; `backup check`, `backup setup`",
    )
    save.add_argument(
        "--output",
        help="file or folder (default: $SYNAPTANE_BACKUP_DIR or ., none with --offsite);"
        ' "-": standard output',
    )
    save.add_argument("--without-repos", action="store_true", help="without the repository clones")
    save.add_argument(
        "--without-key", action="store_true", help="without the master key (to back up separately)"
    )
    save.add_argument(
        "--offsite",
        action="store_true",
        help="encrypt (age) and copy to SYNAPTANE_BACKUP_REMOTES (rclone); code 3 if a "
        "destination fails",
    )
    save.set_defaults(handler=cmd_backup)
    away = commands.add_parser("offsite", help="off-site backup destinations").add_subparsers(
        dest="offsite_command", metavar="<action>", required=True
    )
    away.add_parser(
        "test", help="write, read back and delete a probe file on each destination"
    ).set_defaults(handler=cmd_offsite_test)
    away.add_parser("list", help="archives present on each destination").set_defaults(
        handler=cmd_offsite_list
    )
    backup_cli.register(save, away)
    load = commands.add_parser("restore", help="restore a backup (application and worker stopped)")
    load.add_argument("archive", help='a .tar.gz file, or "-" for standard input')
    load.add_argument(
        "--force", action="store_true", help="replace an existing instance or its key"
    )
    load.set_defaults(handler=cmd_restore)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Command | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return handler(args)
    except (ConfigError, SynaptaneError, BackupError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
