"""An agent that reads everything, finds duplicates, tidies quickly and reports back."""

import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.services import deposits, jobs, mail, tidy, tidy_notices
from synaptane.services.inbox import parse_front_matter
from synaptane.services.inbox_text import CACHE
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, Space
from tests.integration.conftest import actor_of, make_user, token_for
from tests.integration.test_invitations import Inbox
from tests.integration.test_semantic import ConceptEmbedder, enable, index
from tests.integration.test_tidy import Scripted, configure_ai
from tests.integration.test_tidy_robustness import note
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki, docx, native_pdf


def deposit(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    title: str,
    attachments: list[tuple[str, bytes]] | None = None,
) -> str:
    path = deposits.deposit(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        title=title,
        body=f"{title}.",
        source="web",
        attachments=attachments,
    )
    db.commit()
    return path.rsplit("/", 1)[-1]


def left_reason(ctx: Workspace, wiki: Wiki, name: str) -> str:
    data = ctx.local_repository(wiki.infra).blob(f".synaptane/inbox/{name}") or b""
    fields, _ = parse_front_matter(data.decode())
    assert fields.get("status") == "left", fields
    return str(fields["reason"])


# --- Attachments read -------------------------------------------------------------------------


def test_the_agent_reads_the_text_of_a_pdf_attachment(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    pdf = native_pdf("Devis Synology DS923 pour le NAS de la salle serveur")
    name = deposit(
        db,
        database,
        ctx,
        wiki,
        "Devis reçu",
        [("devis.pdf", pdf), ("liste.txt", b"Disques : 4 x 8 To\n")],
    )
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": name}),
                ("read_inbox_item", {"item": name, "attachment": "devis.pdf", "offset": 6}),
                ("read_inbox_item", {"item": name, "attachment": "absente.pdf"}),
                ("leave_in_inbox", {"item": name, "reason": "à relire"}),
            ]
        ]
    )
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    whole, part, unknown, _ = (r.content for r in script.results)
    assert "devis.pdf (extracted text" in whole and "Synology DS923" in whole
    assert "liste.txt (text" in whole and "4 x 8 To" in whole
    assert re.match(r"<inbox_item_\w+ ", whole)  # inside the untrusted-content frame
    assert "Attachment devis.pdf (extracted text)" in part and "Synology" in part
    assert "Unknown attachment" in unknown
    # The text is kept: a later pass does not redo the extraction (nor the OCR).
    assert list((ctx.extract_path(wiki.infra.id) / CACHE).glob("*.txt"))


def test_a_secret_in_an_attachment_never_reaches_the_model(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    # Binary document: the detector does not read it on deposit, only its extracted text.
    office = docx("Identifiants AWS de prod : AKIAIOSFODNN7EXAMPLE")
    name = deposit(db, database, ctx, wiki, "Accès cloud", [("acces.docx", office)])
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": name}),
                ("file_attachment", {"item": name, "attachment": "acces.docx", "path": "a.docx"}),
                ("mark_done", {"item": name, "summary": "rangé"}),
                ("leave_in_inbox", {"item": name, "reason": "autre raison"}),
            ]
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.items_left == 1 and record.items_done == 0
    read, filed, closed, _ = script.results
    assert "automatically left" in read.content and "AWS access key" in read.content
    assert filed.is_error and closed.is_error
    assert all("AKIA" not in r.content for r in script.results)
    assert ctx.local_repository(wiki.infra).blob("content/a.docx") is None
    assert "Likely secret" in left_reason(ctx, wiki, name)


# --- Hybrid search ----------------------------------------------------------------------------


def test_the_agent_search_finds_a_page_by_its_meaning(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_ai(db, ctx, wiki)
    enable(db, ctx, wiki)
    embedder = ConceptEmbedder()
    index(database, ctx, wiki, embedder)
    monkeypatch.setattr("synaptane.ai.embeddings.embed", embedder)
    name = note(db, database, ctx, wiki, "Nouveau backup")
    script = Scripted(
        [
            [
                ("search", {"query": "backup"}),
                ("search", {"query": "sauvegarde"}),
                ("leave_in_inbox", {"item": name, "reason": "x"}),
            ]
        ]
    )
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    meaning, words = (r.content for r in script.results[:2])
    # No word in common: only the search by meaning finds the duplicate.
    assert meaning.startswith("serveurs/proxmox.md — Proxmox (by meaning)")
    assert words.startswith("serveurs/proxmox.md — Proxmox — ") and "by meaning" not in words


# --- Long pages, freshness, strict tools ------------------------------------------------------


def test_append_page_and_history(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    name = note(db, database, ctx, wiki, "Longue procédure")
    script = Scripted(
        [
            [
                ("write_page", {"path": "longue.md", "content": "# Longue\n\nÉtape 1"}),
                ("append_page", {"path": "longue.md", "content": "Étape 2\n"}),
                ("append_page", {"path": "longue.md", "content": "Voir [[proxmox]].\n"}),
                ("append_page", {"path": "serveurs/proxmox.md", "content": "Ajout.\n"}),
                ("append_page", {"path": "rapport.pdf", "content": "x"}),
                ("history", {"path": "serveurs/proxmox.md"}),
                ("history", {"path": "longue.md"}),
                ("history", {"path": "absente.md"}),
                ("read_inbox_item", {"item": name}),
                ("mark_done", {"item": name, "summary": "ok", "pages": ["longue.md"]}),
            ]
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.commit
    results = script.results
    assert results[1].content.startswith("Appended (the page is")
    assert results[4].is_error and "Markdown" in results[4].content
    old, new, absent = results[5], results[6], results[7]
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} — Alice Externe — ", old.content)
    assert "modified in this run" in old.content and old.content.startswith("<history_")
    assert "created in this run" in new.content and absent.is_error
    repo = ctx.local_repository(wiki.infra)
    assert (
        repo.blob("content/longue.md")
        == "# Longue\n\nÉtape 1\nÉtape 2\nVoir [[proxmox]].\n".encode()
    )
    assert (repo.blob("content/serveurs/proxmox.md") or b"").endswith(
        b"accueil](../README.md).\nAjout.\n"
    )
    ops = [(op["op"], op["path"]) for op in record.operations if op["op"] in ("créé", "modifié")]
    assert ops == [("créé", "longue.md"), ("modifié", "serveurs/proxmox.md")]


def test_anthropic_tools_are_strict_when_the_model_allows_it() -> None:
    from synaptane.ai.anthropic_provider import tool_param
    from synaptane.ai.base import AIConfig
    from synaptane.ai.providers import PING
    from synaptane.services.tidy_tools import TOOLS

    opus = AIConfig("anthropic", "claude-opus-5-5", "k")
    # Every agent tool has a closed schema: all strict.
    assert all(tool_param(opus, t).get("strict") is True for t in TOOLS)
    assert "strict" not in tool_param(opus, PING)  # open schema: the API would reject it
    gateway = AIConfig("anthropic", "claude-opus-5-5", "k", "https://gw/v1")
    assert "strict" not in tool_param(gateway, TOOLS[0])
    assert "strict" not in tool_param(AIConfig("anthropic", "claude-3-haiku", "k"), TOOLS[0])


# --- As notes arrive --------------------------------------------------------------------------


def tidy_jobs(db: Session) -> list[Job]:
    db.expire_all()
    return list(db.scalars(select(Job).where(Job.kind == "tidy", Job.status == "queued")))


def test_deposits_schedule_one_grouped_pass_after_the_delay(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_ai(db, ctx, wiki)
    early = note(db, database, ctx, wiki, "Sans délai réglé")
    assert tidy_jobs(db) == []  # by default: at the scheduled time only

    tidy.update_settings(db, wiki.root, wiki.infra, delay=10)
    db.commit()
    start = datetime.now(UTC)
    first = note(db, database, ctx, wiki, "Premier dépôt")
    [job] = tidy_jobs(db)
    assert job.payload == {"trigger": "deposit"}
    assert start + timedelta(minutes=9) < job.run_after < start + timedelta(minutes=11)
    with database.session() as session:
        assert jobs.claim(session, kinds=["tidy"]) is None  # not before the delay
    second = note(db, database, ctx, wiki, "Second dépôt")
    [again] = tidy_jobs(db)
    assert again.id == job.id and again.run_after == job.run_after  # grouped

    # The pass takes deposits made close together.
    # Both are closed: an unclosed item would stay new (interrupted pass).
    script = Scripted(
        [
            [
                ("leave_in_inbox", {"item": first, "reason": "x"}),
                ("leave_in_inbox", {"item": second, "reason": "x"}),
                ("leave_in_inbox", {"item": early, "reason": "x"}),
            ]
        ]
    )
    tidy.run(database, ctx, wiki.infra.id, trigger="deposit", factory=script.factory)
    assert first in script.first and second in script.first
    # Nothing new: no model call, no journal entry (background pass).
    idle = Scripted([])
    tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=idle.factory)
    assert tidy.run(database, ctx, wiki.infra.id, trigger="deposit", factory=idle.factory) is None
    assert idle.first == ""

    # "Tidy now" does not wait for the delay.
    tidy.request_run(db, wiki.alice, wiki.infra)
    db.commit()
    [now] = tidy_jobs(db)
    assert now.payload["trigger"] == "manual" and now.run_after <= datetime.now(UTC)

    # Budget exhausted: no pass scheduled (the nightly one reports it).
    db.delete(now)
    db.commit()
    monkeypatch.setattr(
        "synaptane.services.budget.refusal", lambda session, org_id: "Budget atteint"
    )
    note(db, database, ctx, wiki, "Hors budget")
    assert tidy_jobs(db) == []

    with pytest.raises(Exception, match="Invalid delay"):
        tidy.update_settings(db, wiki.root, wiki.infra, delay=5000)
    db.rollback()
    tidy.update_settings(db, wiki.root, wiki.infra, delay=0)
    assert db.get(Space, wiki.infra.id).tidy_delay is None  # type: ignore[union-attr]


def test_delay_is_set_in_the_tidy_settings_page(
    client: TestClient, db: Session, database: Database, wiki: Wiki
) -> None:
    login(client, "root")
    token = csrf(client, "/o/acme/infra/-/tidy-settings")
    base = {"csrf": token, "tidy_enabled": "true", "tidy_time": "02:00"}
    base["tidy_timezone"] = "Europe/Paris"
    client.post(
        "/o/acme/infra/-/tidy-settings", data={**base, "tidy_soon": "true", "tidy_delay": "20"}
    )
    db.expire_all()
    assert db.get(Space, wiki.infra.id).tidy_delay == 20  # type: ignore[union-attr]
    page = client.get("/o/acme/infra/-/tidy-settings").text
    assert 'name="tidy_soon" value="true" checked' in page and 'value="20"' in page
    client.post("/o/acme/infra/-/tidy-settings", data={**base, "tidy_delay": "20"})
    db.expire_all()
    assert db.get(Space, wiki.infra.id).tidy_delay is None  # type: ignore[union-attr]
    bad = client.post(
        "/o/acme/infra/-/tidy-settings", data={**base, "tidy_soon": "true", "tidy_delay": "0"}
    )
    assert "Délai invalide" in bad.text

    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    url = "/api/v1/organizations/acme/spaces/infra/tidy-settings"
    assert client.patch(url, json={"delay": 30}, headers=root).status_code == 204
    db.expire_all()
    assert db.get(Space, wiki.infra.id).tidy_delay == 30  # type: ignore[union-attr]


# --- Notices to the depositor -----------------------------------------------------------------


@pytest.fixture
def mailbox(db: Session, ctx: Workspace) -> Iterator[Inbox]:
    inbox = Inbox()
    threading.Thread(target=inbox.serve_forever, daemon=True).start()
    postmaster = actor_of(db, make_user(db, "postmaster", superadmin=True))
    mail.configure(
        db,
        postmaster,
        ctx.box,
        host="127.0.0.1",
        port=inbox.server_address[1],
        security="none",
        sender="synaptane@example.com",
    )
    db.commit()
    yield inbox
    inbox.shutdown()


def body_of(message: object) -> str:
    payload = message.get_payload(decode=True)  # type: ignore[attr-defined]
    assert isinstance(payload, bytes)
    return payload.decode().replace("\r\n", "\n")


def test_the_depositor_is_told_where_notes_went_or_why_they_stayed(
    client: TestClient,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    mailbox: Inbox,
) -> None:
    configure_ai(db, ctx, wiki)
    wiki.alice.user.email = "alice@example.com"
    wiki.alice.user.email_verified_at = datetime.now(UTC)
    db.commit()
    filed = note(db, database, ctx, wiki, "Nouveau NAS")
    stays = note(db, database, ctx, wiki, "Question floue")
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": filed}),
                ("write_page", {"path": "stockage/nas.md", "content": "# NAS\n"}),
                ("mark_done", {"item": filed, "summary": "Page NAS", "pages": ["stockage/nas.md"]}),
                ("leave_in_inbox", {"item": stays, "reason": "Quel serveur ?"}),
            ]
        ]
    )
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    [message] = mailbox.messages
    subject = str(make_header(decode_header(message["Subject"])))
    assert message["To"] == "alice@example.com" and subject == "Rangement de « Infra » : vos notes"
    text = body_of(message)
    assert "« Nouveau NAS » rangée dans :" in text and "/o/acme/infra/p/stockage/nas.md" in text
    assert "« Question floue » : laissée dans l'inbox.\n  Quel serveur ?" in text
    assert "Une note laissée attend dans l'inbox" in text and "/o/acme/infra/inbox" in text
    assert "/account" in text  # how to stop receiving them

    # Left again: no repeated notice. A newly tidied note: a notice.
    other = note(db, database, ctx, wiki, "Onduleur")
    script = Scripted(
        [
            [
                ("leave_in_inbox", {"item": stays, "reason": "Toujours flou"}),
                ("read_inbox_item", {"item": other}),
                ("mark_done", {"item": other, "summary": "Doublon"}),
            ]
        ]
    )
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert len(mailbox.messages) == 2
    text = body_of(mailbox.messages[1])
    assert "« Onduleur » : close sans page écrite (Doublon)" in text and "Question" not in text
    assert "Une note laissée" not in text

    # Disabled in "My account": nothing more.
    login(client, "alice")
    client.post("/account/notifications", data={"csrf": csrf(client, "/account")})
    db.expire_all()
    assert wiki.alice.user.notify_tidy is False
    last = note(db, database, ctx, wiki, "Dernière")
    script = Scripted([[("leave_in_inbox", {"item": last, "reason": "x"})]])
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert len(mailbox.messages) == 2
    assert 'name="notify_tidy" value="true" >' in client.get("/account").text


def test_depositor_addresses_and_who_gets_a_notice() -> None:
    assert tidy_notices.addresses("Alice <Alice@Example.com>, Bob <bob@x.example>") == (
        "alice@example.com",
        "bob@x.example",
    )
    assert tidy_notices.addresses("Synaptane") == ()
