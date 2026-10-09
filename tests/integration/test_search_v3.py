"""Search: spellings, stems, ranking, excerpts."""

import os

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.services import content, deposits
from synaptane.services import search as search_service
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki

CLUSTER = """# Cluster

Trois machines dans la baie.

## Nœuds

| Nœud | IP | Rôle |
|---|---|---|
| pve1 | 10.0.0.11 | calcul |
| pve2 | 10.0.0.12 | stockage |

## Sauvegarde

```bash
vzdump --all --mode snapshot
```
"""


def _write(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, files: dict[str, str]
) -> None:
    for path, text in files.items():
        content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, text.encode())
    db.commit()


def _paths(db: Session, ctx: Workspace, wiki: Wiki, text: str) -> list[str]:
    return [h.path for h in search_service.search(db, wiki.alice, ctx, text, limit=50)]


def test_ligatures_and_accents_match_both_ways(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Searching "noeuds" finds "nœuds" (and vice versa), "coeur" "cœur", "Etat" "État"."""
    _write(
        db,
        database,
        ctx,
        wiki,
        {
            "infra/cluster.md": CLUSTER,
            "notes/coeur.md": "# Cœur\n\nL'État du cœur de réseau.\n",
            "notes/ascii.md": "# Ascii\n\nles noeuds ecrits sans ligature\n",
        },
    )
    assert "infra/cluster.md" in _paths(db, ctx, wiki, "noeuds")
    assert "notes/ascii.md" in _paths(db, ctx, wiki, "nœuds")
    assert "notes/coeur.md" in _paths(db, ctx, wiki, "coeur")
    assert "notes/coeur.md" in _paths(db, ctx, wiki, "Etat")


def test_other_forms_of_a_word_are_found(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Searching "sauvegarder" finds "sauvegarde"; the exact word ranks first."""
    assert "serveurs/proxmox.md" in _paths(db, ctx, wiki, "sauvegarder")
    assert "serveurs/proxmox.md" in _paths(db, ctx, wiki, "sauvegardés")
    # Same size, same number of occurrences: only the exact word breaks the tie (the second
    # page, more recent, would rank first otherwise).
    _write(
        db,
        database,
        ctx,
        wiki,
        {
            "notes/exacte.md": "# Note\n\nIl faut sauvegarder ici.\n",
            "notes/proche.md": "# Note\n\nIl faut sauvegarde ici..\n",
        },
    )
    found = _paths(db, ctx, wiki, "sauvegarder")
    assert found.index("notes/exacte.md") < found.index("notes/proche.md")


def test_a_rare_word_weighs_more_than_a_frequent_one(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """In "sauvegarde vzdump", the page with the rare word ranks above the one that repeats
    the common word twenty times (IDF), and a long page does not win by its size."""
    files = {f"filler/page{n}.md": f"# Page {n}\n\nUne sauvegarde de plus.\n" for n in range(8)}
    files["notes/frequent.md"] = "# Fréquent\n\n" + "La sauvegarde du jour. " * 20 + "\n"
    files["notes/rare.md"] = "# Rare\n\nLancer vzdump à la main.\n"
    _write(db, database, ctx, wiki, files)
    found = _paths(db, ctx, wiki, "sauvegarde vzdump")
    assert found.index("notes/rare.md") < found.index("notes/frequent.md")


def test_freshness_breaks_ties(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> None:
    text = "# Même\n\nzorglub en double\n"
    _write(db, database, ctx, wiki, {"egal/alpha.md": text, "egal/beta.md": text})
    folder = ctx.clone_path(wiki.infra.id) / "content" / "egal"
    os.utime(folder / "alpha.md", (1_000_000_000, 1_000_000_000))  # 2001
    os.utime(folder / "beta.md", (1_700_000_000, 1_700_000_000))  # 2023
    assert _paths(db, ctx, wiki, "zorglub") == ["egal/beta.md", "egal/alpha.md"]
    os.utime(folder / "beta.md", (900_000_000, 900_000_000))  # 1998
    assert _paths(db, ctx, wiki, "zorglub") == ["egal/alpha.md", "egal/beta.md"]


def test_hits_carry_a_readable_passage_and_the_section_anchor(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    _write(db, database, ctx, wiki, {"infra/cluster.md": CLUSTER})
    [hit] = search_service.search(db, wiki.alice, ctx, "pve1")
    assert hit.passage[:2] == ["Nœud · IP · Rôle", "pve1 · 10.0.0.11 · calcul"]
    assert not [line for line in hit.passage if "|" in line or "---" in line]
    assert (hit.anchor, hit.section, hit.fragment) == ("nœuds", "Nœuds", "#n%C5%93uds")
    [hit] = search_service.search(db, wiki.alice, ctx, "vzdump")
    assert hit.passage == ["vzdump --all --mode snapshot"]  # neither "bash" nor the fence
    assert (hit.anchor, hit.fragment) == ("sauvegarde", "#sauvegarde")
    [hit] = search_service.search(db, wiki.alice, ctx, "baie")
    assert hit.anchor == "" and hit.section == ""  # top of page: no anchor


def test_search_page_shows_one_readable_excerpt_with_its_section(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """A single block in text font, readable table rows, link to the section (the same
    anchor as on the rendered page)."""
    _write(db, database, ctx, wiki, {"infra/cluster.md": CLUSTER})
    login(client, "alice")
    page = client.get("/search", params={"q": "vzdump pve1"}).text
    hit = page.split('<li class="card search-hit">')[1].split("</li>")[0]
    assert hit.count('class="search-excerpt"') == 1 and "<pre" not in hit
    assert "<mark>pve1</mark> · 10.0.0.11 · calcul" in hit
    assert "bash" not in hit and "|---" not in hit
    assert 'href="/o/acme/infra/p/infra/cluster.md#nœuds"' in hit
    rendered = client.get("/o/acme/infra/p/infra/cluster.md").text
    assert 'id="nœuds"' in rendered and 'id="sauvegarde"' in rendered


def test_pending_notes_alone_are_announced(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """No more "0 results · No results." under matching pending notes; notes are also found
    by another form of the word."""
    deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title="Devis du NAS", body="x", source="web"
    )
    db.commit()
    login(client, "alice")
    page = client.get("/search", params={"q": "devis"}).text
    assert "En attente de rangement" in page
    assert "Aucune page du wiki, mais 1 note en attente ci-dessus." in page
    assert "Aucun résultat" not in page and "0 résultat" not in page
    assert "Aucun résultat." in client.get("/search", params={"q": "zzzintrouvable"}).text
    deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title="Sauvegarde du NAS", body="x", source="web"
    )
    db.commit()
    assert "<mark>Sauvegarde</mark> du NAS" in client.get("/search?q=sauvegarder").text
