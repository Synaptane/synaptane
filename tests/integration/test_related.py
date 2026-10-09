"""Similar pages: service, "See also" panel, "Suggest linking", REST, MCP, tidying agent and
consistency check.

Meaning is tested with simulated embeddings (`ConceptEmbedder`: one axis per concept, French
and English synonyms included); the rest without embeddings."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.services import (
    content,
    deposits,
    related,
    routine_agent,
    routines,
    semantic,
    tidy,
)
from synaptane.services.tidy_tools import Overlay, nearby_for
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.conftest import token_for
from tests.integration.test_mcp import Live, live, mcp_calls  # noqa: F401 — live fixture
from tests.integration.test_semantic import ConceptEmbedder, enable, index
from tests.integration.test_tidy import Scripted, configure_ai
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

VZDUMP = (
    "# Sauvegardes vzdump\n\n"
    "Les sauvegardes vzdump des machines virtuelles partent chaque nuit vers le stockage Ceph. "
    "La rétention vzdump garde sept jours ; un rapport de sauvegarde arrive le matin.\n\n"
    "Voir aussi [le logo](logo.png).\n"
)
RESTORE = (
    "# Restaurer une machine virtuelle\n\n"
    "Pour restaurer une sauvegarde vzdump, choisir l'archive sur le stockage Ceph puis lancer "
    "qmrestore. La machine virtuelle redémarre avec son disque restauré.\n"
)
VLAN = "# Réseau\n\nLes VLAN du commutateur : 10 bureautique, 20 téléphonie, 30 invités.\n"


def write(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, pages: dict[str, Any]
) -> None:
    for path, data in pages.items():
        raw = data if isinstance(data, bytes) else data.encode()
        content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, raw)
    db.commit()


def backups(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> None:
    write(
        db,
        database,
        ctx,
        wiki,
        {
            "sauvegardes/vzdump.md": VZDUMP,
            "sauvegardes/restauration.md": RESTORE,
            "reseau/vlan.md": VLAN,
            "sauvegardes/logo.png": b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4,
        },
    )


def paths(found: list[related.Neighbour]) -> list[str]:
    return [n.path for n in found]


# --- Service ------------------------------------------------------------------------------


def test_close_pages_without_embeddings(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    backups(db, database, ctx, wiki)
    found = related.for_page(db, wiki.bob, ctx, wiki.infra, "sauvegardes/vzdump.md")
    assert paths(found)[0] == "sauvegardes/restauration.md"
    first = found[0]
    assert first.reason == "words" and first.label == "shared words"
    assert "folder" in first.reasons and not first.linked and first.score >= related.THRESHOLD
    assert "sauvegardes/vzdump.md" not in paths(found)  # not the page itself
    assert "reseau/vlan.md" not in paths(found)  # nothing in common: below the threshold
    assert "sauvegardes/logo.png" not in paths(found)  # linked, same folder, but no text
    assert first.title == "Restaurer une machine virtuelle"
    # A page with nothing similar: nothing, rather than noise.
    assert related.for_page(db, wiki.bob, ctx, wiki.infra, "reseau/vlan.md") == []

    # A link from the page to another: "already linked"; the other way, "cites this page".
    linked = VZDUMP + "\nRestauration : [procédure](restauration.md).\n"
    write(db, database, ctx, wiki, {"sauvegardes/vzdump.md": linked})
    [restore] = [
        n
        for n in related.for_page(db, wiki.bob, ctx, wiki.infra, "sauvegardes/vzdump.md")
        if n.path == "sauvegardes/restauration.md"
    ]
    assert restore.linked and "link" in restore.reasons
    back = related.for_page(db, wiki.bob, ctx, wiki.infra, "sauvegardes/restauration.md")
    [vzdump] = [n for n in back if n.path == "sauvegardes/vzdump.md"]
    assert not vzdump.linked and "backlink" in vzdump.reasons
    assert related.LABELS["backlink"] == "cites this page"


def test_extracted_text_counts_and_the_cache_follows_commits(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    write(
        db,
        database,
        ctx,
        wiki,
        {"kubernetes.md": "# Cluster Kubernetes\n\nLe rapport annuel du cluster Kubernetes.\n"},
    )
    found = related.for_page(db, wiki.alice, ctx, wiki.infra, "kubernetes.md")
    assert "rapport.pdf" in paths(found)  # text extracted from the PDF: "Rapport Kubernetes annuel"
    assert "specs.docx" not in paths(found)  # "Cahier Terraform": nothing in common

    # A similar page arrives: the commit changes the cache key, it shows up at once.
    assert "k8s/pods.md" not in paths(found)
    write(
        db,
        database,
        ctx,
        wiki,
        {"k8s/pods.md": "# Pods Kubernetes\n\nLes pods du cluster Kubernetes et leur rapport.\n"},
    )
    again = related.for_page(db, wiki.alice, ctx, wiki.infra, "kubernetes.md")
    assert "k8s/pods.md" in paths(again)
    # The unchanged page's profile is reused (fingerprint), the new one's is read, and
    # profiles are kept on disk: a restart does not read everything again.
    saved = ctx.extract_path(wiki.infra.id) / related.SAVED
    assert saved.is_file()
    related.clear_caches()
    assert "k8s/pods.md" in paths(
        related.for_page(db, wiki.alice, ctx, wiki.infra, "kubernetes.md")
    )


def test_rights_and_missing_pages(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    with pytest.raises(SynaptaneError) as refused:
        related.for_page(db, wiki.alice, ctx, wiki.other, "secret.md")  # invisible space
    assert refused.value.code.value == "forbidden"
    with pytest.raises(SynaptaneError) as missing:
        related.for_page(db, wiki.alice, ctx, wiki.infra, "absente.md")
    assert missing.value.code.value == "not_found"


def test_meaning_finds_the_same_topic_in_other_words(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    enable(db, ctx, wiki)
    write(
        db,
        database,
        ctx,
        wiki,
        {
            "en/backups.md": "# Backups\n\nNightly backups, backup schedule.\n",
            "fr/sauvegarde.md": "# Sauvegarde\n\nLa sauvegarde des serveurs, chaque nuit.\n",
            "fr/kubernetes.md": "# Kubernetes\n\nLe cluster et ses pods.\n",
        },
    )
    embedder = ConceptEmbedder()
    index(database, ctx, wiki, embedder)
    found = related.for_page(db, wiki.alice, ctx, wiki.infra, "en/backups.md")
    first = found[0]
    # No word in common ("backup", "sauvegarde"): meaning brings them together.
    assert first.path in ("fr/sauvegarde.md", "serveurs/proxmox.md")
    assert first.reason == "meaning" and first.label == "same topic"
    assert "fr/kubernetes.md" not in paths(found)
    # Possible duplicates: content very close in meaning.
    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    assert head is not None
    config = semantic.config_for(db, ctx.box, wiki.infra)
    pairs = related.close_pairs(db, ctx, wiki.infra, repo, head, ["en/backups.md"], config=config)
    assert (
        pairs
        and pairs[0].reason == "meaning"
        and "en/backups.md" in (pairs[0].first, pairs[0].second)
    )
    # A note (text outside the wiki): embedded once, compared with the chunks.
    note = related.text_neighbours(
        db, ctx, wiki.infra, repo, head, "Backup", "backup vzdump", config=config, embedder=embedder
    )
    assert note and note[0].reason == "meaning"


def test_a_slow_meaning_query_falls_back_to_words_and_links(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable(db, ctx, wiki)
    backups(db, database, ctx, wiki)
    index(database, ctx, wiki, ConceptEmbedder())
    monkeypatch.setattr(related, "MEANING_TIMEOUT_MS", 50)
    monkeypatch.setattr(related, "_NEAR_PAGE", "SELECT 'x.md', 1.0, 0.0 FROM pg_sleep(1)")
    found = related.for_page(db, wiki.alice, ctx, wiki.infra, "sauvegardes/vzdump.md")
    assert paths(found)[:1] == ["sauvegardes/restauration.md"]  # words and links only
    assert db.execute(text("SELECT 1")).scalar() == 1  # transaction still usable
    assert db.execute(text("SHOW statement_timeout")).scalar() == "0"  # timeout restored


# --- Web ----------------------------------------------------------------------------------


def test_see_also_panel_and_link_proposals(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    backups(db, database, ctx, wiki)
    login(client, "alice")
    page = client.get("/o/acme/infra/p/sauvegardes/vzdump.md").text
    assert 'data-related="/o/acme/infra/-/related/sauvegardes/vzdump.md?v=' in page
    assert 'id="voir-aussi"' not in page  # loaded after the page, without delaying its display
    fragment = client.get("/o/acme/infra/-/related/sauvegardes/vzdump.md")
    assert fragment.status_code == 200 and "private" in fragment.headers["cache-control"]
    html = fragment.text
    assert "Voir aussi" in html and "Restaurer une machine virtuelle" in html
    assert "mots en commun" in html and "pas encore liée" in html
    assert 'href="/o/acme/infra/p/sauvegardes/restauration.md"' in html
    assert (
        "Proposer de relier" in html and 'name="target" value="sauvegardes/restauration.md"' in html
    )
    assert client.get("/o/acme/infra/-/related/reseau/vlan.md").status_code == 204  # nothing

    token = csrf(client, "/o/acme/infra")
    form = {"csrf": token, "path": "sauvegardes/vzdump.md", "target": "sauvegardes/restauration.md"}
    done = client.post("/o/acme/infra/-/related/propose", data=form)
    assert "Proposition déposée dans" in done.text and "Voir la note" in done.text
    [note] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    assert note.title == "Link “Sauvegardes vzdump” and “Restaurer une machine virtuelle”"
    assert note.hint == "sauvegardes/vzdump.md" and note.source == "web"
    assert note.origin == related.proposal_origin(form["path"], form["target"])
    assert "[Restaurer une machine virtuelle](restauration.md)" in note.body
    assert "shared words" in note.body and "do not merge" in note.body
    # The wiki has not changed: only tidying will add the link.
    assert b"restauration.md" not in (
        ctx.local_repository(wiki.infra).blob("content/sauvegardes/vzdump.md") or b""
    )
    shown = client.get("/o/acme/infra/-/related/sauvegardes/vzdump.md").text
    assert "Proposition en attente" in shown and "Proposer de relier" not in shown
    twice = client.post("/o/acme/infra/-/related/propose", data=form)
    assert "Déjà proposé" in twice.text
    assert len(deposits.list_items(wiki.alice, ctx, wiki.infra)) == 1

    # Reader: the panel, without a button; the suggestion is refused.
    client.post("/logout", data={"csrf": token})
    login(client, "bob")
    seen = client.get("/o/acme/infra/-/related/sauvegardes/restauration.md").text
    assert "Voir aussi" in seen and "Proposer de relier" not in seen
    refused = client.post(
        "/o/acme/infra/-/related/propose",
        data={**form, "csrf": csrf(client, "/o/acme/infra")},
    )
    assert 'data-flash="error"' in refused.text and "Proposition déposée" not in refused.text
    assert len(deposits.list_items(wiki.alice, ctx, wiki.infra)) == 1
    # Invisible space: nothing.
    assert client.get("/o/acme/autre/-/related/secret.md").status_code in (403, 404)


# --- REST ---------------------------------------------------------------------------------


def test_rest_related(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    backups(db, database, ctx, wiki)
    headers = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    base = "/api/v1/organizations/acme/spaces/infra/files"
    answer = client.get(f"{base}/sauvegardes/vzdump.md/related", headers=headers)
    assert answer.status_code == 200, answer.text
    [first, *_] = answer.json()
    assert first["path"] == "sauvegardes/restauration.md" and first["reason"] == "words"
    assert first["label"] == "shared words" and first["linked"] is False
    assert 0 < first["score"] <= 1 and "folder" in first["reasons"]
    assert client.get(f"{base}/reseau/vlan.md/related", headers=headers).json() == []
    assert client.get(f"{base}/absente.md/related", headers=headers).status_code == 404
    limited = client.get(f"{base}/sauvegardes/vzdump.md/related?limit=1", headers=headers)
    assert len(limited.json()) == 1
    assert (
        client.get(f"{base}/sauvegardes/vzdump.md/related?limit=99", headers=headers).status_code
        == 422
    )
    # The file itself can still be read at its URL.
    assert client.get(f"{base}/sauvegardes/vzdump.md", headers=headers).json()["kind"] == "markdown"
    other = "/api/v1/organizations/acme/spaces/autre/files/secret.md/related"
    assert client.get(other, headers=headers).status_code in (403, 404)


# --- MCP ----------------------------------------------------------------------------------


def test_mcp_related(live: Live) -> None:  # noqa: F811
    alice = token_for(live.database, "alice")
    infra: dict[str, object] = {"space": "acme/infra"}
    results = mcp_calls(
        live,
        alice,
        ("write_page", {**infra, "path": "sauvegardes/vzdump.md", "content": VZDUMP}),
        ("write_page", {**infra, "path": "sauvegardes/restauration.md", "content": RESTORE}),
        ("related", {**infra, "path": "content/sauvegardes/vzdump.md", "limit": 3}),
        ("related", {**infra, "path": "absente.md"}),
        ("related", {"space": "acme/nope", "path": "x.md"}),
        ("related", {**infra, "path": "README.md", "limit": 50}),
    )
    assert [error for error, _ in results] == [False, False, False, True, True, True], results
    payload = json.loads(results[2][1])
    assert payload["space"] == "acme/infra" and payload["semantic"] is False
    [first, *_] = payload["results"]
    assert first["path"] == "sauvegardes/restauration.md"
    assert first["reason"] == "shared words" and first["linked"] is False
    assert first["url"].endswith("/o/acme/infra/p/sauvegardes/restauration.md")
    assert first["title"] == "Restaurer une machine virtuelle"
    assert "not found" in results[3][1].lower()
    assert "between 1 and 20" in results[5][1]


# --- Agents -------------------------------------------------------------------------------


def test_tidy_agent_sees_close_pages_before_creating_one(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    backups(db, database, ctx, wiki)
    path = deposits.deposit(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        title="Rétention des sauvegardes vzdump",
        body="Passer la rétention vzdump à quatorze jours sur le stockage Ceph.",
        source="web",
    )
    db.commit()
    name = path.rsplit("/", 1)[-1]
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": name}),
                ("related", {"path": "sauvegardes/restauration.md"}),
                ("related", {"item": name}),
                ("related", {}),
                ("mark_done", {"item": name, "summary": "vu"}),
            ],
            [("finish", {"summary": "ok"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done", record
    read, page, item, wrong = (r.content for r in script.results[:4])
    assert "Pages close to this item (to complete rather than duplicate)" in read
    assert "- sauvegardes/vzdump.md — Sauvegardes vzdump (shared words" in read
    assert read.count('untrusted="true"') >= 2  # page titles: data
    assert "Pages close to sauvegardes/restauration.md" in page
    assert "sauvegardes/vzdump.md" in page and "not linked yet" in page
    assert "sauvegardes/vzdump.md" in item
    assert script.results[3].is_error and "Give either" in wrong
    assert "related" in script.system  # the instructions mention it


def test_coherence_routine_gets_close_pairs(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    text_a = "# Ceph\n\nLe stockage Ceph : trois nœuds OSD, réplication triple, pool rbd.\n"
    text_b = "# Stockage Ceph\n\nCeph : trois nœuds OSD, réplication triple, pool rbd, quotas.\n"
    write(db, database, ctx, wiki, {"stockage/ceph.md": text_a, "infra/ceph-stockage.md": text_b})
    routine = routines.create(db, wiki.root, wiki.infra, routines.Fields(), template="coherence")
    db.commit()
    spec = routine_agent.Spec.of(routine)
    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    assert head is not None
    overlay = Overlay(repo, head)
    context = routine_agent.gather(db, ctx, wiki.infra, spec, overlay, datetime.now(UTC))
    assert any("infra/ceph-stockage.md and stockage/ceph.md" in p for p in context.pairs), context
    tools = routine_agent.RoutineTools(
        overlay,
        spec,
        ctx,
        wiki.infra,
        lambda q: [],
        "t",
        nearby_for(database, ctx, wiki.infra, repo, head),
    )
    first = routine_agent.first_message(tools, context, None, datetime.now(UTC), spec)
    assert "Pairs of pages with very similar content" in first
    found = tools.t_related("stockage/ceph.md")
    assert "infra/ceph-stockage.md" in found and "possible duplicate" in found
