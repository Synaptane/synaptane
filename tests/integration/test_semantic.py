import math
import re
from html import unescape
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.ai.base import AIError
from synaptane.ai.embeddings import EmbeddingConfig
from synaptane.core.errors import SynaptaneError
from synaptane.services import content, semantic
from synaptane.services import search as search_service
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.conftest import token_for
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

CONCEPTS = [
    {"sauvegarde", "sauvegardes", "backup", "backups", "vzdump"},
    {"proxmox", "serveur", "serveurs", "vm", "hyperviseur"},
    {"mot", "passe", "coffre", "password", "secret"},
    {"kubernetes", "cluster", "pods"},
]


class ConceptEmbedder:
    """Fake provider: one axis per concept (synonyms included), one "other" axis."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, config: EmbeddingConfig, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self.vector(text) for text in texts]

    @staticmethod
    def vector(text: str) -> list[float]:
        words = re.findall(r"\w+", text.lower())
        values = [float(sum(w in concept for w in words)) for concept in CONCEPTS] + [0.1]
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values]


def enable(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    semantic.configure(
        db, wiki.root, ctx.box, wiki.infra.organization, provider="openai", api_key="sk-x"
    )
    semantic.set_enabled(db, wiki.root, wiki.infra, True)
    db.commit()


def index(
    database: Database, ctx: Workspace, wiki: Wiki, embedder: ConceptEmbedder
) -> tuple[int, int]:
    with database.session() as session:
        config = semantic.org_config(session, ctx.box, wiki.infra.org_id)
    assert config is not None
    repo = ctx.local_repository(wiki.infra)
    return semantic.index_space(database, ctx, wiki.infra.id, repo, config, embedder)


def test_incremental_index(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> None:
    enable(db, ctx, wiki)
    embedder = ConceptEmbedder()
    embedded, removed = index(database, ctx, wiki, embedder)
    assert embedded >= 4 and removed == 0  # README, proxmox, notes.txt, PDF, docx, html
    files, chunks = semantic.stats(db, wiki.infra.id)
    assert files == embedded and chunks >= files
    texts = [t for call in embedder.calls for t in call]
    assert any(t.startswith("Proxmox\n\n") for t in texts)  # page title first
    assert any("Kubernetes" in t for t in texts)  # text extracted from the PDF

    assert index(database, ctx, wiki, embedder) == (0, 0)  # nothing changed
    content.write_file(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        "serveurs/proxmox.md",
        b"# Proxmox\n\nHyperviseur.\n",
    )
    content.delete_file(db, database, wiki.alice, ctx, wiki.infra, "serveurs/notes.txt")
    db.commit()
    assert index(database, ctx, wiki, embedder) == (1, 1)

    semantic.set_enabled(db, wiki.root, wiki.infra, False)
    db.commit()
    assert semantic.stats(db, wiki.infra.id) == (0, 0)


def test_hybrid_search_finds_meaning_and_stays_fair(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    enable(db, ctx, wiki)
    embedder = ConceptEmbedder()
    index(database, ctx, wiki, embedder)

    def run(actor: Any, text: str, **kwargs: Any) -> list[search_service.Hit]:
        return search_service.search(db, actor, ctx, text, embedder=embedder, **kwargs)

    hits = run(wiki.alice, "backup")  # no word in common with the page
    assert [(h.path, h.match) for h in hits][:1] == [("serveurs/proxmox.md", "meaning")]
    assert hits[0].snippets[0].line == 0 and "sauvegarde" in hits[0].snippets[0].text
    assert run(wiki.alice, "backup", mode="text") == []
    assert run(wiki.alice, "back.p", regex=True) == []

    both = run(wiki.root, "sauvegarde")
    by_path = {(h.address, h.path): h.match for h in both}
    assert by_path[("acme/infra", "serveurs/proxmox.md")] == "both"
    assert by_path[("acme/autre", "secret.md")] == "text"  # space without embeddings
    top = [(h.address, h.path) for h in both[:2]]
    assert ("acme/autre", "secret.md") in top  # not demoted

    def broken(config: EmbeddingConfig, texts: list[str]) -> list[list[float]]:
        raise AIError("panne")

    fallback = search_service.search(db, wiki.alice, ctx, "sauvegarde", embedder=broken)
    assert [h.path for h in fallback] == ["serveurs/proxmox.md"]


def test_semantic_interfaces(
    client: TestClient,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = ConceptEmbedder()
    monkeypatch.setattr("synaptane.ai.embeddings.embed", embedder)
    login(client, "root")
    token = csrf(client, "/o/acme/-/embeddings")
    client.post(
        "/o/acme/-/embeddings", data={"csrf": token, "provider": "mistral", "api_key": "mk"}
    )
    assert "Mistral · mistral-embed" in client.get("/o/acme/-/embeddings").text
    client.post("/o/acme/infra/-/semantic", data={"csrf": token, "enabled": "true"})
    assert "Index\xa0: 0 fichier" in client.get("/o/acme/infra/-/semantic").text
    index(database, ctx, wiki, embedder)
    page = client.get("/search", params={"q": "backup"}).text
    assert "proxmox.md" in page and "par le sens" in page

    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    assert (
        client.get("/api/v1/organizations/acme/embeddings", headers=root).json()["has_key"] is True
    )
    hits = client.get("/api/v1/search", params={"q": "backup"}, headers=root).json()
    assert hits[0]["match"] == "meaning"
    assert (
        client.get("/api/v1/search", params={"q": "backup", "mode": "text"}, headers=root).json()
        == []
    )
    # The organization's similarity floor applies to its spaces: out of reach, nothing by meaning.
    floored = client.put(
        "/api/v1/organizations/acme/embeddings",
        json={"provider": "mistral", "meaning_min_similarity": 0.99},
        headers=root,
    )
    assert floored.json()["meaning_min_similarity"] == 0.99
    assert client.get("/api/v1/search", params={"q": "backup"}, headers=root).json() == []
    wrong = client.put(
        "/api/v1/organizations/acme/embeddings",
        json={"provider": "mistral", "meaning_min_similarity": 1.5},
        headers=root,
    )
    assert wrong.status_code == 422
    off = client.put(
        "/api/v1/organizations/acme/spaces/infra/semantic", json={"enabled": False}, headers=root
    )
    assert off.json() == {"enabled": False, "files": 0, "chunks": 0}


def test_meaning_only_hits_need_a_confident_neighbour(ctx: Workspace, wiki: Wiki) -> None:
    from synaptane.services.semantic import SemanticHit

    def hits(*sims: float) -> list[SemanticHit]:
        return [SemanticHit(f"p{n}.md", "T", "texte", s) for n, s in enumerate(sims)]

    clear = search_service._meaning(wiki.infra, [], hits(0.58, 0.43, 0.41, 0.39), ctx)
    assert [h.path for _, h in clear] == ["p0.md"]  # stands out, distant neighbors dropped
    vague = search_service._meaning(wiki.infra, [], hits(0.33, 0.27, 0.26, 0.22), ctx)
    assert vague == []  # question unrelated to the space: nothing "by meaning"


def test_a_similarity_floor_replaces_the_gap_for_similar_pages(ctx: Workspace, wiki: Wiki) -> None:
    from synaptane.services.semantic import SemanticHit

    def hits(*sims: float) -> list[SemanticHit]:
        return [SemanticHit(f"p{n}.md", "T", "texte", s) for n, s in enumerate(sims)]

    similar = hits(0.64, 0.63, 0.62, 0.61, 0.61, 0.60, 0.60)  # relevant, but no outlier
    assert search_service._meaning(wiki.infra, [], similar, ctx) == []
    found = search_service._meaning(wiki.infra, [], similar, ctx, floor=0.55)
    assert [h.path for _, h in found] == ["p0.md", "p1.md", "p2.md", "p3.md", "p4.md"]
    # An unrelated question stands out from its neighbours, but stays below the floor.
    unrelated = hits(0.45, 0.30, 0.29, 0.28)
    assert search_service._meaning(wiki.infra, [], unrelated, ctx) != []
    assert search_service._meaning(wiki.infra, [], unrelated, ctx, floor=0.55) == []


def test_the_similarity_floor_follows_the_organizations_model(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    """Its scale is the model's: kept while the model stays, dropped when it changes (unless
    a new value comes with the change)."""
    org = wiki.infra.organization

    def save(model: str, floor: float | None) -> float | None:
        view = semantic.configure(
            db,
            wiki.root,
            ctx.box,
            org,
            provider="voyage",
            model=model,
            api_key="pa-x",
            meaning_min_similarity=floor,
        )
        assert view.meaning_min_similarity == semantic.meaning_floor(db, org.id)
        return view.meaning_min_similarity

    assert semantic.meaning_floor(db, org.id) is None  # no provider yet
    assert save("voyage-4-large", 0.55) == 0.55
    assert save("voyage-4-large", 0.55) == 0.55  # saved again unchanged: kept
    assert save("voyage-4-lite", 0.55) is None  # other model: the old value means nothing
    assert save("voyage-4-large", 0.5) == 0.5  # new value given with the change: kept
    with pytest.raises(SynaptaneError):
        save("voyage-4-large", 1.5)
    assert semantic.parse_floor(" 0,55 ") == 0.55 and semantic.parse_floor("") is None
    for typed in ("x", "0", "1"):
        with pytest.raises(SynaptaneError):
            semantic.parse_floor(typed)
    db.commit()
    login(client, "root")
    token = csrf(client, "/o/acme/-/embeddings")
    form = {"csrf": token, "provider": "voyage", "model": "voyage-4-large"}
    client.post("/o/acme/-/embeddings", data={**form, "meaning_min_similarity": "0,6"})
    assert (
        'name="meaning_min_similarity" inputmode="decimal" value="0.6"'
        in client.get("/o/acme/-/embeddings").text
    )
    page = client.post("/o/acme/-/embeddings", data={**form, "meaning_min_similarity": "2"})
    assert "Indiquez une similarité minimale entre 0 et 1" in unescape(page.text)
    assert semantic.meaning_floor(db, org.id) == 0.6


def test_files_are_embedded_in_batches_and_a_refused_file_is_skipped(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    enable(db, ctx, wiki)

    class Picky(ConceptEmbedder):
        def __call__(self, config: EmbeddingConfig, texts: list[str]) -> list[list[float]]:
            if any("mot de passe" in t for t in texts):  # refused by the provider
                self.calls.append(texts)
                raise AIError("Embeddings refusés (400)")
            return super().__call__(config, texts)

    embedder = Picky()
    index(database, ctx, wiki, embedder)
    first, *singles = embedder.calls
    assert len({t.split("\n")[0] for t in first}) > 1  # several files in one call
    files, _ = semantic.stats(db, wiki.infra.id)
    assert files == len(singles) - 1  # all indexed, except the refused one

    normal = ConceptEmbedder()
    semantic.clear(db, wiki.infra.id)
    db.commit()
    index(database, ctx, wiki, normal)
    assert len(normal.calls) == 1  # no refusal: a single call for the whole space


def test_embeddings_never_push_a_space_down(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """In multi-space search, the best page of a space with embeddings keeps its rank;
    meaning only adds pages or breaks ties."""
    enable(db, ctx, wiki)  # infra: embeddings; autre: none
    for n in range(10):
        content.write_file(
            db, database, wiki.root, ctx, wiki.other, f"p{n}.md", b"# P\n\nkubernetes\n"
        )
    content.write_file(
        db,
        database,
        wiki.root,
        ctx,
        wiki.infra,
        "cluster.md",
        b"# Cluster kubernetes\n\nkubernetes kubernetes kubernetes kubernetes\n",
    )
    db.commit()
    embedder = ConceptEmbedder()
    index(database, ctx, wiki, embedder)
    hits = search_service.search(db, wiki.root, ctx, "kubernetes", limit=10, embedder=embedder)
    assert "cluster.md" in [h.path for h in hits[:3]]


def test_gemini_embeddings_and_key_help(
    client: TestClient, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini through its OpenAI-compatible API; each provider explains how to get its key."""
    from synaptane.ai.embeddings import EmbeddingConfig

    sent: list[EmbeddingConfig] = []

    def fake_check(config: EmbeddingConfig) -> str:
        sent.append(config)
        return "Connexion réussie."

    monkeypatch.setattr("synaptane.web.tidy_pages.check_embeddings", fake_check)
    login(client, "root")
    token = csrf(client, "/o/acme/-/embeddings")
    client.post(
        "/o/acme/-/embeddings", data={"csrf": token, "provider": "gemini", "api_key": "AIza-x"}
    )
    page = client.get("/o/acme/-/embeddings").text
    assert "Google Gemini · gemini-embedding-001" in page
    for link in ("aistudio.google.com/apikey", "platform.openai.com/api-keys", "ollama pull"):
        assert link in page
    client.post("/o/acme/-/embeddings/test", data={"csrf": token})
    assert sent[0].endpoint == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert sent[0].api_key == "AIza-x"
    ai_page = client.get("/o/acme/-/ai").text
    assert "platform.claude.com/settings/keys" in ai_page
