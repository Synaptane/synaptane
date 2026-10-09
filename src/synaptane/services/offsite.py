"""Off-site copy of backups: encrypted with `age`, copied with `rclone`.

- **Encryption to public keys**: the server encrypts but cannot decrypt. The archive holds
  the master key in clear; without an age recipient, the copy is refused. The private key
  stays with the person who restores (password manager, paper).
- **rclone**: more than 70 kinds of destinations (S3 and compatibles, SFTP, WebDAV, SMB,
  Google Drive, OneDrive, Dropbox…). Their credentials stay in the rclone configuration
  (`rclone config`), never in the database nor on the command line.
- **Several destinations**: a failing destination does not stop the others; each result is
  returned, and retention only applies to a destination that has just received the day's
  copy (old copies are never deleted from a destination that did not receive the new one).

No shell calls: arguments as a list, and `--` before paths.
"""

import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from synaptane.config import Settings
from synaptane.core.errors import BackupError

SUFFIX = ".age"
PATTERN = "synaptane-*.age"  # the only files retention may delete
COPY_TIMEOUT = 4 * 3600  # a stuck copy must not hold the cron forever
SHORT_TIMEOUT = 300
# Never an interactive question (configuration password…): fail instead.
RCLONE_FLAGS = ["--ask-password=false", "--quiet"]


@dataclass(frozen=True)
class Outcome:
    """Result for one destination: `error` empty if all went well."""

    remote: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def target(remote: str, name: str) -> str:
    """`b2:bucket/synaptane` + name → `b2:bucket/synaptane/name`; `nas:` + name → `nas:name`."""
    return remote + name if remote.endswith((":", "/")) else f"{remote}/{name}"


def _env(settings: Settings) -> dict[str, str]:
    return {**os.environ, "RCLONE_CONFIG": str(settings.rclone_config)}


def _tail(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " / ".join(lines[-3:])[-500:] or "unknown error"


def _rclone(
    settings: Settings, *args: str, timeout: int = SHORT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["rclone", *RCLONE_FLAGS, *args],
            env=_env(settings),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timed out ({timeout} s)")


# --- Preparation ------------------------------------------------------------------------------


def missing_tools() -> list[str]:
    return [tool for tool in ("age", "rclone") if shutil.which(tool) is None]


def has_recipients(settings: Settings) -> bool:
    return bool(settings.backup_age_recipients or settings.backup_age_recipients_file)


def require_ready(settings: Settings) -> None:
    """Refuses before starting: nothing is copied if a setting is missing."""
    if not settings.backup_remotes:
        raise BackupError(
            "No off-server destination: set SYNAPTANE_BACKUP_REMOTES "
            '(for example "b2:my-bucket/synaptane"), see docs/admin/backup.md.'
        )
    if not has_recipients(settings):
        raise BackupError(
            "Off-server copy refused: no age recipient. The archive holds the master key in "
            "clear; put your public key in SYNAPTANE_BACKUP_AGE_RECIPIENTS (age-keygen on your "
            "workstation, see docs/admin/backup.md)."
        )
    recipients_file = settings.backup_age_recipients_file
    if recipients_file and not recipients_file.is_file():
        raise BackupError(f"age recipients file not found: {recipients_file}")
    absent = missing_tools()
    if absent:
        raise BackupError(
            f"Not found: {', '.join(absent)}. The Docker image includes them; otherwise, install "
            "the age and rclone packages."
        )


# --- Encryption and copy ----------------------------------------------------------------------


def encrypt(settings: Settings, source: Path, encrypted: Path) -> None:
    """Encrypts `source` for each recipient (any one of the private keys opens it)."""
    command = ["age", "--encrypt"]
    for recipient in settings.backup_age_recipients:
        command += ["--recipient", recipient]
    if settings.backup_age_recipients_file:
        command += ["--recipients-file", str(settings.backup_age_recipients_file)]
    command += ["--output", str(encrypted), "--", str(source)]
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=COPY_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise BackupError("Encryption took too long: abandoned.") from None
    if result.returncode != 0:
        raise BackupError(f"age encryption failed: {_tail(result.stderr)}")


def send(settings: Settings, encrypted: Path) -> list[Outcome]:
    """Copies the encrypted file to each destination, then applies retention to those that
    received it."""
    outcomes = []
    for remote in settings.backup_remotes:
        copy = ["copyto", "--", str(encrypted), target(remote, encrypted.name)]
        result = _rclone(settings, *copy, timeout=COPY_TIMEOUT)
        if result.returncode != 0:
            outcomes.append(Outcome(remote, f"copy: {_tail(result.stderr)}"))
            continue
        outcomes.append(Outcome(remote, prune(settings, remote)))
    return outcomes


def prune(settings: Settings, remote: str) -> str:
    """Deletes remote archives older than the retention. Only the `synaptane-*.age` files at
    the root of the destination are concerned: nothing else is touched, even if the
    destination is shared."""
    days = settings.backup_retention_days
    if not days:
        return ""
    only = ["--max-depth", "1", "--include", f"/{PATTERN}"]
    result = _rclone(settings, "delete", "--min-age", f"{days}d", *only, "--", remote)
    return "" if result.returncode == 0 else f"retention: {_tail(result.stderr)}"


# --- Checks -----------------------------------------------------------------------------------


def check(settings: Settings, remote: str) -> Outcome:
    """Writes, reads back then deletes a small probe file: write, read and delete rights
    (retention needs the latter) checked in one go."""
    name = f"synaptane-test-{secrets.token_hex(4)}.txt"
    payload = f"Synaptane probe {secrets.token_hex(8)}\n"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    local = settings.data_dir / f".{name}"
    local.write_text(payload, encoding="utf-8")
    try:
        written = _rclone(settings, "copyto", "--", str(local), target(remote, name))
        if written.returncode != 0:
            return Outcome(remote, f"write: {_tail(written.stderr)}")
        read = _rclone(settings, "cat", "--", target(remote, name))
        deleted = _rclone(settings, "deletefile", "--", target(remote, name))
        if read.returncode != 0 or read.stdout != payload:
            return Outcome(remote, f"read back: {_tail(read.stderr or 'different content')}")
        if deleted.returncode != 0:
            return Outcome(remote, f"delete: {_tail(deleted.stderr)}")
        return Outcome(remote)
    finally:
        local.unlink(missing_ok=True)


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int
    modified: str


def listing(settings: Settings, remote: str) -> list[RemoteFile]:
    """Archives present on a destination, from oldest to newest."""
    only = ["--files-only", "--max-depth", "1", "--include", f"/{PATTERN}"]
    columns = ["--format", "pst", "--separator", "\t"]
    result = _rclone(settings, "lsf", *only, *columns, "--", remote)
    if result.returncode != 0:
        raise BackupError(f"{remote}: {_tail(result.stderr)}")
    files = []
    for line in result.stdout.splitlines():
        name, size, modified = (line.split("\t") + ["", ""])[:3]
        files.append(RemoteFile(name, int(size) if size.isdigit() else 0, modified))
    return sorted(files, key=lambda file: file.name)


NAMED_REMOTE = re.compile(r"^([^:/\\]+):")


def undefined_remotes(settings: Settings) -> list[str] | None:
    """Named destinations missing from the rclone configuration (None: rclone missing).

    A local folder (`/backups`) or an on-the-fly remote (`:local:`, `:sftp:`…) does not need
    to be declared."""
    if shutil.which("rclone") is None:
        return None
    named = {m.group(1) for r in settings.backup_remotes if (m := NAMED_REMOTE.match(r))}
    if not named:
        return []
    result = _rclone(settings, "listremotes", timeout=30)
    known = {line.strip().rstrip(":") for line in result.stdout.splitlines()}
    return sorted(named - known)
