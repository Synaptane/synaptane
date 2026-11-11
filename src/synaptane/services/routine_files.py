"""Agent routines described by files of the space's repository.

Each routine is a Markdown file `.synaptane/routines/<name>.md`, versioned like the purpose
(`vocation.md`) and the tidy-up rules (`prompts/tidy.md`): a header between two `---` lines,
then the instructions.

    ---
    title: Consistency check
    description: Contradictions and outdated pages, every week
    schedule: weekly monday 06:00
    scope: infra, clients/acme
    report: reports/consistency.md
    inbox: true
    email: admins
    webhook: false
    enabled: true
    template: coherence
    memory: true
    ---
    Read the pages changed since the last run…

A routine can also be part of the night of its space (`services.night`): `schedule: night`
(every night), `night monday` (Monday nights), `night 1` (the night of the 1st), in French
`nuit`, `nuit lundi`, `nuit 1`; with `night: before` or `night: after` (the default), it runs
at the tidy-up's time, just before or after it, and the night's report tells what it did.

The name of a routine is the name of its file (lowercase letters, digits and dashes, like the
team prompts). The header keeps the conventions of the inbox items (a JSON value, or else
plain text) and adds lists: `scope` (and `writes`) take folders separated by commas, a JSON
list, or one `- folder` line each. A field this version does not know (a newer version's) is
reported, kept when Synaptane rewrites the file, and never prevents the reading: the dedicated
reader exists for that, and because a line the inbox reader skips silently would here widen
what an agent reads (a misread `scope` is the whole space).

Git is the source of truth. The `routines` table keeps a copy of each file's settings, kept in
step at each synchronization (`synchronize`, under the space lock), and the state of the runs
(last day requested, last run, waiting request): a changed file never resets that state, so
it never makes a routine run again. A removed file removes the routine; its runs stay in the
log and in the month's budget. An invalid file is not run; its error is shown in Settings →
Routines and in the REST API.

Routines written before this format lived in the database only. A row without a file
(`Routine.sha` is None) is written to the repository by the next synchronization, in one
commit in Synaptane's name: the routines of earlier versions, and those created before the
space's first synchronization (like the purpose kept pending, `vocation.apply_pending`). Its
state stays on the same row: it neither runs twice nor gets lost.

Trust: whoever can push to the repository can change the routines, as they can change the
tidy-up rules. From Synaptane, only the organization's administrators write them; every other
write path is confined to `content/` and the inbox.

Memory: with `memory: true`, the routine keeps notes from one run to the next in
`.synaptane/memory/<name>.md` (`services.routine_memory`), a file of its own so that the
routine's file is never rewritten by a run. The flag lives in the file only (the database
keeps no copy of it): the run reads it at the commit it works on. The memory follows the
routine's name: removed with the routine from Synaptane, and by the next synchronization when a
push removed or renamed the routine without moving its memory (`_drop_orphan_memories`).

Folders a routine keeps (`writes`): the pages it may write itself, besides its outputs
(`services.routine_writes`). The database keeps no copy of them (no column for them): they are
read from the file at the hash the database knows (`writes_of`), and a copy made from the
database (`Config.of`) leaves them unknown (None), so that a save keeps the file's.
"""

import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core import names
from synaptane.core.errors import Label, SynaptaneError, conflict, invalid, not_found
from synaptane.core.secret_scan import added_findings, refuse_secrets
from synaptane.git.repo import GitError, Identity, PathRefused, Repository
from synaptane.services import Actor, jobs
from synaptane.services.content import content_path
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.tidy import parse_time, zone
from synaptane.services.workspace import Workspace, author_identity, trailers
from synaptane.storage.db import Database, try_space_lock
from synaptane.storage.models import Routine, Space

ROUTINES_DIR = ".synaptane/routines"
MEMORY_DIR = ".synaptane/memory"  # one file per routine with `memory: true`
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")  # as the team prompts
FREQUENCIES = ("daily", "weekly", "monthly")
EMAIL_TARGETS = ("", "admins", "members")
NIGHT_PLACES = ("", "before", "after")  # "": at its own time
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MAX_INSTRUCTIONS = 20_000
MAX_SCOPE = 20  # folders
# Folders a routine keeps (`writes`): a few topics of its own (a watch, digests, a tracking
# table), never a share of the wiki large enough to make it a second tidy-up.
MAX_WRITES = 5
MAX_ROUTINES = 20  # per space: each routine costs, proliferation is paid for
MAX_DESCRIPTION = 500
MAX_FILE_SIZE = 64_000  # bytes: instructions, header and a margin
FILES_READ = 2 * MAX_ROUTINES  # files beyond are not even shown (log only)
MAX_WARNINGS = 10  # per file: enough to fix it, never one per line of a large file
# Fields of the header. Any other one is a newer version's: reported, kept, ignored.
KNOWN = (
    "title",
    "description",
    "schedule",
    "scope",
    "writes",
    "report",
    "inbox",
    "email",
    "webhook",
    "enabled",
    "template",
    "memory",
    "night",
)
# Settings that live in the file only, never copied to the `routines` table.
FILE_ONLY = ("memory", "writes")
_KEY = re.compile(r"[A-Za-z_][\w-]*")
_TIME = re.compile(r"\d{1,2}:\d{2}")
_TRUE, _FALSE = ("true", "yes", "on"), ("false", "no", "off")

log = logging.getLogger("synaptane.routines")


# --- Settings and their rules ----------------------------------------------------------------


@dataclass
class Fields:
    """Settings of a routine; None: unchanged (partial update)."""

    title: str | None = None
    description: str | None = None
    instructions: str | None = None
    frequency: str | None = None
    weekday: int | None = None
    monthday: int | None = None
    at: str | None = None
    scope: list[str] | None = None
    output_page: str | None = None
    output_inbox: bool | None = None
    output_email: str | None = None
    output_webhook: bool | None = None
    enabled: bool | None = None
    memory: bool | None = None
    writes: list[str] | None = None  # folders the routine keeps; []: none
    night: str | None = None  # "": at `at`; "before" or "after" the tidy-up, in the night


@dataclass
class Config:
    """What a routine file says, valid (see `apply`). Same attribute names as `Routine`,
    which keeps a copy of it."""

    title: str = ""
    description: str = ""
    instructions: str = ""
    frequency: str = "weekly"
    weekday: int | None = 0
    monthday: int | None = None
    at: str = "06:00"
    scope: list[str] = field(default_factory=list)
    output_page: str = ""
    output_inbox: bool = False
    output_email: str = ""
    output_webhook: bool = False
    enabled: bool = True
    template: str = ""
    # In the file only (`FILE_ONLY`). None: not known (a copy of the database), and a rewrite
    # keeps what the file says (`save`).
    memory: bool | None = None
    # Folders the routine keeps (`writes`, in the file only too). None: unknown, a copy made
    # from the database, which does not keep them; `save` then keeps the file's.
    writes: list[str] | None = field(default_factory=list)
    night: str = ""

    @classmethod
    def of(cls, routine: Routine) -> "Config":
        """The settings the database keeps (`memory` and `writes` unknown: see `memory_of`
        and `writes_of`)."""
        return cls(
            routine.title,
            routine.description,
            routine.instructions,
            routine.frequency,
            routine.weekday,
            routine.monthday,
            routine.at,
            list(routine.scope or []),
            routine.output_page,
            routine.output_inbox,
            routine.output_email,
            routine.output_webhook,
            routine.enabled,
            routine.template,
            writes=None,
            night=routine.night,
        )

    def store(self, routine: Routine) -> None:
        """Copies the settings onto the database row (the state of the runs stays)."""
        for name in self.__dataclass_fields__:
            if name in FILE_ONLY:
                continue
            value = getattr(self, name)
            setattr(routine, name, list(value) if isinstance(value, list) else value)

    @property
    def plan(self) -> tuple[bool, str, int | None, int | None, str, str]:
        """What decides when it runs."""
        return (self.enabled, self.frequency, self.weekday, self.monthday, self.at, self.night)


def clean_folder(folder: str) -> str:
    """Folder of the scope, relative to content/ ("infra/servers")."""
    full = content_path(folder.removeprefix("content/"))
    if full == CONTENT_DIR:
        return ""
    return full[len(CONTENT_DIR) + 1 :]


def clean_scope(folders: list[str]) -> list[str]:
    cleaned = [f for f in dict.fromkeys(clean_folder(f) for f in folders if f.strip()) if f]
    if len(cleaned) > MAX_SCOPE:
        raise invalid("Scope: {n} folders at most.", n=MAX_SCOPE)
    return cleaned


def clean_writes(folders: list[str]) -> list[str]:
    """Folders a routine keeps, relative to content/: never the whole wiki, a hidden folder
    (`.synaptane/`, `.git/`), a path going up (`..`) or an absolute one. A folder inside
    another one given is dropped: the other one already covers it."""
    cleaned: list[str] = []
    for raw in folders:
        text = raw.strip()
        if not text:
            continue
        shown = text[:80]
        if text.startswith(("/", "\\")) or re.match(r"[A-Za-z]:[\\/]", text):
            raise invalid("writes: {folder} is an absolute path.", folder=shown)
        relative = text.replace("\\", "/").removeprefix("./")
        relative = "" if relative.strip("/") == "content" else relative.removeprefix("content/")
        parts = [part for part in relative.split("/") if part]
        if ".." in parts:
            raise invalid("writes: {folder} goes up out of the wiki (..).", folder=shown)
        if any(part.startswith(".") for part in parts if part != "."):
            raise invalid("writes: {folder} is a hidden folder (.synaptane, .git…).", folder=shown)
        if relative.lower().endswith(".md"):
            raise invalid("writes: {folder} is a page; give a folder.", folder=shown)
        folder = clean_folder(relative) if parts and parts != ["."] else ""
        if not folder:
            raise invalid(
                "writes: a routine cannot write in the whole wiki; give its own folders "
                "(e.g. digests)."
            )
        if folder not in cleaned:
            cleaned.append(folder)
    kept = [f for f in cleaned if not any(f.startswith(o + "/") for o in cleaned)]
    if len(kept) > MAX_WRITES:
        raise invalid("writes: {n} folders at most.", n=MAX_WRITES)
    return kept


def clean_page(page: str) -> str:
    """Output page, relative to content/; "": no page."""
    page = page.strip().removeprefix("content/").strip("/")
    if not page:
        return ""
    if not page.lower().endswith(".md"):
        page += ".md"
    full = content_path(page)
    if full == CONTENT_DIR:
        return ""
    return full[len(CONTENT_DIR) + 1 :]


def apply(config: Config, fields: Fields) -> None:
    """Applies `fields` to `config`, then checks the whole: the single set of rules, for the
    forms, the REST API and the files of the repository."""
    if fields.title is not None:
        title = " ".join(fields.title.split())
        if not title or len(title) > 120:
            raise invalid("Title: between 1 and 120 characters.")
        config.title = title
    if fields.description is not None:
        description = " ".join(fields.description.split())
        if len(description) > MAX_DESCRIPTION:
            raise invalid("Description: {n} characters at most.", n=MAX_DESCRIPTION)
        config.description = description
    if fields.instructions is not None:
        text = fields.instructions.strip()
        if not text:
            raise invalid("Instructions: say what the routine must do.")
        if len(text) > MAX_INSTRUCTIONS:
            raise invalid("Instructions too long (20,000 characters at most).")
        config.instructions = text
    if fields.frequency is not None:
        if fields.frequency not in FREQUENCIES:
            raise invalid("Unknown frequency (daily, weekly or monthly).")
        config.frequency = fields.frequency
    if fields.weekday is not None:
        config.weekday = fields.weekday
    if fields.monthday is not None:
        config.monthday = fields.monthday
    if fields.at is not None:
        config.at = parse_time(fields.at).strftime("%H:%M")
    if fields.scope is not None:
        config.scope = clean_scope(fields.scope)
    if fields.output_page is not None:
        config.output_page = clean_page(fields.output_page)
    if fields.output_inbox is not None:
        config.output_inbox = fields.output_inbox
    if fields.output_email is not None:
        if fields.output_email not in EMAIL_TARGETS:
            raise invalid("Unknown email recipients.")
        config.output_email = fields.output_email
    if fields.output_webhook is not None:
        config.output_webhook = fields.output_webhook
    if fields.enabled is not None:
        config.enabled = fields.enabled
    if fields.memory is not None:
        config.memory = fields.memory
    if fields.writes is not None:
        config.writes = clean_writes(fields.writes)
    if fields.night is not None:
        if fields.night not in NIGHT_PLACES:
            raise invalid("Unknown place in the night (before or after the tidy-up).")
        config.night = fields.night
    # Consistency of the whole, once everything is applied.
    if config.frequency == "weekly":
        if config.weekday is None or not 0 <= config.weekday <= 6:
            raise invalid("Choose the day of the week.")
        config.monthday = None
    elif config.frequency == "monthly":
        if config.monthday is None or not 1 <= config.monthday <= 28:
            raise invalid("Day of the month: from 1 to 28.")
        config.weekday = None
    else:
        config.weekday = config.monthday = None
    # Pages kept in its folders are an output too. Unknown folders (a copy made from the
    # database) are checked again by `save`, once read from the file.
    if not (
        config.output_page
        or config.output_inbox
        or config.output_email
        or config.output_webhook
        or config.writes is None
        or config.writes
    ):
        raise invalid("Choose at least one output: page, inbox, email or webhook.")


def skip_today(routine: Routine, space: Space, now: datetime | None = None) -> None:
    """A routine created, re-enabled or rescheduled after its time of the day does not start
    right away (and costs nothing unless asked): "Run now" does that."""
    try:
        local = (now or datetime.now(UTC)).astimezone(zone(space.tidy_timezone))
        at = parse_time(space.tidy_time if routine.night else routine.at)  # a night: its time
    except SynaptaneError:
        return
    if local.time() >= at:
        routine.last_date = local.date()


# --- Names --------------------------------------------------------------------------------------


def check_name(name: str) -> str:
    """Name of a routine file: lowercase letters, digits and dashes, 40 characters at most."""
    name = name.strip().lower().removesuffix(".md")
    if not NAME.fullmatch(name):
        raise invalid(
            "File name: lowercase letters, digits and dashes, 40 characters at most "
            "(e.g. weekly-digest)."
        )
    return name


def name_for(title: str) -> str:
    """File name suggested by a title: "Résumé hebdomadaire" → "resume-hebdomadaire"."""
    slug = re.sub(r"[^a-z0-9]+", "-", names.ascii_text(title)).strip("-")
    return slug[:40].strip("-") or "routine"


def free_name(name: str, taken: Iterable[str]) -> str:
    """`name`, or `name-2`, `name-3`… if it is taken."""
    used = set(taken)
    if name not in used:
        return name
    for number in range(2, 1000):
        suffix = f"-{number}"
        candidate = name[: 40 - len(suffix)].strip("-") + suffix
        if candidate not in used:
            return candidate
    raise conflict("A routine with this name already exists in the space.")


def file_path(name: str) -> str:
    return f"{ROUTINES_DIR}/{name}.md"


def memory_path(name: str) -> str:
    """The memory of the routine `name` (`services.routine_memory`)."""
    return f"{MEMORY_DIR}/{name}.md"


# --- Writing a file -----------------------------------------------------------------------------


def schedule_text(config: Config) -> str:
    """`daily 02:30`, `weekly monday 06:00`, `monthly 1 06:00`; in the night: `night`,
    `night monday`, `night 1`."""
    if config.night:
        if config.frequency == "weekly":
            return f"night {WEEKDAYS[config.weekday or 0]}"
        if config.frequency == "monthly":
            return f"night {config.monthday}"
        return "night"
    if config.frequency == "weekly":
        return f"weekly {WEEKDAYS[config.weekday or 0]} {config.at}"
    if config.frequency == "monthly":
        return f"monthly {config.monthday} {config.at}"
    return f"daily {config.at}"


def _scalar(value: str) -> str:
    """Plain text when it reads back unchanged, JSON (valid YAML) otherwise."""
    plain = (
        value
        and value == value.strip()
        and value[0] not in "\"'[{#-"
        and value[-1] not in "\"'"
        and " #" not in value
    )
    if plain:
        try:
            json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, ensure_ascii=False)


def _list(values: list[str]) -> str:
    if all(_scalar(v) == v and "," not in v for v in values):
        return ", ".join(values)
    return json.dumps(values, ensure_ascii=False)


def render(config: Config, extra: Iterable[str] = ()) -> str:
    """The file of a routine. `extra`: lines of fields this version does not know, kept as
    they are (written by a newer version, or by hand)."""
    lines = ["---", f"title: {_scalar(config.title)}"]
    if config.description:
        lines.append(f"description: {_scalar(config.description)}")
    lines.append(f"schedule: {schedule_text(config)}")
    if config.night:
        lines.append(f"night: {config.night}")
    if config.scope:
        lines.append(f"scope: {_list(config.scope)}")
    if config.writes:
        lines.append(f"writes: {_list(config.writes)}")
    if config.output_page:
        lines.append(f"report: {_scalar(config.output_page)}")
    lines.append(f"inbox: {json.dumps(config.output_inbox)}")
    if config.output_email:
        lines.append(f"email: {config.output_email}")
    lines.append(f"webhook: {json.dumps(config.output_webhook)}")
    lines.append(f"enabled: {json.dumps(config.enabled)}")
    if config.template:
        lines.append(f"template: {config.template}")
    if config.memory:
        lines.append("memory: true")
    lines.extend(extra)
    lines.append("---")
    return "\n".join(lines) + "\n\n" + config.instructions.strip() + "\n"


# --- Reading a file -----------------------------------------------------------------------------


@dataclass
class Parsed:
    """A routine file read: its settings (None: invalid, see `error`), what is worth saying
    about it, and the lines of the fields this version does not know."""

    config: Config | None
    error: Label | None = None
    warnings: list[Label] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


@dataclass
class _Header:
    values: dict[str, object] = field(default_factory=dict)
    extra: list[str] = field(default_factory=list)
    warnings: list[Label] = field(default_factory=list)
    body: str = ""


def _value(raw: str) -> object:
    """A JSON value, or else plain text (as the inbox items)."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip("'\"")


def _header(text: str) -> _Header:
    text = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        raise invalid("The file must start with a header between two lines of three dashes (---).")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        raise invalid("Header not closed: add a line of three dashes (---) after it.")
    found = _Header(body="\n".join(lines[end + 1 :]))
    current: str | None = None  # last field read: list items and indented lines belong to it
    for number, line in enumerate(lines[1:end], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        item = stripped == "-" or stripped.startswith(("- ", "-\t"))
        if current is not None and current not in KNOWN and (item or line[:1] in " \t"):
            found.extra.append(line)  # inside a field of a newer version: kept as it is
            continue
        if item and current is not None and isinstance(found.values.get(current), list):
            target = found.values[current]
            assert isinstance(target, list)
            target.append(_value(stripped[1:].strip()))
            continue
        key, sep, raw = line.partition(":")
        key = key.strip()
        if not sep or line[:1] in " \t" or not _KEY.fullmatch(key):
            raise invalid(
                "Line {line} of the header is not understood: {text}",
                line=number,
                text=stripped[:80],
            )
        current, raw = key, raw.strip()
        if key not in KNOWN:
            found.warnings.append(Label("Unknown field, ignored: {field}.", field=key[:40]))
            found.extra.append(line)
            continue
        if key in found.values:
            found.warnings.append(
                Label("Field given twice, the last value is used: {field}.", field=key)
            )
        # An empty value may be followed by a list, one "- item" per line.
        found.values[key] = [] if raw == "" else _value(raw)
    return found


def _text(header: _Header, key: str) -> str:
    value = header.values.get(key)
    if value is None or value == []:
        return ""
    if isinstance(value, list | dict):
        raise invalid("{field}: a single value is expected.", field=key)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _flag(header: _Header, key: str, default: bool) -> bool:
    value = header.values.get(key)
    if value is None or value == [] or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
        return value.strip().lower() in _TRUE
    raise invalid("{field}: true or false.", field=key)


def _folders(header: _Header, key: str = "scope") -> list[str]:
    value = header.values.get(key)
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    if any(isinstance(v, list | dict) for v in items):
        raise invalid("{field}: a list of folders is expected.", field=key)
    return [str(v).strip() for v in items if str(v).strip()]


# French spellings, read as well (Synaptane always writes the English one back).
_FREQUENCY_WORDS = {
    "daily": "daily",
    "quotidien": "daily",
    "weekly": "weekly",
    "hebdo": "weekly",
    "hebdomadaire": "weekly",
    "monthly": "monthly",
    "mensuel": "monthly",
}
_JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def _weekday(word: str) -> int | None:
    for index, spellings in enumerate(zip(WEEKDAYS, _JOURS, strict=True)):
        if any(word in (day, day[:3]) for day in spellings):
            return index
    return None


def parse_schedule(text: str) -> tuple[str, int | None, int | None, str]:
    """`daily 02:30`, `weekly monday 06:00` (or `mon`), `monthly 1 06:00` → frequency, day of
    the week (0: Monday), day of the month, time. French is read too: `quotidien` or
    `chaque jour`, `hebdo` or `hebdomadaire` with `lundi` (or `lun`)…, `mensuel`."""
    words = text.lower().split()
    if words[:2] == ["chaque", "jour"]:
        words = ["daily", *words[2:]]
    wrong = invalid(
        "schedule: “{value}” is not understood. Examples: daily 02:30, weekly monday 06:00, "
        "monthly 1 06:00, night, night monday.",
        value=text[:60],
    )
    if len(words) < 2 or not _TIME.fullmatch(words[-1]):
        raise wrong
    at = parse_time(words[-1]).strftime("%H:%M")
    frequency, rest = _FREQUENCY_WORDS.get(words[0]), words[1:-1]
    if frequency == "daily" and not rest:
        return "daily", None, None, at
    if frequency == "weekly" and len(rest) == 1 and (day := _weekday(rest[0])) is not None:
        return "weekly", day, None, at
    if frequency == "monthly" and len(rest) == 1 and rest[0].isdigit():
        if not 1 <= int(rest[0]) <= 28:
            raise invalid("Day of the month: from 1 to 28.")
        return "monthly", None, int(rest[0]), at
    raise wrong


# The night (`services.night`): `night`, `night monday`, `night 1`; `nuit`, `chaque nuit`,
# `nuit lundi`, `nuit 1`. Its place: `night: before` or `after` (`avant`, `après`).
_NIGHT_WORDS = ("night", "nuit")
_PLACES = {
    "before": "before",
    "avant": "before",
    "after": "after",
    "après": "after",
    "apres": "after",
}


def night_schedule(text: str) -> tuple[str, int | None, int | None] | None:
    """`night`, `night monday` (or `mon`, `nuit lundi`), `night 1` → frequency, day of the
    week, day of the month. None: not a night schedule (see `parse_schedule`)."""
    words = text.lower().split()
    if words[:2] == ["chaque", "nuit"]:
        words = ["night", *words[2:]]
    if not words or words[0] not in _NIGHT_WORDS:
        return None
    rest = words[1:]
    if not rest:
        return "daily", None, None
    if len(rest) == 1 and (day := _weekday(rest[0])) is not None:
        return "weekly", day, None
    if len(rest) == 1 and rest[0].isdigit():
        if not 1 <= int(rest[0]) <= 28:
            raise invalid("Day of the month: from 1 to 28.")
        return "monthly", None, int(rest[0])
    raise invalid(
        "schedule: “{value}” is not understood. Examples: daily 02:30, weekly monday 06:00, "
        "monthly 1 06:00, night, night monday.",
        value=text[:60],
    )


def _place(header: _Header) -> str:
    """`night: before` or `after` (the default)."""
    value = _text(header, "night").lower()
    if not value:
        return "after"
    if value not in _PLACES:
        raise invalid("night: before or after (the tidy-up).")
    return _PLACES[value]


def _config(header: _Header, name: str) -> Config:
    from synaptane.services.routines import TEMPLATES  # the templates' module reads this one

    schedule = _text(header, "schedule")
    if not schedule:
        raise invalid("schedule: missing (e.g. weekly monday 06:00).")
    in_night = night_schedule(schedule)
    if in_night is not None:
        (frequency, weekday, monthday), at = in_night, None
        night = _place(header)
    else:
        frequency, weekday, monthday, fixed = parse_schedule(schedule)
        at, night = fixed, ""
        if _text(header, "night"):
            header.warnings.append(
                Label(
                    "night: ignored, the schedule is not in the night ({value}).",
                    value=schedule[:60],
                )
            )
    template = _text(header, "template")
    if template and template not in TEMPLATES:
        header.warnings.append(Label("Unknown template, ignored: {field}.", field=template[:40]))
        template = ""
    email = _text(header, "email").lower()
    config = Config(template=template)
    apply(
        config,
        Fields(
            title=_text(header, "title") or name.replace("-", " ").capitalize(),
            description=_text(header, "description"),
            instructions=header.body,
            frequency=frequency,
            weekday=weekday,
            monthday=monthday,
            at=at,
            scope=_folders(header),
            output_page=_text(header, "report"),
            output_inbox=_flag(header, "inbox", False),
            output_email="" if email == "none" else email,
            output_webhook=_flag(header, "webhook", False),
            enabled=_flag(header, "enabled", True),
            memory=_flag(header, "memory", False),
            writes=_folders(header, "writes"),
            night=night,
        ),
    )
    return config


def parse(data: bytes, name: str) -> Parsed:
    """Reads a routine file. Never raises: an invalid file comes back with its error."""
    if len(data) > MAX_FILE_SIZE:
        return Parsed(None, Label("File too large: {n} bytes at most.", n=MAX_FILE_SIZE))
    try:
        header = _header(data.decode("utf-8", "replace"))
        config = _config(header, name)
    except SynaptaneError as error:
        return Parsed(None, Label(error.template, **error.params))
    return Parsed(config, None, header.warnings[:MAX_WARNINGS], header.extra)


def extra_lines(data: bytes | None) -> list[str]:
    """Lines of the unknown fields of a file, to keep them on a rewrite ([] if unreadable)."""
    if not data:
        return []
    try:
        return _header(data.decode("utf-8", "replace")).extra
    except SynaptaneError:
        return []


def memory_of(data: bytes | None) -> bool:
    """Does a routine file ask for a memory (`memory: true`)? Read from its header alone, so
    that an edit of a file invalid for another reason keeps it; False for a missing file, an
    unreadable header or a value that is not a boolean. (An invalid file does not run: its
    memory is neither read nor written.)"""
    if not data or len(data) > MAX_FILE_SIZE:
        return False
    try:
        return _flag(_header(data.decode("utf-8", "replace")), "memory", False)
    except SynaptaneError:
        return False


@dataclass(frozen=True)
class File:
    name: str  # without `.md`
    path: str
    sha: str
    size: int
    data: bytes


def read_files(repo: Repository, rev: str = "HEAD") -> list[File]:
    """Routine files at commit `rev`, by name (the `.md` files directly in the folder)."""
    blobs = sorted(
        (
            b
            for b in repo.files(ROUTINES_DIR, rev)
            if b.path.endswith(".md") and PurePosixPath(b.path).parent.as_posix() == ROUTINES_DIR
        ),
        key=lambda b: b.path,
    )
    if len(blobs) > FILES_READ:
        log.warning("%s: %d routine files, the last %d ignored", repo.path, len(blobs), FILES_READ)
        blobs = blobs[:FILES_READ]
    data = repo.read_many([b.path for b in blobs if b.size <= MAX_FILE_SIZE], rev)
    return [
        File(PurePosixPath(b.path).stem, b.path, b.sha, b.size, data.get(b.path, b""))
        for b in blobs
    ]


def _read(file: File, index: int) -> Parsed:
    if not NAME.fullmatch(file.name):
        return Parsed(
            None,
            Label(
                "Invalid file name: lowercase letters, digits and dashes, 40 characters at "
                "most. Rename the file."
            ),
        )
    if index >= MAX_ROUTINES:
        return Parsed(
            None, Label("More than {n} routines in the space: this one is not run.", n=MAX_ROUTINES)
        )
    if file.size > MAX_FILE_SIZE:
        return Parsed(None, Label("File too large: {n} bytes at most.", n=MAX_FILE_SIZE))
    return parse(file.data, file.name)


# --- The database in step with the repository ---------------------------------------------------


def reconcile(
    session: Session, space: Space, repo: Repository, now: datetime | None = None
) -> None:
    """Brings the routines of the space in step with the files at the repository's HEAD.

    A new file creates its routine (which does not start before its next time); a changed file
    updates the settings, never the state of the runs; a removed file removes the routine (its
    runs stay, `routine_id` set to NULL). A row without a file yet (`sha` None) waits for the
    next synchronization and is left alone. Called under the space lock."""
    rows = {
        r.name: r
        for r in session.scalars(
            select(Routine)
            .where(Routine.space_id == space.id)
            .with_for_update()
            .execution_options(populate_existing=True)  # read again, never a stale copy
        )
    }
    seen: set[str] = set()
    now = now or datetime.now(UTC)
    for index, file in enumerate(f for f in read_files(repo) if len(f.name) <= 120):
        seen.add(file.name)
        row = rows.get(file.name)
        if row is not None and row.sha is None and file.data != render(Config.of(row)).encode():
            continue  # a routine still to write has this name: `_write_pending` settles it
        parsed = _read(file, index)
        error = parsed.error.dumps() if parsed.error else None
        warnings = [w.dumps() for w in parsed.warnings]
        if (
            row is not None
            and row.sha == file.sha
            and row.error == error
            and list(row.warnings or []) == warnings
        ):
            continue  # unchanged
        new = row is None
        if row is None:
            row = Routine(space_id=space.id, name=file.name, last_date=None)
            Config(title=file.name.replace("-", " ").capitalize()).store(row)
            session.add(row)
        before, was_invalid = Config.of(row).plan, row.error is not None
        if parsed.config is not None:
            parsed.config.store(row)
        row.sha, row.error, row.warnings = file.sha, error, warnings
        row.updated_at = now
        if parsed.config is not None and (
            new or was_invalid or (parsed.config.plan != before and row.enabled)
        ):
            skip_today(row, space, now)
        if not new and parsed.config is not None and parsed.config.plan != before:
            log.info("Space %s: routine %s rescheduled by its file", space.id, file.name)
    for name, row in rows.items():
        if name not in seen and row.sha is not None:
            log.info("Space %s: routine %s removed (file removed)", space.id, name)
            session.delete(row)
    session.flush()


def _write_pending(session: Session, space: Space, repo: Repository, committer: Identity) -> bool:
    """Writes to the repository the routines that only exist in the database (`sha` None):
    one commit in Synaptane's name. A file already there with the same text is adopted (an
    attempt interrupted after its commit); a file of the same name with another text keeps its
    name, and the routine of the database takes a free one: Git wins, nothing is lost.
    Returns True if committed."""
    rows = list(
        session.scalars(
            select(Routine)
            .where(Routine.space_id == space.id, Routine.sha.is_(None))
            .order_by(Routine.created_at, Routine.name)
            .with_for_update()
        )
    )
    if not rows:
        return False
    files = {f.name: f.data for f in read_files(repo)}
    known = set(  # routines read from a file: their names are theirs
        session.scalars(
            select(Routine.name).where(Routine.space_id == space.id, Routine.sha.is_not(None))
        )
    )
    waiting = {r.name for r in rows}
    claimed: set[str] = set()
    changes: dict[str, bytes | None] = {}
    for row in rows:
        data = render(Config.of(row)).encode()
        busy = known | claimed | (waiting - {row.name})
        same = [n for n, d in files.items() if d == data and n not in busy]
        if same:
            name = row.name if row.name in same else same[0]
        else:
            wanted = row.name if NAME.fullmatch(row.name) else name_for(row.name)
            name = free_name(wanted, busy | set(files))
            changes[file_path(name)] = data
        if name != row.name:
            log.warning("Space %s: routine %r written as %s.md", space.id, row.name, name)
            row.name = name
        claimed.add(name)
    session.flush()
    if not changes:
        return False
    subject = "Synaptane: routines written to the repository\n\n" + "\n".join(
        f"- {path}" for path in changes
    )
    return repo.commit_changes(changes, committer, subject) is not None


def synchronize(database: Database, repo: Repository, space_id: UUID, committer: Identity) -> bool:
    """After a synchronization's integration, under the space lock (`git_sync`): writes the
    routines waiting in the database, then brings the database in step with the files.
    Returns True if a commit was created (to push).

    A failure here never stops the synchronization of the pages: it is logged, the routines
    keep waiting, and the next synchronization tries again."""
    try:
        with database.session() as session:
            space = session.get(Space, space_id)
            if space is None:
                return False
            created = _write_pending(session, space, repo, committer)
            reconcile(session, space, repo)
    except Exception:  # noqa: BLE001 — the pages' synchronization goes on
        log.exception("Space %s: routines not brought in step with the repository", space_id)
        return False
    # Apart, once the routines are saved: a memory that cannot be removed must not undo them.
    try:
        dropped = _drop_orphan_memories(repo, committer)
    except Exception:  # noqa: BLE001 — the next synchronization tries again
        log.exception("Space %s: memories of removed routines not deleted", space_id)
        dropped = False
    return created or dropped


def _drop_orphan_memories(repo: Repository, committer: Identity) -> bool:
    """Removes the memories whose routine file is gone (removed or renamed by a push that did
    not move the memory): one commit in Synaptane's name. A routine created later under the
    same name never inherits notes written for another one; the history keeps them.
    Only files named as a routine can be (`NAME`): any other one is no memory, never read.
    Returns True if committed."""
    memories = [
        b.path
        for b in repo.files(MEMORY_DIR)
        if b.path.endswith(".md")
        and PurePosixPath(b.path).parent.as_posix() == MEMORY_DIR
        and NAME.fullmatch(PurePosixPath(b.path).stem)
    ]
    if not memories:
        return False
    names = {
        PurePosixPath(b.path).stem
        for b in repo.files(ROUTINES_DIR)
        if b.path.endswith(".md") and PurePosixPath(b.path).parent.as_posix() == ROUTINES_DIR
    }
    orphans = sorted(p for p in memories if PurePosixPath(p).stem not in names)
    if not orphans:
        return False
    subject = "Synaptane: memories of removed routines deleted\n\n" + "\n".join(
        f"- {path}" for path in orphans
    )
    return repo.commit_changes(dict.fromkeys(orphans), committer, subject) is not None


# --- Writing from Synaptane (administrators) ---------------------------------------------------


def _synced(ws: Workspace, space: Space) -> Repository | None:
    try:
        return ws.local_repository(space)
    except SynaptaneError:
        return None  # not synced yet


def save(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    name: str,
    config: Config | None,
    *,
    creating: bool = False,
    base_sha: str | None = None,
    allow_secrets: bool = False,
    source: str = "web",
) -> Routine | None:
    """Writes the routine file `name` (`config`), or removes it (None): a commit in the
    person's name, under the space lock, then the database in step and a synchronization
    requested. Rights are the caller's (organization administrators).

    `creating`: the name must be free. `base_sha`: the file's hash read before editing;
    conflict if it changed in the meantime (a push, another administrator). A space not
    synced yet keeps the routine in the database, written by its first synchronization.

    Returns the routine as saved (None once removed)."""
    path = file_path(name)
    if config is not None:
        _fits(render(config).encode())
    if config is not None and not allow_secrets:  # scanned outside the space lock
        repo = _synced(ws, space)
        old = repo.blob(path) if repo is not None else None
        refuse_secrets(added_findings(path, render(config).encode(), old))
    author = author_identity(actor.user, ws.committer)
    session.commit()  # the connection goes back to the pool during git
    with try_space_lock(database.engine, space.id, timeout=30) as acquired:
        if not acquired:
            raise conflict("The space is busy (synchronization in progress). Try again.")
        # Read again under the lock (`populate_existing`): the copy loaded with the request
        # predates it, and the synchronization that held the lock may have written the
        # routine meanwhile (a stale `sha` None would keep the edit in the database only).
        row = session.scalar(
            select(Routine)
            .where(Routine.space_id == space.id, Routine.name == name)
            .execution_options(populate_existing=True)
        )
        if creating and row is not None:
            raise conflict("A routine with this name already exists in the space.")
        if not creating and row is None:
            raise not_found("Routine not found.")
        repo = _synced(ws, space)  # read again under the lock: a sync may have happened
        if repo is None or (row is not None and row.sha is None):
            if row is not None and row.sha is not None:
                raise invalid(
                    "The space's local copy is not available: try again after its next "
                    "synchronization."
                )
            _save_pending(session, space, row, name, config)
        else:
            if repaired := repo.heal():
                log.warning("Space %s: local copy repaired (%s)", space.id, ", ".join(repaired))
            current = repo.blob_sha(path)
            if creating and current is not None:
                raise conflict("A routine with this name already exists in the space.")
            if not creating and base_sha is not None and current != (base_sha or None):
                raise conflict(
                    "The file {path} has changed since you opened it (push or another "
                    "administrator): reload the page before saving.",
                    path=path,
                )
            data = None
            changes: dict[str, bytes | None] = {path: None, memory_path(name): None}
            if config is not None:
                old = repo.blob(path)
                if config.memory is None:  # settings copied from the database: as the file says
                    config.memory = memory_of(old)
                if config.writes is None:  # a copy made from the database: the file's folders
                    config.writes = [] if creating else _file_writes(old)
                    apply(config, Fields())  # the whole checked again, folders known
                data = _fits(render(config, extra_lines(old)).encode())
                changes = {path: data}
            # Removed: the routine's memory goes with it, in the same commit.
            verb = "updated" if data is not None else "removed"
            subject = f"Routine {verb}: {name}" + trailers(source, actor.via)
            try:
                committed = repo.commit_changes(changes, author, subject)
            except PathRefused as refused:  # the folder made a file or a link by a push
                raise invalid("Write refused: {detail}", detail=str(refused)) from None
            if committed:
                jobs.enqueue(session, "sync", space.id)
            reconcile(session, space, repo)
        saved = session.scalar(
            select(Routine).where(Routine.space_id == space.id, Routine.name == name)
        )
        if saved is not None and creating:
            saved.created_by = actor.user.id
        session.commit()  # under the lock: the next synchronization sees it
    return saved


def _fits(data: bytes) -> bytes:
    """Each field may be within its limits and the file larger than the reader accepts
    (characters of 3 or 4 bytes, long folder names): written, the routine would be invalid
    at once."""
    if len(data) > MAX_FILE_SIZE:
        raise invalid(
            "The routine's file would be too large ({n} bytes at most): shorten the "
            "instructions or the scope.",
            n=MAX_FILE_SIZE,
        )
    return data


def _save_pending(
    session: Session, space: Space, row: Routine | None, name: str, config: Config | None
) -> None:
    """The routine of a space not synced yet, kept in the database until its first
    synchronization writes it (`synchronize`)."""
    if config is not None and config.writes is None:
        # A copy made from the database: a routine not written yet has no folders. Checked
        # again as a whole, as `save` does once the folders are known (at least one output).
        config.writes = []
        apply(config, Fields())
    if config is not None and config.writes:
        # The database keeps no copy of them: they would be lost before the first write.
        raise invalid(
            "Folders kept by the routine (writes): available once the space has been "
            "synchronized for the first time."
        )
    if config is not None and config.memory:
        # The memory flag neither: kept in the file only, it would be dropped silently.
        raise invalid(
            "Memory of the routine (memory): available once the space has been synchronized "
            "for the first time."
        )
    if config is None:
        if row is not None:
            session.delete(row)
        session.flush()
        return
    if row is None:
        row = Routine(space_id=space.id, name=name)
        session.add(row)
        config.store(row)
        skip_today(row, space)
    else:
        before = Config.of(row).plan
        config.store(row)
        if config.plan != before and config.enabled:
            skip_today(row, space)
    row.updated_at = datetime.now(UTC)
    session.flush()


# --- Folders a routine keeps ---------------------------------------------------------------------

_WRITES: OrderedDict[str, tuple[str, ...]] = OrderedDict()  # file hash → folders
_WRITES_LOCK = threading.Lock()
_WRITES_MAX = 256


def _file_writes(data: bytes | None) -> list[str]:
    """The `writes` folders of a routine file, [] if it has none or they cannot be read. Read
    on their own: a file invalid for another reason (schedule…) keeps its folders when the
    form rewrites it."""
    if not data or len(data) > MAX_FILE_SIZE:
        return []
    try:
        return clean_writes(_folders(_header(data.decode("utf-8", "replace")), "writes"))
    except SynaptaneError:
        return []


def writes_of(ws: Workspace, space: Space, routine: Routine) -> list[str]:
    """Folders the routine keeps, read from its file at the hash the database knows
    (`Routine.sha`): the same version as the other settings copied in the database, even if
    a push has moved the file since (it is read again at the next synchronization). []: none,
    a routine not written to the repository yet, or a space not synced."""
    if routine.sha is None:
        return []
    with _WRITES_LOCK:
        if (cached := _WRITES.get(routine.sha)) is not None:
            _WRITES.move_to_end(routine.sha)
            return list(cached)
    repo = _synced(ws, space)
    if repo is None:
        return []
    try:
        folders = _file_writes(repo.object_data(routine.sha))
    except GitError:
        return []
    with _WRITES_LOCK:
        _WRITES[routine.sha] = tuple(folders)
        while len(_WRITES) > _WRITES_MAX:
            _WRITES.popitem(last=False)
    return folders


# --- Shown to people ----------------------------------------------------------------------------


def error_of(routine: Routine) -> Label | None:
    """Why the routine's file is invalid (to translate when shown), or None."""
    return Label.loads(routine.error) if routine.error else None


def warnings_of(routine: Routine) -> list[Label]:
    """What was ignored in the routine's file (unknown field…), to translate when shown."""
    return [Label.loads(w) for w in routine.warnings or []]


def memory_enabled(ws: Workspace, space: Space, name: str) -> bool:
    """Does the routine's file in the local copy now ask for a memory (False: not synced)?"""
    repo = _synced(ws, space)
    return memory_of(repo.blob(file_path(name))) if repo is not None else False


def memory_flags(ws: Workspace, space: Space) -> dict[str, bool]:
    """`memory_enabled` of every routine file, read in a single call (a list of routines)."""
    repo = _synced(ws, space)
    return {f.name: memory_of(f.data) for f in read_files(repo)} if repo is not None else {}


def current_sha(ws: Workspace, space: Space, name: str) -> str | None:
    """Hash of the routine's file in the local copy now (None: missing or not synced)."""
    repo = _synced(ws, space)
    return repo.blob_sha(file_path(name)) if repo is not None else None
