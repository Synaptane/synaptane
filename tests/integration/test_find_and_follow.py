"""Find from anywhere, visible inbox, return after a deposit, a home page that says what is
going on, readers and duplicate files."""

import re
import time
from datetime import UTC, datetime
from urllib.parse import unquote

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.services import deposits
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Space, TidyRun
from synaptane.web.i18n import french_typography as fr
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki


def _note(db: Session, database: Database, ctx: Workspace, wiki: Wiki, title: str) -> str:
    path = deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title=title, body="Texte.", source="web"
    )
    db.commit()
    return path.rsplit("/", 1)[-1]


def _leave(ctx: Workspace, wiki: Wiki, name: str) -> None:
    """Note marked "left" by tidying (as the agent would do)."""
    repo = ctx.local_repository(wiki.infra)
    item = deposits.read_item(wiki.alice, ctx, wiki.infra, name)
    note = repo.blob(item.path) or b""
    left = note.replace(b'type: "note"', b'type: "note"\nstatus: "left"', 1)
    assert left != note
    repo.commit_changes({item.path: left}, ctx.committer, "laissée")


# --- Search from every page -------------------------------------------------------------------


def test_search_is_reachable_from_every_page(client: TestClient, wiki: Wiki) -> None:
    """Magnifier in the top bar (link to /search without JS), palette limited to the current
    space with an "Everywhere" toggle; shortcuts announced."""
    login(client, "alice")
    page = client.get("/o/acme/infra/p/serveurs/proxmox.md").text
    topbar = page.split('class="workspace-topbar"')[1].split("</header>")[0]
    assert 'class="topbar-search" href="/search?space=acme/infra"' in topbar
    assert 'aria-keyshortcuts="Control+K Meta+K /"' in topbar
    palette = page.split('id="search-palette"')[1].split("</dialog>")[0]
    assert 'data-url="/search/titles"' in palette and 'action="/search"' in palette
    assert 'name="space" value="acme/infra" checked' in palette
    assert 'name="space" value=""' in palette and "Partout" in palette
    assert 'role="combobox"' in palette and 'aria-controls="palette-results"' in palette
    for path in ("/", "/o/acme/infra/inbox", "/account", "/deposit"):
        html = client.get(path).text
        assert 'class="topbar-search"' in html and 'id="search-palette"' in html, path
    home = client.get("/").text
    assert 'class="topbar-search" href="/search"' in home  # outside a space: everywhere
    assert 'name="space" value=""' not in home  # no toggle without a current space


def test_palette_titles(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    found = client.get("/search/titles", params={"q": "sauvegarde", "space": "acme/infra"})
    assert found.status_code == 200 and found.headers["cache-control"] == "no-store"
    data = found.json()
    hit = next(r for r in data["results"] if r["path"] == "serveurs/proxmox.md")
    assert hit == {
        "title": "Proxmox",
        "path": "serveurs/proxmox.md",
        "space": "Infra",
        "url": "/o/acme/infra/p/serveurs/proxmox.md",
    }
    assert unquote(data["all"]) == "/search?q=sauvegarde&space=acme/infra"
    everywhere = client.get("/search/titles", params={"q": "sauvegarde"}).json()
    assert all(r["path"] != "secret.md" for r in everywhere["results"])  # invisible space
    assert client.get("/search/titles", params={"q": "s"}).json()["results"] == []
    hidden = client.get("/search/titles", params={"q": "sauvegarde", "space": "acme/autre"})
    assert hidden.status_code == 404 and "Espace introuvable" in hidden.json()["error"]
    client.cookies.clear()
    anonymous = client.get("/search/titles", params={"q": "x"}, follow_redirects=False)
    assert anonymous.status_code == 303 and anonymous.headers["location"].startswith("/login")


# --- Visible inbox, with a counter ---------------------------------------------------------


def test_inbox_line_with_count_and_left_dot(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    login(client, "alice")
    page = client.get("/o/acme/infra/p/serveurs/proxmox.md").text
    sidebar = page.split('class="sidebar-space"')[1].split('class="space-tree"')[0]
    assert 'class="sidebar-inbox" href="/o/acme/infra/inbox"' in sidebar
    assert "sidebar-count" not in sidebar  # empty inbox: no counter
    first = _note(db, database, ctx, wiki, "Panne réseau")
    _note(db, database, ctx, wiki, "Achat NAS")
    _leave(ctx, wiki, first)
    sidebar = (
        client.get("/o/acme/infra/p/serveurs/proxmox.md")
        .text.split('class="sidebar-space"')[1]
        .split('class="space-tree"')[0]
    )
    assert '<span class="sidebar-count" aria-hidden="true">2</span>' in sidebar
    assert "2 éléments en attente" in sidebar and 'class="sidebar-dot"' in sidebar
    assert "dont 1 laissé par le rangement" in sidebar
    inbox = client.get("/o/acme/infra/inbox").text
    assert 'class="sidebar-inbox" href="/o/acme/infra/inbox" aria-current="page"' in inbox
    settings = client.get("/o/acme/infra/-/members").text
    assert 'class="sidebar-space-gear" href="/o/acme/infra/-/settings" aria-current="page"' in (
        settings
    )


# --- Return to the original page, inbox from newest to oldest -------------------------------


def test_deposit_returns_to_the_page_it_came_from(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    login(client, "alice")
    old = _note(db, database, ctx, wiki, "Ancienne note")
    time.sleep(1.1)  # deposit dates to the second: an order without ties
    origin = "/o/acme/infra/p/serveurs/proxmox.md"
    page = client.get(origin).text
    assert 'href="/deposit?space=acme/infra&amp;next=/o/acme/infra/p/serveurs/proxmox.md"' in page
    form = client.get("/deposit", params={"space": "acme/infra", "next": origin}).text
    assert f'name="next" value="{origin}"' in form and f'href="{origin}">Annuler' in form
    token = csrf(client, "/deposit")
    done = client.post(
        "/deposit",
        data={
            "csrf": token,
            "space": "acme/infra",
            "title": "Nouveau switch",
            "body": "24 ports",
            "next": origin,
        },
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == origin
    shown = client.get(origin).text
    notice = shown.split('data-flash="ok">')[1].split("</div>")[0]
    assert notice.startswith(fr("Déposé dans l&#39;inbox de « Infra »"))
    link = re.search(r'<a href="(/o/acme/infra/inbox/[^"]+)">Voir la note</a>', notice)
    assert link is not None and "Nouveau switch" in client.get(link.group(1)).text
    # An external address is never followed (open redirect).
    evil = client.post(
        "/deposit",
        data={"csrf": token, "space": "acme/infra", "title": "T", "body": "x", "next": "//x.fr"},
        follow_redirects=False,
    )
    assert evil.headers["location"] == "/"
    time.sleep(1.1)
    # Inbox: the newest first, highlighted when arriving after a deposit.
    deposit = client.post(
        "/o/acme/infra/-/inbox",
        data={"csrf": token, "title": "Toute fraîche", "body": "x"},
        follow_redirects=False,
    )
    location = deposit.headers["location"]
    assert location.startswith("/o/acme/infra/inbox?new=")
    inbox = client.get(location).text
    listing = inbox.split('class="file-list"')[1]
    assert listing.index("Toute fraîche") < listing.index("Nouveau switch")
    assert listing.index("Nouveau switch") < listing.index("Ancienne note")
    fresh = listing.split('class="is-new"')[1].split("</li>")[0]
    assert "Toute fraîche" in fresh and "Vient d&#39;arriver" in fresh
    assert old not in fresh


# --- The home page says what is waiting and what just changed -------------------------------


def test_home_shows_what_is_waiting_and_recent_pages(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    login(client, "alice")
    calm = client.get("/").text
    assert fr("Rien en attente : tout est rangé.") in calm
    recent = calm.split('id="home-recent"')[1].split("</section>")[0]
    assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in recent
    assert "secret.md" not in recent  # space invisible to Alice
    _note(db, database, ctx, wiki, "Panne réseau")
    finished = datetime(2026, 10, 6, 3, 13, tzinfo=UTC)
    db.add(
        TidyRun(space_id=wiki.infra.id, trigger="schedule", status="failed", finished_at=finished)
    )
    infra = db.get(Space, wiki.infra.id)
    assert infra is not None
    infra.sync_status = "conflict"
    db.commit()
    home = client.get("/").text
    waiting = home.split('id="home-waiting"')[1].split("</section>")[0]
    assert "Inbox de Infra" in waiting and "1 note" in waiting
    assert 'href="/o/acme/infra/inbox"' in waiting
    assert "Rangement en échec" in waiting and 'href="/o/acme/infra/tidy"' in waiting
    assert "Conflit Git" in waiting and 'href="/o/acme/infra/-/git"' in waiting
    assert waiting.index("Conflit Git") < waiting.index("Inbox de Infra")  # problems first
    card = home.split('class="card space-card"')[1].split("</article>")[0]
    assert "1 en attente" in card and "Aucune description" in card
    # A successful run after the failure: no more alert.
    db.add(TidyRun(space_id=wiki.infra.id, trigger="manual", status="done"))
    db.commit()
    assert "Rangement en échec" not in client.get("/").text


# --- Readers and files that already exist --------------------------------------------------


def test_readers_get_an_explicit_403_on_edit_forms(client: TestClient, wiki: Wiki) -> None:
    login(client, "bob")
    for path in ("/o/acme/infra/edit/README.md", "/o/acme/infra/new"):
        refused = client.get(path)
        assert refused.status_code == 403, path
        assert fr("Vous êtes lecteur de l&#39;espace « Infra »") in refused.text
    token = csrf(client, "/o/acme/infra")
    posted = client.post(
        "/o/acme/infra/edit/README.md", data={"csrf": token, "text": "x", "base_sha": "abc"}
    )
    assert posted.status_code == 403
    created = client.post("/o/acme/infra/-/new", data={"csrf": token, "name": "x"})
    assert created.status_code == 403


def test_adding_a_file_that_exists_says_so(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    token = csrf(client, "/o/acme/infra")
    upload = client.post(
        "/o/acme/infra/-/upload",
        data={"csrf": token, "folder": "serveurs"},
        files={"upload": ("proxmox.md", b"# Autre\n", "text/markdown")},
        follow_redirects=False,
    )
    assert upload.headers["location"] == "/o/acme/infra/p/serveurs"
    folder = client.get("/o/acme/infra/p/serveurs").text
    assert fr("Un fichier nommé « proxmox.md » existe déjà dans ce dossier.") in folder
    assert '<a href="/o/acme/infra/p/serveurs/proxmox.md">Ouvrir ce fichier</a>' in folder
    assert "La page a changé" not in folder
    page = client.post(
        "/o/acme/infra/-/new",
        data={"csrf": token, "folder": "serveurs", "name": "proxmox", "text": "Brouillon"},
    )
    assert page.status_code == 400 and "existe déjà dans ce dossier" in page.text
    assert "Brouillon" in page.text  # input kept


# --- Token created, landmarks, identifiers -------------------------------------------------


def test_new_token_is_shown_first(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    token = csrf(client, "/account/assistants")
    response = client.post("/account/tokens", data={"csrf": token, "name": "Claude Code"})
    page = response.text
    assert 'id="jeton-cree" data-revealed' in page
    assert page.index('id="jeton-cree"') < page.index("Brancher un assistant IA (MCP)")
    assert page.index("revealed-value") < page.index("Applications connectées")


def test_landmarks_headings_and_identifiers(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    home = client.get("/").text
    assert '<aside class="workspace-sidebar" id="workspace-sidebar"' in home
    assert 'aria-label="Navigation principale"' in home
    assert "<h1>Déposer une information</h1>" in client.get("/deposit").text
    account = client.get("/account").text
    assert '<p class="eyebrow"><span class="ident">@alice</span></p>' in account
