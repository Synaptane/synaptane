"""A fake IMAP server in memory, in place of `imaplib.IMAP4` and `imaplib.IMAP4_SSL`: the
capture by email is tested without any real server (no network).

`install(monkeypatch)` returns the server; every connection opened by the code under test
reaches it. It answers like imaplib does (`(typ, data)`, data in bytes), records the commands
it received and can be told to fail (`refuse_login`, `drop_after`).
"""

import imaplib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

import pytest

# When the fake server received an email, by default: a minute after the default `Date`.
RECEIVED = datetime(2026, 11, 9, 9, 33, tzinfo=UTC)


class Socket:
    """What the code may do with an IMAP connection's socket: bound and cut it."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.shut = False

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def shutdown(self, how: int) -> None:
        self.shut = True


@dataclass
class Server:
    username: str = "brain@capture.test"
    password: str = "mot-de-passe-imap"
    capabilities: tuple[str, ...] = ("IMAP4rev1", "MOVE", "UIDPLUS")
    delimiter: str = "/"
    folders: dict[str, dict[int, bytes]] = field(default_factory=lambda: {"INBOX": {}})
    next_uid: int = 1
    commands: list[str] = field(default_factory=list)
    connections: list["FakeIMAP"] = field(default_factory=list)
    refuse_login: bool = False
    refuse_create: set[str] = field(default_factory=set)
    drop_after: int | None = None  # commands before the connection is lost
    delay: float = 0.0  # seconds each command takes (a slow server)
    internaldates: dict[int, datetime | None] = field(default_factory=dict)

    def deliver(
        self, raw: bytes, folder: str = "INBOX", received: datetime | None = RECEIVED
    ) -> int:
        """`received`: the INTERNALDATE of the email (None: the server gives none)."""
        uid = self.next_uid
        self.next_uid += 1
        self.folders.setdefault(folder, {})[uid] = raw
        self.internaldates[uid] = received
        return uid

    def inbox(self) -> list[bytes]:
        return list(self.folders["INBOX"].values())

    def folder(self, name: str) -> list[bytes]:
        return list(self.folders.get(name, {}).values())


def _unquote(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


class FakeIMAP:
    error = imaplib.IMAP4.error
    abort = imaplib.IMAP4.abort
    server: Server  # set by `install`

    def __init__(
        self,
        host: str = "",
        port: int = 143,
        timeout: float | None = None,
        ssl_context: object = None,
    ) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.tls = ssl_context is not None
        self.selected: str | None = None
        self.readonly = False
        self.logged_in = False
        self.sock = Socket()
        self.server.connections.append(self)

    def _log(self, command: str) -> None:
        server = self.server
        server.commands.append(command)
        if server.delay:
            time.sleep(server.delay)
        if self.sock.shut:
            raise OSError("socket shut down")
        if server.drop_after is not None and len(server.commands) > server.drop_after:
            raise imaplib.IMAP4.abort("connection lost")

    def starttls(self, ssl_context: object = None) -> tuple[str, list[bytes]]:
        self._log("STARTTLS")
        self.tls = True
        return "OK", [b"Begin TLS"]

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self._log(f"LOGIN {user}")  # never the password
        if self.server.refuse_login or (user, password) != (
            self.server.username,
            self.server.password,
        ):
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logged_in = True
        return "OK", [b"Logged in"]

    def capability(self) -> tuple[str, list[bytes]]:
        self._log("CAPABILITY")
        return "OK", [" ".join(self.server.capabilities).encode()]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        name = _unquote(mailbox)
        self._log(f"SELECT {name}")
        if name not in self.server.folders:
            return "NO", [b"Mailbox does not exist"]
        self.selected, self.readonly = name, readonly
        return "OK", [str(len(self.server.folders[name])).encode()]

    def create(self, mailbox: str) -> tuple[str, list[bytes]]:
        name = _unquote(mailbox)
        self._log(f"CREATE {name}")
        if name in self.server.refuse_create:
            return "NO", [b"[CANNOT] Not allowed"]
        self.server.folders.setdefault(name, {})
        return "OK", [b"Created"]

    def expunge(self) -> tuple[str, list[bytes | None]]:
        self._log("EXPUNGE")
        folder = self._folder()
        for uid in [u for u in folder if u in self._deleted]:
            del folder[uid]
        return "OK", [None]

    _deleted: set[int] = set()

    def _folder(self) -> dict[int, bytes]:
        assert self.selected is not None, "no folder selected"
        return self.server.folders[self.selected]

    def uid(self, command: str, *args: str) -> tuple[str, list[object]]:
        command = command.upper()
        self._log(f"UID {command} {' '.join(args)}")
        folder = self._folder()
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(folder)).encode()]
        uid = int(args[0])
        if uid not in folder:
            return "OK", [None]
        raw = folder[uid]
        if command == "FETCH":
            part = args[1]
            if "RFC822.SIZE" in part:
                when = self.server.internaldates.get(uid)
                date = f' INTERNALDATE "{when:%d-%b-%Y %H:%M:%S %z}"' if when else ""
                return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)}{date})".encode()]
            head, _, _ = (
                raw.partition(b"\r\n\r\n") if b"\r\n\r\n" in raw else raw.partition(b"\n\n")
            )
            if "HEADER.FIELDS" in part:
                names = re.findall(r"[A-Z-]+", part.split("(", 2)[2].upper())
                lines = re.split(rb"\r?\n(?![ \t])", head)
                kept = [ln for ln in lines if ln.split(b":", 1)[0].upper().decode() in names]
                data = b"\r\n".join(kept) + b"\r\n\r\n"
            elif "HEADER" in part:
                data = head + b"\r\n\r\n"
            else:
                data = raw
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(data)}}}".encode(), data), b")"]
        if command in ("MOVE", "COPY"):
            target = _unquote(args[1])
            if target not in self.server.folders:
                return "NO", [b"[TRYCREATE] No such mailbox"]
            self.server.deliver(raw, target)
            if command == "MOVE":
                del folder[uid]
            return "OK", [None]
        if command == "STORE":
            self._deleted = {*self._deleted, uid}
            return "OK", [None]
        if command == "EXPUNGE":
            if uid in self._deleted:
                del folder[uid]
            return "OK", [None]
        return "BAD", [b"unknown command"]

    def logout(self) -> tuple[str, list[bytes]]:
        self._log("LOGOUT")
        return "BYE", [b"Logging out"]

    # Last: in the class body, `list` names this method from here on.
    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, list[bytes | None]]:
        self._log(f"LIST {directory} {pattern}")
        wanted = _unquote(pattern)
        delimiter = self.server.delimiter
        if wanted == "":
            return "OK", [f'(\\Noselect) "{delimiter}" ""'.encode()]
        if wanted in self.server.folders:
            return "OK", [f'(\\HasNoChildren) "{delimiter}" "{wanted}"'.encode()]
        return "OK", [None]


def install(monkeypatch: pytest.MonkeyPatch, server: Server | None = None) -> Server:
    server = server or Server()
    fake = type("BoundFakeIMAP", (FakeIMAP,), {"server": server})
    monkeypatch.setattr(imaplib, "IMAP4", fake)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake)
    return server


CAPTURE = "brain@capture.test"
TRUSTED = "mx.capture.test"


def message(
    *,
    sender: str = "Alice <alice@acme.test>",
    to: str = "brain+acme.infra@capture.test",
    subject: str = "Nouveau NAS",
    body: str = "Le NAS passe à 8 To.",
    html: str | None = None,
    auth: str | list[str] | None = f"{TRUSTED}; dkim=pass header.d=acme.test; dmarc=pass "
    "header.from=acme.test",
    headers: dict[str, str] | None = None,
    attachments: list[tuple[str, str, bytes]] = (),  # type: ignore[assignment]
    message_id: str | None = None,
    date: datetime = datetime(2026, 11, 9, 9, 32, tzinfo=UTC),
    cc: str | None = None,
) -> bytes:
    """An email as the capture mailbox receives it: the receiving server's
    Authentication-Results header on top (`auth`: one or several, topmost first)."""
    mail = EmailMessage()
    for value in [auth] if isinstance(auth, str) else (auth or []):
        mail["Authentication-Results"] = value
    for name, value in (headers or {}).items():
        mail[name] = value
    mail["From"] = sender
    mail["To"] = to
    mail["Subject"] = subject
    if cc:
        mail["Cc"] = cc
    mail["Date"] = format_datetime(date)
    mail["Message-ID"] = message_id or make_msgid(domain="acme.test")
    mail.set_content(body)
    if html is not None:
        mail.add_alternative(html, subtype="html")
    for name, kind, data in attachments:
        maintype, subtype = kind.split("/", 1)
        mail.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return mail.as_bytes()
