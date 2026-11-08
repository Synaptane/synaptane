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
from synaptane.services.tidy import REFUSED, STEP_CAP
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


CHARS_PER_TOKEN = 3.5  # ordre de grandeur pour du français, du Markdown et du JSON


class Measured(Scripted):
    """Modèle simulé qui compte, sans rien appeler, ce qu'un vrai fournisseur facturerait pour
    le même chemin : à chaque tour, toute la conversation (consignes, outils, messages,
    résultats) en entrée, relue depuis le cache sauf ce qui est nouveau depuis le tour
    précédent (historique en ajout seul, comme le fournisseur Anthropic), et les appels
    d'outils en sortie. Les jetons sont estimés à `CHARS_PER_TOKEN` caractères ; le
    raisonnement du modèle (effort) n'est pas compté."""

    def __init__(self, turns: Turns, cache: bool = True) -> None:
        super().__init__(turns)
        self.cache = cache  # False : tout est facturé en entrée, au plein tarif
        self.context = 0  # caractères de la conversation envoyée au prochain tour
        self.previous = 0  # jetons de la requête précédente (en cache)
        self.seen = 0  # résultats déjà comptés

    @staticmethod
    def tokens(chars: int) -> int:
        return max(1, round(chars / CHARS_PER_TOKEN))

    def factory(self, config: AIConfig, system: str, first: str, tools: list[ToolSpec]) -> Any:
        schemas = [(t.name, t.description, t.input_schema) for t in tools]
        self.context = len(system) + len(first) + len(repr(schemas))
        return self

    def step(self) -> Turn:
        self.context += sum(len(r.content) for r in self.results[self.seen :])
        self.seen = len(self.results)
        prompt = self.tokens(self.context)
        written = max(0, prompt - self.previous) if self.cache else prompt
        turn = super().step()
        out = len(turn.text) + sum(len(repr(c.input)) + len(c.name) for c in turn.tool_calls)
        self.context += out
        self.previous = prompt
        turn.input_tokens, turn.cache_write_tokens = written, written if self.cache else 0
        turn.cached_tokens, turn.output_tokens = prompt - written, self.tokens(out + 40)
        return turn

    def nudge(self, text: str) -> None:
        self.context += len(text)


BUDGET_REACHED = "Budget du banc atteint."  # raison donnée par la limite du banc


class Watched:
    """Conversation d'un fournisseur, observée par le banc : modèles qui ont servi chaque tour,
    et erreur du fournisseur s'il y en a une (le passage, lui, la transforme en interruption
    sans la lever)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.models: list[str] = []
        self.error: AIError | None = None

    def step(self) -> Turn:
        try:
            turn: Turn = self.inner.step()
        except AIError as error:
            self.error = error
            raise
        if turn.model:
            self.models.append(turn.model)
        return turn

    def reply(self, results: list[ToolResult]) -> None:
        self.inner.reply(results)

    def nudge(self, text: str) -> None:
        self.inner.nudge(text)


def provider_error(error: AIError) -> dict[str, Any]:
    """L'erreur du fournisseur, sans son texte libre (adresse, message de l'API) : sa classe,
    son modèle de message, son statut HTTP, transitoire ou non."""
    status = error.label.params.get("status")
    return {
        "type": type(error).__name__,
        "message": error.label.template,
        "status": status if isinstance(status, int) else None,
        "transient": error.transient,
    }


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
    # Passage coupé : "budget", "provider" (erreur du fournisseur), "error" (du banc ou de Git),
    # ou par le modèle lui-même : "step_cap" (plafond de tours), "refusal". None : allé au bout.
    interrupted: str | None = None
    error: dict[str, Any] | None = None  # erreur du fournisseur (`provider_error`)
    served_models: list[str] = field(default_factory=list)
    detail: str = ""  # texte complet de l'erreur : pour la console seulement, jamais écrit

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
    inner = factory(names)
    watched: list[Watched] = []

    def observed(config: AIConfig, system: str, first: str, tools: list[ToolSpec]) -> Any:
        try:
            conversation = Watched(inner(config, system, first, tools))
        except AIError as error:  # le fournisseur refuse dès l'ouverture
            result.error, result.interrupted = provider_error(error), "provider"
            raise
        watched.append(conversation)
        return conversation

    try:
        outcome = tidy.tidy_space(
            env.database,
            env.ws,
            space,
            config,
            factory=observed,
            spent=result.usage,
            limit=limit,
        )
    except (tidy.TidyFailure, AIError, SynaptaneError, GitError) as error:
        _interruption(result, watched)
        result.interrupted = result.interrupted or "error"
        # Le texte libre d'une erreur peut citer l'adresse du fournisseur : la classe seule.
        result.failures["échec"] = f"passage interrompu ({result.interrupted})"
        result.detail = str(error)
        result.cost = pricing.cost(result.usage, price)
        return result
    _interruption(result, watched)
    result.metrics, result.items_done = outcome.metrics, outcome.items_done
    result.cost = pricing.cost(result.usage, price)
    result.failures = grade(env, space, scenario, base, outcome, titles)
    if result.interrupted in ("budget", "provider"):
        result.failures["échec"] = f"passage interrompu ({result.interrupted})"
        result.detail = outcome.interruption or ""
    return result


def _interruption(result: Result, watched: list[Watched]) -> None:
    """Pourquoi le passage s'est arrêté avant la fin, d'après la conversation et le journal."""
    result.served_models = sorted({m for w in watched for m in w.models})
    errors = [w.error for w in watched if w.error is not None]
    if errors:
        result.error, result.interrupted = provider_error(errors[-1]), "provider"
    elif result.interrupted:
        return
    elif BUDGET_REACHED in result.usage.notes:
        result.interrupted = "budget"
    elif STEP_CAP in result.usage.notes:
        result.interrupted = "step_cap"
    elif REFUSED in result.usage.notes:
        result.interrupted = "refusal"


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


def measured(
    env: Env, scenario: Scenario, price: pricing.Price | None, cache: bool = True
) -> Result:
    """Le bon script, compté comme le facturerait un vrai fournisseur (`Measured`) : le coût
    du chemin idéal, sans aucun appel. `cache` : historique relu depuis le cache (Anthropic)."""
    config = AIConfig(provider="anthropic", model="banc-estime", api_key="k")
    return run_scenario(
        env,
        scenario,
        mode="estimé",
        config=config,
        factory=lambda names: Measured(scenario.good(names), cache).factory,
        price=price,
    )
