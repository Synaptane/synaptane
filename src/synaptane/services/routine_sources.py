"""External HTTP sources of a space's routines: declared by the administrators, chosen by name.

A routine can read an outside service (a status API, a ticket tool, a wiki of another tool)
with the `fetch` tool (`services.routine_fetch`). Whoever can push to the repository can change
a routine; if its file could give an address, a push would be enough to send the wiki to any
server. So the sources live here, in the database, and only the organization's administrators
declare them, space by space: a short name (`zendesk`), a base address (HTTPS, a host, a path
prefix) and, optionally, a secret and how to send it (`Authorization: Bearer`, a named header,
or Basic). A routine's file only names the sources it reads (`sources: zendesk, status`).

Scope: the space. A secret entered for one space never reaches another one's routines, and a
space's administrators see at a glance what its routines may read. Every routine of the space
may read every source of the space, and whoever can push can write a routine: declare in a
space only sources whose content may end up in its wiki.

The secret is encrypted (`SecretBox`), bound to the source and to its origin (scheme, host,
port): changing the host asks for the secret again, as for the AI keys (`secrets`), so a saved
secret is never sent elsewhere. It is never read back: the pages and the REST API only say
when it was saved; the audit log names the source and its host.

Adding or removing a source brings the space's routines in step at once (`routine_files.recheck`):
a routine naming a removed source becomes invalid and does not run.

The instance can turn the feature off (`SYNAPTANE_ROUTINE_SOURCES=false`): the routines then
run without the `fetch` tool, and their page says so (`notices`).
"""

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import Label, conflict, invalid, not_found
from synaptane.core.security import SecretBox
from synaptane.services import Actor, audit, routine_fetch, routine_files
from synaptane.services.routine_fetch import Base, Endpoint, Refused
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Routine, RoutineSource, Space, utcnow

AUTH = ("", "bearer", "header", "basic")
MAX_SOURCES = 20  # per space
MAX_URL = 500
MAX_SECRET = 4_096
_HEADER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?")
# Headers Synaptane sets itself, or that would change how the request travels.
RESERVED_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "connection",
        "content-length",
        "expect",
        "host",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "user-agent",
    }
)
_PRINTABLE = re.compile(r"[\x20-\x7e -￿]+")

KEY_REQUIRED = (
    "Enter the secret again: the address now leads to another host, and a saved secret is "
    "never sent to any destination other than its own."
)


def _context(source: RoutineSource, base: Base) -> str:
    """Binds the ciphertext to the source and to its origin: copied to another source, or
    read for another host, it does not decrypt."""
    return f"routine-source:{source.id}:{base.origin}"


def base_of(url: str) -> Base:
    """A base address given by an administrator, checked and normalized (`routine_fetch`)."""
    if len(url.strip()) > MAX_URL:
        raise invalid("Address too long ({n} characters at most).", n=MAX_URL)
    try:
        base = routine_fetch.parse_base(url)
    except ValueError as error:
        reason = str(error)
        if reason == "https":
            raise invalid("The address must start with https://.") from None
        if reason == "userinfo":
            raise invalid(
                "No user name nor password in the address: enter the secret in its own field."
            ) from None
        if reason == "query":
            raise invalid(
                "An address without ? nor #: the routine gives the query of each request."
            ) from None
        raise invalid("Invalid address (e.g. https://status.example.com/api/v2/).") from None
    host = base.host
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise invalid("A source is a public service: local addresses are refused.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return base
    if not address.is_global:
        raise invalid("A source is a public service: local addresses are refused.")
    return base


def _check_name(name: str) -> str:
    name = name.strip().lower()
    if not routine_files.SOURCE_NAME.fullmatch(name):
        raise invalid(
            "Name of a source: lowercase letters, digits and dashes, 32 characters at most "
            "(e.g. status, zendesk)."
        )
    return name


def _check_header(auth: str, header: str) -> str:
    if auth != "header":
        return ""
    header = header.strip()
    if not _HEADER.fullmatch(header):
        raise invalid("Header name: letters, digits and dashes (e.g. X-Api-Key).")
    if header.lower() in RESERVED_HEADERS:
        raise invalid("This header is set by Synaptane: choose another one (e.g. X-Api-Key).")
    return header


def _check_secret(auth: str, secret: str) -> str:
    if len(secret) > MAX_SECRET or not _PRINTABLE.fullmatch(secret):
        raise invalid("Secret: one line of text, {n} characters at most.", n=MAX_SECRET)
    if auth == "basic" and ":" not in secret:
        raise invalid("Basic authentication: enter the secret as user:password.")
    return secret


# --- Administration (organization admins) -------------------------------------------------------


def list_sources(session: Session, actor: Actor, space: Space) -> list[RoutineSource]:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    return list(
        session.scalars(
            select(RoutineSource)
            .where(RoutineSource.space_id == space.id)
            .order_by(RoutineSource.name)
        )
    )


def get(session: Session, actor: Actor, space: Space, name: str) -> RoutineSource:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    source = session.scalar(
        select(RoutineSource).where(
            RoutineSource.space_id == space.id, RoutineSource.name == name.strip().lower()
        )
    )
    if source is None:
        raise not_found("External source not found.")
    return source


def used_by(session: Session, space: Space, name: str) -> list[str]:
    """Titles of the routines that name the source (as the database knows them)."""
    rows = session.scalars(select(Routine).where(Routine.space_id == space.id))
    return sorted(r.title or r.name for r in rows if name in (r.sources or []))


def create(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    *,
    name: str,
    url: str,
    auth: str = "",
    header: str = "",
    secret: str = "",
) -> RoutineSource:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    name = _check_name(name)
    base = base_of(url)
    if auth not in AUTH:
        raise invalid("Unknown authentication.")
    header = _check_header(auth, header)
    if auth and not secret:
        raise invalid("Enter the secret, or choose no authentication.")
    taken = list(
        session.scalars(select(RoutineSource.name).where(RoutineSource.space_id == space.id))
    )
    if name in taken:
        raise conflict("The space already has an external source with this name.")
    if len(taken) >= MAX_SOURCES:
        raise invalid("{n} external sources at most per space.", n=MAX_SOURCES)
    source = RoutineSource(
        space_id=space.id, name=name, base_url=base.url, auth=auth, header=header
    )
    source.created_by = actor.user.id
    session.add(source)
    session.flush()
    if auth:
        source.secret = ws.box.encrypt(_check_secret(auth, secret), _context(source, base))
        source.secret_set_at = utcnow()
    audit(session, actor, "space.source_created", space=space, source=name, host=base.host)
    session.commit()  # seen by the reconciliation, in its own session
    routine_files.recheck(database, ws, space.id)
    return source


def update(
    session: Session,
    actor: Actor,
    ws: Workspace,
    space: Space,
    source: RoutineSource,
    *,
    url: str | None = None,
    auth: str | None = None,
    header: str | None = None,
    secret: str | None = None,
) -> RoutineSource:
    """Changes a source; None: unchanged. `secret`: a new one replaces the saved one ("" or
    None keeps it). Moving to another host asks for the secret again (`KEY_REQUIRED`), and no
    authentication removes it. The name never changes: the routines' files name it."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    old = routine_fetch.parse_base(source.base_url)
    base = base_of(url) if url is not None else old
    kind = source.auth if auth is None else auth
    if kind not in AUTH:
        raise invalid("Unknown authentication.")
    named = _check_header(kind, source.header if header is None else header)
    changed = []
    if base.url != old.url:
        changed.append("url")
    if kind != source.auth or named != source.header:
        changed.append("auth")
    replaced = bool(secret) and bool(kind)  # without authentication, no secret to keep
    keep = source.secret if kind else None
    if keep is not None and not replaced and base.origin != old.origin:
        raise invalid(KEY_REQUIRED)
    if kind and keep is None and not replaced:
        raise invalid("Enter the secret, or choose no authentication.")
    if replaced:
        assert secret is not None
        _check_secret(kind, secret)
    elif keep is not None and kind == "basic" and source.auth != "basic":
        # A bearer token is not a user:password: Basic needs its own secret.
        raise invalid("Basic authentication: enter the secret as user:password.")
    source.base_url, source.auth, source.header = base.url, kind, named
    if replaced:
        assert secret is not None
        source.secret = ws.box.encrypt(secret, _context(source, base))
        source.secret_set_at = utcnow()
    elif not kind:
        source.secret, source.secret_set_at = None, None
    source.updated_at = utcnow()
    if changed:
        audit(
            session,
            actor,
            "space.source_updated",
            space=space,
            source=source.name,
            host=base.host,
            fields=" ".join(changed),
        )
    if replaced:
        audit(
            session,
            actor,
            "space.source_secret_replaced",
            space=space,
            source=source.name,
            host=base.host,
        )
    session.flush()
    return source


def delete(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    source: RoutineSource,
) -> list[str]:
    """Removes a source and its secret. Returns the titles of the routines that named it:
    invalid from now on (they do not run until their file stops naming it)."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    users = used_by(session, space, source.name)
    host = routine_fetch.parse_base(source.base_url).host
    audit(session, actor, "space.source_deleted", space=space, source=source.name, host=host)
    session.delete(source)
    session.commit()  # seen by the reconciliation, in its own session
    routine_files.recheck(database, ws, space.id)
    return users


@dataclass(frozen=True)
class Probe:
    """What the administrators' test found: an HTTP status and a type, or why it failed.
    Never the content."""

    status: int | None
    mime: str = ""
    error: Label | None = None


def check(
    session: Session, actor: Actor, ws: Workspace, space: Space, source: RoutineSource
) -> Probe:
    """ "Test": a GET on the source's base address, with its secret, through the same
    safeguards as a run. Only the status and the content's type come back."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    endpoint = _endpoint(ws.box, source)
    try:
        answer = routine_fetch.request(endpoint, endpoint.base.path, read=False)
    except Refused as refused:
        return Probe(None, error=Label(refused.template, **refused.params))
    return Probe(answer.status, answer.mime)


# --- Runs ----------------------------------------------------------------------------------------


def _endpoint(box: SecretBox, source: RoutineSource) -> Endpoint:
    base = routine_fetch.parse_base(source.base_url)
    try:
        secret = box.decrypt(source.secret, _context(source, base)) if source.secret else ""
    except Exception:  # noqa: BLE001 — any decryption failure: another key, a copied value
        raise invalid(
            "The secret of the external source {name} cannot be read (master key replaced?): "
            "enter it again in Settings → External sources.",
            name=source.name,
        ) from None
    return Endpoint(source.name, base, source.auth, source.header, secret)


def endpoints(
    session: Session, box: SecretBox, space_id: object, names: list[str]
) -> list[Endpoint]:
    """The sources a run may call, secrets decrypted, in the order the routine names them.
    A name the space does not know is left out (the caller refuses the run)."""
    found = {
        s.name: s
        for s in session.scalars(
            select(RoutineSource).where(
                RoutineSource.space_id == space_id, RoutineSource.name.in_(names)
            )
        )
    }
    return [_endpoint(box, found[n]) for n in names if n in found]


def notices(ws: Workspace, routine: Routine) -> list[Label]:
    """What to say about a routine's sources on its page: off for the whole instance."""
    if routine.sources and not ws.routine_sources:
        return [
            Label(
                "External sources are turned off on this instance (SYNAPTANE_ROUTINE_SOURCES): "
                "the routine runs without them."
            )
        ]
    return []


def secret_saved(source: RoutineSource) -> datetime | None:
    """When the secret was saved (shown instead of the secret); None: no secret."""
    return source.secret_set_at if source.secret is not None else None
