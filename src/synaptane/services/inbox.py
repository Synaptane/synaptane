"""Inbox items of a space: a Markdown file with front matter under `.synaptane/inbox/`.

Item types:
- `note`: information deposited (MCP, REST, web), with optional attachments in a directory of
  the same name;
- `change`: page edited directly in `content/` (web, REST, MCP, Git push); computed from the
  history (`services.changes`), without a file.

This module only formats and parses; deposits go through `services.deposits`.
"""

import json
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath

from synaptane.core import names
from synaptane.git.repo import Identity

INBOX_DIR = ".synaptane/inbox"
CONTENT_DIR = "content"
# Author of tidy commits: its commits are not "changes" to review.
AGENT_NAME = "Synaptane tidy-up"
# Earlier names of that author, still recognized when reading the history.
LEGACY_AGENT_NAMES = ("Synaptane – rangement",)


def is_tidy_agent(author: str) -> bool:
    """True for a commit of the tidy-up agent, under its current name or an earlier one."""
    return author == AGENT_NAME or author in LEGACY_AGENT_NAMES


# Author of a routine's commits, followed by the routine's name in parentheses.
ROUTINE_NAME = "Synaptane routine"
LEGACY_ROUTINE_NAME = "Synaptane – routine"


def is_routine_agent(author: str) -> bool:
    """True for a commit of an agent routine (any routine, current or earlier name)."""
    return author.startswith((ROUTINE_NAME, LEGACY_ROUTINE_NAME))


@dataclass
class InboxItem:
    path: str  # in the repository
    type: str
    title: str
    deposited_by: str
    deposited_at: str
    source: str
    status: str  # pending or left (left by the tidy run, with a reason)
    reason: str = ""
    hint: str = ""
    via: str = ""  # client: OAuth application, token, web
    origin: str = ""  # where the information comes from: URL, conversation…
    attachments: list[str] = field(default_factory=list)
    body: str = ""
    item_name: str = ""  # changes computed from Git (no file)

    @property
    def name(self) -> str:
        return self.item_name or PurePosixPath(self.path).name


def _yaml_value(value: object) -> str:
    # JSON is valid YAML: safe escaping without an extra dependency.
    return json.dumps(value, ensure_ascii=False)


def front_matter(fields: dict[str, object]) -> str:
    lines = [f"{key}: {_yaml_value(value)}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---\n"


def parse_front_matter(text: str) -> tuple[dict[str, object], str]:
    """`---` header of an item (JSON values, otherwise raw text) and body."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    fields: dict[str, object] = {}
    for line in text[4:end].splitlines():
        key, sep, raw = line.partition(":")
        if not sep or not key.strip():
            continue
        raw = raw.strip()
        try:
            fields[key.strip()] = json.loads(raw)
        except ValueError:
            fields[key.strip()] = raw.strip("'\"")
    body = text[end + 4 :].lstrip("\n")
    return fields, body


def note_item(
    author: Identity,
    source: str,
    title: str,
    body: str,
    hint: str,
    attachments: Sequence[str],
    at: datetime,
    via: str = "",
    origin: str = "",
) -> tuple[str, bytes]:
    """`note` item: a piece of information to file, with its optional attachments."""
    fields: dict[str, object] = {
        "type": "note",
        "title": title,
        "source": source,
        "deposited_at": at.astimezone(UTC).isoformat(timespec="seconds"),
        "deposited_by": [f"{author.name} <{author.email}>"],
    }
    if via:
        fields["via"] = via
    if origin.strip():
        fields["origin"] = origin.strip()[:2000]
    if hint.strip():
        fields["hint"] = hint.strip()
    if attachments:
        fields["attachments"] = list(attachments)
    path = item_name(at, "note", title)
    # The title is in the header: the body does not repeat it (otherwise it shows twice).
    text = front_matter(fields) + "\n" + body.strip() + "\n"
    return path, text.encode()


def attachments_dir(item_path: str) -> str:
    """Attachments directory of an item: same name, without `.md`."""
    return item_path[: -len(".md")] if item_path.endswith(".md") else item_path + ".files"


def item_name(at: datetime, kind: str, slug: str) -> str:
    """Unique, sortable name: date, type, summary, random suffix."""
    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    clean = re.sub(r"[^a-z0-9]+", "-", names.ascii_text(slug)).strip("-")[:60] or "element"
    return f"{INBOX_DIR}/{stamp}-{kind}-{clean}-{secrets.token_hex(2)}.md"


def skeleton(space_name: str, description: str, existing: set[str]) -> dict[str, bytes]:
    """Minimal files of a space, only the missing ones (docs/reference/repository-format.md)."""
    files: dict[str, bytes] = {}
    if CONTENT_DIR not in existing:
        intro = f"# {space_name}\n\n{description.strip()}\n" if description.strip() else ""
        files[f"{CONTENT_DIR}/README.md"] = (intro or f"# {space_name}\n").encode()
    if INBOX_DIR not in existing:
        files[f"{INBOX_DIR}/.gitkeep"] = b""
    return files
