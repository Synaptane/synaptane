"""Capture by email, end to end: settings, poll of a fake IMAP mailbox (`tests.fakeimap`, no
network), notes dropped, refusals, idempotence, answers, health, metrics, web pages.

Acme's "infra" space (wiki fixture): Alice contributes, Bob reads; "autre" is visible to
neither; root is super admin, member of nothing. Each has a verified address at acme.test.
"""

import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole, SpaceRole
from synaptane.core.errors import SynaptaneError, conflict
from synaptane.core.metrics import Exposition
from synaptane.git.repo import GitError
from synaptane.services import (
    activity,
    deposits,
    email_capture,
    groups,
    instance,
    mail,
    metrics,
    organizations,
    spaces,
)
from synaptane.services.inbox import InboxItem
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, CaptureSettings, EmailCapture, Job, Space, User
from tests.fakeimap import CAPTURE, TRUSTED, Server, install, message
from tests.integration.conftest import make_user
from tests.integration.gitutil import git
from tests.integration.test_invitations import Inbox
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

PROCESSED = "Synaptane/Processed"
REFUSED = "Synaptane/Refused"


def verify(db: Session, user: User, address: str) -> None:
    user.email, user.email_verified_at = address, datetime.now(UTC)
    db.commit()


@pytest.fixture
def server(db: Session, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch) -> Server:
    """The fake mailbox, and the capture configured to read it."""
    found = install(monkeypatch)
    email_capture.configure(
        db,
        wiki.root,
        ctx.box,
        host="imap.capture.test",
        port=993,
        security="ssl",
        username=found.username,
        password=found.password,
        address=CAPTURE,
        authserv_ids=TRUSTED,
    )
    for actor, address in ((wiki.alice, "alice"), (wiki.bob, "bob"), (wiki.root, "root")):
        verify(db, actor.user, f"{address}@acme.test")
    return found


@pytest.fixture
def smtp(db: Session, wiki: Wiki, ctx: Workspace) -> Iterator[Inbox]:
    """Email sending, to a test SMTP server: the answers to senders arrive there."""
    inbox = Inbox()
    threading.Thread(target=inbox.serve_forever, daemon=True).start()
    mail.configure(
        db,
        wiki.root,
        ctx.box,
        host="127.0.0.1",
        port=inbox.server_address[1],
        security="none",
        sender="synaptane@example.com",
    )
    db.commit()
    yield inbox
    inbox.shutdown()
    inbox.server_close()


def subject_of(received: Message) -> str:
    return str(make_header(decode_header(received["Subject"])))


def body_of(received: Message) -> str:
    payload = received.get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload.decode(received.get_content_charset() or "utf-8")


def notes(wiki: Wiki, ctx: Workspace, space: Space | None = None) -> list[InboxItem]:
    return deposits.list_items(wiki.root, ctx, space or wiki.infra)


def poll(database: Database, ctx: Workspace) -> email_capture.Summary:
    summary = email_capture.poll(database, ctx)
    assert summary.error == "", summary.error
    return summary


def reasons(db: Session) -> list[str]:
    db.expire_all()
    return [c.reason for c in db.scalars(select(EmailCapture).order_by(EmailCapture.at))]


# --- Accepted ---------------------------------------------------------------------------------


def test_an_authenticated_email_becomes_a_note_of_its_author(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    server.deliver(
        message(
            subject="Fwd: Nouveau NAS",
            body="Le NAS passe à **8 To**.",
            attachments=[("devis.pdf", "application/pdf", b"%PDF-1.4 devis")],
        )
    )
    summary = poll(database, ctx)
    assert (summary.accepted, summary.refused, summary.deferred) == (1, 0, 0)
    [note] = notes(wiki, ctx)
    assert note.title == "Nouveau NAS"  # forwarding prefix removed
    assert note.source == "email" and note.via == ""
    assert note.deposited_by.startswith("Alice <alice@acme.test>")
    # Alice reads French; her name, not her address (shown to every reader of the space).
    assert note.origin == "E-mail de Alice, 2026-11-09 09:32 UTC"
    assert "8 To" in note.body and note.attachments == ["devis.pdf"]
    assert deposits.read_attachment(wiki.bob, ctx, wiki.infra, note.name, "devis.pdf") == (
        b"%PDF-1.4 devis"
    )
    # Moved out of the folder read, into the folder of handled emails, created for it.
    assert server.inbox() == [] and len(server.folder(PROCESSED)) == 1
    assert "CREATE Synaptane/Processed" in server.commands
    assert all(c.tls for c in server.connections)  # SSL/TLS
    assert not any("mot-de-passe-imap" in c for c in server.commands)
    # The commit is the author's, through email: a person, not an AI.
    clone = ctx.clone_path(wiki.infra.id)
    assert git(clone, "log", "-1", "--format=%an|%s|%(trailers:key=Source,valueonly)") == (
        "Alice|Inbox: Nouveau NAS|email"
    )
    [event] = activity.space_activity(wiki.alice, ctx, wiki.infra)[0][:1]
    assert (event.origin, event.ai, event.kind) == ("email", False, "inbox")
    # Same audit and rate limit as every drop.
    deposited = db.scalar(
        select(func.count()).where(
            AuditEvent.action == "inbox.deposited", AuditEvent.actor_id == wiki.alice.user.id
        )
    )
    assert deposited == 1
    [capture] = db.scalars(select(EmailCapture)).all()
    assert (capture.status, capture.sender_domain, capture.target) == (
        "accepted",
        "acme.test",
        "acme.infra",
    )
    assert capture.path == f".synaptane/inbox/{note.name}"
    # Health: the last poll worked.
    db.expire_all()
    row = db.get(CaptureSettings, 1)
    assert row is not None and row.poll_ok_at is not None and row.poll_error is None


def test_html_email_and_several_spaces(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    spaces.set_member(db, wiki.root, wiki.other, wiki.alice.user, SpaceRole.CONTRIBUTOR)
    db.commit()
    raw = message(
        to="brain+acme.infra@capture.test",
        headers={"Cc": "Brain <BRAIN+ACME.AUTRE@capture.test>"},
        body="",
        html='<p>Bonjour <img src="https://track.test/p.gif"><a href="https://ok.test">ici</a>'
        "</p><script>alert(1)</script>",
        subject="",
    )
    server.deliver(raw)
    assert poll(database, ctx).accepted == 1
    for space in (wiki.infra, wiki.other):
        [note] = notes(wiki, ctx, space)
        assert note.title == "E-mail sans objet"
        assert note.body.strip() == "Bonjour [ici](https://ok.test)"
    assert reasons(db) == ["", ""]


def test_a_blind_copy_is_refused_and_the_envelope_picks_the_copy(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server, smtp: Inbox
) -> None:
    """The capture address must be in To or Cc: a signed email replayed in blind copy to the
    capture is not dropped. When To and Cc name two spaces, each copy goes to its own."""
    server.deliver(
        message(to="equipe@acme.test", headers={"Delivered-To": "brain+acme.infra@capture.test"})
    )
    assert poll(database, ctx).refused == 1
    assert reasons(db) == ["not_in_recipients"] and notes(wiki, ctx) == []
    [answer] = smtp.messages  # Alice may have used Bcc by mistake: told how to write
    assert "copie cachée" in body_of(answer)
    spaces.set_member(db, wiki.root, wiki.other, wiki.alice.user, SpaceRole.CONTRIBUTOR)
    db.commit()
    raw = message(
        message_id="<deux@acme.test>",
        to="brain+acme.infra@capture.test",
        cc="brain+acme.autre@capture.test",
    )
    for copy in ("acme.infra", "acme.autre"):
        server.deliver(f"Delivered-To: brain+{copy}@capture.test\r\n".encode() + raw)
    assert poll(database, ctx).accepted == 2
    assert len(notes(wiki, ctx)) == 1 and len(notes(wiki, ctx, wiki.other)) == 1


@pytest.mark.parametrize(
    "name", ["Subject", "To", "Cc", "Date", "Message-ID", "Reply-To", "References"]
)
def test_a_replayed_email_with_a_header_added_on_top_is_refused_in_silence(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    smtp: Inbox,
    name: str,
) -> None:
    """Someone resends Alice's authentic, signed email, with a header of theirs added above
    the original (Python reads the first, DKIM the last)."""
    original = {"Reply-To": "alice@acme.test", "References": "<fil@acme.test>"}
    raw = message(cc="bob@acme.test", headers=original)
    added = {
        "To": "brain+acme.autre@capture.test",
        "Cc": "brain+acme.autre@capture.test",
        "Date": "Mon, 9 Nov 2026 09:33:00 +0000",
    }.get(name, "<x@evil.test>" if name in ("Message-ID", "References") else "Remplacé")
    after_auth = raw.index(b"\nFrom:") + 1  # below the receiving server's header
    forged = raw[:after_auth] + f"{name}: {added}\r\n".encode() + raw[after_auth:]
    server.deliver(forged)
    assert poll(database, ctx).refused == 1
    assert reasons(db) == ["duplicate_headers"] and notes(wiki, ctx) == []
    assert smtp.messages == []


def test_an_old_email_is_refused(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server, smtp: Inbox
) -> None:
    """An authentic email replayed days later keeps its signed date: refused. The reception is
    the IMAP INTERNALDATE, otherwise the topmost Received header."""
    old = datetime(2026, 11, 6, 9, 32, tzinfo=UTC)
    server.deliver(message(date=old, subject="Ancien"))
    later = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)  # 47 h 28 after its date: accepted
    server.deliver(message(date=old, subject="Encore bon"), received=later)
    received = (
        "Received: from mail.acme.test by mx.capture.test; Fri, 6 Nov 2026 10:00:00 +0000\r\n"
    )
    server.deliver(
        received.encode() + message(date=old, subject="Sans INTERNALDATE"), received=None
    )
    no_date = message().replace(b"Date: ", b"X-Old-Date: ", 1)
    server.deliver(no_date)
    poll(database, ctx)
    assert reasons(db) == ["stale_date", "", "", "stale_date"]
    assert sorted(n.title for n in notes(wiki, ctx)) == ["Encore bon", "Sans INTERNALDATE"]
    told = [body_of(m) for m in smtp.messages]
    assert len(told) == 2 and all("48 heures" in t for t in told)


def test_a_replay_hiding_its_duplicates_below_a_broken_line_is_refused(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server, smtp: Inbox
) -> None:
    """Headers of one's own (To, Subject, Date), a line that is not a header, then the signed
    email: Python reads only the added headers and finds no duplicate."""
    signed = message(to="eve@evil.test", subject="Original")
    added = (
        f"Authentication-Results: {FORGED}\r\nFrom: alice@acme.test\r\n"
        "To: brain+acme.infra@capture.test\r\nSubject: Choisi\r\n"
        "Date: Mon, 09 Nov 2026 09:32:00 +0000\r\nnot a header\r\n"
    ).encode()
    server.deliver(added + signed.split(b"\n", 1)[1])  # below the server's own header
    assert poll(database, ctx).refused == 1
    assert reasons(db) == ["malformed_headers"] and notes(wiki, ctx) == []
    assert smtp.messages == []


def _timers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if isinstance(t, threading.Timer) and t.is_alive()]


def test_the_connection_is_closed_and_the_watchdog_stopped_whatever_happens(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    """imaplib leaves the socket open when its LOGOUT fails; STARTTLS refused after the
    connection opened: closed all the same, and no watchdog left behind."""
    server.refuse_logout = True
    server.deliver(message())
    assert poll(database, ctx).accepted == 1
    assert server.connections[-1].closed and _timers() == []
    row = db.get(CaptureSettings, 1)
    assert row is not None
    row.security, row.port = "starttls", 143
    db.commit()
    server.refuse_starttls = True
    assert "STARTTLS refused" in email_capture.poll(database, ctx).error
    assert server.connections[-1].closed and _timers() == []
    with pytest.raises(email_capture.CaptureError):
        email_capture.test(db, wiki.root, ctx.box, allow_plain=False)
    assert server.connections[-1].closed and _timers() == []


def test_the_poll_never_lasts_beyond_its_deadline(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every operation answers just in time: the poll still stops at POLL_DEADLINE, its socket
    bounded by what is left and cut by the watchdog."""
    monkeypatch.setattr(email_capture, "POLL_DEADLINE", 1.0)
    monkeypatch.setattr(email_capture, "POLL_BUDGET", 60.0)
    server.delay = 0.12
    for index in range(10):
        server.deliver(message(subject=f"Note {index}"))
    started = time.monotonic()
    summary = email_capture.poll(database, ctx)
    assert time.monotonic() - started < 5.0  # 1 s, give or take a loaded machine
    assert "deadline" in summary.error or "shut down" in summary.error
    [connection] = server.connections
    assert connection.sock.timeouts and max(connection.sock.timeouts) <= 1.0
    assert connection.sock.timeouts[-1] < connection.sock.timeouts[0]  # shrinks over time
    assert len(server.inbox()) > 0  # the rest waits for the next poll
    server.delay = 0
    monkeypatch.setattr(email_capture, "POLL_DEADLINE", 300.0)
    waiting = len(server.inbox())
    assert poll(database, ctx).accepted == waiting
    assert len(notes(wiki, ctx)) == 10


def test_the_same_email_is_dropped_once(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    """Two copies (same Message-ID), and an email whose move failed after its drop: a single
    note each time."""
    raw = message(message_id="<unique@acme.test>")
    server.deliver(raw)
    server.deliver(raw)
    summary = poll(database, ctx)
    assert summary.accepted == 2 and len(notes(wiki, ctx)) == 1
    assert len(server.folder(PROCESSED)) == 2 and server.inbox() == []
    server.deliver(raw)  # put back in the folder by hand, or a move that failed
    assert poll(database, ctx).accepted == 1
    assert len(notes(wiki, ctx)) == 1
    # Another member reusing that Message-ID does not pass for a copy of it, nor block it.
    spaces.set_member(db, wiki.root, wiki.infra, wiki.bob.user, SpaceRole.CONTRIBUTOR)
    db.commit()
    server.deliver(message(sender="bob@acme.test", message_id="<unique@acme.test>"))
    assert poll(database, ctx).accepted == 1
    assert len(notes(wiki, ctx)) == 2


def test_a_move_without_the_move_extension(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    server.capabilities = ("IMAP4rev1",)  # neither MOVE nor UIDPLUS: COPY, STORE, EXPUNGE
    server.delimiter = "."
    server.deliver(message())
    assert poll(database, ctx).accepted == 1
    assert server.inbox() == [] and len(server.folder("Synaptane.Processed")) == 1
    assert any(c.startswith("UID COPY") for c in server.commands) and "EXPUNGE" in server.commands


# --- Refused ----------------------------------------------------------------------------------

FORGED = f"{TRUSTED}; dkim=pass header.d=acme.test; dmarc=pass header.from=acme.test"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (message(auth=None), "not_authenticated"),  # nothing proves the From
        (message(auth="mx.evil.test; dmarc=pass header.from=acme.test"), "not_authenticated"),
        # The sender wrote a "pass" under the receiving server's real "fail".
        (
            message(auth=[f"{TRUSTED}; dmarc=fail header.from=acme.test", FORGED]),
            "not_authenticated",
        ),
        # A foreign server's header on top: the trusted one below is the sender's.
        (message(auth=["mx.other.test; none", FORGED]), "not_authenticated"),
        (message(auth=f"{TRUSTED}; dkim=pass header.d=evil.test"), "not_authenticated"),
        (message(auth=f"{TRUSTED}; spf=pass smtp.mailfrom=alice@acme.test"), "not_authenticated"),
        (message(auth="dmarc=pass header.from=acme.test"), "not_authenticated"),  # no identifier
        (message(sender="Eve <eve@acme.test>"), "unknown_sender"),
        (message(sender="Alice <alice@acme.test>, Eve <eve@evil.test>"), "malformed"),
        (message(headers={"Auto-Submitted": "auto-replied"}), "automatic"),
        (message(headers={"List-Id": "<team.acme.test>"}), "automatic"),
    ],
)
def test_unauthenticated_or_unknown_senders_are_refused_in_silence(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    smtp: Inbox,
    raw: bytes,
    reason: str,
) -> None:
    server.deliver(raw)
    summary = poll(database, ctx)
    assert (summary.accepted, summary.refused) == (0, 1)
    assert notes(wiki, ctx) == []
    assert server.inbox() == [] and len(server.folder(REFUSED)) == 1
    assert reasons(db) == [reason]
    assert smtp.messages == []  # never an answer: no backscatter


FORGERIES = {
    # The sender's own header, below the receiving server's (which found nothing).
    "below": message(auth=[f"{TRUSTED}; dmarc=none", FORGED]),
    # Another server's header on top: what is below is not looked at.
    "other_server": message(auth=["mx.evil.test; dmarc=pass header.from=acme.test", FORGED]),
    "lookalike_id": message(auth=f"{TRUSTED}.evil.test; dmarc=pass header.from=acme.test"),
    "prefixed_id": message(auth=f"x{TRUSTED}; dmarc=pass header.from=acme.test"),
    # The server's header folded over several lines still counts, and fails.
    "folded": (
        f"Authentication-Results: {TRUSTED};\r\n\tdmarc=fail\r\n header.from=acme.test\r\n"
    ).encode()
    + message(auth=FORGED),
    "dmarc_other_domain": message(auth=f"{TRUSTED}; dmarc=pass header.from=evil.test"),
    # Alice for Python, Eve for the receiving server: DMARC passed for evil.test.
    "display_name": message(
        sender="alice@acme.test <eve@evil.test>",
        auth=f"{TRUSTED}; dkim=pass header.d=evil.test; dmarc=pass",
    ),
    "encoded_word": message(
        sender="=?utf-8?b?YWxpY2VAYWNtZS50ZXN0?= <eve@evil.test>",
        auth=f"{TRUSTED}; dmarc=pass header.from=evil.test",
    ),
    "parent_tld": message(auth=f"{TRUSTED}; dkim=pass header.d=test"),
    "comment": message(
        auth=f'{TRUSTED}; spf=pass (domain of "a) ; dmarc=pass header.from=acme.test (b"@evil.test)'
        " smtp.mailfrom=b@evil.test"
    ),
    "two_dmarc": message(
        auth=f"{TRUSTED}; spf=pass smtp.helo=x;dmarc=pass header.from=acme.test; dmarc=none "
        "header.from=acme.test"
    ),
}


@pytest.mark.parametrize("name", list(FORGERIES))
def test_forged_authentication_results_are_refused(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    smtp: Inbox,
    name: str,
) -> None:
    server.deliver(FORGERIES[name])
    assert poll(database, ctx).refused == 1
    assert reasons(db) == ["not_authenticated"] and notes(wiki, ctx) == []
    assert smtp.messages == []


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        # Two From headers: refused before even reading them (a header added above).
        (message().replace(b"From: ", b"From: eve@evil.test\nFrom: ", 1), "duplicate_headers"),
        (message(sender="g: alice@acme.test, eve@evil.test;"), "malformed"),
        (message(sender="\u0430lice@acme.test"), "unknown_sender"),  # Cyrillic "а"
        (message(sender="alice+capture@acme.test"), "unknown_sender"),
        (message(sender="ALICE@ACME.TEST", subject="Casse"), ""),  # the same address
    ],
)
def test_the_sender_is_exactly_the_verified_address(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    smtp: Inbox,
    raw: bytes,
    reason: str,
) -> None:
    server.deliver(raw)
    poll(database, ctx)
    assert reasons(db) == [reason] and smtp.messages == []


def test_an_envelope_header_written_by_the_sender_aims_at_nothing(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    """Anyone can write `Delivered-To` in their own email: it only picks among the spaces of
    To and Cc, never adds one."""
    forged = {"Delivered-To": "brain+acme.autre@capture.test"}
    server.deliver(message(headers=forged))
    assert poll(database, ctx).accepted == 1
    assert len(notes(wiki, ctx)) == 1 and notes(wiki, ctx, wiki.other) == []


def test_a_space_unavailable_for_a_day_refuses_the_email(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    smtp: Inbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository that keeps failing must not have its emails read again at every poll
    forever: after MAX_DEFERRAL, refused, and the sender told."""

    def broken(*_args: object, **_kwargs: object) -> str:
        raise GitError("push refused")

    monkeypatch.setattr(deposits, "deposit", broken)
    server.deliver(message(subject="Bloqué"))
    assert poll(database, ctx).deferred == 1
    db.expire_all()
    [row] = db.scalars(select(EmailCapture)).all()
    first = row.at
    assert poll(database, ctx).deferred == 1  # the first deferral is what counts
    db.expire_all()
    assert db.scalars(select(EmailCapture)).one().at == first
    row.at = datetime.now(UTC) - email_capture.MAX_DEFERRAL - timedelta(minutes=1)
    db.commit()
    assert poll(database, ctx).refused == 1
    assert reasons(db) == ["unavailable"] and server.inbox() == []
    [answer] = smtp.messages
    assert "pendant 24 heures" in body_of(answer)


def test_emails_left_for_later_do_not_hold_back_the_others(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = deposits.deposit

    def busy(*args: object, **kwargs: object) -> str:
        if kwargs.get("title") == "Bloqué":
            raise conflict("The space is busy (synchronization in progress). Try again.")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(deposits, "deposit", busy)
    monkeypatch.setattr(email_capture, "MAX_PER_POLL", 1)
    server.deliver(message(subject="Bloqué"))
    server.deliver(message(subject="Bloqué"))
    server.deliver(message(subject="Bon"))
    summary = poll(database, ctx)
    assert (summary.accepted, summary.deferred) == (1, 2)
    assert [n.title for n in notes(wiki, ctx)] == ["Bon"] and len(server.inbox()) == 2


def test_answers_to_an_account_are_capped_per_hour(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    smtp: Inbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auto-responder that ignores `Auto-Submitted` and answers each refusal would
    otherwise exchange emails with the capture forever."""
    monkeypatch.setattr(email_capture, "MAX_ANSWERS_PER_HOUR", 2)
    for _ in range(4):
        server.deliver(message(sender="bob@acme.test"))  # a reader: refused, told why
    assert poll(database, ctx).refused == 4
    assert len(smtp.messages) == 2


def test_a_too_complex_email_is_refused_without_being_parsed(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server, smtp: Inbox
) -> None:
    levels = 1000  # 67 KB: half a minute of Python's parser
    parts = "".join(
        f'--b{i - 1}\nContent-Type: multipart/mixed; boundary="b{i}"\n\n' for i in range(1, levels)
    )
    bomb = message().replace(
        b"Content-Type: text/plain",
        f'Content-Type: multipart/mixed; boundary="b0"\n\n{parts}--b{levels - 1}\n'
        "Content-Type: text/plain".encode(),
        1,
    )
    server.deliver(bomb.replace(b"alice@", b"inconnu@"))  # unknown: headers only, silent
    server.deliver(bomb)
    assert poll(database, ctx).refused == 2
    assert reasons(db) == ["unknown_sender", "too_complex"]
    [answer] = smtp.messages
    assert "trop de parties" in body_of(answer)


def test_an_unexpected_error_of_the_mailbox_is_kept_without_its_words(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def odd(_self: object) -> list[str]:
        raise ValueError(f"odd answer, {server.password}")

    monkeypatch.setattr(email_capture.Mailbox, "uids", odd)
    summary = email_capture.poll(database, ctx)
    assert "ValueError" in summary.error and server.password not in summary.error
    db.expire_all()
    row = db.get(CaptureSettings, 1)
    assert row is not None and row.poll_error and server.password not in row.poll_error
    with pytest.raises(email_capture.CaptureError) as caught:
        email_capture.test(db, wiki.root, ctx.box, allow_plain=False)
    assert server.password not in str(caught.value.label.params)
    conf = email_capture._conf(row, ctx.box)
    assert conf.password == server.password and server.password not in repr(conf)


def test_account_checks_disabled_unverified_super_admin(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server, smtp: Inbox
) -> None:
    alice = wiki.alice.user
    alice.email_verified_at = None  # address not verified: not this account's
    db.commit()
    server.deliver(message())
    poll(database, ctx)
    verify(db, alice, "alice@acme.test")
    alice.is_active = False  # disabled account
    db.commit()
    server.deliver(message())
    poll(database, ctx)
    assert reasons(db) == ["unknown_sender", "unknown_sender"] and smtp.messages == []
    # The super admin reads every space, but only drops by email where they are a member.
    server.deliver(message(sender="root@acme.test"))
    poll(database, ctx)
    assert reasons(db)[-1] == "no_access"
    assert notes(wiki, ctx) == []
    [answer] = smtp.messages  # authenticated, with an account: told why
    assert answer["To"] == "root@acme.test"


@pytest.mark.parametrize(
    ("sender", "to", "reason"),
    [
        ("bob@acme.test", "brain+acme.infra@capture.test", "no_access"),  # reader
        ("alice@acme.test", "brain+acme.autre@capture.test", "no_access"),  # not a member
        ("alice@acme.test", "brain+acme.inconnu@capture.test", "no_access"),  # no such space
        ("alice@acme.test", "brain+acme.Infra.x@capture.test", "no_access"),
        ("alice@acme.test", "brain@capture.test", "no_space"),
    ],
)
def test_authenticated_members_are_told_why(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    smtp: Inbox,
    sender: str,
    to: str,
    reason: str,
) -> None:
    server.deliver(message(sender=sender, to=to, subject="Mon idée"))
    assert poll(database, ctx).refused == 1
    assert reasons(db) == [reason] and notes(wiki, ctx) == []
    [answer] = smtp.messages
    assert answer["To"] == sender
    assert answer["Auto-Submitted"] == "auto-replied"  # no robot answers it back
    assert subject_of(answer) == "Non déposé dans Synaptane : Mon idée"
    text = body_of(answer)
    if reason == "no_space":
        assert "n'indique aucun espace" in text
    else:
        assert "ne peut pas y déposer de notes" in text
    # Polled again (put back in the folder): refused again, but told only once.
    server.deliver(message(sender=sender, to=to, subject="Mon idée", message_id=None))
    poll(database, ctx)
    assert len(smtp.messages) == 2  # another email (another Message-ID): told again


def test_a_refusal_is_told_once_per_email(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server, smtp: Inbox
) -> None:
    raw = message(sender="bob@acme.test", message_id="<idee@acme.test>")
    server.deliver(raw)
    poll(database, ctx)
    server.deliver(raw)
    poll(database, ctx)
    assert len(smtp.messages) == 1


def test_restricted_spaces_groups_and_default_roles(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    """Same rules as every drop: the organization's default role gives no access to a
    restricted space; a group's role does."""
    acme = wiki.infra.organization
    dora = make_user(db, "dora")
    verify(db, dora, "dora@acme.test")
    organizations.set_member(db, wiki.root, acme, dora, OrgRole.MEMBER)
    organizations.update_organization(db, wiki.root, acme, default_space_role="contributor")
    db.commit()
    server.deliver(message(sender="dora@acme.test", subject="Par défaut"))
    assert poll(database, ctx).accepted == 1
    spaces.update_space(db, wiki.root, wiki.infra, restricted=True)
    db.commit()
    server.deliver(message(sender="dora@acme.test", subject="Restreint"))
    assert poll(database, ctx).refused == 1
    group = groups.create_group(db, wiki.root, acme, name="Infra")
    groups.add_member(db, wiki.root, group, dora)
    groups.set_space_role(db, wiki.root, group, wiki.infra, SpaceRole.CONTRIBUTOR)
    db.commit()
    server.deliver(message(sender="dora@acme.test", subject="Par le groupe"))
    assert poll(database, ctx).accepted == 1
    assert sorted(n.title for n in notes(wiki, ctx)) == ["Par défaut", "Par le groupe"]
    assert reasons(db) == ["", "no_access", ""]


def test_content_refusals_secret_too_many_attachments_too_large(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    smtp: Inbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server.deliver(message(body="Clé : AKIAABCDEFGHIJKLMNOP", subject="Accès"))
    attachments = [(f"f{i}.txt", "text/plain", b"x") for i in range(11)]
    server.deliver(message(attachments=attachments, subject="Pièces"))
    monkeypatch.setattr(email_capture, "MAX_MESSAGE", 20_000)
    server.deliver(message(body="x" * 50_000, subject="Gros"))
    fetched: list[str] = []
    summary = poll(database, ctx)
    fetched = [c for c in server.commands if "BODY.PEEK[]" in c]
    assert summary.refused == 3 and notes(wiki, ctx) == []
    assert reasons(db) == ["secret", "too_many_attachments", "too_large"]
    assert len(fetched) == 2  # the large one: its headers only
    told = {subject_of(m): body_of(m) for m in smtp.messages}
    assert "clé d'accès AWS" in told["Non déposé dans Synaptane : Accès"]
    assert "renvoyez l'e-mail" in told["Non déposé dans Synaptane : Accès"]
    assert "10 au maximum" in told["Non déposé dans Synaptane : Pièces"]
    assert "Mo au maximum" in told["Non déposé dans Synaptane : Gros"]
    db.expire_all()
    stored = " ".join(
        f"{c.reason} {c.target} {c.sender_domain}" for c in db.scalars(select(EmailCapture))
    )
    assert "AKIA" not in stored and "Accès" not in stored  # neither content nor subject


def test_answers_can_be_confirmations_or_nothing(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server, smtp: Inbox
) -> None:
    row = db.get(CaptureSettings, 1)
    assert row is not None
    row.replies = "all"
    db.commit()
    server.deliver(message(subject="Compte rendu"))
    poll(database, ctx)
    [answer] = smtp.messages
    assert subject_of(answer) == "Déposé dans « Infra » : Compte rendu"
    [note] = notes(wiki, ctx)
    assert f"/o/acme/infra/inbox/{note.name}" in body_of(answer)
    row.replies = "off"
    db.commit()
    server.deliver(message(sender="bob@acme.test"))
    poll(database, ctx)
    assert len(smtp.messages) == 1


# --- Robustness -------------------------------------------------------------------------------


def test_a_broken_email_does_not_stop_the_others(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server.deliver(message(subject="Cassé"))
    server.deliver(message(subject="Bon"))
    real = email_capture.handle

    def handle(*args: object, **kwargs: object) -> str:
        parsed = args[3]
        if getattr(parsed, "subject", "") == "Cassé":
            raise RuntimeError("bug")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(email_capture, "handle", handle)
    summary = poll(database, ctx)
    assert (summary.accepted, summary.refused) == (1, 1)
    assert [n.title for n in notes(wiki, ctx)] == ["Bon"]
    assert "error" in reasons(db)


def test_busy_space_or_rate_limit_leaves_the_email_for_the_next_poll(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deposits, "MAX_DEPOSITS_PER_HOUR", 0)
    server.deliver(message())
    summary = poll(database, ctx)
    assert summary.deferred == 1 and len(server.inbox()) == 1
    db.expire_all()
    assert [c.status for c in db.scalars(select(EmailCapture))] == ["deferred"]
    monkeypatch.setattr(deposits, "MAX_DEPOSITS_PER_HOUR", 120)
    assert poll(database, ctx).accepted == 1 and server.inbox() == []


def test_mailbox_errors_are_kept_for_the_health_page(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server, settings: object
) -> None:
    server.refuse_login = True
    summary = email_capture.poll(database, ctx)
    assert "AUTHENTICATIONFAILED" in summary.error
    db.expire_all()
    report = instance.report(db, database, ctx, settings)  # type: ignore[arg-type]
    [check] = [c for c in report.checks if c.title == "Capture by email"]
    assert check.state == instance.ERROR and "AUTHENTICATIONFAILED" in check.message
    assert "mot-de-passe-imap" not in summary.error
    server.refuse_login = False
    server.drop_after = 6  # connection lost while reading
    server.deliver(message())
    assert email_capture.poll(database, ctx).error
    server.drop_after = None
    assert poll(database, ctx).accepted == 1  # picked up again
    db.expire_all()
    report = instance.report(db, database, ctx, settings)  # type: ignore[arg-type]
    [check] = [c for c in report.checks if c.title == "Capture by email"]
    assert check.state == instance.OK and report.capture is not None
    assert report.capture.accepted == 1


def test_a_single_poll_at_a_time(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    server.deliver(message())
    with email_capture._poll_lock(database) as acquired:
        assert acquired
        assert email_capture.poll(database, ctx).skipped
    assert server.connections == [] and len(server.inbox()) == 1
    assert poll(database, ctx).accepted == 1


def test_unencrypted_connection_only_when_allowed(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    with pytest.raises(SynaptaneError, match="unencrypted"):
        email_capture.configure(
            db,
            wiki.root,
            ctx.box,
            host="localhost",
            port=143,
            security="none",
            username="u",
            address=CAPTURE,
            authserv_ids=TRUSTED,
        )
    email_capture.configure(
        db,
        wiki.root,
        ctx.box,
        host="localhost",
        port=143,
        security="none",
        username=server.username,
        address=CAPTURE,
        authserv_ids=TRUSTED,
        allow_plain=True,
    )
    db.commit()
    assert "refused" in email_capture.poll(database, ctx).error  # the worker checks again
    server.deliver(message())
    assert email_capture.poll(database, ctx, allow_plain=True).accepted == 1
    assert not server.connections[-1].tls
    email_capture.configure(
        db,
        wiki.root,
        ctx.box,
        host="imap.capture.test",
        port=143,
        security="starttls",
        username=server.username,
        address=CAPTURE,
        authserv_ids=TRUSTED,
    )
    db.commit()
    server.deliver(message())
    assert poll(database, ctx).accepted == 1
    assert [c.tls for c in server.connections][-1] and "STARTTLS" in server.commands


def test_old_captures_are_forgotten(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    db.add(EmailCapture(key="x" * 64, status="refused", at=datetime.now(UTC) - timedelta(days=91)))
    db.commit()
    poll(database, ctx)
    assert reasons(db) == []


# --- Settings, scheduling, worker -------------------------------------------------------------


def test_settings_are_validated(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    base = {
        "host": "imap.capture.test",
        "port": 993,
        "security": "ssl",
        "username": "brain@capture.test",
        "password": "secret",
        "address": CAPTURE,
        "authserv_ids": TRUSTED,
    }
    for change, words in (
        ({"address": "brain+x@capture.test"}, "without “+”"),
        ({"address": "pas-une-adresse"}, "capture address"),
        ({"authserv_ids": ""}, "identifier of the receiving server"),
        ({"authserv_ids": "not a host!"}, "domain names"),
        ({"authserv_ids": "*"}, "domain names"),
        ({"spf_domains": "*.corp.test"}, "domain names"),  # no wildcard there
        ({"spf_domains": "corp test"}, "domain names"),
        ({"port": 0}, "port"),
        ({"security": "tls"}, "port or security"),
        ({"host": ""}, "IMAP server"),
        ({"folder": "Boîte"}, "Folder"),
        ({"interval": 0}, "Interval"),
        ({"replies": "maybe"}, "answers"),
        ({"password": ""}, "password"),
        ({"password": "été-2026"}, "without accents"),  # imaplib sends it in ASCII
    ):
        with pytest.raises(SynaptaneError, match=re.escape(words)):
            email_capture.configure(db, wiki.root, ctx.box, **{**base, **change})  # type: ignore[arg-type]
        db.rollback()
    with pytest.raises(SynaptaneError):
        email_capture.configure(db, wiki.alice, ctx.box, **base)  # type: ignore[arg-type]
    # Microsoft 365 alone: no identifier needed.
    email_capture.configure(
        db,
        wiki.root,
        ctx.box,
        **{**base, "authserv_ids": "", "unnamed_results": True},  # type: ignore[arg-type]
    )
    db.commit()
    row = db.get(CaptureSettings, 1)
    assert row is not None and row.authserv_ids == "" and row.unnamed_results
    assert row.password is not None and b"secret" not in row.password  # encrypted
    email_capture.configure(db, wiki.root, ctx.box, **{**base, "password": None})  # type: ignore[arg-type]
    assert email_capture.view(db, wiki.root).has_password  # type: ignore[union-attr]


def test_schedule_and_worker(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server, settings: object
) -> None:
    from synaptane.worker.runner import LANES, Worker

    db.commit()
    assert email_capture.schedule(database)
    assert not email_capture.schedule(database)  # one waits already
    worker = Worker(settings, database, ctx)  # type: ignore[arg-type]
    server.deliver(message())
    assert worker.run_once(LANES["email"])
    assert server.inbox() == [] and len(notes(wiki, ctx)) == 1
    # Polled just now: not due before `interval` minutes.
    assert not email_capture.schedule(database)
    later = datetime.now(UTC) + timedelta(minutes=2)
    assert email_capture.schedule(database, later)
    with database.session() as session:
        assert session.scalar(select(func.count()).where(Job.kind == "email")) == 2


def test_no_capture_no_poll(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> None:
    assert not email_capture.schedule(database)
    assert email_capture.poll(database, ctx) == email_capture.Summary()
    assert email_capture.address_for(db, wiki.infra) is None
    assert not email_capture.request(db, wiki.root)


def test_metrics(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    server.deliver(message())
    server.deliver(message(auth=None))
    poll(database, ctx)
    out = Exposition()
    db.expire_all()
    metrics.write_instance(out, db, ctx)
    text = out.text()
    assert 'synaptane_email_capture_messages_24h{status="accepted",reason="none"} 1' in text
    assert (
        'synaptane_email_capture_messages_24h{status="refused",reason="not_authenticated"} 1'
        in text
    )
    assert "synaptane_email_capture_poll_failing 0" in text
    assert "synaptane_email_capture_last_poll_timestamp_seconds" in text


# --- Web --------------------------------------------------------------------------------------


def test_settings_page_test_button_and_audit(
    client: TestClient,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = install(monkeypatch)
    verify(db, wiki.alice.user, "alice@acme.test")
    login(client, "alice")
    assert client.get("/admin/mail/capture").status_code == 403
    client.cookies.clear()
    login(client, "root")
    page = client.get("/admin/mail/capture")
    assert page.status_code == 200 and "Capture par e-mail" in page.text
    assert "Aucune (développement uniquement)" not in page.text  # no plain IMAP here
    form = {
        "csrf": csrf(client, "/admin/mail/capture"),
        "host": "imap.capture.test",
        "port": "993",
        "security": "ssl",
        "username": server.username,
        "password": server.password,
        "folder": "INBOX",
        "address": "Brain@Capture.test",
        "authserv_ids": TRUSTED,
        "spf_domains": "corp.test",
        "interval": "3",
        "replies": "all",
    }
    assert client.post("/admin/mail/capture", data={**form, "csrf": "x"}).status_code == 403
    saved = client.post("/admin/mail/capture", data=form)
    assert saved.status_code == 200 and "Capture par e-mail enregistrée" in saved.text
    assert server.password not in saved.text
    db.expire_all()
    [event] = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "instance.capture_configured")
    ).all()
    assert event.details == {
        "host": "imap.capture.test",
        "address": "brain@capture.test",
        "spf": "True",
        "unnamed": "False",
    }
    journal = client.get("/admin/journal").text
    assert "Capture par e-mail configurée" in journal and "brain@capture.test" in journal
    assert "Domaines internes acceptés sur SPF seul" in journal
    # Test: who authenticated the latest emails.
    server.deliver(message())
    server.deliver(message(auth="spf=pass smtp.mailfrom=x.test; dmarc=pass header.from=x.test"))
    tested = client.post("/admin/mail/capture/test", data={"csrf": form["csrf"]})
    assert "2 e-mails en attente" in tested.text and f"{TRUSTED} (1)" in tested.text
    assert "Microsoft 365" in tested.text
    server.refuse_login = True
    failed = client.post("/admin/mail/capture/test", data={"csrf": form["csrf"]})
    assert "AUTHENTICATIONFAILED" in failed.text
    # Read now, then turn off.
    asked = client.post("/admin/mail/capture/poll", data={"csrf": form["csrf"]})
    assert "Relève demandée" in asked.text
    assert db.scalar(select(func.count()).where(Job.kind == "email")) == 1
    removed = client.post("/admin/mail/capture/delete", data={"csrf": form["csrf"]})
    assert "Capture par e-mail désactivée" in removed.text
    db.expire_all()
    assert db.get(CaptureSettings, 1) is None
    assert (
        db.scalar(select(func.count()).where(AuditEvent.action == "instance.capture_removed")) == 1
    )


def test_inbox_page_shows_the_address_to_those_who_may_drop(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    db.commit()
    login(client, "alice")
    page = client.get("/o/acme/infra/inbox").text
    assert "brain+acme.infra@capture.test" in page and 'data-copy="capture-address"' in page
    client.cookies.clear()
    login(client, "bob")  # reader: cannot drop
    assert "brain+acme.infra@capture.test" not in client.get("/o/acme/infra/inbox").text
    db.delete(db.get(CaptureSettings, 1))
    db.commit()
    client.cookies.clear()
    login(client, "alice")
    assert "capture-address" not in client.get("/o/acme/infra/inbox").text


def test_health_page_lists_refusals_without_content(
    client: TestClient,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    server: Server,
) -> None:
    server.deliver(message(auth=None, subject="Sujet confidentiel"))
    poll(database, ctx)
    login(client, "root")
    page = client.get("/admin/health").text
    assert "E-mails refusés par la capture" in page
    assert "Expéditeur non authentifié" in page and "de acme.test" in page
    assert "Sujet confidentiel" not in page and "alice@acme.test" not in page


def test_folders_created_even_if_the_parent_is_refused(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    server.refuse_create = {"Synaptane"}  # some servers create the parent with the child
    server.deliver(message())
    assert poll(database, ctx).accepted == 1
    assert len(server.folder(PROCESSED)) == 1
    server.refuse_create = {REFUSED}  # the child itself refused: the poll stops, nothing lost
    server.deliver(message(auth=None))
    summary = email_capture.poll(database, ctx)
    assert "CANNOT" in summary.error and len(server.inbox()) == 1


def test_the_address_is_shown_only_where_emails_would_be_accepted(
    db: Session, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    """Same rule as the capture: verified address of an active account that may drop notes
    there; a super admin only in the spaces they belong to."""
    address = "brain+acme.infra@capture.test"
    shown = email_capture.address_shown
    assert shown(db, wiki.alice.user, wiki.infra) == address
    assert shown(db, wiki.bob.user, wiki.infra) is None  # reader
    assert shown(db, wiki.alice.user, wiki.other) is None  # not a member
    assert shown(db, wiki.root.user, wiki.infra) is None  # super admin, member of nothing
    spaces.set_member(db, wiki.root, wiki.infra, wiki.root.user, SpaceRole.CONTRIBUTOR)
    db.commit()
    assert shown(db, wiki.root.user, wiki.infra) == address
    wiki.alice.user.email_verified_at = None  # their emails would be refused
    db.commit()
    assert shown(db, wiki.alice.user, wiki.infra) is None
    verify(db, wiki.alice.user, "alice@acme.test")
    wiki.alice.user.is_active = False
    db.commit()
    assert shown(db, wiki.alice.user, wiki.infra) is None


def test_super_admin_pages_follow_the_same_rule(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki, server: Server
) -> None:
    db.commit()
    login(client, "root")
    assert "brain+acme.infra@capture.test" not in client.get("/o/acme/infra/inbox").text
