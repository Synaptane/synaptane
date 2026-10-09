"""Invitations, forgotten password, sign-in by e-mail, password to change, SMTP."""

import re
import socket
import socketserver
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import Message

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole
from synaptane.services import accounts, mail, organizations, spaces
from synaptane.services.workspace import Workspace
from synaptane.storage.models import User
from tests.integration.conftest import PASSWORD, actor_of, make_user
from tests.integration.test_web import csrf, login


class Inbox(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        self.messages: list[Message] = []
        super().__init__(("127.0.0.1", 0), _SMTP)


class _SMTP(socketserver.StreamRequestHandler):
    """Minimal SMTP server (no encryption or authentication) for the tests."""

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, Inbox)
        self.wfile.write(b"220 test\r\n")
        while line := self.rfile.readline():
            command = line.strip().upper()
            if command.startswith((b"EHLO", b"HELO")):
                self.wfile.write(b"250 test\r\n")
            elif command.startswith((b"MAIL", b"RCPT", b"RSET", b"NOOP")):
                self.wfile.write(b"250 ok\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 go\r\n")
                data = b""
                while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                    data += chunk
                server.messages.append(message_from_bytes(data))
                self.wfile.write(b"250 queued\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"502 no\r\n")


@pytest.fixture
def smtp(db: Session, ctx: Workspace) -> Iterator[Inbox]:
    inbox = Inbox()
    thread = threading.Thread(target=inbox.serve_forever, daemon=True)
    thread.start()
    root = actor_of(db, make_user(db, "postmaster", superadmin=True))
    mail.configure(
        db,
        root,
        ctx.box,
        host="127.0.0.1",
        port=inbox.server_address[1],
        security="none",
        sender="synaptane@example.com",
    )
    db.commit()
    yield inbox
    inbox.shutdown()


def link_in(message: Message) -> str:
    body = message.get_payload(decode=True)
    assert isinstance(body, bytes)
    found = re.search(r"http://localhost(/\S+)", body.decode())
    assert found
    return found.group(1)


def org_admin_setup(db: Session) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    carla = make_user(db, "carla")
    acme = organizations.create_organization(db, root, slug="acme", name="Acme")
    spaces.create_space(db, root, acme, slug="infra", name="Infra")
    organizations.set_member(db, root, acme, carla, OrgRole.ADMIN)
    db.commit()


def test_org_admin_invites_by_email_and_the_guest_joins(
    client: TestClient, db: Session, smtp: Inbox
) -> None:
    org_admin_setup(db)
    login(client, "carla")  # organization admin, not super admin
    token = csrf(client, "/o/acme/-/members")
    page = client.get("/o/acme/-/members").text
    space_value = re.search(r'value="([0-9a-f-]{36}):contributor"', page)
    assert space_value
    sent = client.post(
        "/o/acme/-/invitations",
        data={
            "csrf": token,
            "email": "Dana@Example.com",
            "org_role": "member",
            "space_role": [f"{space_value.group(1)}:contributor"],
        },
    )
    assert "Invitation envoyée" in sent.text and "dana@example.com" in sent.text
    [message] = smtp.messages
    assert message["To"] == "dana@example.com"
    assert "vous invite sur Synaptane" in str(message["Subject"])
    path = link_in(message)

    client.cookies.clear()
    form = client.get(path)
    assert "Rejoindre «\u202fAcme\u202f»" in form.text
    joined = client.post(
        path, data={"username": "dana", "display_name": "Dana", "password": PASSWORD}
    )
    assert joined.url.path == "/" and "Bienvenue" in joined.text
    assert client.get("/o/acme/infra").status_code == 200  # contributor to the space
    assert client.get(path).status_code == 410  # single use


def test_invitation_without_smtp_shows_the_link_once(client: TestClient, db: Session) -> None:
    org_admin_setup(db)
    login(client, "carla")
    token = csrf(client, "/o/acme/-/members")
    page = client.post("/o/acme/-/invitations", data={"csrf": token, "email": "eve@example.com"})
    assert "transmettez ce lien" in page.text
    assert re.search(r"/invite/inv_[\w-]+", page.text)
    assert not re.search(r"/invite/inv_[\w-]+", client.get("/o/acme/-/members").text)
    assert "eve@example.com" in client.get("/o/acme/-/members").text  # pending


def test_inviting_an_existing_account_grants_access_at_once(
    client: TestClient, db: Session
) -> None:
    org_admin_setup(db)
    frank = make_user(db, "frank")
    frank.email, frank.email_verified_at = "frank@example.com", datetime.now(UTC)
    db.commit()
    login(client, "carla")
    token = csrf(client, "/o/acme/-/members")
    done = client.post("/o/acme/-/invitations", data={"csrf": token, "email": "frank@example.com"})
    assert "Compte existant" in done.text and "@frank" in done.text


def test_forgotten_password_by_email(client: TestClient, db: Session, smtp: Inbox) -> None:
    user = make_user(db, "gina")
    user.email, user.email_verified_at = "gina@example.com", datetime.now(UTC)
    db.commit()
    for login_value in ("gina@example.com", "inconnu@example.com"):
        answer = client.post("/login/forgot", data={"login": login_value})
        assert "Si un compte correspond" in answer.text  # same answer: nothing leaks
    [message] = smtp.messages
    path = link_in(message)
    assert "gina" in client.get(path).text
    done = client.post(path, data={"password": "un nouveau mot de passe"})
    assert done.url.path == "/" and "Mot de passe changé" in done.text
    assert client.get(path).status_code == 410
    client.cookies.clear()
    login(client, "gina@example.com", "un nouveau mot de passe")  # sign-in by e-mail


def test_password_set_by_an_admin_must_be_changed(client: TestClient, db: Session) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    accounts.create_user(
        db, root, username="hugo", display_name="Hugo", password=PASSWORD, locale="fr"
    )
    db.commit()
    login(client, "hugo")
    blocked = client.get("/")
    assert blocked.url.path == "/account/security" and "propre mot de passe" in blocked.text
    token = csrf(client, "/account/security")
    client.post(
        "/account/password",
        data={"csrf": token, "current": PASSWORD, "password": "mon mot de passe à moi"},
    )
    login(client, "hugo", "mon mot de passe à moi")
    assert client.get("/").url.path == "/"
    db.expire_all()
    hugo = db.scalar(select(User).where(User.username == "hugo"))
    assert hugo is not None and not hugo.must_change_password


def test_admin_configures_and_tests_mail(client: TestClient, db: Session, smtp: Inbox) -> None:
    root = make_user(db, "root", superadmin=True)
    root.email = "root@example.com"
    db.commit()
    login(client, "root")
    token = csrf(client, "/admin/mail")
    page = client.post("/admin/mail/test", data={"csrf": token})
    assert "E-mail de test envoyé" in page.text
    assert smtp.messages[-1]["To"] == "root@example.com"


def test_unreachable_smtp_is_reported(db: Session, ctx: Workspace) -> None:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    mail.configure(
        db, root, ctx.box, host="127.0.0.1", port=port, security="none", sender="s@example.com"
    )
    with pytest.raises(mail.MailError):
        mail.send(db, ctx.box, "x@example.com", "Sujet", "Corps")


def test_invitations_over_rest(client: TestClient, db: Session, database: object) -> None:
    from tests.integration.conftest import token_for

    org_admin_setup(db)
    headers = {"Authorization": f"Bearer {token_for(database, 'carla')}"}  # type: ignore[arg-type]
    created = client.post(
        "/api/v1/organizations/acme/invitations",
        json={"email": "ivan@example.com", "spaces": {"infra": "reader"}},
        headers=headers,
    )
    assert created.status_code == 201 and "/invite/inv_" in created.json()["link"]
    listed = client.get("/api/v1/organizations/acme/invitations", headers=headers).json()
    assert [i["email"] for i in listed] == ["ivan@example.com"]
    assert listed[0]["link"] is None  # the link is only shown at creation
    bad = client.post(
        "/api/v1/organizations/acme/invitations",
        json={"email": "x@example.com", "spaces": {"nulle-part": "reader"}},
        headers=headers,
    )
    assert bad.status_code == 404
    gone = client.delete(
        f"/api/v1/organizations/acme/invitations/{listed[0]['id']}", headers=headers
    )
    assert gone.status_code == 204


def test_a_declared_email_gives_no_access(client: TestClient, db: Session, smtp: Inbox) -> None:
    """Putting a new hire's address on one's own account does not capture the invitation."""
    org_admin_setup(db)
    mallory = make_user(db, "mallory")
    db.commit()
    login(client, "mallory")
    token = csrf(client, "/account")
    client.post(
        "/account",
        data={
            "csrf": token,
            "display_name": "M",
            "locale": "fr",
            "email": "recrue@acme.example",
            "current": PASSWORD,
        },
    )
    db.expire_all()
    assert mallory.email == "recrue@acme.example" and mallory.email_verified_at is None
    assert "/verify-email/" in str(smtp.messages[-1].get_payload())  # link sent to the new hire

    client.cookies.clear()
    login(client, "carla")
    token = csrf(client, "/o/acme/-/members")
    sent = client.post(
        "/o/acme/-/invitations",
        data={"csrf": token, "email": "recrue@acme.example", "org_role": "admin"},
    )
    assert "Invitation envoyée" in sent.text  # not the "existing account" message
    path = link_in(smtp.messages[-1])
    client.cookies.clear()
    joined = client.post(
        path, data={"username": "recrue", "display_name": "Recrue", "password": PASSWORD}
    )
    assert joined.url.path == "/"
    db.expire_all()
    recrue = db.scalar(select(User).where(User.username == "recrue"))
    assert recrue is not None and recrue.email == "recrue@acme.example"
    assert recrue.email_verified_at is not None
    assert db.get(User, mallory.id).email is None  # type: ignore[union-attr]


def test_a_verification_link_only_verifies_the_account_that_asked(
    client: TestClient, db: Session, smtp: Inbox
) -> None:
    """The new hire opens the link requested by Mallory (or their mail antivirus opens it on
    its own): the address is not verified for Mallory's account."""
    mallory = make_user(db, "mallory")
    make_user(db, "recrue")
    db.commit()
    login(client, "mallory")
    token = csrf(client, "/account")
    client.post(
        "/account",
        data={
            "csrf": token,
            "display_name": "M",
            "locale": "fr",
            "email": "recrue@acme.example",
            "current": PASSWORD,
        },
    )
    message = smtp.messages[-1]
    body = message.get_payload(decode=True)
    assert isinstance(body, bytes) and "« mallory »" in body.decode()  # the account is named
    path = link_in(message)
    client.cookies.clear()

    scanned = client.get(path, follow_redirects=False)  # antivirus: no session
    assert scanned.status_code == 303
    assert scanned.headers["location"] == f"/login?next={path}"

    back = client.post(
        "/login",
        data={"username": "recrue", "password": PASSWORD, "next": path},
        follow_redirects=False,
    )
    assert back.headers["location"] == path  # after sign-in, back to the link
    page = client.get(path)
    assert "@mallory" in page.text and f'action="{path}"' not in page.text
    refused = client.post(path, data={"csrf": csrf(client, path)})
    assert refused.status_code == 403 and "autre compte" in refused.text
    db.expire_all()
    assert db.get(User, mallory.id).email_verified_at is None  # type: ignore[union-attr]


def test_changing_email_needs_the_password_and_a_link(
    client: TestClient, db: Session, smtp: Inbox
) -> None:
    user = make_user(db, "ines")
    user.email, user.email_verified_at = "ines@example.com", datetime.now(UTC)
    db.commit()
    login(client, "ines")
    token = csrf(client, "/account")
    form = {"csrf": token, "display_name": "Inès", "locale": "fr", "email": "ines@autre.example"}
    refused = client.post("/account", data=form)
    assert "mot de passe actuel" in refused.text
    client.post("/account", data={**form, "current": PASSWORD})
    old, check = smtp.messages[-2], smtp.messages[-1]
    assert old["To"] == "ines@example.com"  # the old address is notified
    assert check["To"] == "ines@autre.example"
    path = link_in(check)
    page = client.get(path)  # opening the link verifies nothing: confirmation needed
    assert f'action="{path}"' in page.text
    db.expire_all()
    pending = db.scalar(select(User).where(User.username == "ines"))
    assert pending is not None and pending.email_verified_at is None
    done = client.post(path, data={"csrf": csrf(client, path)})
    assert "Adresse e-mail vérifiée" in done.text
    db.expire_all()
    found = db.scalar(select(User).where(User.username == "ines"))
    assert found is not None and found.email_verified_at is not None
