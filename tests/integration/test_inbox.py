import base64
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.services import activity, content, deposits
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.conftest import token_for
from tests.integration.gitutil import git
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki


def test_deposit_commits_note_and_attachments(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    path = deposits.deposit(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        title="Nouveau NAS",
        body="Le NAS passe à **8 To**.",
        source="web",
        hint="infra/stockage",
        attachments=[("devis.pdf", b"%PDF-1.4 devis"), ("photo.jpg", b"jpeg")],
    )
    clone = ctx.clone_path(wiki.infra.id)
    files = git(clone, "show", "--name-only", "--format=%an|%s", "HEAD").split("\n")
    assert files[0] == "Alice|Inbox: Nouveau NAS"
    assert path in files and f"{path[:-3]}/devis.pdf" in files
    [item] = [i for i in deposits.list_items(wiki.bob, ctx, wiki.infra) if i.type == "note"]
    assert (item.title, item.hint, item.attachments) == (
        "Nouveau NAS",
        "infra/stockage",
        ["devis.pdf", "photo.jpg"],
    )
    assert "8 To" in item.body and item.deposited_by.startswith("Alice <")
    assert (
        deposits.read_attachment(wiki.bob, ctx, wiki.infra, item.name, "devis.pdf")
        == b"%PDF-1.4 devis"
    )

    with pytest.raises(SynaptaneError) as denied:
        deposits.deposit(db, database, wiki.bob, ctx, wiki.infra, title="x", body="y", source="web")
    assert denied.value.code == "forbidden"  # a reader cannot drop notes
    for bad in ([("", b"x")], [(".cache", b"x")], [("a.txt", b"x"), ("dir/a.txt", b"y")]):
        with pytest.raises(SynaptaneError):
            deposits.deposit(
                db,
                database,
                wiki.alice,
                ctx,
                wiki.infra,
                title="x",
                body="y",
                source="web",
                attachments=bad,
            )
    with pytest.raises(SynaptaneError):
        deposits.deposit(
            db, database, wiki.alice, ctx, wiki.infra, title="vide", body=" ", source="web"
        )


def test_direct_edits_show_up_as_changes(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    from synaptane.services import content

    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# A\n")
    db.commit()
    assert deposits.list_items(wiki.alice, ctx, wiki.infra) == []  # no inbox file
    [change] = deposits.inbox(db, wiki.alice, ctx, wiki.infra)
    assert change.type == "change" and change.title == "Page created: a.md"
    detail = deposits.describe_item(db, wiki.alice, ctx, wiki.infra, change.name)
    assert "+# A" in detail.body and "Alice" in detail.body


def test_inbox_web_page(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    token = csrf(client, "/o/acme/infra/inbox")
    response = client.post(
        "/o/acme/infra/-/inbox",
        data={"csrf": token, "title": "Compte rendu", "body": "Décision : **migrer**.", "hint": ""},
        files=[("files", ("notes.txt", b"brouillon", "text/plain"))],
    )
    page = response.text
    assert "Compte rendu" in page and "ce sera rangé" in page
    name = page.split("/o/acme/infra/inbox/")[1].split('"')[0]
    item = client.get(f"/o/acme/infra/inbox/{name}").text
    assert "<strong>migrer</strong>" in item and "notes.txt" in item
    attachment = client.get(f"/o/acme/infra/inbox/{name}/files/notes.txt")
    assert (
        attachment.content == b"brouillon"
        and "sandbox" in attachment.headers["content-security-policy"]
    )

    refused = client.post(
        "/o/acme/infra/-/inbox",
        data={"csrf": token, "title": "   ", "body": "Un compte rendu très long", "hint": "x/y"},
    )
    assert refused.status_code == 400 and "Un compte rendu très long" in refused.text

    secret = {"csrf": token, "title": "Accès", "body": "mdp = Pr0xm0x!2026"}
    flagged = client.post("/o/acme/infra/-/inbox", data=secret)
    assert flagged.status_code == 400 and "semble contenir un secret" in flagged.text
    assert 'name="allow_secrets"' in flagged.text  # checkbox to confirm
    confirmed = client.post("/o/acme/infra/-/inbox", data={**secret, "allow_secrets": "true"})
    assert "ce sera rangé" in confirmed.text

    client.cookies.clear()
    login(client, "bob")
    reader_page = client.get("/o/acme/infra/inbox").text
    assert "Compte rendu" in reader_page and "Déposer une information" not in reader_page


def test_inbox_rest(client: TestClient, database: Database, wiki: Wiki) -> None:
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    base = "/api/v1/organizations/acme/spaces/infra/inbox"
    created = client.post(
        base,
        json={
            "title": "Lien utile",
            "content": "Doc Proxmox",
            "source": "https://pve.proxmox.com",
            "attachments": [{"name": "a.txt", "content_base64": base64.b64encode(b"a").decode()}],
        },
        headers=alice,
    )
    assert created.status_code == 201
    items = client.get(base, headers=alice).json()
    assert [(i["title"], i["attachments"]) for i in items] == [("Lien utile", ["a.txt"])]
    assert (items[0]["origin"], items[0]["via"]) == ("https://pve.proxmox.com", "API · tests")
    leak = client.post(
        base, json={"title": "Clé", "content": "AKIAIOSFODNN7EXAMPLE"}, headers=alice
    )
    assert leak.status_code == 422 and leak.json()["error"] == "secret_detected"
    bad = client.post(
        base,
        json={"title": "x", "attachments": [{"name": "a", "content_base64": "@@"}]},
        headers=alice,
    )
    assert bad.status_code == 400


def test_secrets_are_refused_before_any_commit(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    note: dict[str, Any] = {
        "title": "Accès Proxmox",
        "body": "root / mdp = Pr0xm0x!2026",
        "source": "mcp",
    }
    with pytest.raises(SynaptaneError) as refused:
        deposits.deposit(db, database, wiki.alice, ctx, wiki.infra, **note)
    assert refused.value.code is ErrorCode.SECRET and repo.head() == head
    with pytest.raises(SynaptaneError):
        deposits.deposit(
            db,
            database,
            wiki.alice,
            ctx,
            wiki.infra,
            title="Clé",
            body="voir pièce jointe",
            source="web",
            attachments=[("id_ed25519", b"-----BEGIN OPENSSH PRIVATE KEY-----\n")],
        )
    assert repo.head() == head
    deposits.deposit(db, database, wiki.alice, ctx, wiki.infra, **note, allow_secrets=True)
    assert repo.head() != head  # confirmed by the person

    with pytest.raises(SynaptaneError):
        content.write_file(
            db, database, wiki.alice, ctx, wiki.infra, "acces.md", b"# A\n\ntoken: Zx9-abcdefgh\n"
        )
    content.write_file(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        "acces.md",
        b"# A\n\ntoken: Zx9-abcdefgh\n",
        allow_secrets=True,
    )
    # The page already contains this secret: editing it elsewhere is still possible.
    content.write_file(
        db, database, wiki.alice, ctx, wiki.infra, "acces.md", b"# Acces\n\ntoken: Zx9-abcdefgh\n"
    )


def test_contributors_manage_notes(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    def note(title: str) -> str:
        path = deposits.deposit(
            db,
            database,
            wiki.alice,
            ctx,
            wiki.infra,
            title=title,
            body="Brouillon.",
            source="web",
            attachments=[("schema.txt", b"schema")],
        )
        db.commit()
        return path.rsplit("/", 1)[-1]

    first, second, third = note("Panne réseau"), note("Achat NAS"), note("Mauvais espace")
    repo = ctx.local_repository(wiki.infra)
    # A note left by tidying, then corrected, becomes pending again.
    item = deposits.read_item(wiki.alice, ctx, wiki.infra, first)
    left = (repo.blob(item.path) or b"").replace(b"type: note", b"type: note\nstatus: left")
    repo.commit_changes({item.path: left}, ctx.committer, "laissée")
    deposits.update_item(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        first,
        title="Panne réseau du 3 octobre",
        body="Le switch du rack 2 a lâché.",
        hint="infra/incidents",
    )
    db.commit()
    edited = deposits.read_item(wiki.alice, ctx, wiki.infra, first)
    assert (edited.title, edited.status, edited.hint) == (
        "Panne réseau du 3 octobre",
        "pending",
        "infra/incidents",
    )
    assert "rack 2" in edited.body and edited.attachments == ["schema.txt"]

    with pytest.raises(SynaptaneError):  # Bob is a reader
        deposits.delete_item(db, database, wiki.bob, ctx, wiki.infra, second)
    deposits.delete_item(db, database, wiki.alice, ctx, wiki.infra, second)
    db.commit()
    assert repo.blob(item.path.replace(first, second)) is None

    with pytest.raises(SynaptaneError):  # Alice does not contribute to the other space
        deposits.move_item(db, database, wiki.alice, ctx, wiki.infra, third, wiki.other)
    deposits.move_item(db, database, wiki.root, ctx, wiki.infra, third, wiki.other)
    db.commit()
    [moved] = deposits.list_items(wiki.root, ctx, wiki.other)
    assert moved.title == "Mauvais espace" and moved.attachments == ["schema.txt"]
    assert deposits.read_attachment(wiki.root, ctx, wiki.other, moved.name, "schema.txt") == (
        b"schema"
    )
    remaining = deposits.list_items(wiki.alice, ctx, wiki.infra)
    assert [i.title for i in remaining] == ["Panne réseau du 3 octobre"]
    assert [i.title for i in deposits.matching(remaining, "rack")] == ["Panne réseau du 3 octobre"]
    assert deposits.matching(remaining, "inconnu") == []


def test_a_change_can_be_dismissed(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "typo.md", b"# Typo\n")
    db.commit()
    [change] = [i for i in deposits.inbox(db, wiki.alice, ctx, wiki.infra) if i.type == "change"]
    with pytest.raises(SynaptaneError):
        deposits.dismiss_change(db, wiki.bob, ctx, wiki.infra, change.name)
    deposits.dismiss_change(db, wiki.alice, ctx, wiki.infra, change.name)
    db.commit()
    db.expire_all()
    assert [i for i in deposits.inbox(db, wiki.alice, ctx, wiki.infra) if i.type == "change"] == []


def test_inbox_management_and_activity_in_the_web(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title="Note web", body="x", source="web"
    )
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "guide.md", b"# Guide\n")
    db.commit()
    events, _ = activity.space_activity(wiki.alice, ctx, wiki.infra)
    assert [e.kind for e in events[:2]] == ["page", "inbox"]
    assert datetime.fromisoformat(events[0].date)  # clean ISO date (git -z output)
    assert "system" in {e.kind for e in events} and events[0].pages == ("guide.md",)
    assert any(e.external for e in events)  # content pushed directly to the repository
    login(client, "alice")
    listing = client.get("/o/acme/infra/inbox?q=web&kind=note").text
    assert "Note web" in listing and "Page créée\u00a0: guide.md" not in listing
    name = listing.split("/o/acme/infra/inbox/")[1].split('"')[0]
    token = csrf(client, f"/o/acme/infra/inbox/{name}")
    page = client.get(f"/o/acme/infra/inbox/{name}").text
    assert 'href="?edit=1"' in page and "/delete" in page
    editing = client.get(f"/o/acme/infra/inbox/{name}?edit=1").text
    assert "/-/inbox/" + name + "/edit" in editing
    edited = client.post(
        f"/o/acme/infra/-/inbox/{name}/edit",
        data={"csrf": token, "title": "Note corrigée", "body": "y"},
    )
    assert "Note modifiée" in edited.text and "Note corrigée" in edited.text
    deleted = client.post(f"/o/acme/infra/-/inbox/{name}/delete", data={"csrf": token})
    assert "supprimée" in deleted.text

    timeline = client.get("/o/acme/infra/activity").text
    assert "Inbox: delete “Note corrigée”" in timeline
    assert "/o/acme/infra/diff/" in timeline and "guide.md" in timeline
    pages_only = client.get("/o/acme/infra/activity?kind=page").text
    assert "guide.md" in pages_only and "Inbox: delete" not in pages_only


def test_a_left_change_can_be_dismissed(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    from synaptane.services import changes

    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# A\n")
    db.commit()
    [(item, change)] = changes.pending(db, ctx, wiki.infra)
    head = ctx.local_repository(wiki.infra).head()
    assert head is not None
    changes.record(db, wiki.infra, {change.path: ("left", "à trancher", change)}, head)
    db.flush()
    changes.compact(db, ctx, wiki.infra)  # the marker moves on: item left, no commit
    db.commit()
    deposits.dismiss_change(db, wiki.alice, ctx, wiki.infra, item.name)
    db.commit()
    db.expire_all()
    assert changes.pending(db, ctx, wiki.infra) == []


def test_inbox_is_paginated(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    for n in range(3):
        deposits.deposit(
            db, database, wiki.alice, ctx, wiki.infra, title=f"Note {n}", body="x", source="web"
        )
    db.commit()
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    base = "/api/v1/organizations/acme/spaces/infra/inbox"
    first = client.get(f"{base}?kind=note&limit=2", headers=alice)
    assert first.headers["X-Total-Count"] == "3" and len(first.json()) == 2
    rest = client.get(f"{base}?kind=note&limit=2&offset=2", headers=alice).json()
    assert [i["title"] for i in rest] == ["Note 2"]


def test_pending_notes_are_found_by_search(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title="Accès VPN", body="x", source="web"
    )
    db.commit()
    login(client, "alice")
    page = client.get("/search?q=acces+vpn").text  # without accent: still found
    assert "En attente de rangement" in page and "<mark>Accès</mark> <mark>VPN</mark>" in page


def test_two_notes_with_the_same_name_do_not_overwrite_each_other(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same second, same title, same random suffix: the name is checked under the lock, and
    the second note takes another one (including when moving to a space where the name is
    already taken)."""
    from synaptane.services import inbox

    fixed = f"{inbox.INBOX_DIR}/2026-10-06T10-00-00Z-note-import-abcd.md"
    monkeypatch.setattr(inbox, "item_name", lambda *_: fixed)
    for body in ("première", "seconde"):
        deposits.deposit(
            db,
            database,
            wiki.root,
            ctx,
            wiki.infra,
            title="Import",
            body=body,
            source="api",
            attachments=[("pj.txt", body.encode())],
        )
        db.commit()
    notes = deposits.list_items(wiki.root, ctx, wiki.infra)
    assert len(notes) == 2 and fixed in {n.path for n in notes}
    bodies = {deposits.read_item(wiki.root, ctx, wiki.infra, n.name).body.strip() for n in notes}
    assert bodies == {"première", "seconde"}
    for note in notes:  # each one keeps its own attachment
        data = deposits.read_attachment(wiki.root, ctx, wiki.infra, note.name, "pj.txt")
        assert (
            data.decode() == deposits.read_item(wiki.root, ctx, wiki.infra, note.name).body.strip()
        )

    deposits.deposit(
        db, database, wiki.root, ctx, wiki.other, title="Import", body="déjà là", source="api"
    )
    db.commit()
    moved = deposits.move_item(db, database, wiki.root, ctx, wiki.infra, notes[0].name, wiki.other)
    db.commit()
    assert moved != fixed
    there = {
        deposits.read_item(wiki.root, ctx, wiki.other, n.name).body.strip()
        for n in deposits.list_items(wiki.root, ctx, wiki.other)
    }
    assert len(there) == 2 and "déjà là" in there  # the existing note is not overwritten
