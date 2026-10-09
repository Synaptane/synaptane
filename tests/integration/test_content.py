import re
import shutil
import time
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.core.access import OrgRole
from synaptane.core.errors import SynaptaneError
from synaptane.server import create_app
from synaptane.services import Actor, content, deposits, organizations
from synaptane.services import search as search_service
from synaptane.services.extraction import update_index
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job
from tests.integration.conftest import actor_of, make_user, token_for
from tests.integration.gitutil import git
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

# --- Services ---------------------------------------------------------------------------------


def test_tree_and_reading(ctx: Workspace, wiki: Wiki) -> None:
    paths = {b.path for b in content.list_files(wiki.bob, ctx, wiki.infra)}
    assert {"README.md", "serveurs/proxmox.md", "rapport.pdf", "specs.docx"} <= paths
    page = content.read_file(wiki.bob, ctx, wiki.infra, "serveurs/proxmox.md")
    assert page.kind == "markdown" and page.text and "sauvegarde" in page.text
    assert content.read_file(wiki.bob, ctx, wiki.infra, "rapport.pdf").kind == "pdf"
    assert content.folder_index(content.list_files(wiki.bob, ctx, wiki.infra), "") == "README.md"
    assert content.backlinks(wiki.bob, ctx, wiki.infra, "serveurs/proxmox.md") == [
        ("README.md", "Infra")
    ]
    with pytest.raises(SynaptaneError):
        content.read_file(wiki.alice, ctx, wiki.other, "secret.md")
    with pytest.raises(SynaptaneError):
        content.read_file(wiki.bob, ctx, wiki.infra, "../.synaptane/inbox/.gitkeep")


def test_write_commits_page_and_change_item(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    page = content.read_file(wiki.alice, ctx, wiki.infra, "serveurs/proxmox.md")
    sha = content.write_file(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        "serveurs/proxmox.md",
        b"# Proxmox\n\nSauvegarde a 3h.\n",
        message="Nouvel horaire",
        base_sha=page.sha,
    )
    assert sha
    clone = ctx.clone_path(wiki.infra.id)
    log = git(clone, "log", "-1", "--format=%an|%B", "--name-only")
    assert log.startswith("Alice|Nouvel horaire") and "Source: web" in log
    # A single file touched: no more inbox item, the commit is enough for tidying.
    assert "content/serveurs/proxmox.md" in log and ".synaptane/inbox/" not in log
    from synaptane.services import deposits

    [change] = [i for i in deposits.inbox(db, wiki.alice, ctx, wiki.infra) if i.type == "change"]
    assert change.title == "Page changed: serveurs/proxmox.md" and change.deposited_by == "Alice"
    assert {j.kind for j in db.scalars(select(Job))} >= {"sync", "index"}

    with pytest.raises(SynaptaneError) as stale:
        content.write_file(
            db,
            database,
            wiki.alice,
            ctx,
            wiki.infra,
            "serveurs/proxmox.md",
            b"x",
            base_sha=page.sha,
        )
    assert stale.value.code == "conflict"
    with pytest.raises(SynaptaneError) as denied:
        content.write_file(db, database, wiki.bob, ctx, wiki.infra, "x.md", b"x")
    assert denied.value.code == "forbidden"
    for bad in ("../evil.md", "/../x", "a/../../b"):
        with pytest.raises(SynaptaneError):
            content.write_file(db, database, wiki.alice, ctx, wiki.infra, bad, b"x")


def test_move_and_delete(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> None:
    content.move_file(db, database, wiki.alice, ctx, wiki.infra, "serveurs/notes.txt", "notes.txt")
    paths = {b.path for b in content.list_files(wiki.alice, ctx, wiki.infra)}
    assert "notes.txt" in paths and "serveurs/notes.txt" not in paths
    content.delete_file(db, database, wiki.alice, ctx, wiki.infra, "notes.txt")
    assert "notes.txt" not in {b.path for b in content.list_files(wiki.alice, ctx, wiki.infra)}
    assert len(content.history(wiki.alice, ctx, wiki.infra, "README.md")) == 1


def test_extraction_cache_follows_the_repository(
    ctx: Workspace, db: Session, database: Database, wiki: Wiki
) -> None:
    cache = ctx.extract_path(wiki.infra.id) / "content"
    assert "Kubernetes" in (cache / "rapport.pdf.txt").read_text()
    assert "Cahier Terraform\nFin" in (cache / "specs.docx.txt").read_text()
    content.delete_file(db, database, wiki.alice, ctx, wiki.infra, "rapport.pdf")
    assert update_index(ctx, wiki.infra.id, ctx.local_repository(wiki.infra)) == (0, 1)
    assert not (cache / "rapport.pdf.txt").exists()


SCAN_TEXT = (
    "Le projet Atlas regroupe les notes et les documents de l'équipe. Chaque source conserve "
    "son format et son historique. Alice peut modifier les procédures ; Bob consulte les "
    "informations autorisées."
)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract absent")
def test_scanned_pdf_is_ocrized(
    ctx: Workspace, db: Session, database: Database, wiki: Wiki
) -> None:
    scan = (Path(__file__).parents[1] / "fixtures" / "scan-fr.pdf").read_bytes()
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "scan.pdf", scan)
    update_index(ctx, wiki.infra.id, ctx.local_repository(wiki.infra))
    text = (ctx.extract_path(wiki.infra.id) / "content" / "scan.pdf.txt").read_text()
    expected = SCAN_TEXT.split(" ")
    recognised = [word for word in expected if word in text]
    assert len(recognised) >= len(expected) - 3, text[:500]


def test_search_respects_rights_and_ranks(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    def addresses(actor: Actor, text: str, **kwargs: object) -> list[str]:
        hits = search_service.search(db, actor, ctx, text, **kwargs)  # type: ignore[arg-type]
        return [f"{h.address}:{h.path}" for h in hits]

    assert addresses(wiki.alice, "sauvegarde") == ["acme/infra:serveurs/proxmox.md"]
    assert set(addresses(wiki.root, "sauvegarde")) == {
        "acme/infra:serveurs/proxmox.md",
        "acme/autre:secret.md",
    }
    assert addresses(wiki.alice, "kubernetes") == ["acme/infra:rapport.pdf"]
    assert addresses(wiki.alice, "TERRAFORM") == ["acme/infra:specs.docx"]
    assert addresses(wiki.alice, r"sauv\w+ noct", regex=True) == ["acme/infra:serveurs/proxmox.md"]
    hits = search_service.search(db, wiki.alice, ctx, "nocturne proxmox")
    assert hits[0].path == "serveurs/proxmox.md" and hits[0].title == "Proxmox"
    assert [s.line for s in hits[0].snippets] == [1, 3]
    assert "acme/infra:serveurs/notes.txt" in addresses(wiki.alice, "notes")  # file name
    # Invisible or nonexistent space: same answer, "not found" (nothing leaks).
    for hidden in (["acme/autre"], ["acme/nulle-part"]):
        with pytest.raises(SynaptaneError, match="not found"):
            search_service.search(db, wiki.alice, ctx, "sauvegarde", addresses=hidden)
    with pytest.raises(SynaptaneError):
        search_service.search(db, wiki.alice, ctx, "sauv(", regex=True)


# --- Web --------------------------------------------------------------------------------------


def test_wiki_pages(client: TestClient, wiki: Wiki) -> None:
    login(client, "bob")
    home = client.get("/o/acme/infra").text
    assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in home
    page = client.get("/o/acme/infra/p/serveurs/proxmox.md").text
    assert "sauvegarde nocturne" in page and "Pages qui pointent ici" in page
    assert 'href="/o/acme/infra/p/README.md"' in page
    assert "Modifier" not in page  # Bob is a reader
    folder = client.get("/o/acme/infra/p/serveurs").text
    assert "notes.txt" in folder
    pdf = client.get("/o/acme/infra/p/rapport.pdf").text
    assert "doc-frame" in pdf and "Rapport Kubernetes" in pdf
    raw = client.get("/o/acme/infra/raw/page.html")
    assert raw.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in raw.headers["content-security-policy"]
    # An HTML page is shown rendered, in an opaque-origin frame; its source one click away.
    shown = client.get("/o/acme/infra/p/page.html").text
    assert 'sandbox="allow-scripts' in shown and "?preview=1" in shown
    frame = {"Sec-Fetch-Dest": "iframe"}
    preview = client.get("/o/acme/infra/raw/page.html", params={"preview": 1}, headers=frame)
    csp = preview.headers["content-security-policy"]
    assert preview.headers["content-disposition"].startswith("inline")
    assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp and "form-action 'none'" in csp
    assert preview.headers["cache-control"] == "no-store"
    # Opened as a full page (shared link, phishing): redirected to the wiki page.
    top = client.get(
        "/o/acme/infra/raw/page.html",
        params={"preview": 1},
        headers={"Sec-Fetch-Dest": "document"},
        follow_redirects=False,
    )
    assert top.status_code == 303 and top.headers["location"] == "/o/acme/infra/p/page.html"
    source = client.get("/o/acme/infra/p/page.html", params={"source": 1}).text
    assert "?preview=1" not in source and "Voir la page rendue" in source
    history = client.get("/o/acme/infra/history/serveurs/proxmox.md").text
    sha = history.split("/o/acme/infra/diff/")[1].split("/")[0]
    assert (
        "+La sauvegarde nocturne"
        in client.get(f"/o/acme/infra/diff/{sha}/serveurs/proxmox.md").text
    )
    absent = client.get("/o/acme/infra/p/guides/absent.md")
    assert absent.status_code == 404 and "existe pas encore" in absent.text
    assert "Créer cette page" not in absent.text  # Bob is a reader
    assert client.get("/o/acme/autre").status_code == 404


def test_wiki_editing(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    absent = client.get("/o/acme/infra/p/guides/absent.md").text
    assert "new?folder=guides&amp;name=absent.md" in absent  # "Créer cette page"
    assert 'value="absent.md"' in client.get("/o/acme/infra/new?folder=guides&name=absent.md").text
    form = client.get("/o/acme/infra/edit/serveurs/proxmox.md").text
    token = csrf(client, "/o/acme/infra/edit/serveurs/proxmox.md")
    base = form.split('name="base_sha" value="')[1].split('"')[0]
    saved = client.post(
        "/o/acme/infra/edit/serveurs/proxmox.md",
        data={"csrf": token, "text": "# Proxmox\n\nNouvelle **version**.\n", "base_sha": base},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "<strong>version</strong>" in client.get("/o/acme/infra/p/serveurs/proxmox.md").text
    stale = client.post(
        "/o/acme/infra/edit/serveurs/proxmox.md",
        data={"csrf": token, "text": "écrasement", "base_sha": base},
    )
    assert stale.status_code == 409 and "écrasement" in stale.text
    assert "Nouvelle **version**" in stale.text  # the difference is shown
    # Resubmitting the same form must not silently overwrite (regression).
    resent_base = stale.text.split('name="base_sha" value="')[1].split('"')[0]
    assert resent_base == base
    again = client.post(
        "/o/acme/infra/edit/serveurs/proxmox.md",
        data={"csrf": token, "text": "écrasement", "base_sha": resent_base},
    )
    assert again.status_code == 409
    assert "<strong>version</strong>" in client.get("/o/acme/infra/p/serveurs/proxmox.md").text
    overwrite = stale.text.split('name="overwrite_sha" value="')[1].split('"')[0]
    chosen = client.post(
        "/o/acme/infra/edit/serveurs/proxmox.md",
        data={"csrf": token, "text": "écrasement", "base_sha": base, "overwrite_sha": overwrite},
        follow_redirects=False,
    )
    assert chosen.status_code == 303
    assert "écrasement" in client.get("/o/acme/infra/p/serveurs/proxmox.md").text

    kept = client.post(
        "/o/acme/infra/-/new",
        data={"csrf": token, "folder": "guides", "name": "../x", "text": "Mon long texte"},
    )
    assert kept.status_code == 400 and "Mon long texte" in kept.text  # input kept

    client.post(
        "/o/acme/infra/-/new", data={"csrf": token, "folder": "guides", "name": "demarrage"}
    )
    assert "<h1" in client.get("/o/acme/infra/p/guides/demarrage.md").text
    upload = client.post(
        "/o/acme/infra/-/upload",
        data={"csrf": token, "folder": "guides"},
        files={"upload": ("schema.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        follow_redirects=False,
    )
    assert upload.headers["location"] == "/o/acme/infra/p/guides/schema.png"
    client.post("/o/acme/infra/-/delete-file", data={"csrf": token, "path": "guides/schema.png"})
    assert client.get("/o/acme/infra/p/guides/schema.png").status_code == 404


def test_search_page(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    page = client.get("/search", params={"q": "sauvegarde"}).text
    assert "<mark>sauvegarde</mark>" in page and "secret.md" not in page
    assert (
        "Expression régulière invalide"
        in client.get("/search", params={"q": "(", "regex": "true"}).text
    )


# --- REST -------------------------------------------------------------------------------------


def test_rest_content(client: TestClient, database: Database, wiki: Wiki) -> None:
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    bob = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    base = "/api/v1/organizations/acme/spaces/infra"

    tree = client.get(f"{base}/tree", headers=bob).json()
    assert "serveurs/proxmox.md" in {f["path"] for f in tree}
    pdf = client.get(f"{base}/files/rapport.pdf", headers=bob).json()
    assert pdf["kind"] == "pdf" and "Kubernetes" in pdf["text"]
    assert client.put(f"{base}/files/x.md", json={"content": "x"}, headers=bob).status_code == 403

    created = client.put(
        f"{base}/files/journal/2026.md",
        json={"content": "# Journal\n", "message": "Création", "base_sha": ""},
        headers=alice,
    )
    assert created.status_code == 200 and created.json()["commit"]
    again = client.put(
        f"{base}/files/journal/2026.md", json={"content": "x", "base_sha": ""}, headers=alice
    )
    assert again.status_code == 409
    moved = client.post(
        f"{base}/move", json={"from": "journal/2026.md", "to": "journal/annee.md"}, headers=alice
    )
    assert moved.status_code == 204
    assert client.get(f"{base}/history/journal/annee.md", headers=bob).json()[0]["subject"]
    assert client.delete(f"{base}/files/journal/annee.md", headers=alice).status_code == 204
    assert client.get(f"{base}/files/journal/annee.md", headers=bob).status_code == 404

    hits = client.get("/api/v1/search", params={"q": "sauvegarde"}, headers=bob).json()
    assert [(h["space"], h["path"]) for h in hits] == [("acme/infra", "serveurs/proxmox.md")]
    assert hits[0]["snippets"][0]["line"] == 3


def test_org_admin_reads_every_space(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    dave = make_user(db, "dave")
    acme = wiki.infra.organization
    organizations.set_member(db, wiki.root, acme, dave, OrgRole.ADMIN)
    hits = search_service.search(db, actor_of(db, dave), ctx, "sauvegarde")
    assert {h.address for h in hits} == {"acme/infra", "acme/autre"}


def test_catastrophic_regex_cannot_freeze_the_server(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    # (.|.)+! blows up Python's `re` engine on a 30-character line; ripgrep, without
    # backtracking, answers at once (regression: several minutes, GIL blocked).
    line = "e" + "x" * 40
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "piege.md", f"{line}\n".encode())
    db.commit()
    started = time.monotonic()
    hits = search_service.search(db, wiki.alice, ctx, "(.|.)+!|e", regex=True)
    assert time.monotonic() - started < 5
    assert "piege.md" in {h.path for h in hits}
    with pytest.raises(SynaptaneError, match="Invalid regular expression"):
        search_service.search(db, wiki.alice, ctx, "(?<=lookbehind)", regex=True)


def test_tree_only_unfolds_the_current_path(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    for name in ("a.md", "b.md"):
        content.write_file(db, database, wiki.alice, ctx, wiki.infra, f"loin/{name}", b"# X\n")
    db.commit()
    login(client, "alice")
    page = client.get("/o/acme/infra/p/serveurs/proxmox.md").text
    assert "/o/acme/infra/p/loin" in page  # closed folder: a link
    assert "/o/acme/infra/p/loin/a.md" not in page  # its content is not sent
    assert "/o/acme/infra/p/serveurs/notes.txt" in page  # current folder expanded
    css = page.split('href="/static/app.css?v=')[1].split('"')[0]
    cached = client.get(f"/static/app.css?v={css}")
    assert "immutable" in cached.headers["cache-control"]


def test_large_files_are_streamed_not_loaded(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    big = bytes(range(256)) * (3 * 4096)  # 3 MB, beyond MAX_TEXT_VIEW
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "gros.bin", big)
    db.commit()
    file = content.read_file(wiki.alice, ctx, wiki.infra, "gros.bin")
    assert file.size == len(big) and file.data == b""  # nothing in memory
    login(client, "alice")
    raw = client.get("/o/acme/infra/raw/gros.bin")
    assert raw.content == big and raw.headers["content-length"] == str(len(big))


def test_search_ignores_accents(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.write_file(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        "procedures/reseau.md",
        "# Procédure réseau\n\nLa PROCÉDURE de bascule du pare-feu.\n".encode(),
    )
    db.commit()
    for text in ("procedure", "procédure", "PROCEDURE", "réseau bascule"):
        hits = search_service.search(db, wiki.alice, ctx, text)
        assert [h.path for h in hits][:1] == ["procedures/reseau.md"], text


def test_search_handles_plurals_apostrophes_and_short_words(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.write_file(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        "notes/art.md",
        "# Art\n\nLe serveur d’accès et l’art de la sauvegarde.\n".encode(),
    )
    db.commit()

    def paths(text: str) -> list[str]:
        return [h.path for h in search_service.search(db, wiki.alice, ctx, text)]

    assert "notes/art.md" in paths("serveurs")  # plural → singular
    assert "notes/art.md" in paths("d'acces")  # straight apostrophe, no accent
    assert "notes/art.md" in paths("art")
    assert not [p for p in paths("art") if "partie" in p]  # "art": whole word


def test_snippet_is_centered_on_the_match(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    line = "remplissage " * 60 + "kubernetes est ici" + " fin" * 50
    content.write_file(
        db, database, wiki.alice, ctx, wiki.infra, "k8s/long.md", f"# Long\n\n{line}\n".encode()
    )
    db.commit()
    [hit] = [
        h
        for h in search_service.search(db, wiki.alice, ctx, "kubernetes")
        if h.path == "k8s/long.md"
    ]
    assert "kubernetes" in hit.snippets[0].text and hit.snippets[0].text.startswith("…")


def test_document_bar_and_outline(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Info and actions in the top bar; table of contents of a Markdown's h1 to h4 headings."""
    text = "# Guide\n\nIntro.\n\n## Installer\n\n### Prérequis\n\n#### Détail\n\n##### Trop fin\n"
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "guide.md", text.encode())
    db.commit()
    login(client, "alice")
    page = client.get("/o/acme/infra/p/guide.md").text
    bar = page.split('<header class="workspace-topbar">')[1].split("</header>")[0]
    assert "Historique" in bar and "Modifier" in bar and "modifié le" in bar
    assert "doc-bar" not in page and '<h1 id="guide">Guide</h1>' in page
    toc = page.split('class="doc-toc"')[1].split("</details>")[0]
    for anchor in ("#guide", "#installer", "#prérequis", "#détail"):
        assert f'href="{anchor}"' in toc, anchor
    assert "Trop fin" not in toc  # h5: not in the table of contents
    short = client.get("/o/acme/infra/p/serveurs/notes.txt").text
    assert 'class="doc-toc"' not in short


def test_space_navigation(client: TestClient, wiki: Wiki) -> None:
    """Sidebar: the tree right away, the gear icon to the settings, the "Inbox" line (Inbox,
    Tidying, Activity, Settings tabs); search on the home page."""
    login(client, "alice")
    home = client.get("/o/acme/infra").text
    sidebar = home.split('class="sidebar-space"')[1].split('class="space-tree"')[0]
    assert 'class="sidebar-space-gear" href="/o/acme/infra/-/settings"' in sidebar
    assert 'class="sidebar-inbox" href="/o/acme/infra/inbox"' in sidebar
    assert "Chercher dans" not in sidebar and 'href="/o/acme/infra/tidy"' not in sidebar
    assert 'class="page-search"' in home and 'name="space" value="acme/infra"' in home
    for path, current in (("inbox", "Inbox"), ("tidy", "Rangement"), ("activity", "Activité")):
        page = client.get(f"/o/acme/infra/{path}").text
        tabs = page.split('aria-label="Sections de l’espace"')[1].split("</nav>")[0]
        assert f'aria-current="page">{current}</a>' in tabs
        assert 'href="/o/acme/infra/-/settings"' in tabs


def test_global_search_and_administration_links(client: TestClient, wiki: Wiki) -> None:
    """Streamlined sidebar: global search is on the home page; administration is offered on
    the home page and in My account, to the super-admin only."""
    login(client, "alice")
    home = client.get("/").text
    sidebar = home.split('id="workspace-sidebar"')[1].split('class="sidebar-tree"')[0]
    assert "Chercher partout" not in sidebar and 'href="/admin"' not in sidebar
    assert 'id="home-q"' in home and 'href="/admin"' not in home
    assert 'href="/admin"' not in client.get("/account").text
    footer = home.split('class="sidebar-footer"')[1].split("</div>\n  </div>")[0]
    assert "data-theme-option" not in footer  # appearance is set in My account
    assert "data-theme-option" in client.get("/account").text
    client.cookies.clear()
    login(client, "root")
    assert 'href="/admin"' in client.get("/").text
    assert 'href="/admin"' in client.get("/account").text


def test_wikilinks_in_pages_and_backlinks(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.write_file(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        "emploi/README.md",
        b"# Emploi\n\n- [[lettre-a]] pour Selescope\n- CV : https://cv.example.fr/\n",
    )
    content.write_file(
        db, database, wiki.alice, ctx, wiki.infra, "emploi/lettres/lettre-a.md", b"# Lettre A\n"
    )
    db.commit()
    login(client, "alice")
    page = client.get("/o/acme/infra/p/emploi/README.md").text
    assert 'href="/o/acme/infra/p/emploi/lettres/lettre-a.md"' in page
    assert 'href="https://cv.example.fr/" target="_blank"' in page
    letter = client.get("/o/acme/infra/p/emploi/lettres/lettre-a.md").text
    assert "Pages qui pointent ici" in letter and "emploi/README.md" in letter


def test_deposit_from_anywhere(client: TestClient, wiki: Wiki) -> None:
    """The "Deposit information" button in the sidebar (and on the home page, for mobile),
    choice of space (preselected from a space), deposit into the chosen inbox, return to
    the original page."""
    login(client, "alice")
    home = client.get("/").text
    assert home.count('href="/deposit?next=/"') == 2  # sidebar and home page (mobile)
    assert 'class="button button-primary home-deposit" href="/deposit?next=/"' in home  # mobile
    from_space = client.get("/o/acme/infra").text
    assert 'href="/deposit?space=acme/infra&amp;next=/o/acme/infra"' in from_space
    form = client.get("/deposit", params={"space": "acme/infra"}).text
    assert '<option value="acme/infra" selected>' in form
    token = csrf(client, "/deposit")
    refused = client.post(
        "/deposit", data={"csrf": token, "space": "acme/autre", "title": "X"}
    )  # Alice does not contribute to it
    assert refused.status_code == 400 and "Choisissez un espace" in refused.text
    done = client.post(
        "/deposit",
        data={"csrf": token, "space": "acme/infra", "title": "Nouveau switch", "body": "24 ports"},
        files=[("files", ("a.txt", b"a")), ("files", ("b.txt", b"b"))],
        follow_redirects=False,
    )
    location = done.headers["location"]
    assert done.status_code == 303 and location.startswith("/o/acme/infra/inbox?new=")
    inbox = client.get(location).text
    assert "Nouveau switch" in inbox and 'class="is-new"' in inbox
    client.cookies.clear()
    login(client, "bob")  # reader only: no button
    assert 'href="/deposit' not in client.get("/").text


def test_action_links_and_error_pages_v3(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """A name with "#" or "?" stays editable; 400 and 500 in the application's look and feel;
    bounded tidying suggestion."""
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "zz/note #1.md", b"# N\n")
    db.commit()
    login(client, "alice")
    page = client.get("/o/acme/infra/p/zz/note%20%231.md").text
    assert 'href="/o/acme/infra/edit/zz/note%20%231.md"' in page
    assert 'href="/o/acme/infra/history/zz/note%20%231.md"' in page
    assert client.get("/o/acme/infra/edit/zz/note%20%231.md").status_code == 200
    bad = client.get("/o/acme/infra/activity", params={"skip": "abc"})
    assert bad.status_code == 400 and "<html" in bad.text and "Requête invalide" in bad.text
    with pytest.raises(SynaptaneError, match="Filing suggestion too long"):
        deposits.deposit(
            db,
            database,
            wiki.alice,
            ctx,
            wiki.infra,
            title="T",
            body="x",
            source="web",
            hint="h" * 501,
        )


def test_an_unexpected_error_gets_an_application_page(
    settings: Settings, database: Database
) -> None:
    def boom() -> None:
        raise RuntimeError("panne imprévue")

    app = create_app(settings, database)
    for path in ("/boom", "/api/v1/boom"):
        app.router.routes.insert(0, APIRoute(path, boom))
    client = TestClient(app, raise_server_exceptions=False)
    page = client.get("/boom")
    assert page.status_code == 500 and "<html" in page.text
    assert re.search(r"référence [0-9a-f]{8}", page.text)
    api = client.get("/api/v1/boom")
    assert api.status_code == 500 and api.json()["error"] == "internal" and api.json()["ref"]
