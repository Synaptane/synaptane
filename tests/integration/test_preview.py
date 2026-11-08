"""The editor's preview: the text not saved yet, rendered exactly as the page will read, with
nothing written (no commit, no index, no inbox), and only for those who can edit."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from markupsafe import Markup
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from synaptane.core.access import SpaceRole
from synaptane.core.errors import not_found
from synaptane.services import content, deposits, git_sync, spaces
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.web import wiki as wiki_module
from synaptane.web.wiki import MAX_PREVIEW
from tests.htmlutil import data_test, select, select_one
from tests.integration.gitutil import git
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

PREVIEW = "/o/acme/infra/-/preview"
PAGE = "serveurs/proxmox.md"
TEXT = """# Proxmox

> [!warning] Attention
> La sauvegarde ==nocturne== tourne à 2h. %% note cachée %%

Un paragraphe à citer ^bloc-1

Voir [[README]], [[absente]], l'[accueil](../README.md) et ![[schema.png|300]].

```mermaid
graph TD
  A["<b>Proxmox</b>"] --> B
```

```python
print("ok")
```

<script>alert(1)</script><img src=x onerror=alert(2)> [piège](javascript:alert(3))
"""


def french(text: str) -> str:
    """Without the non-breaking spaces the translation puts before `:`."""
    return text.replace("\N{NO-BREAK SPACE}", " ")


def signed_in(client: TestClient, username: str) -> str:
    client.cookies.clear()
    login(client, username)
    return csrf(client, "/account")


def preview(client: TestClient, token: str, url: str = PREVIEW, **data: str) -> Response:
    return client.post(url, data={"csrf": token, **data}, follow_redirects=False)


def test_the_preview_is_the_reading_view(client: TestClient, wiki: Wiki, ctx: Workspace) -> None:
    token = signed_in(client, "alice")
    shown = preview(client, token, text=TEXT, path=PAGE)
    assert shown.status_code == 200
    assert shown.headers["content-type"].startswith("text/html")
    assert shown.headers["cache-control"] == "no-store"
    html = shown.text
    # Obsidian syntax, links resolved in the space, code highlighted, Mermaid left as code.
    assert '<h1 id="proxmox">Proxmox</h1>' in html
    assert 'class="callout callout-warn"' in html and "<mark>nocturne</mark>" in html
    assert "note cachée" not in html and "^bloc-1" not in html
    assert '<a href="/o/acme/infra/p/README.md" rel="noopener noreferrer">README</a>' in html
    assert 'href="/o/acme/infra/p/serveurs/absente.md" class="wikilink-missing"' in html
    assert ">accueil</a>" in html and html.count('href="/o/acme/infra/p/README.md"') == 2
    assert 'src="/o/acme/infra/raw/serveurs/schema.png"' in html and 'width="300"' in html
    assert '<code class="language-mermaid">' in html
    assert '<span class="nb">print</span>' in html
    # HTML written in the page stays text; a dangerous link is not one.
    assert "<script" not in html and "<img src=x" not in html and 'href="javascript' not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&lt;img src=x onerror=alert(2)&gt;" in html
    # Saved, the page reads exactly as previewed.
    saved = client.post(
        f"/o/acme/infra/edit/{PAGE}",
        data={
            "csrf": token,
            "text": TEXT,
            "base_sha": content.read_file(wiki.alice, ctx, wiki.infra, PAGE).sha,
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    reading = client.get(f"/o/acme/infra/p/{PAGE}").text
    assert f'<div class="markdown hl">{html}</div>' in reading


def test_wiki_links_and_images_are_found_anywhere_in_the_space(
    client: TestClient, wiki: Wiki, database: Database, ctx: Workspace
) -> None:
    wiki.remote.push({"content/images/schema.png": "image"}, "Image")
    git_sync.run_sync(database, ctx, wiki.infra.id)
    token = signed_in(client, "alice")
    html = preview(
        client, token, text="[[proxmox]] [[notes.txt]] ![[schema.png]]", path="README.md"
    )
    assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in html.text
    assert 'href="/o/acme/infra/p/serveurs/notes.txt"' in html.text
    assert '<img src="/o/acme/infra/raw/images/schema.png" alt="schema.png">' in html.text


def test_a_new_page_links_from_where_it_will_be(client: TestClient, wiki: Wiki) -> None:
    token = signed_in(client, "alice")
    text = "[Accueil](../README.md) [Voisine](proxmox.md)"
    html = preview(client, token, text=text, folder="serveurs", name="nouvelle").text
    assert 'href="/o/acme/infra/p/README.md"' in html
    assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in html
    # No name yet: the page will be in the folder all the same.
    html = preview(client, token, text=text, folder="serveurs").text
    assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in html
    # At the root.
    root = preview(client, token, text="[Proxmox](serveurs/proxmox.md)")
    assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in root.text
    # Its reading address: what the editor resolves the links left relative from.
    assert root.headers["x-preview-page"] == "/o/acme/infra/p/untitled.md"
    named = preview(client, token, text="# x", folder="serveurs", name="Notes été")
    assert named.headers["x-preview-page"] == "/o/acme/infra/p/serveurs/Notes%20%C3%A9t%C3%A9.md"


def test_a_new_page_links_to_itself_as_once_saved(client: TestClient, wiki: Wiki) -> None:
    token = signed_in(client, "alice")
    html = preview(client, token, text="[[autolien-x]]", folder="infra", name="autolien-x").text
    assert 'href="/o/acme/infra/p/infra/autolien-x.md" rel=' in html
    assert "wikilink-missing" not in html
    # A name already taken elsewhere: the link leads to the page being written, the nearest.
    html = preview(client, token, text="[[proxmox]]", folder="autre", name="proxmox").text
    assert 'href="/o/acme/infra/p/autre/proxmox.md"' in html


def test_only_markdown_pages_are_previewed(client: TestClient, wiki: Wiki) -> None:
    token = signed_in(client, "alice")
    for name in ("notes.txt", "page.html"):
        refused = preview(client, token, text="# Titre", folder="serveurs", name=name)
        assert refused.status_code == 400 and "<h1" not in refused.text
        assert french(refused.text) == "Seules les pages Markdown ont un aperçu."
    refused = preview(client, token, text="# Titre", path="serveurs/notes.txt")
    assert refused.status_code == 400
    for name in ("notes", "notes.md", "notes.MARKDOWN"):
        assert preview(client, token, text="# T", folder="x", name=name).status_code == 200


def test_an_empty_text_previews_as_nothing(client: TestClient, wiki: Wiki) -> None:
    token = signed_in(client, "alice")
    empty = preview(client, token, path=PAGE)
    assert empty.status_code == 200 and empty.text == ""


def test_nothing_is_written(client: TestClient, wiki: Wiki, ctx: Workspace, db: Session) -> None:
    token = signed_in(client, "alice")
    head = ctx.local_repository(wiki.infra).head()
    before = content.read_file(wiki.alice, ctx, wiki.infra, PAGE)
    inbox = deposits.inbox(db, wiki.alice, ctx, wiki.infra)
    remote_head = git(wiki.remote.bare, "rev-parse", "main")
    for text in (TEXT, "Mot de passe : hunter2-tres-secret", "# Autre"):
        assert preview(client, token, text=text, path=PAGE).status_code == 200
    assert preview(client, token, text="# Neuve", folder="", name="neuve").status_code == 200
    assert ctx.local_repository(wiki.infra).head() == head
    assert git(wiki.remote.bare, "rev-parse", "main") == remote_head
    after = content.read_file(wiki.alice, ctx, wiki.infra, PAGE)
    assert (after.sha, after.text) == (before.sha, before.text)
    files = {b.path for b in content.list_files(wiki.alice, ctx, wiki.infra)}
    assert "neuve.md" not in files
    db.expire_all()
    assert deposits.inbox(db, wiki.alice, ctx, wiki.infra) == inbox


def test_only_contributors_preview(client: TestClient, wiki: Wiki) -> None:
    # A reader cannot edit, so has nothing to preview.
    token = signed_in(client, "bob")
    refused = preview(client, token, text="# Essai", path=PAGE)
    assert refused.status_code == 403
    assert refused.headers["content-type"].startswith("text/plain")
    assert "lecteur" in refused.text and "Essai" not in refused.text
    # A space Alice cannot see does not exist for her; nor does an unknown space.
    token = signed_in(client, "alice")
    hidden = preview(client, token, "/o/acme/autre/-/preview", text="# Essai")
    assert hidden.status_code == 404 and "Essai" not in hidden.text
    assert preview(client, token, "/o/acme/absent/-/preview", text="# x").status_code == 404
    assert preview(client, token, "/o/nulle/infra/-/preview", text="# x").status_code == 404


def test_the_form_token_is_required(client: TestClient, wiki: Wiki) -> None:
    signed_in(client, "alice")
    forged = preview(client, "faux-jeton", text="# Essai", path=PAGE)
    assert forged.status_code == 403 and "expiré" in forged.text
    missing = client.post(PREVIEW, data={"text": "# Essai"}, follow_redirects=False)
    assert missing.status_code in (400, 422) and "Essai" not in missing.text
    # Signed out: the sign-in page, never a rendering.
    client.cookies.clear()
    anonymous = preview(client, "x", text="# Essai", path=PAGE)
    assert anonymous.status_code == 303 and anonymous.headers["location"].startswith("/login")


def test_a_text_too_long_is_not_rendered(client: TestClient, wiki: Wiki) -> None:
    token = signed_in(client, "alice")
    assert preview(client, token, text="a" * MAX_PREVIEW, path=PAGE).status_code == 200
    long = preview(client, token, text="a" * (MAX_PREVIEW + 1), path=PAGE)
    assert long.status_code == 400 and long.headers["content-type"].startswith("text/plain")
    assert (
        french(long.text)
        == "Ce texte est trop long pour l'aperçu : enregistrez-le, puis lisez la page."
    )


def test_the_longest_text_fits_in_a_form_field(client: TestClient, wiki: Wiki) -> None:
    """Sent as the editor sends it (multipart): even four bytes per character fit."""
    token = signed_in(client, "alice")
    for character in ("é", "\N{GRINNING FACE}"):
        shown = client.post(
            PREVIEW,
            data={"csrf": token, "path": PAGE},
            files={"text": (None, character * MAX_PREVIEW)},
            follow_redirects=False,
        )
        assert shown.status_code == 200, shown.text[:200]


def test_the_message_follows_the_interface_language(client: TestClient, wiki: Wiki) -> None:
    token = signed_in(client, "alice")
    client.post("/account/locale", data={"csrf": token, "locale": "en"})
    long = preview(client, token, text="a" * (MAX_PREVIEW + 1), path=PAGE)
    assert long.text == "This text is too long to preview: save it, then read the page."


def test_the_editors_offer_the_preview(client: TestClient, wiki: Wiki) -> None:
    signed_in(client, "alice")
    page = client.get(f"/o/acme/infra/edit/{PAGE}").text
    [box] = select(page, "[data-preview]")
    assert box.attrs["data-preview"] == PREVIEW
    assert json.loads(box.attrs["data-preview-params"]) == {"path": PAGE}
    assert box.attrs["data-preview-fields"] == "" and box.attrs["data-preview-name"] == ""
    assert (
        french(box.attrs["data-preview-failed"]) == "L'aperçu n'a pas pu être chargé : réessayez."
    )
    assert box.attrs["data-preview-empty"] == "(texte vide)"
    assert box.attrs["data-preview-max"] == str(MAX_PREVIEW)
    assert french(box.attrs["data-preview-expired"]) == (
        "Votre session a expiré : reconnectez-vous dans un autre onglet, votre texte est conservé."
    )
    assert french(box.attrs["data-preview-too-long"]).startswith("Ce texte est trop long")
    modes = select_one(page, data_test("editor-modes"))
    assert "hidden" in modes.attrs and modes.attrs["role"] == "group"  # shown by preferences.js
    assert modes.attrs["aria-label"] == "Affichage de l'éditeur"
    assert [b.text for b in select(page, "[data-preview-mode]")] == [
        "Écrire",
        "Aperçu",
        "Côte à côte",
    ]
    assert select_one(page, data_test("editor-preview")).get("aria-controls") == "text-preview"
    pane = select_one(page, "#text-preview")
    assert "hidden" in pane.attrs and select_one(page, "#text-preview [data-preview-output]")
    assert select_one(page, "textarea#text").attrs["name"] == "text"  # always submitted
    # A new page too, without a path: its folder and name say where it will be.
    new = client.get("/o/acme/infra/new?folder=serveurs").text
    [box] = select(new, "[data-preview]")
    assert json.loads(box.attrs["data-preview-params"]) == {}
    assert box.attrs["data-preview-fields"] == "folder name"
    assert box.attrs["data-preview-name"] == "name"
    assert select_one(new, data_test("editor-split")).get("aria-pressed") == "false"
    # A text file that is not Markdown: no rendering to preview.
    text_file = client.get("/o/acme/infra/edit/serveurs/notes.txt").text
    assert select(text_file, "[data-preview]") == [] and select_one(text_file, "textarea#text")


def test_a_space_not_synced_yet_previews_without_resolving_wiki_links(
    client: TestClient, wiki: Wiki, db: Session
) -> None:
    space = spaces.create_space(db, wiki.root, wiki.infra.organization, slug="neuf", name="Neuf")
    spaces.set_member(db, wiki.root, space, wiki.alice.user, SpaceRole.CONTRIBUTOR)
    db.commit()
    token = signed_in(client, "alice")
    html = preview(client, token, "/o/acme/neuf/-/preview", text="# Neuf\n\n[[proxmox]]").text
    assert '<h1 id="neuf">Neuf</h1>' in html
    assert 'href="/o/acme/neuf/p/proxmox.md" class="wikilink-missing"' in html


def checked_out(database: Database) -> int:
    """Connections of the pool in use."""
    pool = database.engine.pool
    assert isinstance(pool, QueuePool)
    return pool.checkedout()


def test_one_preview_at_a_time_and_the_connection_is_given_back(
    client: TestClient, wiki: Wiki, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long rendering (thousands of `[`) neither holds a database connection nor piles up:
    a second preview of the same account while the first renders is refused (429)."""
    token = signed_in(client, "alice")
    started, finish = threading.Event(), threading.Event()
    connections: list[int] = []
    render = wiki_module.render_at

    def slow(*args: Any) -> Markup:
        if args[1] == "# Long":  # the preview, not the reading of a page
            connections.append(checked_out(database))
            started.set()
            assert finish.wait(10)
        return render(*args)

    monkeypatch.setattr(wiki_module, "render_at", slow)
    idle = checked_out(database)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(preview, client, token, text="# Long", path=PAGE)
        assert started.wait(10)
        assert connections == [idle]  # none taken by the request being rendered
        # Meanwhile, the rest of the site answers, and a second preview waits its turn.
        assert client.get(f"/o/acme/infra/p/{PAGE}").status_code == 200
        second = preview(client, token, text="# Autre", path=PAGE)
        assert second.status_code == 429
        assert second.headers["content-type"].startswith("text/plain")
        assert french(second.text) == (
            "Un aperçu est déjà en préparation pour votre compte : il suivra."
        )
        finish.set()
        assert first.result(10).status_code == 200 and "Long" in first.result().text
    # The turn is given back, also after an error during the rendering.
    assert preview(client, token, text="# Après", path=PAGE).status_code == 200

    def broken(*args: Any) -> Markup:
        raise not_found("Page not found.")

    monkeypatch.setattr(wiki_module, "render_at", broken)
    assert preview(client, token, text="# x", path=PAGE).status_code == 404
    monkeypatch.setattr(wiki_module, "render_at", render)
    assert preview(client, token, text="# x", path=PAGE).status_code == 200
    assert set() == wiki_module._PREVIEWING
