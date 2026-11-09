"""Capture by email: an email sent or forwarded to `brain+org.space@example.com` becomes a note
in the inbox of that space, filed later by the tidy-up (docs/admin/email-capture.md).

The worker reads a dedicated mailbox over IMAP every few minutes (`schedule`, then `poll`):
no webhook, no incoming SMTP server. Each email is accepted only if all of this holds:
- its `From` is a single address, that of an **active account whose address is verified**;
- the receiving server of the mailbox **authenticated** it (`core.authres`: DMARC, or DKIM
  aligned on the `From` domain, read from the topmost `Authentication-Results` header with a
  trusted identifier): a `From` can be forged, a DKIM signature cannot;
- the address names a space (`+org.space`, see `targets`) where this account may drop notes
  (same rules as the web form and `inbox_add`: restricted spaces and groups included; a super
  admin only where they are a member);
- it is not a signed email replayed by someone else: no header allowed once (RFC 5322) appears
  twice, its `Date` is within MAX_CLOCK_GAP of its reception, and the capture address is in
  To or Cc (`targets`, `addressed`);
- it passes the checks of every deposit (`deposits.deposit`): size, attachments, secrets, rate.

Emails handled are moved out of the read folder, to `Synaptane/Processed` or
`Synaptane/Refused` (created if absent); an email that cannot be handled now (space busy, rate
limit) stays and is tried again at the next poll, for MAX_DEFERRAL at most. The parts of an
email are only read once its sender is authenticated and has an account. Nothing ever answers
a sender who is not authenticated (no backscatter); an authenticated account holder may get a
confirmation or the reason of a refusal (`replies`, MAX_ANSWERS_PER_HOUR at most). Refusals
are kept for the health page, without content.
"""

import hashlib
import imaplib
import logging
import re
import socket
import ssl
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from synaptane.core import authres, names
from synaptane.core.access import Action, Limits, can, require
from synaptane.core.email_message import (
    Parsed,
    parse,
    read_content,
    received_at,
    too_complex,
)
from synaptane.core.errors import ErrorCode, Label, SynaptaneError, invalid
from synaptane.core.security import SecretBox
from synaptane.git.repo import GitError
from synaptane.services import Actor, audit, deposits, jobs, load_actor, mail
from synaptane.services.inbox import INBOX_DIR
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import (
    CaptureSettings,
    EmailCapture,
    Job,
    Organization,
    Space,
    User,
)

log = logging.getLogger("synaptane.email_capture")

EMAIL = "email"  # job kind
SECURITY = ("ssl", "starttls", "none")
REPLIES = ("off", "refusals", "all")
DEFAULT_INTERVAL = 2  # minutes
TIMEOUT = 30  # seconds, for each network operation of a poll
TEST_TIMEOUT = 15  # seconds, for the "Test the connection" button (a web request waits)
MAX_MESSAGE = 35 * 1024 * 1024  # bytes, as received (attachments encoded in base64)
MAX_PER_POLL = 25  # emails handled per poll: the others wait for the next one
# Emails looked at per poll, those waiting for a busy space included: they must not hold
# back the others.
MAX_EXAMINED = 100
POLL_BUDGET = 240.0  # seconds: no new email taken after that
# Seconds after which a poll's connection is cut, whatever each operation does: every one is
# bounded by TIMEOUT and by what is left, and a watchdog closes the socket at the deadline (a
# server that sends a byte every 29 seconds keeps no poll alive).
POLL_DEADLINE = 300.0
LOGOUT_TIMEOUT = 5.0  # seconds: the poll is over, the server need not answer at length
# An email dated more than this away from its reception is refused: an authentic, signed
# email replayed later by someone else keeps its old date (signed).
MAX_CLOCK_GAP = timedelta(hours=48)
# An email that still cannot be dropped after this long (space unavailable) is refused: it
# would otherwise be read again at every poll, forever.
MAX_DEFERRAL = timedelta(hours=24)
# Answers to one account per hour, at most: an auto-responder that ignores `Auto-Submitted`
# and answers each of them must not start an endless exchange.
MAX_ANSWERS_PER_HOUR = 20
KEEP = timedelta(days=90)  # handled emails remembered (idempotence, health page)
PARENT, PROCESSED, REFUSED = "Synaptane", "Processed", "Refused"
_CONTEXT = "imap:password"
# Advisory locks: one poll at a time on the whole instance, one queued poll job.
POLL_LOCK = 7_310_524_861_903_211
QUEUE_LOCK = 7_310_524_861_903_212

ACCEPTED, REFUSED_STATUS, DEFERRED = "accepted", "refused", "deferred"

# Why an email was refused: shown on the health page (never its content).
REASONS: dict[str, Label] = {
    "automatic": Label("Automatic message (auto-reply, bounce, mailing list)"),
    "malformed": Label("No single sender address"),
    "duplicate_headers": Label("A header that may appear once appears twice (replayed email?)"),
    "malformed_headers": Label("A line of the headers is not a header (replayed email?)"),
    "stale_date": Label("Date missing, or more than 48 hours away from the reception"),
    "not_in_recipients": Label("Capture address neither in To nor in Cc"),
    "not_authenticated": Label("Sender not authenticated (neither DMARC nor aligned DKIM)"),
    "unknown_sender": Label("No active account with this verified address"),
    "no_space": Label("No space in the address"),
    "no_access": Label("Unknown space, or no right to drop notes there"),
    "space_not_ready": Label("Space not synchronized yet"),
    "too_large": Label("Email too large"),
    "too_many_attachments": Label("Too many attachments"),
    "too_complex": Label("Too many parts, or parts nested too deep"),
    "secret": Label("Likely secret in the content"),
    "invalid": Label("Content refused (empty, or an attachment too large)"),
    "unavailable": Label("Space unavailable for 24 hours"),
    "error": Label("Unexpected error while reading it"),
}
# Refusals told to the sender (authenticated, with an account): the others are silent.
_TOLD = frozenset(
    {"no_space", "no_access", "space_not_ready", "too_large", "too_many_attachments", "secret"}
    | {"invalid", "unavailable", "too_complex", "stale_date", "not_in_recipients"}
)


class CaptureError(Exception):
    """The mailbox cannot be read (server unreachable, credentials refused…). `label`: the
    message to translate, without any secret."""

    def __init__(self, label: Label) -> None:
        super().__init__(str(label))
        self.label = label


# --- Settings -------------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureView:
    host: str
    port: int
    security: str
    username: str
    has_password: bool
    folder: str
    address: str
    authserv_ids: str
    unnamed_results: bool
    spf_domains: str
    interval: int
    replies: str
    polled_at: datetime | None
    poll_ok_at: datetime | None
    poll_error: Label | None


def view(session: Session, actor: Actor) -> CaptureView | None:
    require(actor.access, Action.MANAGE_INSTANCE)
    row = session.get(CaptureSettings, 1)
    if row is None:
        return None
    return CaptureView(
        row.host,
        row.port,
        row.security,
        row.username,
        bool(row.password),
        row.folder,
        row.address,
        row.authserv_ids,
        row.unnamed_results,
        row.spf_domains,
        row.interval,
        row.replies,
        row.polled_at,
        row.poll_ok_at,
        Label.loads(row.poll_error) if row.poll_error else None,
    )


def configured(session: Session) -> bool:
    return session.get(CaptureSettings, 1) is not None


_HOST = re.compile(r"^[A-Za-z0-9.-]{1,255}$|^\[?[0-9A-Fa-f:.]+\]?$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]{1,63})+$")
_LOCAL = re.compile(r"^[A-Za-z0-9!#$%&'*/=?^_`{|}~.-]{1,64}$")  # no "+": it opens the space


def _address(value: str) -> str:
    """The public capture address, lowercase; refused if it cannot take `+org.space`."""
    value = value.strip().lower()
    local, at, domain = value.rpartition("@")
    if not at or not _LOCAL.fullmatch(local) or not _DOMAIN.fullmatch(domain):
        raise invalid(
            "Enter the capture address, without “+” (brain@example.com): each space is reached "
            "at brain+organization.space@example.com."
        )
    return value


def _list(value: str, what: str, *, wildcard: bool = False) -> str:
    found = authres.identifiers(value)
    names = [item[2:] if wildcard and item.startswith("*.") else item for item in found]
    if any(not _DOMAIN.fullmatch(name) for name in names):
        raise invalid("{field}: domain names expected (mx.google.com).", field=Label(what))
    return ", ".join(found)


def configure(
    session: Session,
    actor: Actor,
    box: SecretBox,
    *,
    host: str,
    port: int,
    security: str,
    username: str,
    address: str,
    authserv_ids: str,
    password: str | None = None,
    folder: str = "INBOX",
    unnamed_results: bool = False,
    spf_domains: str = "",
    interval: int = DEFAULT_INTERVAL,
    replies: str = "refusals",
    allow_plain: bool = False,
) -> None:
    """`password=None` keeps the stored password. `allow_plain`: the instance accepts an
    unencrypted connection (SYNAPTANE_ALLOW_INSECURE_IMAP, development only)."""
    require(actor.access, Action.MANAGE_INSTANCE)
    host, username, folder = host.strip(), username.strip(), folder.strip() or "INBOX"
    if not host or not _HOST.fullmatch(host) or not username:
        raise invalid("Enter the IMAP server and the mailbox's username.")
    if security not in SECURITY or not 0 < port < 65536:
        raise invalid("Invalid IMAP port or security.")
    if security == "none" and not allow_plain:
        raise invalid(
            "An unencrypted connection would send the password and every email in clear: "
            "choose SSL/TLS or STARTTLS."
        )
    if len(folder) > 255 or not folder.isascii() or not folder.isprintable():
        raise invalid("Folder: letters without accents, digits and punctuation only.")
    if not 1 <= interval <= 60:
        raise invalid("Interval: between 1 and 60 minutes.")
    if replies not in REPLIES:
        raise invalid("Invalid choice of answers.")
    ids = _list(authserv_ids, "Trusted servers", wildcard=True)
    if not ids and not unnamed_results:
        raise invalid(
            "Enter the identifier of the receiving server (mx.google.com…): without it, no "
            "sender can be authenticated."
        )
    if not username.isascii() or (password and not password.isascii()):
        # imaplib sends them in ASCII (LOGIN): an accent would fail at every poll.
        raise invalid(
            "The username and password may only hold characters without accents: use an app "
            "password."
        )
    address, spf = _address(address), _list(spf_domains, "SPF domains")
    row = session.get(CaptureSettings, 1)
    if row is None:
        if not password:
            raise invalid("Enter the mailbox's password.")
        row = CaptureSettings(id=1)
        session.add(row)
    row.host, row.port, row.security, row.username = host, port, security, username
    row.folder, row.address, row.authserv_ids = folder, address, ids
    row.unnamed_results, row.spf_domains = unnamed_results, spf
    row.interval, row.replies, row.updated_at = interval, replies, datetime.now(UTC)
    if password:
        row.password = box.encrypt(password, _CONTEXT)
    session.flush()
    audit(
        session,
        actor,
        "instance.capture_configured",
        host=host,
        address=row.address,
        spf=bool(row.spf_domains),
        unnamed=row.unnamed_results,
    )


def remove(session: Session, actor: Actor) -> None:
    require(actor.access, Action.MANAGE_INSTANCE)
    row = session.get(CaptureSettings, 1)
    if row is not None:
        session.delete(row)
        audit(session, actor, "instance.capture_removed", address=row.address)


def address_shown(session: Session, user: User, space: Space) -> str | None:
    """The capture address of a space, for this person: only where their emails would be
    accepted (verified address of an active account, right to drop notes there as the capture
    checks it: a super admin only where they are a member). None otherwise."""
    if not user.is_active or not user.email or user.email_verified_at is None:
        return None
    address = address_for(session, space)
    if address is None:
        return None
    actor = load_actor(session, user, limits=Limits(members_only=True))
    allowed = can(actor.access, Action.DEPOSIT_SPACE, org_id=space.org_id, space_id=space.id)
    return address if allowed else None


def address_for(session: Session, space: Space) -> str | None:
    """The capture address of a space, or None if the capture is not configured."""
    row = session.get(CaptureSettings, 1)
    if row is None:
        return None
    local, _, domain = row.address.rpartition("@")
    return f"{local}+{space.organization.slug}.{space.slug}@{domain}"


# --- Routing ----------------------------------------------------------------------------------


def _tag(address: str, capture: str) -> str | None:
    """`org.space` of `brain+org.space@example.com`; "" for the bare capture address; None for
    another address. Case is ignored."""
    local, _, domain = capture.lower().rpartition("@")
    found_local, at, found_domain = address.lower().rpartition("@")
    if not at or found_domain != domain:
        return None
    if found_local == local:
        return ""
    if found_local.startswith(local + "+"):
        return found_local[len(local) + 1 :]
    return None


MAX_TARGETS = 5


def targets(parsed: Parsed, capture: str) -> list[str]:
    """The spaces aimed at, as written (`org.space`), from To and Cc only: those the sender
    wrote, signed by DKIM with the rest of the email (Gmail, most servers; not Microsoft 365).
    A blind copy, or an address only in the envelope (Delivered-To, X-Original-To…), aims at
    nothing: a signed email replayed to the capture by someone else is not dropped there. The
    envelope only chooses, when To and Cc name several spaces, the one of this copy (each
    space gets its own copy). At most MAX_TARGETS."""
    named: list[str] = []
    for address in parsed.recipients:
        tag = _tag(address, capture)
        if tag and tag not in named:
            named.append(tag)
    envelope = {_tag(address, capture) for address in parsed.envelope} - {None, ""}
    this_copy = [tag for tag in named if tag in envelope]
    return (this_copy or named)[:MAX_TARGETS]


def addressed(parsed: Parsed, capture: str) -> bool:
    """The capture address, with or without a space, is in To or Cc."""
    return any(_tag(address, capture) is not None for address in parsed.recipients)


def find_space(session: Session, tag: str) -> Space | None:
    """The space of `org.space`; slugs hold neither dots nor capitals, so the split is
    unambiguous."""
    org_slug, dot, space_slug = tag.partition(".")
    if not dot or "." in space_slug:
        return None
    try:
        org_slug, space_slug = names.slug(org_slug), names.slug(space_slug)
    except SynaptaneError:
        return None
    return session.scalar(
        select(Space)
        .join(Organization, Organization.id == Space.org_id)
        .where(Organization.slug == org_slug, Space.slug == space_slug)
    )


# --- IMAP ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Conf:
    """The settings of a poll, read once (password decrypted)."""

    host: str
    port: int
    security: str
    username: str
    password: str = field(repr=False)  # never in a log or an error, even by mistake
    folder: str
    address: str
    authserv_ids: tuple[str, ...]
    unnamed_results: bool
    spf_domains: tuple[str, ...]
    replies: str


def _conf(row: CaptureSettings, box: SecretBox) -> Conf:
    return Conf(
        row.host,
        row.port,
        row.security,
        row.username,
        box.decrypt(row.password, _CONTEXT) if row.password else "",
        row.folder,
        row.address,
        authres.identifiers(row.authserv_ids),
        row.unnamed_results,
        authres.identifiers(row.spf_domains),
        row.replies,
    )


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unexpected(error: BaseException) -> Label:
    """Any other error: its class only (its words could quote anything)."""
    return Label("Unexpected error while reading the mailbox: {kind}", kind=type(error).__name__)


def _describe(error: BaseException) -> Label:
    """What went wrong, for the super admin: the class and the server's words, never the
    password (imaplib does not put it in its errors)."""
    detail = f"{type(error).__name__}: {error}".replace("\r", " ").replace("\n", " ")
    return Label("Mailbox unreachable or refused: {detail}", detail=detail[:300])


class Mailbox:
    """An IMAP connection, opened with `with`: encrypted (TLS or STARTTLS) unless the
    instance allows otherwise, every operation bounded by `timeout`."""

    def __init__(
        self,
        conf: Conf,
        *,
        allow_plain: bool = False,
        timeout: float = TIMEOUT,
        deadline: float | None = None,
    ):
        """`deadline`: moment (`time.monotonic`) the connection is cut at, at the latest."""
        self.conf, self.allow_plain, self.timeout = conf, allow_plain, timeout
        self.deadline = deadline
        self._watchdog: threading.Timer | None = None
        self.imap: imaplib.IMAP4 | None = None
        self.capabilities: set[str] = set()
        self.delimiter = "/"
        self._known: set[str] = set()

    def _left(self) -> float:
        """Seconds an operation may take now: TIMEOUT, or less near the deadline. Raises
        TimeoutError once the deadline has passed."""
        if self.deadline is None:
            return self.timeout
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("poll deadline reached")
        return min(self.timeout, left)

    def _arm(self) -> None:
        """Before each operation: the socket waits at most what is left."""
        left = self._left()
        sock = getattr(self.imap, "sock", None)
        if sock is not None:
            with suppress(OSError):
                sock.settimeout(left)

    def _cut(self) -> None:
        """The watchdog, at the deadline: whatever is waiting on the socket fails now."""
        sock = getattr(self.imap, "sock", None)
        if sock is not None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    def __enter__(self) -> "Mailbox":
        conf = self.conf
        context = ssl.create_default_context()
        left = self._left()
        if conf.security == "ssl":
            self.imap = imaplib.IMAP4_SSL(conf.host, conf.port, ssl_context=context, timeout=left)
        elif conf.security == "starttls":
            self.imap = imaplib.IMAP4(conf.host, conf.port, timeout=left)  # TLS below
        elif self.allow_plain:
            self.imap = imaplib.IMAP4(conf.host, conf.port, timeout=left)
        else:
            raise CaptureError(Label("Unencrypted connection refused."))
        try:
            if self.deadline is not None:
                left = max(self.deadline - time.monotonic(), 0)
                self._watchdog = threading.Timer(left, self._cut)
                self._watchdog.daemon = True
                self._watchdog.start()
            if conf.security == "starttls":
                self._arm()
                self.imap.starttls(ssl_context=context)
            self._arm()
            self._ok(self.imap.login(conf.username, conf.password))
            self._arm()
            typ, data = self.imap.capability()
            self._ok((typ, data))
            words = b" ".join(d for d in data if isinstance(d, bytes)).decode("ascii", "replace")
            self.capabilities = {w.upper() for w in words.split()}
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self._close()

    def _close(self) -> None:
        """Logs out within LOGOUT_TIMEOUT (the watchdog still armed: a server that drips its
        answer cannot hold it), then closes the socket whatever happened: imaplib leaves it
        open when its LOGOUT fails."""
        imap = self.imap
        if imap is not None:
            wait = LOGOUT_TIMEOUT
            if self.deadline is not None:  # never beyond the deadline (nor zero: blocking)
                wait = max(min(wait, self.deadline - time.monotonic()), 0.1)
            sock = getattr(imap, "sock", None)
            if sock is not None:
                with suppress(OSError):
                    sock.settimeout(wait)
            with suppress(Exception):
                imap.logout()
            with suppress(Exception):
                imap.shutdown()  # after a failed LOGOUT; harmless after a good one
        self.imap = None
        if self._watchdog is not None:
            self._watchdog.cancel()
            if self._watchdog is not threading.current_thread():
                self._watchdog.join(1.0)  # its thread ends now: none left behind, poll after poll
            self._watchdog = None

    @property
    def _imap(self) -> imaplib.IMAP4:
        assert self.imap is not None
        return self.imap

    @staticmethod
    def _ok(response: tuple[str, Sequence[object]]) -> Sequence[object]:
        typ, data = response
        if typ != "OK":
            words = b" ".join(d for d in data if isinstance(d, bytes))
            raise imaplib.IMAP4.error(words.decode("utf-8", "replace")[:200] or typ)
        return data

    def select(self, folder: str, *, readonly: bool = False) -> int:
        self._arm()
        data = self._ok(self._imap.select(_quote(folder), readonly=readonly))
        first = data[0] if data else b"0"
        return int(first) if isinstance(first, bytes) and first.isdigit() else 0

    def uids(self) -> list[str]:
        self._arm()
        # Not those flagged as deleted: copied already by a move cut short (without MOVE).
        data = self._ok(self._imap.uid("SEARCH", "UNDELETED"))
        first = data[0] if data else b""
        return first.decode().split() if isinstance(first, bytes) else []

    def size(self, uid: str) -> tuple[int, datetime | None]:
        """Size of the email and when the server received it (INTERNALDATE)."""
        self._arm()
        data = self._ok(self._imap.uid("FETCH", uid, "(RFC822.SIZE INTERNALDATE)"))
        size, received = 0, None
        for item in data:
            raw = item[0] if isinstance(item, tuple) else item
            if not isinstance(raw, bytes):
                continue
            if found := re.search(rb"RFC822\.SIZE (\d+)", raw):
                size = int(found.group(1))
            if found := re.search(rb'INTERNALDATE "([^"]+)"', raw):
                with suppress(ValueError):
                    received = datetime.strptime(
                        found.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z"
                    )
        return size, received

    def fetch(self, uid: str, part: str = "BODY.PEEK[]") -> bytes:
        self._arm()
        data = self._ok(self._imap.uid("FETCH", uid, f"({part})"))
        for item in data:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                return item[1]
        return b""

    def _delimiter(self) -> str:
        self._arm()
        data = self._ok(self._imap.list('""', '""'))
        first = data[0] if data else b""
        if isinstance(first, bytes) and (found := re.search(rb'\) "(.)"', first)):
            self.delimiter = found.group(1).decode()
        return self.delimiter

    def folder(self, name: str) -> str:
        """`Synaptane/<name>` with the server's delimiter, created if absent."""
        if not self._known:
            self._delimiter()
        full = f"{PARENT}{self.delimiter}{name}"
        if full in self._known:
            return full
        for wanted in (PARENT, full):
            self._arm()
            found = self._ok(self._imap.list('""', _quote(wanted)))
            if not any(found):
                typ, data = self._imap.create(_quote(wanted))
                if typ != "OK" and wanted == full:
                    self._ok((typ, data))  # the parent may be refused (Gmail): not the child
        self._known.add(full)
        return full

    def move(self, uid: str, folder: str) -> None:
        self._arm()
        if "MOVE" in self.capabilities:
            self._ok(self._imap.uid("MOVE", uid, _quote(folder)))
            return
        self._ok(self._imap.uid("COPY", uid, _quote(folder)))
        self._ok(self._imap.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)"))
        if "UIDPLUS" in self.capabilities:
            self._ok(self._imap.uid("EXPUNGE", uid))
        else:
            self._ok(self._imap.expunge())


@dataclass(frozen=True)
class TestResult:
    waiting: int  # emails in the folder
    identifiers: list[tuple[str, int]]  # authserv-id of the latest emails, and how often
    unnamed: int  # latest emails whose header has no identifier (Microsoft 365)


def test(session: Session, actor: Actor, box: SecretBox, *, allow_plain: bool) -> TestResult:
    """ "Test the connection": signs in, counts the emails waiting and lists who wrote the
    topmost `Authentication-Results` header of the 10 latest (to fill the trusted servers).
    Raises CaptureError."""
    require(actor.access, Action.MANAGE_INSTANCE)
    row = session.get(CaptureSettings, 1)
    if row is None:
        raise CaptureError(Label("Capture by email is not configured."))
    conf = _conf(row, box)
    counts: dict[str, int] = {}
    unnamed = 0
    try:
        deadline = time.monotonic() + 2 * TEST_TIMEOUT  # a web request waits for it
        with Mailbox(
            conf, allow_plain=allow_plain, timeout=TEST_TIMEOUT, deadline=deadline
        ) as mailbox:
            waiting = mailbox.select(conf.folder, readonly=True)
            for uid in mailbox.uids()[-10:]:
                head = mailbox.fetch(uid, "BODY.PEEK[HEADER.FIELDS (AUTHENTICATION-RESULTS)]")
                found = parse(head, with_content=False).auth_results
                if not found:
                    continue
                identifier = authres.parse(found[0]).authserv_id
                if identifier is None:
                    unnamed += 1
                else:
                    counts[identifier] = counts.get(identifier, 0) + 1
    except CaptureError:
        raise
    except (imaplib.IMAP4.error, OSError, EOFError) as error:
        raise CaptureError(_describe(error)) from None
    except Exception as error:  # noqa: BLE001 — a server answer imaplib did not expect
        log.exception("Capture by email: connection test failed")
        raise CaptureError(_unexpected(error)) from None
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return TestResult(waiting, ranked, unnamed)


# --- Poll -------------------------------------------------------------------------------------


@dataclass
class Summary:
    accepted: int = 0
    refused: int = 0
    deferred: int = 0
    skipped: bool = False  # another worker was polling
    error: str = ""
    outcomes: list[str] = field(default_factory=list)


def pending(session: Session) -> bool:
    found = session.scalar(
        select(Job.id).where(Job.kind == EMAIL, Job.status.in_(("queued", "running"))).limit(1)
    )
    return found is not None


def schedule(database: Database, now: datetime | None = None) -> bool:
    """Queues a poll when one is due (worker maintenance, every minute). True if queued."""
    moment = now or datetime.now(UTC)
    with database.session() as session:
        row = session.get(CaptureSettings, 1)
        if row is None:
            return False
        # A few seconds of slack: maintenance runs every 60 s, a poll lasts a few seconds.
        if row.polled_at is not None and moment - row.polled_at < timedelta(
            minutes=row.interval, seconds=-15
        ):
            return False
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": QUEUE_LOCK})
        if pending(session):
            return False
        jobs.enqueue(session, EMAIL, None)
        return True


def request(session: Session, actor: Actor) -> bool:
    """ "Check now" on the settings page: a poll by the worker, in a moment."""
    require(actor.access, Action.MANAGE_INSTANCE)
    if not configured(session):
        return False
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": QUEUE_LOCK})
    if pending(session):
        return True
    jobs.enqueue(session, EMAIL, None)
    return True


@contextmanager
def _poll_lock(database: Database) -> Iterator[bool]:
    """One poll at a time on the instance, whatever the number of workers."""
    with database.engine.connect() as connection:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": POLL_LOCK}
        ).scalar_one()
        connection.commit()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": POLL_LOCK})
                connection.commit()


def poll(
    database: Database,
    ws: Workspace,
    *,
    allow_plain: bool = False,
    now: datetime | None = None,
) -> Summary:
    """Reads the mailbox once: at most MAX_PER_POLL emails handled, oldest first (those left
    for a later poll do not count, within MAX_EXAMINED). An email that fails does not stop the
    others; a lost connection ends the poll (tried again at the
    next one). Never raises for the mailbox: the error is kept for the health page."""
    summary = Summary()
    with _poll_lock(database) as acquired:
        if not acquired:
            summary.skipped = True
            return summary
        with database.session() as session:
            row = session.get(CaptureSettings, 1)
            if row is None:
                return summary
            conf = _conf(row, ws.box)
        started = time.monotonic()
        try:
            deadline = started + POLL_DEADLINE
            with Mailbox(conf, allow_plain=allow_plain, deadline=deadline) as mailbox:
                mailbox.select(conf.folder)
                for uid in mailbox.uids()[:MAX_EXAMINED]:
                    if (
                        summary.accepted + summary.refused >= MAX_PER_POLL
                        or time.monotonic() - started > POLL_BUDGET
                    ):
                        break
                    outcome = _one(database, ws, conf, mailbox, uid)
                    summary.outcomes.append(outcome)
                    if outcome == ACCEPTED:
                        summary.accepted += 1
                        mailbox.move(uid, mailbox.folder(PROCESSED))
                    elif outcome == REFUSED_STATUS:
                        summary.refused += 1
                        mailbox.move(uid, mailbox.folder(REFUSED))
                    else:
                        summary.deferred += 1
        except CaptureError as error:
            summary.error = error.label.dumps()
        except (imaplib.IMAP4.error, OSError, EOFError) as error:
            summary.error = _describe(error).dumps()
        except Exception as error:  # noqa: BLE001 — kept for the health page, not lost
            log.exception("Capture by email: poll failed")
            summary.error = _unexpected(error).dumps()
        if summary.error:
            log.warning("Capture by email: %s", Label.loads(summary.error))
        _finish(database, summary, now or datetime.now(UTC))
    return summary


def _finish(database: Database, summary: Summary, moment: datetime) -> None:
    with database.session() as session:
        row = session.get(CaptureSettings, 1)
        if row is not None:
            row.polled_at = moment
            if summary.error:
                row.poll_error = summary.error
            else:
                row.poll_ok_at, row.poll_error = moment, None
        session.execute(delete(EmailCapture).where(EmailCapture.at < moment - KEEP))


def _one(database: Database, ws: Workspace, conf: Conf, mailbox: Mailbox, uid: str) -> str:
    """One email of the mailbox: ACCEPTED, REFUSED_STATUS or DEFERRED. Mailbox errors go up
    (the connection is lost); anything else refuses this email only."""
    size, received = mailbox.size(uid)
    if size > MAX_MESSAGE:
        raw = mailbox.fetch(uid, "BODY.PEEK[HEADER]")  # the headers are enough to answer
        too_large = True
    else:
        raw = mailbox.fetch(uid)
        too_large = False
    try:
        # Headers only: the parts are read once the sender is known to have an account.
        parsed = parse(raw, with_content=False)
        return handle(database, ws, conf, parsed, raw, too_large=too_large, received=received)
    except Exception:  # noqa: BLE001 — one broken email must not stop the others
        log.exception("Capture by email: email %s not handled", uid)
        with database.session() as session:
            _record(session, f"error:{uid}:{time.time_ns()}", REFUSED_STATUS, "error", "", "")
        return REFUSED_STATUS


# --- One email --------------------------------------------------------------------------------


def _key(parsed: Parsed, raw: bytes, target: str) -> str:
    """Identity of an email for a space: its Message-ID (the same email in two copies is one),
    or its bytes when it has none. With its sender: someone else's email reusing that
    Message-ID would not pass for a copy already dropped."""
    identity = parsed.message_id.strip() or hashlib.sha256(raw).hexdigest()
    sender = parsed.sender or ",".join(parsed.from_addresses)
    return hashlib.sha256(f"capture\0{identity}\0{sender}\0{target}".encode()).hexdigest()


def _record(
    session: Session,
    key: str,
    status: str,
    reason: str,
    sender_domain: str,
    target: str,
    *,
    space: Space | None = None,
    user: User | None = None,
    path: str | None = None,
) -> bool:
    """Remembers an email handled. False if a refusal of this very email was already
    remembered (its sender was told then: not again). A deferral is not a refusal."""
    key = key if len(key) <= 64 else hashlib.sha256(key.encode()).hexdigest()
    before = session.scalar(select(EmailCapture.status).where(EmailCapture.key == key))
    values = {
        "status": status,
        "reason": reason,
        "sender_domain": sender_domain[:255],
        "target": target[:130],
        "space_id": space.id if space else None,
        "user_id": user.id if user else None,
        "path": path,
        "at": datetime.now(UTC),
    }
    statement = insert(EmailCapture).values(key=key, **values)
    session.execute(statement.on_conflict_do_update(index_elements=["key"], set_=values))
    return before != REFUSED_STATUS


def _accepted(session: Session, key: str) -> bool:
    found = session.scalar(select(EmailCapture.status).where(EmailCapture.key == key))
    return found == ACCEPTED


FORWARDED = re.compile(r"^(?:(?:fwd?|tr|wg|fw)\s*:\s*)+", re.I)


def _title(parsed: Parsed, locale: str) -> str:
    from synaptane.web.i18n import translator

    title = FORWARDED.sub("", parsed.subject).strip()
    if not title:
        return translator(locale)(Label("Email without subject"))
    return title if len(title) <= 120 else title[:119].rstrip() + "…"


def _origin(parsed: Parsed, user: User) -> str:
    """Where the note comes from: an email of this person, on that day. Their name, not their
    address: every reader of the space sees the origin (addresses are for administrators)."""
    from synaptane.web.i18n import translator

    moment = (parsed.date or datetime.now(UTC)).astimezone(UTC)
    when = moment.strftime("%Y-%m-%d %H:%M UTC")
    label = Label("Email from {sender}, {date}", sender=user.display_name, date=when)
    return translator(user.locale)(label)


def _body(parsed: Parsed, locale: str) -> str:
    from synaptane.web.i18n import translator

    body = parsed.body
    if len(body) > deposits.MAX_BODY:
        cut = Label(
            "Text cut: the email was longer than {limit} characters.", limit=deposits.MAX_BODY
        )
        body = body[: deposits.MAX_BODY - 300].rstrip() + "\n\n*" + translator(locale)(cut) + "*"
    return body


@dataclass
class _Outcome:
    status: str
    reason: str = ""
    target: str = ""
    space: Space | None = None
    path: str = ""
    told: Label | None = None  # what the sender is told of a refusal
    new: bool = True  # first time this refusal is seen


def handle(
    database: Database,
    ws: Workspace,
    conf: Conf,
    parsed: Parsed,
    raw: bytes,
    *,
    too_large: bool = False,
    received: datetime | None = None,
) -> str:
    """Decides on one email and drops it (see the module's rules). ACCEPTED if at least one
    space got it, DEFERRED if one must be tried again later, REFUSED_STATUS otherwise.
    `received`: when the mailbox's server took it (IMAP INTERNALDATE); otherwise the date of
    its topmost `Received` header."""
    sender = parsed.sender
    first = parsed.from_addresses[0] if parsed.from_addresses else ""
    domain = (sender or first).rpartition("@")[2][:255]
    silent = None
    if parsed.automatic:
        silent = "automatic"
    elif parsed.duplicated:  # a header added above the signed one: never told, never read
        silent = "duplicate_headers"
    elif parsed.broken_headers:  # headers Python does not read: duplicates could hide there
        silent = "malformed_headers"
    elif not sender:
        silent = "malformed"
    else:
        header = authres.trusted_header(
            parsed.auth_results, conf.authserv_ids, conf.unnamed_results
        )
        if not authres.verdict(header, domain, conf.spf_domains):
            silent = "not_authenticated"
    with database.session() as session:
        user = None
        if silent is None:
            user = session.scalar(
                select(User).where(
                    User.email == sender,  # stored in lowercase, compared as such
                    User.email_verified_at.is_not(None),
                    User.is_active.is_(True),
                )
            )
            if user is None:
                silent = "unknown_sender"
        if silent is not None or user is None:
            log.info("Capture by email: refused (%s) from %s", silent, domain)
            _record(session, _key(parsed, raw, ""), REFUSED_STATUS, silent or "", domain, "")
            return REFUSED_STATUS
        # Authenticated, with an account: from here on, the sender may be answered.
        actor = load_actor(session, user, limits=Limits(members_only=True))
        aimed = targets(parsed, conf.address)
        outcomes: list[_Outcome] = []
        if not _timely(parsed, received):
            outcomes.append(_refusal(session, parsed, raw, domain, user, "", "stale_date"))
        elif not addressed(parsed, conf.address):
            outcomes.append(_refusal(session, parsed, raw, domain, user, "", "not_in_recipients"))
        elif not aimed:
            outcomes.append(_refusal(session, parsed, raw, domain, user, "", "no_space"))
        elif too_large:
            outcomes.append(_refusal(session, parsed, raw, domain, user, "", "too_large"))
        elif not parsed.has_content and too_complex(raw):
            outcomes.append(_refusal(session, parsed, raw, domain, user, "", "too_complex"))
        else:
            read_content(parsed, raw)
            for tag in aimed:
                outcomes.append(_drop(session, database, ws, actor, parsed, raw, domain, tag))
        user_id = user.id
    try:
        _answer(database, ws, conf, parsed, user_id, outcomes)
    except Exception:  # noqa: BLE001 — the note is dropped: an answer that fails changes nothing
        log.exception("Capture by email: answer not sent")
    statuses = {o.status for o in outcomes}
    if DEFERRED in statuses:
        return DEFERRED
    return ACCEPTED if ACCEPTED in statuses else REFUSED_STATUS


def _timely(parsed: Parsed, received: datetime | None) -> bool:
    """The email's date (signed with it) is within MAX_CLOCK_GAP of its reception."""
    reference = received or received_at(parsed.received) or datetime.now(UTC)
    return parsed.date is not None and abs(reference - parsed.date) <= MAX_CLOCK_GAP


def _refusal(
    session: Session,
    parsed: Parsed,
    raw: bytes,
    domain: str,
    user: User,
    tag: str,
    reason: str,
    *,
    space: Space | None = None,
    told: Label | None = None,
) -> _Outcome:
    log.info("Capture by email: refused (%s) for %s", reason, tag or "-")
    new = _record(
        session, _key(parsed, raw, tag), REFUSED_STATUS, reason, domain, tag, space=space, user=user
    )
    session.commit()
    return _Outcome(REFUSED_STATUS, reason, tag, space, told=told or _told(reason), new=new)


def _told(reason: str) -> Label:
    texts = {
        "no_space": Label(
            "The address names no space: write to the capture address of a space, with its "
            "organization and its name after “+” (shown on the space's Inbox page)."
        ),
        "no_access": Label(
            "This space does not exist, or your account cannot drop notes there (contributors "
            "only)."
        ),
        "space_not_ready": Label("This space is not synchronized with its repository yet."),
        "too_large": Label(
            "The email is too large: {size} MB at most, attachments included.",
            size=MAX_MESSAGE // (1024 * 1024),
        ),
        "too_many_attachments": Label(
            "Too many attachments: {limit} at most.", limit=deposits.MAX_ATTACHMENTS
        ),
        "stale_date": Label(
            "Its date is missing, or more than 48 hours away from when it arrived: an older "
            "email is not dropped (it could be replayed by someone else). Forward it instead: "
            "the forward gets today's date."
        ),
        "not_in_recipients": Label(
            "The capture address must be in To or Cc, not in a blind copy (Bcc)."
        ),
        "too_complex": Label(
            "The email has too many parts, or parts nested too deep, to be read: send its "
            "text and attachments in a new email."
        ),
        "unavailable": Label(
            "The space could not take it for 24 hours (busy or unavailable): send it again "
            "later, or tell an administrator."
        ),
    }
    return texts.get(reason, REASONS.get(reason, Label(reason)))


def _drop(
    session: Session,
    database: Database,
    ws: Workspace,
    actor: Actor,
    parsed: Parsed,
    raw: bytes,
    domain: str,
    tag: str,
) -> _Outcome:
    """Drops the email in the space `tag`, if the sender may."""
    user = actor.user
    key = _key(parsed, raw, tag)
    space = find_space(session, tag)
    if space is None or not can(
        actor.access, Action.DEPOSIT_SPACE, org_id=space.org_id, space_id=space.id
    ):
        return _refusal(session, parsed, raw, domain, user, tag, "no_access")  # same answer
    if _accepted(session, key):  # already dropped (a poll stopped before moving it)
        return _Outcome(ACCEPTED, target=tag, space=space, new=False)
    if len(parsed.attachments) > deposits.MAX_ATTACHMENTS:
        return _refusal(session, parsed, raw, domain, user, tag, "too_many_attachments")
    try:
        path = deposits.deposit(
            session,
            database,
            actor,
            ws,
            space,
            title=_title(parsed, user.locale),
            body=_body(parsed, user.locale),
            source="email",
            origin=_origin(parsed, user),
            attachments=parsed.attachments,
        )
    except SynaptaneError as error:
        session.rollback()
        if error.code in (ErrorCode.TOO_MANY, ErrorCode.CONFLICT, ErrorCode.UNAVAILABLE):
            log.info("Capture by email: %s deferred (%s)", tag, error.code)
            return _defer(session, parsed, raw, domain, user, tag, space)
        reason = {
            ErrorCode.SECRET: "secret",
            ErrorCode.FORBIDDEN: "no_access",
            ErrorCode.NOT_FOUND: "space_not_ready",
        }.get(error.code, "invalid")
        told: Label | None = None
        if reason == "secret":  # no "confirm" by email: the sender removes it
            told = Label(
                "It seems to contain a secret: {detail}. Remove it (keep it in a password "
                "manager) and send the email again.",
                detail=error.params.get("detail", ""),
            )
        elif reason == "invalid":
            told = Label(error.template, **error.params)
        return _refusal(session, parsed, raw, domain, user, tag, reason, space=space, told=told)
    except GitError as error:
        session.rollback()
        log.warning("Capture by email: %s deferred (%s)", tag, error)
        return _defer(session, parsed, raw, domain, user, tag, space)
    _record(session, key, ACCEPTED, "", domain, tag, space=space, user=user, path=path)
    session.commit()
    log.info("Capture by email: dropped in %s", space.address)
    return _Outcome(ACCEPTED, target=tag, space=space, path=path)


def _defer(
    session: Session,
    parsed: Parsed,
    raw: bytes,
    domain: str,
    user: User,
    tag: str,
    space: Space,
) -> _Outcome:
    """Leaves the email for a later poll, and remembers since when; refused (and its sender
    told) once it has waited MAX_DEFERRAL."""
    key, now = _key(parsed, raw, tag), datetime.now(UTC)
    values = {
        "status": DEFERRED,
        "reason": "",
        "sender_domain": domain[:255],
        "target": tag[:130],
        "space_id": space.id,
        "user_id": user.id,
        "path": None,
        "at": now,
    }
    statement = insert(EmailCapture).values(key=key, **values)
    session.execute(  # the first deferral counts, not the latest
        statement.on_conflict_do_update(
            index_elements=["key"], set_=values, where=EmailCapture.status != DEFERRED
        )
    )
    since = session.scalar(select(EmailCapture.at).where(EmailCapture.key == key)) or now
    session.commit()
    if now - since >= MAX_DEFERRAL:
        return _refusal(session, parsed, raw, domain, user, tag, "unavailable", space=space)
    return _Outcome(DEFERRED, target=tag, space=space)


def _answer(
    database: Database,
    ws: Workspace,
    conf: Conf,
    parsed: Parsed,
    user_id: object,
    outcomes: list[_Outcome],
) -> None:
    """Tells the authenticated sender, if the super admin chose so and email sending is set
    up: a confirmation (`all`), the reason of a refusal (`refusals` and `all`)."""
    if conf.replies == "off":
        return
    with database.session() as session:
        if not mail.configured(session):
            return
        user = session.get(User, user_id)
        if user is None or not user.email:
            return
        recent = session.scalar(
            select(func.count())
            .select_from(EmailCapture)
            .where(
                EmailCapture.user_id == user.id,
                EmailCapture.at >= datetime.now(UTC) - timedelta(hours=1),
            )
        )
        if (recent or 0) > MAX_ANSWERS_PER_HOUR:
            log.warning("Capture by email: answers to %s paused for the hour", user.username)
            return
        subject = parsed.subject or "—"
        for outcome in outcomes:
            if outcome.status == ACCEPTED and conf.replies == "all" and outcome.path:
                space = session.get(Space, outcome.space.id) if outcome.space else None
                if space is None:
                    continue
                name = outcome.path.removeprefix(INBOX_DIR + "/")
                link = f"{ws.public_url}/o/{space.organization.slug}/{space.slug}/inbox/{name}"
                title, body = mail.compose(
                    "capture_accepted",
                    user.locale,
                    space=space.name,
                    org=space.organization.name,
                    title=_title(parsed, user.locale),
                    subject=subject,
                    link=link,
                )
            elif (
                outcome.status == REFUSED_STATUS
                and outcome.new
                and outcome.reason in _TOLD
                and outcome.told is not None
            ):
                address = conf.address
                if outcome.target:
                    local, _, domain = conf.address.rpartition("@")
                    address = f"{local}+{outcome.target}@{domain}"
                title, body = mail.compose(
                    "capture_refused",
                    user.locale,
                    subject=subject,
                    address=address,
                    reason=outcome.told,
                )
            else:
                continue
            try:
                mail.send(session, ws.box, user.email, title, body, automatic=True)
            except mail.MailError as error:
                log.warning("Capture by email: answer not sent: %s", error)


# --- Health and metrics -----------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    at: datetime
    reason: Label
    sender_domain: str
    target: str


@dataclass(frozen=True)
class State:
    polled_at: datetime | None
    poll_ok_at: datetime | None
    error: Label | None
    accepted: int  # last 7 days
    refused: int
    latest: list[Refusal]
    interval: int = DEFAULT_INTERVAL  # minutes between two polls


def state(session: Session, now: datetime | None = None) -> State | None:
    """For the health page: last poll, emails of the last 7 days, the latest refusals. None
    if the capture is not configured."""
    row = session.get(CaptureSettings, 1)
    if row is None:
        return None
    since = (now or datetime.now(UTC)) - timedelta(days=7)
    counts = dict(
        session.execute(
            select(EmailCapture.status, func.count())
            .where(EmailCapture.at >= since)
            .group_by(EmailCapture.status)
        ).all()
    )
    latest = [
        Refusal(c.at, REASONS.get(c.reason, Label(c.reason)), c.sender_domain, c.target)
        for c in session.scalars(
            select(EmailCapture)
            .where(EmailCapture.status == REFUSED_STATUS, EmailCapture.at >= since)
            .order_by(EmailCapture.at.desc())
            .limit(10)
        )
    ]
    return State(
        row.polled_at,
        row.poll_ok_at,
        Label.loads(row.poll_error) if row.poll_error else None,
        counts.get(ACCEPTED, 0),
        counts.get(REFUSED_STATUS, 0),
        latest,
        row.interval,
    )


def counts_since(session: Session, since: datetime) -> dict[str, dict[str, int]]:
    """Emails handled since `since`: status → reason → number (metrics)."""
    found: dict[str, dict[str, int]] = {}
    for status, reason, count in session.execute(
        select(EmailCapture.status, EmailCapture.reason, func.count())
        .where(EmailCapture.at >= since)
        .group_by(EmailCapture.status, EmailCapture.reason)
    ):
        found.setdefault(status, {})[reason] = count
    return found
