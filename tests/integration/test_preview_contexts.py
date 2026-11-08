"""The preview of the other Markdown editors: inbox notes (new or corrected, from the inbox or
from anywhere), the purpose, the filing instructions and the team prompts. The same rendering
as their reading, the same protections as the page editor's preview (form token, length, one
at a time, nothing written), and exactly the rights of the corresponding edit."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole
from synaptane.services import content, deposits, organizations, team_prompts, vocation
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent
from synaptane.web import wiki as wiki_module
from synaptane.web.inbox_pages import note_place
from synaptane.web.wiki import MAX_PREVIEW
from tests.htmlutil import data_test, select_one
from tests.htmlutil import select as select_html
from tests.integration.conftest import make_user
from tests.integration.gitutil import git
from tests.integration.test_preview import checked_out, french, preview, signed_in
from tests.integration.wikidata import Wiki

NOTES = "/o/acme/infra/-/inbox/preview"
DOCUMENTS = "/o/acme/infra/-/vocation/preview"
BODY = """Décision : **migrer**. Voir [[proxmox]], [la voisine](proxmox.md),
l'[accueil](../README.md) et ![[schema.png]].

```mermaid
graph TD
  A --> B
```

<script>alert(1)</script> [piège](javascript:alert(3))
"""
PROMPT = """---
title: Revue
description: La revue de la semaine
arguments: sujet
---
## Revue de {{sujet}}

Modèle : {{include serveurs/proxmox.md}}
"""


def drop(db: Session, database: Database, ctx: Workspace, wiki: Wiki, hint: str = "") -> str:
    path = deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title="Note", body=BODY, source="web", hint=hint
    )
    db.commit()
    return path.rsplit("/", 1)[1]


def audit_count(db: Session) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(AuditEvent)) or 0


# --- Notes ------------------------------------------------------------------------------------


@pytest.mark.parametrize("hint", ["", "serveurs/", "serveurs/proxmox.md", "dans le dossier"])
def test_a_note_previews_as_it_reads(
    client: TestClient, wiki: Wiki, db: Session, database: Database, ctx: Workspace, hint: str
) -> None:
    name = drop(db, database, ctx, wiki, hint)
    token = signed_in(client, "alice")
    shown = preview(client, token, NOTES, text=BODY, note=name, hint=hint)
    assert shown.status_code == 200, shown.text
    assert shown.headers["cache-control"] == "no-store"
    assert shown.headers["x-preview-page"] == f"/o/acme/infra/inbox/{name}"
    html = shown.text
    assert (
        "<strong>migrer</strong>" in html and 'href="/o/acme/infra/p/serveurs/proxmox.md"' in html
    )
    assert "<script" not in html and 'href="javascript' not in html
    assert '<code class="language-mermaid">' in html
    # Exactly what the note shows in the inbox.
    reading = client.get(f"/o/acme/infra/inbox/{name}").text
    assert f'<div class="markdown hl">{html}</div>' in reading
    # Relative links: from the folder the hint names, from the root otherwise.
    neighbour = html.split(">la voisine</a>")[0].rsplit("<a ", 1)[1]
    if hint.startswith("serveurs"):
        assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in neighbour
        assert html.count('href="/o/acme/infra/p/README.md"') == 1  # ../README.md
    else:
        assert 'href="/o/acme/infra/p/proxmox.md"' in neighbour


def test_where_the_links_of_a_note_start_from() -> None:
    paths = ["README.md", "serveurs/proxmox.md", "serveurs/réseau/vlan.md"]
    assert note_place("", paths) == ""
    assert note_place("serveurs", paths) == "serveurs/note.md"
    assert note_place("/serveurs/réseau/", paths) == "serveurs/réseau/note.md"
    assert note_place("serveurs/proxmox.md", paths) == "serveurs/proxmox.md"
    assert note_place("serv", paths) == ""  # not a folder: a prefix of one
    assert note_place("dans serveurs, page Proxmox", paths) == ""
    assert note_place("serveurs", None) == ""  # space not synced yet


def test_a_new_note_previews_from_the_inbox_and_from_anywhere(
    client: TestClient, wiki: Wiki
) -> None:
    token = signed_in(client, "alice")
    shown = preview(client, token, NOTES, text="[voisine](proxmox.md)", hint="serveurs")
    assert shown.status_code == 200
    assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in shown.text
    assert shown.headers["x-preview-page"] == "/o/acme/infra/inbox"
    # The inbox's drop form: Write and Preview (a narrow column, no side by side).
    inbox = client.get("/o/acme/infra/inbox").text
    [box] = select_html(inbox, "[data-preview]")
    assert box.attrs["data-preview"] == NOTES and box.attrs["data-preview-fields"] == "hint"
    assert (
        box.attrs["data-preview-space"] == "" and json.loads(box.attrs["data-preview-params"]) == {}
    )
    assert select_one(inbox, "textarea#body").attrs["name"] == "body"
    assert select_html(inbox, data_test("editor-split")) == []
    assert select_one(inbox, data_test("editor-preview")).get("aria-controls") == "body-preview"
    # The drop page reachable from everywhere: in the space chosen in the form.
    anywhere = client.get("/deposit").text
    [box] = select_html(anywhere, "[data-preview]")
    assert box.attrs["data-preview"] == "/o/{space}/-/inbox/preview"
    assert box.attrs["data-preview-space"] == "space"
    assert select_html(anywhere, data_test("editor-split")) == []


def test_only_those_who_may_write_the_note_preview_it(
    client: TestClient, wiki: Wiki, db: Session, database: Database, ctx: Workspace
) -> None:
    name = drop(db, database, ctx, wiki)
    # A reader can neither drop nor correct a note.
    token = signed_in(client, "bob")
    for data in ({"note": name}, {}):
        refused = preview(client, token, NOTES, text="# Essai", **data)
        assert refused.status_code == 403 and "Essai" not in refused.text
        assert refused.headers["content-type"].startswith("text/plain")
    assert (
        select_html(client.get(f"/o/acme/infra/inbox/{name}?edit=1").text, "[data-preview]") == []
    )
    # A space Alice cannot see does not exist for her.
    token = signed_in(client, "alice")
    hidden = preview(client, token, "/o/acme/autre/-/inbox/preview", text="# Essai")
    assert hidden.status_code == 404 and "Essai" not in hidden.text
    # Only a note of this inbox: not an unknown one, nor a changed page (not edited this way).
    for unknown in ("absente.md", "../README.md", "x"):
        assert preview(client, token, NOTES, text="# x", note=unknown).status_code == 404
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "neuve.md", b"# Neuve\n")
    db.commit()
    [change] = [i for i in deposits.inbox(db, wiki.alice, ctx, wiki.infra) if i.type == "change"]
    assert preview(client, token, NOTES, text="# x", note=change.name).status_code == 404


def test_the_note_editor_offers_the_preview(
    client: TestClient, wiki: Wiki, db: Session, database: Database, ctx: Workspace
) -> None:
    name = drop(db, database, ctx, wiki, "serveurs/")
    signed_in(client, "alice")
    page = client.get(f"/o/acme/infra/inbox/{name}?edit=1").text
    [box] = select_html(page, "[data-preview]")
    assert box.attrs["data-preview"] == NOTES
    assert json.loads(box.attrs["data-preview-params"]) == {"note": name}
    assert box.attrs["data-preview-fields"] == "hint"
    assert box.attrs["data-preview-max"] == str(MAX_PREVIEW)
    assert french(box.attrs["data-preview-too-long"]) == "Ce texte est trop long pour l'aperçu."
    assert [b.text for b in select_html(page, "[data-preview-mode]")] == [
        "Écrire",
        "Aperçu",
        "Côte à côte",
    ]
    assert select_one(page, data_test("editor-split")).get("aria-controls") == (
        "edit-body edit-body-preview"
    )
    textarea = select_one(page, "textarea#edit-body")
    assert textarea.attrs["name"] == "body" and "editor" in textarea.classes
    assert select_one(page, "label[for=edit-body]").text == "Contenu (Markdown)"
    pane = select_one(page, "#edit-body-preview")
    assert "hidden" in pane.attrs
    region = select_one(page, "#edit-body-preview [role=region]")
    assert region.get("aria-labelledby") == "edit-body-preview-label"


def test_previewing_a_note_writes_nothing(
    client: TestClient, wiki: Wiki, db: Session, database: Database, ctx: Workspace
) -> None:
    name = drop(db, database, ctx, wiki)
    token = signed_in(client, "alice")
    head = ctx.local_repository(wiki.infra).head()
    remote_head = git(wiki.remote.bare, "rev-parse", "main")
    before = deposits.read_item(wiki.alice, ctx, wiki.infra, name)
    inbox = deposits.inbox(db, wiki.alice, ctx, wiki.infra)
    events = audit_count(db)
    for data in ({"note": name}, {}):
        for text in ("# Autre", "Mot de passe : hunter2-tres-secret"):
            assert (
                preview(client, token, NOTES, text=text, hint="serveurs", **data).status_code == 200
            )
    assert ctx.local_repository(wiki.infra).head() == head
    assert git(wiki.remote.bare, "rev-parse", "main") == remote_head
    assert deposits.read_item(wiki.alice, ctx, wiki.infra, name) == before
    db.expire_all()
    assert deposits.inbox(db, wiki.alice, ctx, wiki.infra) == inbox
    assert audit_count(db) == events


def test_a_note_preview_needs_the_form_token_and_a_text_of_a_reasonable_length(
    client: TestClient, wiki: Wiki
) -> None:
    signed_in(client, "alice")
    forged = preview(client, "faux-jeton", NOTES, text="# Essai")
    assert forged.status_code == 403 and "expiré" in forged.text
    token = signed_in(client, "alice")
    long = preview(client, token, NOTES, text="a" * (MAX_PREVIEW + 1))
    assert long.status_code == 400
    assert french(long.text) == "Ce texte est trop long pour l'aperçu."
    client.cookies.clear()
    anonymous = preview(client, "x", NOTES, text="# Essai")
    assert anonymous.status_code == 303 and anonymous.headers["location"].startswith("/login")


# --- Purpose, filing instructions, team prompts -----------------------------------------------


def nojs(client: TestClient, url: str, token: str, **data: str) -> str:
    """What the form's own "Preview" button shows (without JS), as HTML."""
    shown = client.post(url, data={"csrf": token, "action": "preview", **data})
    assert shown.status_code == 200, shown.text[:300]
    return shown.text


def test_the_purpose_previews_as_without_js(client: TestClient, wiki: Wiki, ctx: Workspace) -> None:
    token = signed_in(client, "root")
    text = "# Vocation\n\n<!-- aide du modèle -->Les [[proxmox]] et l'[accueil](README.md).\n"
    shown = preview(client, token, DOCUMENTS, text=text, kind="vocation")
    assert shown.status_code == 200 and shown.headers["cache-control"] == "no-store"
    assert shown.headers["x-preview-page"] == "/o/acme/infra/-/vocation"
    html = shown.text
    assert '<h1 id="vocation">Vocation</h1>' in html and "aide du modèle" not in html
    assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in html  # wiki links resolved
    assert 'href="/o/acme/infra/p/README.md"' in html
    page = nojs(client, "/o/acme/infra/-/vocation", token, text=text)
    assert f'<div class="markdown">{html}</div>' in page
    # The filing instructions: the text as written.
    rules = "- Une page par <!-- fournisseur --> **fournisseur**."
    shown = preview(client, token, DOCUMENTS, text=rules, kind="tidy-rules")
    assert "<strong>fournisseur</strong>" in shown.text
    page = nojs(client, "/o/acme/infra/-/vocation/tidy-rules", token, text=rules)
    assert f'<div class="markdown">{shown.text}</div>' in page
    assert vocation.space_vocation(wiki.root, ctx, wiki.infra) is None  # nothing saved


def test_a_prompt_previews_as_without_js(client: TestClient, wiki: Wiki) -> None:
    token = signed_in(client, "root")
    shown = preview(client, token, DOCUMENTS, text=PROMPT, kind="prompt")
    assert shown.status_code == 200
    assert shown.headers["x-preview-page"] == "/o/acme/infra/-/prompts"
    html = shown.text
    assert '<h2 id="revue-de-sujet">Revue de {{sujet}}</h2>' in html
    assert "title: Revue" not in html  # the front matter is not part of the text
    assert "{{include serveurs/proxmox.md}}" in html  # nothing included: no page read
    # A new prompt (its name not checked to preview), then an existing one.
    page = nojs(client, "/o/acme/infra/-/prompts", token, name="Pas Valide", text=PROMPT)
    assert f'<div class="markdown">{html}</div>' in page and 'value="Pas Valide"' in page
    assert team_prompts.check_name("revue")
    created = client.post(
        "/o/acme/infra/-/prompts", data={"csrf": token, "name": "revue", "text": PROMPT}
    )
    assert "Prompt enregistré" in created.text
    edited = PROMPT.replace("Revue de", "Bilan de")
    page = nojs(client, "/o/acme/infra/-/prompts/revue", token, text=edited)
    assert "Bilan de {{sujet}}</h2>" in page and "pas encore enregistré" in page
    shown = preview(client, token, DOCUMENTS, text=edited, kind="prompt")
    assert f'<div class="markdown">{shown.text}</div>' in page
    # Nothing saved by the preview.
    assert "Revue de" in client.get("/o/acme/infra/-/prompts").text


def test_only_the_organizations_admins_preview_its_settings(
    client: TestClient, wiki: Wiki, db: Session
) -> None:
    admin = make_user(db, "olga")
    organizations.set_member(db, wiki.root, wiki.infra.organization, admin, OrgRole.ADMIN)
    db.commit()
    for username in ("alice", "bob"):  # a contributor, a reader: the page does not exist
        token = signed_in(client, username)
        assert client.get("/o/acme/infra/-/vocation").status_code == 404
        for kind in ("vocation", "tidy-rules", "prompt"):
            refused = preview(client, token, DOCUMENTS, text="# Essai", kind=kind)
            assert refused.status_code == 404 and "Essai" not in refused.text
        for url in ("/o/acme/infra/-/vocation", "/o/acme/infra/-/prompts"):
            refused = client.post(url, data={"csrf": token, "text": "# Essai", "action": "preview"})
            assert refused.status_code == 404 and 'id="essai"' not in refused.text
    token = signed_in(client, "olga")
    for kind in ("vocation", "tidy-rules", "prompt"):
        assert preview(client, token, DOCUMENTS, text="# Essai", kind=kind).status_code == 200
    assert preview(client, token, DOCUMENTS, text="# x", kind="autre").status_code == 404
    other = preview(client, token, "/o/acme/autre/-/vocation/preview", text="# Autre")
    assert other.status_code == 200  # every space of the organization


def test_previewing_the_settings_writes_nothing(
    client: TestClient, wiki: Wiki, db: Session, ctx: Workspace
) -> None:
    token = signed_in(client, "root")
    head = ctx.local_repository(wiki.infra).head()
    remote_head = git(wiki.remote.bare, "rev-parse", "main")
    events = audit_count(db)
    for kind in ("vocation", "tidy-rules", "prompt"):
        assert preview(client, token, DOCUMENTS, text="# Essai", kind=kind).status_code == 200
    nojs(client, "/o/acme/infra/-/vocation", token, text="# Essai")
    nojs(client, "/o/acme/infra/-/vocation/tidy-rules", token, text="# Essai")
    nojs(client, "/o/acme/infra/-/prompts", token, name="essai", text="# Essai")
    assert ctx.local_repository(wiki.infra).head() == head
    assert git(wiki.remote.bare, "rev-parse", "main") == remote_head
    for document in (vocation.VOCATION, vocation.RULES, team_prompts.kind("essai")):
        assert vocation.document(wiki.root, ctx, wiki.infra, document).sha is None
    assert team_prompts.of_space(ctx, wiki.infra) == []
    assert audit_count(db) == events


def test_settings_previews_need_the_token_and_a_text_of_a_reasonable_length(
    client: TestClient, wiki: Wiki
) -> None:
    signed_in(client, "root")
    forged = preview(client, "faux-jeton", DOCUMENTS, text="# Essai")
    assert forged.status_code == 403 and "expiré" in forged.text
    token = signed_in(client, "root")
    too_long = "Ce texte est trop long pour l'aperçu."
    long = preview(client, token, DOCUMENTS, text="a" * (MAX_PREVIEW + 1), kind="tidy-rules")
    assert long.status_code == 400 and french(long.text) == too_long
    # The preview without JS too: the error in the card, the text kept, nothing rendered.
    for url in ("/o/acme/infra/-/vocation/tidy-rules", "/o/acme/infra/-/prompts"):
        refused = client.post(
            url, data={"csrf": token, "action": "preview", "text": "a" * (MAX_PREVIEW + 1)}
        )
        assert refused.status_code == 400 and too_long in french(unescape(refused.text))
        assert "vocation-preview" not in refused.text and "a" * 1000 in refused.text


def test_the_settings_editors_offer_the_preview(client: TestClient, wiki: Wiki) -> None:
    token = signed_in(client, "root")
    page = client.get("/o/acme/infra/-/vocation").text
    boxes = select_html(page, "[data-preview]")
    assert [json.loads(b.attrs["data-preview-params"]) for b in boxes] == [
        {"kind": "vocation"},
        {"kind": "tidy-rules"},
    ]
    assert {b.attrs["data-preview"] for b in boxes} == {DOCUMENTS}
    for key in ("vocation", "tidy-rules"):
        textarea = select_one(page, f"textarea#{key}-text")
        assert textarea.attrs["name"] == "text" and "vocation-editor" in textarea.classes
        assert textarea.get("aria-describedby") == f"{key}-text-help"
        assert "20" in select_one(page, f"#{key}-text-help").text
        assert "hidden" in select_one(page, f"#{key}-text-preview").attrs
    # The reloading "Preview" stays, for a browser without JS; preferences.js hides it.
    fallbacks = select_html(page, "button[data-preview-fallback]")
    assert [(b.get("name"), b.get("value")) for b in fallbacks] == [("action", "preview")] * 2
    # Team prompts: each one, and the new one.
    client.post("/o/acme/infra/-/prompts", data={"csrf": token, "name": "revue", "text": PROMPT})
    page = client.get("/o/acme/infra/-/prompts").text
    boxes = select_html(page, "[data-preview]")
    assert [json.loads(b.attrs["data-preview-params"]) for b in boxes] == [{"kind": "prompt"}] * 2
    assert select_one(page, "label[for=prompt-revue-text]").text == (
        "Texte (Markdown) · .synaptane/prompts/revue.md"
    )
    assert select_one(page, "textarea#new-prompt-text").attrs["name"] == "text"
    assert len(select_html(page, "button[data-preview-fallback]")) == 2
    assert len(select_html(page, data_test("editor-split"))) == 2


# --- Shared ---------------------------------------------------------------------------------------


def test_one_preview_at_a_time_whatever_the_editor(
    client: TestClient,
    wiki: Wiki,
    database: Database,
    db: Session,
    ctx: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a note's preview renders (its database connection given back), the same account's
    other previews wait their turn: a page's, the purpose's."""
    name = drop(db, database, ctx, wiki)
    token = signed_in(client, "root")
    started, finish = threading.Event(), threading.Event()
    connections: list[int] = []
    render = wiki_module.render_at  # what the inbox imports

    def slow(*args: Any) -> Markup:
        if args[1] == "# Long":
            connections.append(checked_out(database))
            started.set()
            assert finish.wait(10)
        return render(*args)

    monkeypatch.setattr("synaptane.web.inbox_pages.render_at", slow)
    idle = checked_out(database)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(preview, client, token, NOTES, text="# Long", note=name)
        assert started.wait(10)
        assert connections == [idle]
        for url, data in (
            (NOTES, {}),
            ("/o/acme/infra/-/preview", {"path": "README.md"}),
            (DOCUMENTS, {"kind": "vocation"}),
        ):
            assert preview(client, token, url, text="# Autre", **data).status_code == 429
        refused = client.post(
            "/o/acme/infra/-/vocation", data={"csrf": token, "action": "preview", "text": "# x"}
        )
        assert refused.status_code == 429 and "il suivra" in french(refused.text)
        finish.set()
        assert first.result(10).status_code == 200
    assert preview(client, token, DOCUMENTS, text="# Après").status_code == 200
    assert set() == wiki_module._PREVIEWING
