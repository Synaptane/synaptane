"""Configuration: a TOML file, overridden by the `SYNAPTANE_*` environment variables.

Example (`synaptane.toml`):

    public_url = "https://brain.example.com"
    database_url = "postgresql+psycopg://synaptane:secret@localhost:5432/synaptane"
    data_dir = "/var/lib/synaptane"
"""

import os
import re
import tomllib
from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

ENV_PREFIX = "SYNAPTANE_"
DEFAULT_CONFIG = Path("synaptane.toml")
METRICS_TOKEN_MIN = 32  # characters: a random token, not a word


class ConfigError(Exception):
    pass


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    public_url: str = "http://127.0.0.1:8000"
    database_url: str
    data_dir: Path
    session_days: int = 30
    session_idle_days: int = 7  # without a request, the web session ends; 0: never
    superadmin_2fa: bool = True  # super admins who sign in with a password need a second factor
    poll_minutes: int = 5  # fallback sync when no webhook arrives
    git_timeout: int = 300  # seconds: overall timeout of an exchange with the Git host
    allow_local_repos: bool = False  # file:// repositories: development and tests only
    ocr_languages: str = "fra+eng"  # Tesseract languages for scanned PDFs and images
    ocr_images: bool = True  # extract text from images for search
    allow_private_webhooks: bool = False  # webhooks to the local network: development only
    api_docs: bool = False  # publish /api/docs and the OpenAPI schema (lists every route)
    # Bearer token of /metrics (Prometheus); empty: /metrics does not exist.
    metrics_token: str = ""
    # Interface language when the browser asks for none that is supported, and initial
    # language of accounts created without a browser (CLI, administrator).
    default_locale: Literal["en", "fr"] = "en"
    # Off-site copy (`synaptane backup --offsite`, docs/admin/backup.md). Lists are written in
    # TOML (`["a", "b"]`) or, as an environment variable, separated by commas or newlines.
    backup_age_recipients: tuple[str, ...] = ()  # age public keys (`age1…`, `ssh-ed25519 …`)
    backup_age_recipients_file: Path | None = None  # or a recipients file (`age -R`)
    backup_remotes: tuple[str, ...] = ()  # rclone destinations: `remote:path`, or a folder
    backup_retention_days: int = 30  # older remote archives deleted; 0: never
    backup_rclone_config: Path | None = None  # default: $RCLONE_CONFIG, else <data_dir>/rclone/
    # Automatic backup by the worker (`services.backup_schedule`): every day at `backup_time`
    # (HH:MM, server time; "off": never) into `backup_dir` (/backups in the image; none: no
    # automatic backup), whose archives older than `backup_local_retention_days` are deleted.
    # A time that cannot be read falls back to 04:00, with a warning (doctor, logs): a typo
    # must not keep the application from starting.
    backup_dir: Path | None = None
    backup_time: str = "04:00"
    backup_local_retention_days: int = 7
    # Longest a database dump (or its read-back) may take, in minutes: a stuck pg_dump must
    # not hold the backup lock forever.
    backup_timeout_minutes: int = 120

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("public_url must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("metrics_token")
    @classmethod
    def _metrics_token(cls, value: str) -> str:
        value = value.strip()
        if value and len(value) < METRICS_TOKEN_MIN:
            raise ValueError(
                f"metrics_token: {METRICS_TOKEN_MIN} characters at least (openssl rand -hex 32)"
            )
        return value

    @field_validator("backup_age_recipients", "backup_remotes", mode="before")
    @classmethod
    def _list(cls, value: object) -> object:
        """A string (environment variable) becomes a list. Neither spaces nor ";" as
        separators: an SSH key contains spaces, and so does a Google Drive folder."""
        if isinstance(value, str):
            value = value.replace("\n", ",").split(",")
        if isinstance(value, list | tuple):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return value

    @field_validator("backup_age_recipients")
    @classmethod
    def _recipients(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for recipient in value:
            if not recipient.startswith(("age1", "ssh-ed25519 ", "ssh-rsa ")):
                raise ValueError("age public key expected (age1…, ssh-ed25519 … or ssh-rsa …)")
        return value

    @field_validator("backup_remotes")
    @classmethod
    def _remotes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for remote in value:
            check_remote(remote)
        return value

    @field_validator(
        "backup_age_recipients_file", "backup_rclone_config", "backup_dir", mode="before"
    )
    @classmethod
    def _optional_path(cls, value: object) -> object:
        return None if value == "" else value  # empty variable in compose.yaml

    @field_validator("backup_retention_days", "backup_local_retention_days")
    @classmethod
    def _retention(cls, value: int) -> int:
        if value < 0:
            raise ValueError("a number of days of zero or more is expected")
        return value

    @field_validator("backup_time")
    @classmethod
    def _backup_time(cls, value: str) -> str:
        value = value.strip().lower()
        if value in ("", "off"):
            return "off"
        found = parse_time(value)
        return found.strftime("%H:%M") if found else value  # kept as written: warned about

    @field_validator("backup_timeout_minutes")
    @classmethod
    def _timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError("a number of minutes of 1 or more is expected")
        return value

    @property
    def rclone_config(self) -> Path:
        """rclone configuration (destinations and their credentials), outside the database."""
        if self.backup_rclone_config:
            return self.backup_rclone_config
        if os.environ.get("RCLONE_CONFIG"):
            return Path(os.environ["RCLONE_CONFIG"])
        return self.data_dir / "rclone" / "rclone.conf"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def key_file(self) -> Path:
        """Master key encrypting the secrets (32 bytes, to back up outside the database)."""
        return self.data_dir / "master.key"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"


def check_remote(remote: str) -> None:
    """Refuses an rclone option disguised as a destination, and connection parameters
    (`:s3,secret_access_key=…:`): credentials stay in the rclone configuration."""
    if not remote or remote.startswith("-") or "\0" in remote or "\n" in remote:
        raise ValueError("invalid rclone destination")
    if ":" in remote:
        backend = remote.split(":", 2)[1] if remote.startswith(":") else remote.split(":")[0]
        if "," in backend or "=" in backend:
            raise ValueError("connection parameters not allowed: use rclone config")


_TIME = re.compile(r"^(\d{1,2})(?:[:h](\d{2})?(?::(\d{2}))?)?$")


def parse_time(value: str) -> time | None:
    """A time of the day, as people write it: `04:00`, `4:00`, `04h00`, `4h`, `04:00:00`
    (24 hours; seconds ignored). None if it is not one."""
    found = _TIME.match(value.strip().lower())
    if found is None or (found.group(3) and found.group(2) is None):
        return None
    hours, minutes = int(found.group(1)), int(found.group(2) or 0)
    if hours > 23 or minutes > 59 or int(found.group(3) or 0) > 59:
        return None
    if ":" not in value and "h" not in value.lower():
        return None  # "4" alone: not a time
    return time(hours, minutes)


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get(ENV_PREFIX + "CONFIG", DEFAULT_CONFIG))
    data: dict[str, object] = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"Unreadable configuration ({path}): {error}") from None
    for field in Settings.model_fields:
        env = os.environ.get(ENV_PREFIX + field.upper())
        if env is not None:
            data[field] = env
    data_dir = data.get("data_dir")
    if isinstance(data_dir, str) and not Path(data_dir).is_absolute():
        data["data_dir"] = (path.parent / data_dir).resolve()
    try:
        return Settings.model_validate(data)
    except ValidationError as error:
        # No values in the message: database_url can contain a password.
        fields = ", ".join(".".join(map(str, e["loc"])) for e in error.errors())
        raise ConfigError(f"Invalid or incomplete configuration: {fields}") from None
