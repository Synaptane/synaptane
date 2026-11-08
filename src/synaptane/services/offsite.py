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

Public keys and destinations come from the settings (`SYNAPTANE_BACKUP_AGE_RECIPIENTS`,
`SYNAPTANE_BACKUP_REMOTES`) and, in addition, from two files of the data folder written by
`synaptane backup setup`, `offsite key` and `offsite add` (`<data_dir>/backup/`): set up from
the command line, without editing `.env` or restarting. The private key is never written.

No shell calls: arguments as a list, and `--` before paths.
"""

import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from synaptane.config import Settings, check_remote
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


# --- Settings saved from the command line -------------------------------------------------------

RECIPIENTS_FILE = "age-recipients.txt"
REMOTES_FILE = "remotes.txt"
# age refuses anything after an age1 key; an SSH key may end with its comment.
PUBLIC_KEY = re.compile(
    r"^(?:age1[0-9a-z]{20,}|(?:ssh-ed25519|ssh-rsa) [A-Za-z0-9+/=]+(?: \S.*)?)$"
)


def setup_dir(settings: Settings) -> Path:
    return settings.data_dir / "backup"


def _saved(path: Path) -> list[str]:
    """Lines of a saved file, without blank lines and `#` comments."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    stripped = (line.strip() for line in lines)
    return [line for line in stripped if line and not line.startswith("#")]


def _write(path: Path, lines: list[str], header: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(f"{path}.new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write("".join(f"{line}\n" for line in [header, *lines]))
    os.replace(f"{path}.new", path)


def saved_recipients(settings: Settings) -> list[str]:
    """Saved public keys; a line edited by hand that is not one is left out (age would refuse
    the whole copy every night)."""
    return [key for key in _saved(setup_dir(settings) / RECIPIENTS_FILE) if PUBLIC_KEY.match(key)]


def recipients(settings: Settings) -> list[str]:
    """Public keys given in the settings and saved by `offsite key` (not those of an
    `age -R` file named in the settings, read by age itself)."""
    return list(dict.fromkeys([*settings.backup_age_recipients, *saved_recipients(settings)]))


def save_recipient(settings: Settings, public_key: str) -> bool:
    """Adds a public key (`age1…`, `ssh-ed25519 …`). False if it was already there."""
    public_key = public_key.strip()
    if not PUBLIC_KEY.match(public_key):
        raise BackupError("Not an age public key (age1…, ssh-ed25519 … or ssh-rsa …).")
    saved = saved_recipients(settings)
    if public_key in saved or public_key in settings.backup_age_recipients:
        return False
    _write(
        setup_dir(settings) / RECIPIENTS_FILE,
        [*saved, public_key],
        "# age public keys of the off-site copy, saved by `synaptane offsite key`.",
    )
    return True


def generate_identity() -> tuple[str, str]:
    """A new age key pair, in memory: (private key as written by age-keygen, public key).
    Nothing is written to disk; showing the private key once is up to the caller."""
    if shutil.which("age-keygen") is None:
        raise BackupError("age-keygen not found: install age (the Docker image includes it).")
    result = subprocess.run(
        ["age-keygen"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30
    )
    found = re.search(r"^# public key: (age1\S+)$", result.stdout, re.M)
    if result.returncode != 0 or found is None or "AGE-SECRET-KEY-" not in result.stdout:
        raise BackupError(f"age-keygen failed: {_tail(result.stderr)}")
    return result.stdout, found.group(1)


def saved_remotes(settings: Settings) -> list[str]:
    found = []
    for remote in _saved(setup_dir(settings) / REMOTES_FILE):
        try:
            check_remote(remote)
        except ValueError:
            continue  # edited by hand: never passed to rclone
        found.append(remote)
    return found


def remotes(settings: Settings) -> tuple[str, ...]:
    """Destinations: those of the settings, then those saved by `offsite add`."""
    return tuple(dict.fromkeys([*settings.backup_remotes, *saved_remotes(settings)]))


def save_remote(settings: Settings, remote: str) -> bool:
    """Adds a destination. False if it was already there."""
    remote = remote.strip()
    try:
        check_remote(remote)
    except ValueError as error:
        raise BackupError(f"Destination refused: {error}.") from None
    if remote in remotes(settings):
        return False
    _write(
        setup_dir(settings) / REMOTES_FILE,
        [*saved_remotes(settings), remote],
        "# Off-site destinations (rclone), saved by `synaptane offsite add`.",
    )
    return True


def remove_remote(settings: Settings, remote: str) -> bool:
    """Removes a destination saved by `offsite add`. False if it was not saved there (a
    destination of SYNAPTANE_BACKUP_REMOTES is removed from `.env`)."""
    saved = saved_remotes(settings)
    if remote.strip() not in saved:
        return False
    _write(
        setup_dir(settings) / REMOTES_FILE,
        [r for r in saved if r != remote.strip()],
        "# Off-site destinations (rclone), saved by `synaptane offsite add`.",
    )
    return True


def rclone_remotes(settings: Settings) -> list[str]:
    """Names declared in the rclone configuration (`rclone config`)."""
    if shutil.which("rclone") is None:
        return []
    result = _rclone(settings, "listremotes", timeout=30)
    return [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]


# --- Preparation ------------------------------------------------------------------------------


def missing_tools() -> list[str]:
    return [tool for tool in ("age", "rclone") if shutil.which(tool) is None]


def has_recipients(settings: Settings) -> bool:
    return bool(recipients(settings) or settings.backup_age_recipients_file)


def require_ready(settings: Settings) -> None:
    """Refuses before starting: nothing is copied if a setting is missing."""
    if not remotes(settings):
        raise BackupError(
            "No off-server destination: set SYNAPTANE_BACKUP_REMOTES "
            '(for example "b2:my-bucket/synaptane"), see docs/admin/backup.md.'
        )
    if not has_recipients(settings):
        raise BackupError(
            "Off-server copy refused: no age recipient. The archive holds the master key in "
            "clear; run `synaptane offsite key`, or put your public key in "
            "SYNAPTANE_BACKUP_AGE_RECIPIENTS (see docs/admin/backup.md)."
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
    for recipient in recipients(settings):
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
    for remote in remotes(settings):
        command = ["copyto", "--", str(encrypted), target(remote, encrypted.name)]
        result = _rclone(settings, *command, timeout=COPY_TIMEOUT)
        if result.returncode != 0:
            outcomes.append(Outcome(remote, f"copy: {_tail(result.stderr)}"))
            continue
        outcomes.append(Outcome(remote, prune(settings, remote)))
    return outcomes


def copy(settings: Settings, archive: Path, scratch: Path) -> list[Outcome]:
    """Encrypts `archive` into `scratch`, then copies it to every destination. Raises
    BackupError if encryption fails (nothing was sent)."""
    encrypted = scratch / f"{archive.name}{SUFFIX}"
    try:
        encrypt(settings, archive, encrypted)
        return send(settings, encrypted)
    finally:
        encrypted.unlink(missing_ok=True)


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
    named = {m.group(1) for r in remotes(settings) if (m := NAMED_REMOTE.match(r))}
    if not named:
        return []
    return sorted(named - set(rclone_remotes(settings)))
