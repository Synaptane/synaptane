"""Backup commands that set things up: `backup check`, `backup setup`, `offsite key`,
`offsite add` and `offsite remove` (docs/admin/backup.md).

`backup check` says what works and, for what is missing, the exact command to run;
`backup setup` does the missing steps, question by question (encryption key, destination,
first backup). Both run where `synaptane` runs: with Docker,
`docker compose exec -it app synaptane backup setup`.
"""

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from synaptane.config import Settings, check_remote, load_settings
from synaptane.core.errors import BackupError

OK, WARNING, FAILED = "ok", "warning", "FAILED"


@dataclass(frozen=True)
class Line:
    state: str
    label: str
    hint: str = ""

    def __str__(self) -> str:
        return f"[{self.state}] {self.label}" + (f" — {self.hint}" if self.hint else "")


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(args.config)


def _local(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _size(count: int) -> str:
    return f"{count / 1024**3:.1f} GB" if count >= 1024**3 else f"{count // 1024**2} MB"


# --- backup check -----------------------------------------------------------------------------


def checklist(settings: Settings, *, probe: bool = True) -> list[Line]:
    """State of every piece of the backups, with what to run for what is missing. `probe`:
    writes, reads back and deletes a test file on each destination."""
    from synaptane.services import backup_schedule, instance

    now = datetime.now(UTC)
    lines: list[Line] = []
    add = lines.append
    folder = settings.backup_dir
    if backup_schedule.host_in_charge(settings, now):
        add(
            Line(
                OK,
                "Daily backup: by the server's cron job (deploy/host/backup.sh); the automatic "
                "one steps aside while it runs",
            )
        )
    elif backup_schedule.enabled(settings):
        when = backup_schedule.time_text(settings)
        add(Line(OK, f"Automatic backup: every day at {when}, in {folder}"))
    elif folder is None:
        add(Line(WARNING, "Automatic backup", "no folder: set SYNAPTANE_BACKUP_DIR (backup_dir)"))
    else:
        add(
            Line(
                WARNING,
                "Automatic backup",
                "off (SYNAPTANE_BACKUP_TIME=off): a cron job must run deploy/host/backup.sh "
                "every night",
            )
        )
    if backup_schedule.time_invalid(settings):
        add(
            Line(
                WARNING,
                "Automatic backup time",
                f'"{settings.backup_time}" is not a time: 04:00 is used (HH:MM, or off)',
            )
        )
    if folder is not None:
        lines += _folder(folder, settings.backup_local_retention_days)
    pg = shutil.which("pg_dump") is not None and shutil.which("pg_restore") is not None
    add(Line(OK, "PostgreSQL client") if pg else Line(FAILED, "PostgreSQL client", "install it"))
    last = instance.last_backup(settings)
    if last is None:
        add(Line(WARNING, "Last verified backup", "none yet: `synaptane backup` makes one now"))
    elif now - last > instance.BACKUP_LATE:
        add(Line(WARNING, "Last verified backup", f"{_local(last)}, late"))
    else:
        add(Line(OK, f"Last verified backup: {_local(last)}"))
    lines += _offsite_lines(settings, now, probe)
    return lines


def _folder(path: Path, days: int) -> list[Line]:
    from synaptane.services import backup_schedule

    existing = path if path.exists() else path.parent
    if not os.access(existing, os.W_OK):
        return [Line(FAILED, f"Backup folder {path}", "not writable")]
    lines = []
    usage = shutil.disk_usage(existing)
    free = usage.free
    needed = backup_schedule.space_needed(path, usage.total)
    if free < needed:
        reserve = _size(backup_schedule.reserve(usage.total))
        lines.append(
            Line(
                WARNING,
                "Free space",
                f"{_size(free)} free, {_size(needed)} needed (the next archive, and {reserve} "
                "left for the database)",
            )
        )
    else:
        lines.append(Line(OK, f"Free space: {_size(free)} in {path}"))
    if days:
        keep = backup_schedule.KEEP_AT_LEAST
        lines.append(
            Line(
                OK,
                f"Local retention: {days} days for the automatic backups (the {keep} newest "
                "always kept)",
            )
        )
    else:
        lines.append(
            Line(
                WARNING,
                "Local retention",
                "none (SYNAPTANE_BACKUP_LOCAL_RETENTION_DAYS=0): the folder grows without limit",
            )
        )
    return lines


def _offsite_lines(settings: Settings, now: datetime, probe: bool) -> list[Line]:
    from synaptane.services import instance, offsite

    lines: list[Line] = []
    add = lines.append
    keys = offsite.recipients(settings)
    if keys or settings.backup_age_recipients_file:
        count = len(keys) + (1 if settings.backup_age_recipients_file else 0)
        add(Line(OK, f"Encryption: {count} public key(s)"))
    else:
        add(
            Line(
                WARNING,
                "Encryption",
                "no public key: `synaptane offsite key` (or `synaptane backup setup`)",
            )
        )
    missing = offsite.missing_tools()
    if missing:
        add(Line(WARNING, "age and rclone", f"not found: {', '.join(missing)}"))
    remotes = offsite.remotes(settings)
    if not remotes:
        add(
            Line(
                WARNING,
                "Off-site destination",
                "none: `synaptane backup setup` (or `synaptane offsite add NAME:PATH`)",
            )
        )
        return lines
    undefined = set(offsite.undefined_remotes(settings) or [])
    for remote in remotes:
        name = remote.split(":", 1)[0]
        if name in undefined:
            add(Line(FAILED, f"Destination {remote}", "not declared in rclone: `rclone config`"))
        elif probe and "rclone" not in missing:
            outcome = offsite.check(settings, remote)
            add(
                Line(OK, f"Destination {remote}: writable")
                if outcome.ok
                else Line(FAILED, f"Destination {remote}", outcome.error)
            )
        else:
            add(Line(OK, f"Destination {remote}"))
    days = settings.backup_retention_days
    if days:
        add(Line(OK, f"Off-site retention: {days} days"))
    else:
        add(Line(OK, "Off-site retention: none, copies kept (the storage's own rules apply)"))
    last = instance.marked(settings, instance.OFFSITE_DONE)
    if last is None:
        add(Line(WARNING, "Last off-site copy", "none complete yet"))
    elif now - last > instance.BACKUP_LATE:
        add(Line(WARNING, "Last off-site copy", f"{_local(last)}, late"))
    else:
        add(Line(OK, f"Last off-site copy: {_local(last)}"))
    return lines


def cmd_backup_check(args: argparse.Namespace) -> int:
    """What works, and the command to run for what is missing. Code 1 on a failure."""
    settings = _settings(args)
    lines = checklist(settings, probe=not args.no_test)
    for line in lines:
        print(line)
    if any(line.state == FAILED for line in lines):
        return 1
    if any(line.state == WARNING for line in lines):
        print("\nTo finish, step by step: synaptane backup setup")
    else:
        print("\nBackups are set up: verified every night, encrypted and copied off the server.")
    return 0


# --- offsite key, add, remove -----------------------------------------------------------------

KEY_WARNING = """\
This is the PRIVATE key of the off-site copies. It is shown only once and saved nowhere:
- store it now in your password manager, and a second copy off this server (paper, USB stick);
- never on this server: whoever holds the server and this key can read the copies;
- without it, the off-site copies cannot be opened, not even by you."""


def _show_private_key(private: str, out: Callable[[str], None]) -> None:
    out("-" * 72)
    out(private.rstrip("\n"))
    out("-" * 72)
    out(KEY_WARNING)


def cmd_offsite_key(args: argparse.Namespace) -> int:
    """Without an argument: a new key pair, the private key printed once, the public key
    saved. With a public key (`age1…`): only saves it (pair made on your computer)."""
    from synaptane.services import offsite

    settings = _settings(args)
    if args.public_key:
        added = offsite.save_recipient(settings, args.public_key)
        print("Public key saved." if added else "This public key was already there.")
        return 0
    private, public = offsite.generate_identity()
    print(private, end="" if private.endswith("\n") else "\n")
    print(KEY_WARNING, file=sys.stderr)
    offsite.save_recipient(settings, public)
    print(f"Public key saved on the server: {public}", file=sys.stderr)
    return 0


def _probe(settings: Settings, remote: str) -> str:
    """Error of a destination ("" if it works), after the checks that need no network."""
    from synaptane.services import offsite

    try:
        check_remote(remote)
    except ValueError as error:
        return str(error)
    if shutil.which("rclone") is None:
        return "rclone not found"
    named = offsite.NAMED_REMOTE.match(remote)
    if named and named.group(1) not in offsite.rclone_remotes(settings):
        return f'"{named.group(1)}" is not declared in rclone: run `rclone config` first'
    return offsite.check(settings, remote).error


def cmd_offsite_add(args: argparse.Namespace) -> int:
    """Tests a destination (write, read back, delete), then saves it."""
    from synaptane.services import offsite

    settings = _settings(args)
    remote = args.destination.strip()
    error = _probe(settings, remote)
    if error:
        print(f"[FAILED] {remote} — {error}. Not saved.", file=sys.stderr)
        return 1
    added = offsite.save_remote(settings, remote)
    print(f"[ok] {remote}" + (" — saved." if added else " — already there."))
    return 0


def cmd_offsite_remove(args: argparse.Namespace) -> int:
    from synaptane.services import offsite

    settings = _settings(args)
    remote = args.destination.strip()
    if offsite.remove_remote(settings, remote):
        print(f"{remote} removed: the next backups no longer copy to it.")
        return 0
    if remote in settings.backup_remotes:
        print(f"{remote} comes from SYNAPTANE_BACKUP_REMOTES: remove it there.", file=sys.stderr)
    else:
        print(f"{remote} is not a destination.", file=sys.stderr)
    return 1


# --- backup setup -----------------------------------------------------------------------------


def interactive() -> bool:
    return sys.stdin.isatty()


def _ask(question: str, default: str = "") -> str:
    shown = f"{question} [{default}] " if default else f"{question} "
    answer = input(shown).strip()
    return answer or default


def _yes(question: str, default: bool = True) -> bool:
    answer = _ask(f"{question} {'[Y/n]' if default else '[y/N]'}").lower()
    return default if not answer else answer in ("y", "yes", "o", "oui")


def cmd_backup_setup(args: argparse.Namespace) -> int:
    """Guided setup: what is already done is said and skipped; what is missing is asked."""
    if not interactive():
        print(
            "This command asks questions: run it in a terminal, with Docker\n"
            "  docker compose exec -it app synaptane backup setup\n"
            "Without questions: synaptane offsite key, synaptane offsite add NAME:PATH, "
            "synaptane backup check.",
            file=sys.stderr,
        )
        return 2
    settings = _settings(args)
    try:
        print("Synaptane backups: set up, step by step.\n")
        _setup_schedule(settings)
        _setup_key(settings)
        _setup_destination(settings)
        _setup_first_backup(settings)
    except (EOFError, KeyboardInterrupt):
        print("\nStopped: what was saved before stays.", file=sys.stderr)
        return 1
    print("\nCheck at any time: synaptane backup check (and Administration → Health).")
    return 0


def _setup_schedule(settings: Settings) -> None:
    from synaptane.services import backup_schedule

    print("1. Backup on this server")
    if backup_schedule.host_in_charge(settings):
        print(
            "   Made every night by the server's cron job (deploy/host/backup.sh): nothing to do."
        )
    elif backup_schedule.enabled(settings):
        days = settings.backup_local_retention_days
        kept = f"kept {days} days" if days else "all kept"
        print(
            f"   Automatic every day at {backup_schedule.time_text(settings)}, in "
            f"{settings.backup_dir} "
            f"({kept}): nothing to do."
        )
    elif settings.backup_dir is None:
        print("   No automatic backup: set SYNAPTANE_BACKUP_DIR (backup_dir) to turn it on.")
    else:
        print(
            "   Automatic backup off (SYNAPTANE_BACKUP_TIME=off): make sure a cron job runs "
            "deploy/host/backup.sh."
        )


def _setup_key(settings: Settings) -> None:
    from synaptane.services import offsite

    print("\n2. Encryption key of the off-site copies")
    if offsite.has_recipients(settings):
        count = len(offsite.recipients(settings))
        print(f"   Already set ({count} public key(s)): nothing to do.")
        return
    print(
        "   The copies are encrypted for a public key; the private key, the only one that\n"
        "   opens them, stays with you. Either paste the public key of a pair you made on\n"
        "   your computer (age-keygen), or press Enter to make one now."
    )
    while True:
        answer = _ask("   Public key (age1…), or Enter:")
        if not answer:
            break
        try:
            offsite.save_recipient(settings, answer)
        except BackupError as error:
            print(f"   {error}")
            continue
        print("   Public key saved.")
        return
    private, public = offsite.generate_identity()
    print()
    _show_private_key(private, print)
    print()
    while _ask('   Type "saved" once it is stored off this server:').lower() != "saved":
        pass
    offsite.save_recipient(settings, public)
    print("   Public key saved on the server.")


def _setup_destination(settings: Settings) -> None:
    from synaptane.services import offsite

    print("\n3. Off-site destination")
    remotes = offsite.remotes(settings)
    for remote in remotes:
        error = _probe(settings, remote)
        print(f"   [{FAILED if error else OK}] {remote}" + (f" — {error}" if error else ""))
    if remotes and not _yes("   Add another destination?", default=False):
        return
    if shutil.which("rclone") is None:
        print("   rclone not found: install it (the Docker image includes it).")
        return
    declared = offsite.rclone_remotes(settings)
    if declared:
        print(f"   Storage declared in rclone: {', '.join(declared)}.")
    else:
        print(
            "   The storage (S3, SFTP, Nextcloud, Google Drive…) and its access are declared\n"
            "   once in rclone, with its wizard; give it a name you will reuse below."
        )
    if _yes("   Start the rclone wizard now?", default=not declared):
        settings.rclone_config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(
            ["rclone", "config"],
            env={**os.environ, "RCLONE_CONFIG": str(settings.rclone_config)},
            check=False,
        )
        declared = offsite.rclone_remotes(settings)
    suggestion = f"{declared[0]}:synaptane-backups" if len(declared) == 1 else ""
    while True:
        remote = _ask("   Destination, as name:folder (empty: later):", suggestion)
        if not remote:
            print("   No destination for now: run `synaptane backup setup` again later.")
            return
        error = _probe(settings, remote)
        if not error:
            offsite.save_remote(settings, remote)
            print(f"   [ok] {remote}: test file written, read back and deleted. Saved.")
            return
        print(f"   [FAILED] {remote} — {error}")
        suggestion = ""


def _setup_first_backup(settings: Settings) -> None:
    from synaptane.server import load_secret_box
    from synaptane.services import backup_schedule, offsite
    from synaptane.storage.db import Database

    print("\n4. First backup")
    if settings.backup_dir is None:
        print("   No backup folder (SYNAPTANE_BACKUP_DIR): run `synaptane backup --offsite`.")
        return
    if not _yes("   Make one now? It can take a few minutes."):
        return
    database = Database(settings.database_url)
    try:
        result = backup_schedule.run(
            settings,
            database,
            load_secret_box(settings),
            trigger=backup_schedule.MANUAL,
            public_url=settings.public_url,
        )
    except BackupError as error:
        print(f"   [FAILED] {error}")
        return
    finally:
        database.dispose()
    if result.skipped:
        print(
            "   Another backup is running: it will do. Check later with `synaptane backup check`."
        )
        return
    assert result.archive is not None
    print(f"   [ok] Backup verified: {result.archive}")
    if result.offsite_error:
        print(f"   [FAILED] Off-site copy — {result.offsite_error}")
    for outcome in result.outcomes:
        where = offsite.target(outcome.remote, f"{result.archive.name}{offsite.SUFFIX}")
        print(
            f"   [{OK if outcome.ok else FAILED}] {where}"
            + (f" — {outcome.error}" if outcome.error else "")
        )


# --- Parser -----------------------------------------------------------------------------------


def register(
    save: argparse.ArgumentParser,
    away: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    """Adds `backup check|setup` under the `backup` command, `offsite key|add|remove` under
    `offsite`."""
    steps = save.add_subparsers(dest="backup_command", metavar="[check|setup]")
    check = steps.add_parser(
        "check", help="what works, and the exact command to run for what is missing"
    )
    check.add_argument(
        "--no-test", action="store_true", help="do not write a test file to the destinations"
    )
    check.set_defaults(handler=cmd_backup_check)
    steps.add_parser(
        "setup", help="set up the encrypted off-site copy, question by question"
    ).set_defaults(handler=cmd_backup_setup)
    key = away.add_parser(
        "key",
        help="make an encryption key pair (private key shown once, public key saved), or save "
        "a public key",
    )
    key.add_argument("public_key", nargs="?", help="age public key made on your computer")
    key.set_defaults(handler=cmd_offsite_key)
    add = away.add_parser("add", help="test a destination (rclone name:path), then save it")
    add.add_argument("destination")
    add.set_defaults(handler=cmd_offsite_add)
    remove = away.add_parser("remove", help="remove a destination saved with `offsite add`")
    remove.add_argument("destination")
    remove.set_defaults(handler=cmd_offsite_remove)
