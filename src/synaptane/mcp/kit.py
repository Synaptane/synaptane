"""What MCP tools share: call context, designated space, explicit refusals, untrusted content
fenced off.

Every tool has a readable title and every parameter a description: the Claude connector
directory requires them, and ChatGPT relies mostly on the schemas to choose its arguments.
Descriptions stay short: the tool list is reread on every turn.
Everything an assistant reads (descriptions, responses, errors) is in English: it then
answers in the user's language.
"""

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.mcp.oauth import token_identity
from synaptane.services import Actor, assistants, load_actor, organizations, spaces
from synaptane.services import content as files
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.tidy_tools import untrusted
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Space, User

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Every write is announced as destructive, inbox drops and verification included: a client
# often approves a "non-destructive" tool without asking, and it is through a write that an
# instruction hidden in a page would move content from one space to another. `idempotent`:
# calling the tool again with the same arguments changes nothing more (the second call fails
# or finds nothing to do); false for what adds something on every call.
CHANGES = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
ADDS = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

# Common parameters: a short description, the same everywhere.
SpaceArg = Annotated[str, Field(description="Space org/space, e.g. acme/infra")]
PathArg = Annotated[str, Field(description="Path in the space, e.g. infra/proxmox.md")]
MessageArg = Annotated[str, Field(description="Commit message (optional)")]
SecretArg = Annotated[
    bool,
    Field(description="true only if the user confirms it is not a secret"),
]
SinceArg = Annotated[
    str,
    Field(description="Since this date, YYYY-MM-DD[THH:MM]; empty: no limit"),
]
NoteIdArg = Annotated[
    str,
    Field(description="Note id inbox:org/space/note.md, from inbox_add or inbox_list"),
]

READ_LIMIT = 50_000  # characters per read: beyond that, the rest is read with `offset`
FETCH_LIMIT = 50_000  # fetch (ChatGPT connectors): beyond that, the rest is read with `read`


@dataclass
class Call:
    session: Session
    actor: Actor
    ws: Workspace
    database: Database
    settings: Settings
    read_only: bool = False  # connection authorized read-only (chosen at consent)
    personal: bool = False  # personal token (not an OAuth application)
    inbox_only: bool = False  # reading and the inbox, no direct change (chosen at consent)
    space: str = ""  # last designated space, for a refusal that names it


def quoted(kind: str, text: str, source: str = "") -> str:
    """Page or note content returned to an assistant, fenced off as untrusted: the tidy-up
    agent's mechanism (`tidy_tools.untrusted`), with a suffix drawn for each response (a
    closing tag cannot be guessed) and imitated tags neutralized.
    `kind`: page, inbox_item or history; `source`: org/space/path."""
    return untrusted(kind, secrets.token_hex(4), text, name=source)


def relative(path: str) -> str:
    """Path relative to the content; the "content/" prefix is accepted (as seen in Git)."""
    path = path.strip().lstrip("/")
    return path.removeprefix("content/")


def since_date(value: str) -> str | None:
    """Validated ISO date (nothing else reaches `git log --since`), or None if empty."""
    value = value.strip()
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(
            "`since` must be YYYY-MM-DD (or YYYY-MM-DDTHH:MM), for example 2026-10-01."
        ) from None
    if not 1970 <= when.year <= 2099:  # beyond that, Git silently ignores the date
        raise ToolError("`since`: choose a date between 1970 and 2099.")
    return value


def note_id(value: str) -> tuple[str, str]:
    """(org/space, name) of a note designated by its id `inbox:org/space/name`."""
    raw = value.strip().removeprefix("inbox:").strip("/")
    org, _, rest = raw.partition("/")
    space, _, name = rest.partition("/")
    name = name.removeprefix(".synaptane/inbox/")
    if not (org and space and name):
        raise ToolError(
            'Expected id: "inbox:organization/space/note.md", as given by inbox_add, '
            "inbox_list and search."
        )
    return f"{org}/{space}", name


def read_only_note(c: Call) -> str:
    """Why a connection does not write, and how to fix it: the fix differs for a personal
    token and for an application authorized through OAuth."""
    if c.personal:
        return (
            "Read-only personal token: to write or drop notes, create a token without this "
            "limit (My account → Personal tokens)."
        )
    return (
        "Read-only connection, chosen when it was authorized: to write or drop notes, "
        "reconnect the application without this limit."
    )


def inbox_only_note(c: Call) -> str:
    """What a connection limited to the inbox can do, and how to widen it."""
    where = (
        "create a token without this limit (My account → Personal tokens)"
        if c.personal
        else "reconnect the application without this limit"
    )
    return (
        "Connection limited to the inbox, chosen when it was authorized: it reads, and drops "
        "notes with inbox_add (the nightly tidy-up files them), but changes no page directly. "
        f"To write pages, {where}."
    )


def refusal(c: Call, error: SynaptaneError) -> str:
    """A bare "Action not allowed." does not say what to do: the cause, and the fix."""
    if error.code is not ErrorCode.FORBIDDEN:
        return error.message
    if c.read_only:
        return read_only_note(c)
    if c.inbox_only:
        return inbox_only_note(c)
    where = c.space or "this space"
    return (
        f"You are a reader of {where}: only contributors drop notes and write. Ask an "
        "administrator of the organization for the contributor role, or choose a space "
        "where you have it (list_spaces gives your role)."
    )


@dataclass
class Kit:
    settings: Settings
    database: Database
    ws: Workspace

    @contextmanager
    def call(self) -> Iterator[Call]:
        """Context of a call: account, token rights, session. A business error becomes a
        tool error, and a permission refusal says why."""
        token = get_access_token()
        if token is None or token.subject is None:
            raise ToolError("Authentication required.")
        try:
            with self.database.session() as session:
                user = session.get(User, UUID(token.subject))
                if user is None or not user.is_active:
                    raise ToolError("Inactive account.")
                via, limits = token_identity(session, token.client_id, token.token)
                actor = load_actor(session, user, via, limits)
                assistants.record(self.database, user.id, via)  # separate, short transaction
                current = Call(
                    session,
                    actor,
                    self.ws,
                    self.database,
                    self.settings,
                    limits.read_only,
                    personal=token.client_id == "personal-token",
                    inbox_only=limits.inbox_only,
                )
                try:
                    yield current
                except SynaptaneError as error:
                    raise ToolError(refusal(current, error)) from None
        except SynaptaneError as error:
            raise ToolError(error.message) from None

    def space_of(self, c: Call, address: str) -> Space:
        org_slug, _, space_slug = address.strip().strip("/").partition("/")
        try:
            org = organizations.get_organization(c.session, c.actor, org_slug)
            found = spaces.get_space(c.session, c.actor, org, space_slug)
        except SynaptaneError:
            raise ToolError(
                f'Space "{address}" not found. Use list_spaces to see the spaces you can '
                "access (organization/space)."
            ) from None
        c.space = found.address
        return found

    def web_url(self, org: str, space: str, path: str, anchor: str = "") -> str:
        url = f"{self.settings.public_url}/o/{org}/{space}/p/{quote(path, safe='/')}"
        return url + (f"#{anchor}" if anchor else "")

    def text_of(self, c: Call, space: Space, file: files.File) -> tuple[str | None, bool]:
        """Text of a file: its own, or the extracted one (PDF, Office, image); extracted?"""
        if file.text is not None:
            return file.text, False
        extracted = c.ws.extracted_text(space.id, f"{CONTENT_DIR}/{file.path}")
        return extracted, extracted is not None
