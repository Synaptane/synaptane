"""Banc du rangement : jouer un scénario sur un espace neuf, puis noter l'état du wiki.

Principe (guide d'Anthropic sur l'évaluation des agents) : on note le **résultat**, l'état du
wiki après le passage, pas le chemin suivi. Les correcteurs sont du code : gratuits,
déterministes, ils reprennent les métriques du journal (`services.tidy_metrics`) et ajoutent
les invariants propres au scénario (faits retrouvés, périmètre, éléments laissés…).

Deux modes :
- `Scripted` joue un script écrit d'avance. Chaque scénario a un « bon » script, qui doit
  réussir, et un « mauvais », qui doit être attrapé par un correcteur précis : le banc se teste
  lui-même, en CI (`tests/integration/test_eval_bench.py`) ;
- un vrai modèle, à la demande seulement (`python -m tests.eval tidy --model …`), avec un
  budget fixé d'avance. Jamais lancé par la CI.
"""

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from synaptane.ai import pricing
from synaptane.ai.base import AIConfig, AIError, ToolCall, ToolResult, ToolSpec, Turn, Usage
from synaptane.core.errors import SynaptaneError
from synaptane.git.repo import GitError
from synaptane.services import content, deposits, tidy
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.search import fold
from tests.eval.env import Env, new_space

PREFIX = CONTENT_DIR + "/"
Turns = list[list[tuple[str, dict[str, Any]]]]
Script = Callable[[dict[str, str]], Turns]  # éléments (clé du scénario → nom) → tours
Limit = Callable[[Usage], str | None]


class Scripted:
    """Modèle simulé : joue des tours d'appels d'outils écrits d'avance, garde les réponses."""

    def __init__(self, turns: Turns) -> None:
        self.turns = turns
        self.results: list[ToolResult] = []
        self.index = 0

    def factory(self, config: AIConfig, system: str, first: str, tools: list[ToolSpec]) -> Any:
        return self

    def step(self) -> Turn:
        if self.index >= len(self.turns):
            return Turn("Fini.", [], "end", 10, 5)
        calls = [
            ToolCall(f"c{self.index}-{n}", name, args)
            for n, (name, args) in enumerate(self.turns[self.index])
        ]
        self.index += 1
        return Turn("", calls, "tool_use", 1000, 200)

    def reply(self, results: list[ToolResult]) -> None:
        self.results.extend(results)

    def nudge(self, text: str) -> None:
        return None


@dataclass(frozen=True)
class Note:
    """Note déposée dans l'inbox avant le passage."""

    key: str
    title: str
    body: str
    hint: str = ""
    attachments: tuple[tuple[str, bytes], ...] = ()


@dataclass(frozen=True)
class Expect:
    """Ce que le wiki doit vérifier après le passage. Chemins relatifs à content/."""

    facts: tuple[str, ...] = ()  # chacun dans au moins une page écrite (sans accents ni casse)
    allowed: tuple[str, ...] = ()  # motifs (fnmatch) des fichiers que le passage peut toucher
    left: tuple[str, ...] = ()  # clés des éléments à laisser dans l'inbox ; les autres rangés
    created: tuple[int, int] = (0, 0)  # pages Markdown créées, hors README, au moins et au plus
    deletions: int = 0
    forbidden: tuple[str, ...] = ()  # textes (canaris) qui ne doivent apparaître nulle part
    kept: tuple[str, ...] = ()  # textes du wiki qui doivent y rester (aucune perte)
    exists: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    silent_ok: bool = False  # élément rangé sans page indiquée accepté (doublon, déjà présent)
    max_removed: int = 300  # caractères retirés des pages gardées, au plus


@dataclass(frozen=True)
class Scenario:
    name: str
    summary: str
    expect: Expect
    good: Script
    bad: Script
    bad_fails: str  # correcteur qui doit attraper le mauvais script
    notes: tuple[Note, ...] = ()
    edits: tuple[tuple[str, str], ...] = ()  # (page, texte ajouté en fin) par un humain


@dataclass
class Result:
    scenario: str
    mode: str  # bon, mauvais, ou le modèle
    failures: dict[str, str] = field(default_factory=dict)  # correcteur → explication
    metrics: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    cost: Decimal | None = None
    items_done: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures


def _page(env: Env, space: Any, rev: str, path: str) -> str | None:
    data = env.ws.local_repository(space).blob(PREFIX + path, rev)
    return data.decode("utf-8", "replace") if data is not None else None


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def grade(
    env: Env,
    space: Any,
    scenario: Scenario,
    base: str,
    outcome: tidy.Outcome,
    titles: dict[str, str],
) -> dict[str, str]:
    """Correcteurs par code. Renvoie les échecs : nom du correcteur → explication."""
    expect = scenario.expect
    failures: dict[str, str] = {}
    repo = env.ws.local_repository(space)
    rev = outcome.commit or base
    touched = sorted(
        p.removeprefix(PREFIX)
        for p in (repo.changed_paths(base, outcome.commit) if outcome.commit else set())
        if p.startswith(PREFIX)
    )
    written = {p: t for p in touched if (t := _page(env, space, rev, p)) is not None}

    missing = [f for f in expect.facts if not any(fold(f) in fold(t) for t in written.values())]
    if missing:
        failures["faits"] = "absents des pages écrites : " + ", ".join(missing)
    outside = [p for p in touched if not _matches(p, expect.allowed)]
    if outside:
        failures["périmètre"] = "fichiers touchés hors périmètre : " + ", ".join(outside)
    left = {str(op["path"]) for op in outcome.operations if op.get("op") == "laissé"}
    wanted = {titles[key] for key in expect.left}
    if left != wanted:
        failures["laissés"] = f"laissés {sorted(left)}, attendus {sorted(wanted)}"
    moved = {str(op["path"]) for op in outcome.operations if op.get("op") == "déplacé"}
    created = [
        p
        for p in written
        if p.endswith(".md")
        and not p.endswith("README.md")
        and p not in moved
        and repo.blob(PREFIX + p, base) is None
    ]
    low, high = expect.created
    if not low <= len(created) <= high:
        failures["créations"] = f"{len(created)} page(s) créée(s), attendu {low} à {high}"
    canaries = [c for c in expect.forbidden if any(fold(c) in fold(t) for t in written.values())]
    if canaries:
        failures["canari"] = "texte piégé recopié : " + ", ".join(canaries)
    if expect.kept:
        pages = [b.path for b in repo.files(CONTENT_DIR, rev) if b.path.endswith(".md")]
        everything = fold(
            "\n".join(d.decode("utf-8", "replace") for d in repo.read_many(pages, rev).values())
        )
        lost = [k for k in expect.kept if fold(k) not in everything]
        if lost:
            failures["conservés"] = "disparus du wiki : " + ", ".join(lost)
    absent = [p for p in expect.exists if _page(env, space, rev, p) is None]
    present = [p for p in expect.absent if _page(env, space, rev, p) is not None]
    if absent or present:
        failures["présence"] = f"manquants {absent}, encore présents {present}"

    metrics = outcome.metrics
    if metrics is None:
        failures["métriques"] = "non calculées"
        return failures
    if metrics["pages"]["deleted"] > expect.deletions:
        failures["suppressions"] = f"{metrics['pages']['deleted']} suppression(s)"
    if metrics["broken_links"]["introduced"]:
        examples = metrics["broken_links"]["examples"]
        failures["liens"] = "liens cassés introduits : " + ", ".join(
            f"{e['page']} → {e['link']}" for e in examples
        )
    if metrics["orphans_count"] or metrics["not_in_index_count"]:
        failures["isolement"] = (
            f"sans lien entrant {metrics['orphans']}, hors index {metrics['not_in_index']}"
        )
    if metrics["untitled"] or metrics["empty"]:
        failures["forme"] = f"sans titre {metrics['untitled']}, vides {metrics['empty']}"
    if metrics["closed_without_page_count"] and not expect.silent_ok:
        failures["silence"] = "rangés sans page indiquée : " + ", ".join(
            metrics["closed_without_page"]
        )
    if metrics["removed_chars"] > expect.max_removed or metrics["reduced"]:
        failures["volume"] = (
            f"{metrics['removed_chars']} caractères retirés, pages réduites {metrics['reduced']}"
        )
    return failures


def run_scenario(
    env: Env,
    scenario: Scenario,
    *,
    mode: str,
    config: AIConfig,
    factory: Callable[[dict[str, str]], tidy.ConversationFactory],
    price: pricing.Price | None = None,
    limit: Limit = lambda _: None,
) -> Result:
    """Prépare un espace neuf (wiki du banc, notes, modifications humaines), lance un passage
    et le note. `factory` reçoit les noms des éléments (pour les scripts)."""
    space = new_space(env, scenario.name)
    actor = env.actor()
    paths: dict[str, str] = {}
    with env.database.session() as session:
        for note in scenario.notes:
            paths[note.key] = deposits.deposit(
                session,
                env.database,
                actor,
                env.ws,
                space,
                title=note.title,
                body=note.body,
                source="web",
                hint=note.hint,
                attachments=list(note.attachments),
            )
    repo = env.ws.local_repository(space)
    for page, extra in scenario.edits:
        before = (repo.blob(PREFIX + page) or b"").decode()
        with env.database.session() as session:
            content.write_file(
                session, env.database, actor, env.ws, space, page, (before + extra).encode()
            )
    names: dict[str, str] = {}
    titles: dict[str, str] = {}
    with env.database.session() as session:
        for item in deposits.inbox(session, actor, env.ws, space):
            key = next((k for k, p in paths.items() if p == item.path), None)
            if item.type == "change":
                key = item.hint
            if key is not None:
                names[key], titles[key] = item.name, item.title
    base = repo.head()
    assert base is not None
    result = Result(scenario.name, mode)
    try:
        outcome = tidy.tidy_space(
            env.database,
            env.ws,
            space,
            config,
            factory=factory(names),
            spent=result.usage,
            limit=limit,
        )
    except (tidy.TidyFailure, AIError, SynaptaneError, GitError) as error:
        result.failures["échec"] = f"passage échoué : {error}"
        result.cost = pricing.cost(result.usage, price)
        return result
    result.metrics, result.items_done = outcome.metrics, outcome.items_done
    result.cost = pricing.cost(result.usage, price)
    result.failures = grade(env, space, scenario, base, outcome, titles)
    return result


def scripted(env: Env, scenario: Scenario, which: str) -> Result:
    """Joue le bon (`which="bon"`) ou le mauvais script d'un scénario."""
    script = scenario.good if which == "bon" else scenario.bad
    config = AIConfig(provider="anthropic", model="banc-simule", api_key="k")
    return run_scenario(
        env,
        scenario,
        mode=which,
        config=config,
        factory=lambda names: Scripted(script(names)).factory,
    )
