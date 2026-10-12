"""Banc de la recherche : requêtes fixes sur le wiki du banc, avec les pages attendues.

Mesures, par type de requête (français exact, sans accents, anglais sur des pages françaises,
paraphrases) :
- **rappel@5** : part des pages attendues présentes dans les 5 premiers résultats ;
- **MRR** : moyenne de 1 / rang de la première page attendue (0 si aucune dans les 10).

Deux passages : recherche par mots seuls, puis hybride (mots et sens) avec des embeddings
**simulés** (`ConceptEmbedder` : un axe par concept, synonymes français et anglais compris).
Ces embeddings mesurent la fusion (RRF, filtre de confiance), pas la qualité d'un vrai modèle
d'embeddings : pour celle-ci, voir docs/dev/eval.md (piste). Les requêtes qui échouent
aujourd'hui (ligature « cœur », anglais sans embeddings, paraphrases) sont gardées exprès :
elles montreront le gain des corrections de la recherche (lot G4).
"""

import math
import re
from dataclasses import dataclass, field

from synaptane.ai.embeddings import EmbeddingConfig
from synaptane.services import search as search_service
from synaptane.services import semantic
from synaptane.storage.models import Organization, Space
from tests.eval.env import Env, new_space

TOP = 5
DEPTH = 10

# (requête, pages attendues, type)
QUERIES: list[tuple[str, tuple[str, ...], str]] = [
    ("sauvegarde vzdump", ("infra/sauvegardes.md", "infra/proxmox.md"), "fr"),
    ("WireGuard", ("outils/vpn.md",), "fr"),
    ("Vaultwarden", ("outils/coffre-mots-de-passe.md",), "fr"),
    ("réunion d'équipe", ("equipe/reunions.md",), "fr"),
    ("OPNsense", ("reseau/pare-feu.md",), "fr"),
    ("Let's Encrypt", ("infra/certificats.md",), "fr"),
    ("télétravail", ("decisions/2026-02-teletravail.md",), "fr"),
    ("planning d'astreinte", ("equipe/astreinte.md",), "fr"),
    ("Zabbix alertes", ("infra/supervision.md",), "fr"),
    ("Scaleway", ("projets/migration-cloud.md",), "fr"),
    ("GLPI", ("outils/tickets.md", "clients/support.md"), "fr"),
    ("congés Lucca", ("equipe/conges.md",), "fr"),
    ("teletravail", ("decisions/2026-02-teletravail.md",), "sans-accents"),
    ("reunion equipe", ("equipe/reunions.md",), "sans-accents"),
    ("procedure restauration", ("procedures/restauration.md",), "sans-accents"),
    ("deploiement", ("procedures/deploiement.md",), "sans-accents"),
    ("depart collaborateur", ("procedures/depart-collaborateur.md",), "sans-accents"),
    ("coeur", ("reseau/topologie.md",), "sans-accents"),  # ligature : « cœur » dans la page
    (
        "mots de passe comptes de service",
        ("procedures/changement-mot-de-passe.md",),
        "sans-accents",
    ),
    ("backup schedule", ("infra/sauvegardes.md",), "en"),
    ("password manager", ("outils/coffre-mots-de-passe.md",), "en"),
    ("firewall", ("reseau/pare-feu.md",), "en"),
    ("on-call", ("equipe/astreinte.md",), "en"),
    ("remote work policy", ("decisions/2026-02-teletravail.md",), "en"),
    ("team meeting", ("equipe/reunions.md",), "en"),
    ("certificate renewal", ("infra/certificats.md",), "en"),
    ("monitoring alerts", ("infra/supervision.md",), "en"),
    ("travailler depuis la maison", ("decisions/2026-02-teletravail.md",), "paraphrase"),
    ("qui appeler la nuit", ("equipe/astreinte.md",), "paraphrase"),
    ("accès à distance", ("outils/vpn.md",), "paraphrase"),
    ("renouvellement des certificats", ("infra/certificats.md",), "paraphrase"),
    ("ouvrir un port", ("reseau/pare-feu.md",), "paraphrase"),
    ("nouvel arrivant", ("equipe/onboarding.md", "procedures/arrivee-poste.md"), "paraphrase"),
    ("plan de reprise d'activité", ("procedures/restauration.md",), "paraphrase"),
    ("hyperviseur", ("infra/proxmox.md",), "paraphrase"),
    ("intégration continue", ("outils/gitlab.md", "decisions/2025-03-gitlab-ci.md"), "paraphrase"),
]

CONCEPTS: list[set[str]] = [
    {"sauvegarde", "sauvegardes", "sauvegardées", "backup", "backups", "vzdump", "schedule"},
    {"mot", "mots", "passe", "password", "passwords", "coffre", "vaultwarden", "manager"},
    {"pare", "feu", "firewall", "opnsense", "port", "ports", "ouverture", "ouvrir"},
    {"astreinte", "call", "appeler", "nuit", "permanence"},
    {"télétravail", "teletravail", "remote", "work", "maison", "domicile"},
    {"réunion", "reunion", "meeting", "team"},
    {"certificat", "certificats", "certificate", "renewal", "renouvellement", "renouvelés"},
    {"supervision", "zabbix", "monitoring", "alertes", "alerts", "alerte"},
    {"vpn", "wireguard", "distant", "distance", "accès"},
    {"arrivant", "arrivée", "onboarding", "nouvel", "accueil"},
]


class ConceptEmbedder:
    """Faux fournisseur d'embeddings : un axe par concept, plus un axe « autre »."""

    def __call__(self, config: EmbeddingConfig, texts: list[str]) -> list[list[float]]:
        return [self.vector(text) for text in texts]

    @staticmethod
    def vector(text: str) -> list[float]:
        words = re.findall(r"\w+", text.lower())
        values = [float(sum(w in concept for w in words)) for concept in CONCEPTS] + [0.1]
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values]


@dataclass
class Score:
    recall: float = 0.0
    mrr: float = 0.0
    count: int = 0
    by_kind: dict[str, "Score"] = field(default_factory=dict)
    misses: list[str] = field(default_factory=list)  # requêtes sans page attendue dans le top 5


def _score(rows: list[tuple[str, float, float]]) -> Score:
    if not rows:
        return Score()
    return Score(
        recall=round(sum(r for _, r, _ in rows) / len(rows), 3),
        mrr=round(sum(m for _, _, m in rows) / len(rows), 3),
        count=len(rows),
    )


def measure(results: dict[str, list[str]]) -> Score:
    """Rappel@5 et MRR, global et par type, à partir des chemins renvoyés par requête."""
    rows: dict[str, list[tuple[str, float, float]]] = {}
    misses = []
    for query, expected, kind in QUERIES:
        found = results.get(query, [])[:DEPTH]
        recall = len(set(found[:TOP]) & set(expected)) / len(expected)
        rank = next((i for i, path in enumerate(found, start=1) if path in expected), None)
        rows.setdefault(kind, []).append((query, recall, 1 / rank if rank else 0.0))
        if not recall:
            misses.append(f"{query} ({kind})")
    total = _score([row for kind_rows in rows.values() for row in kind_rows])
    total.by_kind = {kind: _score(kind_rows) for kind, kind_rows in rows.items()}
    total.misses = misses
    return total


def run(env: Env, embedder: ConceptEmbedder | None = None) -> dict[str, Score]:
    """Les deux passages (« mots », puis « sens » : hybride) sur un espace neuf."""
    embedder = embedder or ConceptEmbedder()
    space = new_space(env, "recherche")
    actor = env.actor()

    def ask(mode: str) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        with env.database.session() as session:
            for query, _, _ in QUERIES:
                hits = search_service.search(
                    session,
                    actor,
                    env.ws,
                    query,
                    addresses=[space.address],
                    limit=DEPTH,
                    mode=mode,
                    embedder=embedder,
                )
                found[query] = [hit.path for hit in hits]
        return found

    lexical = measure(ask("text"))
    with env.database.session() as session:
        org = session.get(Organization, env.org_id)
        stored = session.get(Space, space.id)
        assert org is not None and stored is not None
        semantic.configure(
            session, actor, env.ws.box, org, provider="openai", api_key="sk-banc-simule"
        )
        semantic.set_enabled(session, actor, stored, True)
    with env.database.session() as session:
        config = semantic.org_config(session, env.ws.box, env.org_id)
    assert config is not None
    repo = env.ws.local_repository(space)
    semantic.index_space(env.database, env.ws, space.id, repo, config, embedder)
    return {"mots": lexical, "sens": measure(ask("auto"))}


def report(scores: dict[str, Score]) -> str:
    """Tableau lisible : rappel@5 et MRR par passage et par type de requête."""
    kinds = ("fr", "sans-accents", "en", "paraphrase")
    lines = [f"{'passage':<8} {'type':<14} {'n':>3} {'rappel@5':>9} {'MRR':>6}"]
    for name, score in scores.items():
        for kind in kinds:
            part = score.by_kind.get(kind, Score())
            lines.append(
                f"{name:<8} {kind:<14} {part.count:>3} {part.recall:>9.3f} {part.mrr:>6.3f}"
            )
        lines.append(
            f"{name:<8} {'(tout)':<14} {score.count:>3} {score.recall:>9.3f} {score.mrr:>6.3f}"
        )
        if score.misses:
            lines.append(f"  sans résultat attendu ({name}) : " + " ; ".join(score.misses))
    return "\n".join(lines)
