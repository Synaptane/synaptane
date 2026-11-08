"""Résultats publiables du banc du rangement : fichier de lancement, puis rapport agrégé.

Deux formats JSON, versionnés (`format`, `format_version`) :
- le **lancement** (`synaptane-tidy-bench-run`), écrit par `tidy --model …` : la version de
  Synaptane (commit, arbre modifié ou non), les dates et la durée, les empreintes du banc et de
  chaque scénario, le modèle, le fournisseur, l'effort, l'invite (empreinte et chemin), les
  outils (empreinte), le tarif appliqué, le budget, et chaque essai (scénario, numéro, réussite,
  correcteurs en échec, jetons, coût, durée, métriques du journal) ;
- le **rapport** (`synaptane-tidy-bench-results`), écrit par `report` à partir d'un ou
  plusieurs lancements : une configuration par ligne (modèle, effort, invite…), taux de réussite
  avec son intervalle de Wilson à 95 %, score par balayage (moyenne, écart-type), coût, jetons
  et durée, et le détail par scénario ; plus deux tableaux Markdown (anglais, français) avec la
  méthode en tête.

Le rapport est reproductible : il ne dépend que des lancements (aucune date du jour), et le tri
est stable. Il refuse ce qui ne se compare pas honnêtement : un lancement interrompu (budget),
trop peu d'essais, des scénarios différents d'un lancement à l'autre, un arbre modifié (sauf
`--allow-dirty`), et tout texte qui ressemble à un secret ou à un chemin de la machine.
"""

import hashlib
import json
import math
import re
import statistics
import string
import subprocess
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import synaptane
from synaptane.ai import pricing
from synaptane.services import tidy
from synaptane.services.tidy_tools import INSTRUCTIONS, TOOLS
from tests.eval.tidy_bench import Result, Scenario
from tests.eval.wikidata import PAGES

RUN_FORMAT = "synaptane-tidy-bench-run"
RESULTS_FORMAT = "synaptane-tidy-bench-results"
FORMAT_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "eval" / "results"
RUNS_DIR = RESULTS_DIR / "runs"
BENCH_FILES = ("tests/eval/tidy_bench.py", "tests/eval/scenarios.py", "tests/eval/wikidata.py")
DEFAULT_PROMPT = "src/synaptane/services/tidy_tools.py (INSTRUCTIONS)"
MIN_TRIALS = 3
Z95 = 1.959964  # quantile à 97,5 % de la loi normale : intervalle à 95 %
# Champs de l'invite du rangement (`tidy.tidy_space`) : une autre invite n'en a pas d'autres.
PROMPT_FIELDS = frozenset(
    {"name", "address", "description", "language", "vocation", "custom", "max_deletions"}
)


class ReportError(Exception):
    """Lancements impossibles à publier tels quels : le message dit pourquoi."""


# --- Empreintes -----------------------------------------------------------------------------


def sha256(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def scenario_fingerprint(scenario: Scenario) -> str:
    """Empreinte des données du scénario (inbox, modifications humaines, attentes, correcteur
    du mauvais script) : elle change dès que le scénario change, pas avec ses scripts."""
    notes = [
        {
            "key": n.key,
            "title": n.title,
            "body": n.body,
            "hint": n.hint,
            "attachments": [[name, sha256(data)] for name, data in n.attachments],
        }
        for n in scenario.notes
    ]
    return sha256(
        _canonical(
            {
                "name": scenario.name,
                "summary": scenario.summary,
                "notes": notes,
                "edits": [list(e) for e in scenario.edits],
                "expect": asdict(scenario.expect),
                "bad_fails": scenario.bad_fails,
            }
        )
    )


def bench_fingerprint() -> dict[str, Any]:
    """Empreinte du banc : ses fichiers (correcteurs, scénarios, wiki) et le wiki lui-même."""
    files = {name: sha256((ROOT / name).read_bytes()) for name in BENCH_FILES}
    return {
        "sha256": sha256(_canonical(files)),
        "files": files,
        "wiki_sha256": sha256(_canonical(PAGES)),
        "wiki_pages": len(PAGES),
    }


def tools_fingerprint() -> str:
    """Empreinte des outils de l'agent (noms, descriptions, schémas) : ils font partie de
    l'invite au même titre que les consignes."""
    return sha256(_canonical([asdict(t) for t in TOOLS]))


def source_state() -> dict[str, Any]:
    """Version et commit de Synaptane ; `dirty` : des fichiers suivis par Git ont changé
    depuis ce commit (le lancement ne se reproduit alors pas depuis le commit seul)."""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        dirty: bool | None = bool(status.stdout.strip()) if status.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        dirty = None
    return {"version": synaptane.__version__, "commit": synaptane.git_commit(), "dirty": dirty}


# --- Invite ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    template: str
    source: str  # "default" : INSTRUCTIONS ; "file" : un fichier donné au banc
    path: str  # relatif au dépôt ; hors du dépôt, le nom du fichier seul

    @property
    def sha256(self) -> str:
        return sha256(self.template)

    def as_json(self) -> dict[str, str]:
        return {"source": self.source, "path": self.path, "sha256": self.sha256}


def load_prompt(path: str = "") -> Prompt:
    """L'invite par défaut, ou celle d'un fichier : mêmes champs `{…}` que INSTRUCTIONS, sans
    autre (sinon `ValueError`)."""
    if not path:
        return Prompt(INSTRUCTIONS, "default", DEFAULT_PROMPT)
    file = Path(path).resolve()
    template = file.read_text(encoding="utf-8")
    try:
        fields = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    except ValueError as error:
        raise ValueError(f"invite illisible ({error}) : doublez les accolades littérales") from None
    if unknown := sorted(f for f in fields if f not in PROMPT_FIELDS):
        raise ValueError(
            f"champs inconnus dans l'invite : {', '.join(unknown)} "
            f"(connus : {', '.join(sorted(PROMPT_FIELDS))})"
        )
    shown = file.relative_to(ROOT).as_posix() if file.is_relative_to(ROOT) else file.name
    return Prompt(template, "file", shown)


@contextmanager
def instructions(prompt: Prompt) -> Iterator[None]:
    """Le passage utilise cette invite le temps du bloc (le banc seulement, jamais l'instance)."""
    # `tidy_space` lit le nom importé dans son module : c'est lui qu'on remplace.
    saved = getattr(tidy, "INSTRUCTIONS")  # noqa: B009
    setattr(tidy, "INSTRUCTIONS", prompt.template)  # noqa: B010
    try:
        yield
    finally:
        setattr(tidy, "INSTRUCTIONS", saved)  # noqa: B010


# --- Secrets et chemins ---------------------------------------------------------------------

# Clés d'API courantes, jetons de forge, en-têtes, et chemins de la machine qui a lancé le banc.
_SECRET = re.compile(
    r"\bsk-[A-Za-z0-9_\-]{12,}|\bsk-ant-[A-Za-z0-9_\-]{6,}|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|glpat-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{30,}|\bBearer\s+[A-Za-z0-9._\-]{12,}"
    r"|\bapi[_-]?key\b"
    r"|(?<![\w.])/(?:home|Users|root|tmp|private|var/folders)/[^\s\"']*",
    re.IGNORECASE,
)


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Le texte sans les secrets connus (la clé du lancement) ni rien qui leur ressemble."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[secret]")
    return _SECRET.sub("[masqué]", text)


def _scrubbed(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, dict):
        return {k: _scrubbed(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrubbed(v, secrets) for v in value]
    return value


def leaks(text: str) -> list[str]:
    """Ce qui ressemble à un secret ou à un chemin de la machine dans un texte à publier."""
    return sorted({m.group(0) for m in _SECRET.finditer(text)})


# --- Lancement ------------------------------------------------------------------------------


@dataclass
class Trial:
    trial: int
    result: Result
    duration_s: float


def trial_json(trial: Trial) -> dict[str, Any]:
    r = trial.result
    return {
        "scenario": r.scenario,
        "trial": trial.trial,
        "passed": r.passed,
        "failures": dict(sorted(r.failures.items())),
        "duration_s": round(trial.duration_s, 3),
        "tokens": {
            "input": r.usage.input_tokens,
            "output": r.usage.output_tokens,
            "cached": r.usage.cached_tokens,
            "cache_write": r.usage.cache_write_tokens,
        },
        "turns": r.usage.turns,
        "cost": float(r.cost) if r.cost is not None else None,
        "items_done": r.items_done,
        "metrics": r.metrics,  # métriques du journal (wiki fictif du banc)
    }


def run_record(
    *,
    scenarios: Sequence[Scenario],
    trials: Sequence[Trial],
    trials_requested: int,
    provider: str,
    model: str,
    effort: str,
    custom_base_url: bool,
    prompt: Prompt,
    price: pricing.Price,
    budget: float,
    label: str,
    started: datetime,
    finished: datetime,
    stopped: bool,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    """Le fichier d'un lancement, prêt à écrire : sans secret ni chemin de la machine."""
    expected = len(scenarios) * trials_requested
    record = {
        "format": RUN_FORMAT,
        "format_version": FORMAT_VERSION,
        "synaptane": source_state(),
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds(), 1),
        "bench": bench_fingerprint(),
        "scenarios": [
            {"name": s.name, "summary": s.summary, "sha256": scenario_fingerprint(s)}
            for s in scenarios
        ],
        "config": {
            "label": label,
            "provider": provider,
            "model": model,
            "effort": effort,
            "base_url": "custom" if custom_base_url else "default",
            "prompt": prompt.as_json(),
            "tools_sha256": tools_fingerprint(),
        },
        "price": {
            "input": float(price.input),
            "output": float(price.output),
            "cache_read": float(price.cache_read),
            "cache_write": float(price.cache_write),
            "long_prompt": price.long_prompt,
            "public": price.public,
            "currency": "USD" if price.public else "",
            "unit": "USD per million tokens (public list price)"
            if price.public
            else "as entered, per million tokens",
        },
        "budget": budget,
        "trials_requested": trials_requested,
        "complete": not stopped and len(trials) == expected,
        "stopped_by_budget": stopped,
        "trials": [trial_json(t) for t in trials],
    }
    clean: dict[str, Any] = _scrubbed(record, list(secrets))
    return clean


def run_path(model: str, effort: str, label: str, day: str, directory: Path = RUNS_DIR) -> Path:
    """`runs/2026-11-08-claude-sonnet-5-5-medium[-label].json`, sans écraser un fichier."""
    parts = [day, model, effort] + ([label] if label else [])
    stem = re.sub(r"[^a-z0-9.]+", "-", "-".join(parts).lower()).strip("-")
    path = directory / f"{stem}.json"
    n = 2
    while path.exists():
        path = directory / f"{stem}-{n}.json"
        n += 1
    return path


def dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_run(path: Path, record: dict[str, Any]) -> None:
    text = dump(record)
    if found := leaks(text):
        raise ReportError("secret ou chemin dans le lancement : " + ", ".join(found))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- Agrégation -----------------------------------------------------------------------------


def wilson(passed: int, total: int) -> tuple[float, float]:
    """Intervalle de Wilson à 95 % d'une proportion : juste même près de 0 ou de 1 et avec
    peu d'essais (où l'intervalle « p ± 1,96 σ » sort de [0, 1])."""
    if total <= 0:
        return (0.0, 1.0)
    p = passed / total
    z2 = Z95 * Z95
    centre = (p + z2 / (2 * total)) / (1 + z2 / total)
    half = Z95 * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / (1 + z2 / total)
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def _spread(values: Sequence[float]) -> dict[str, float | int | None]:
    """Moyenne, écart-type (échantillon, n − 1), minimum et maximum."""
    if not values:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "sd": round(statistics.stdev(values), 4) if len(values) > 1 else None,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def _key(run: dict[str, Any]) -> str:
    """Ce qui fait une configuration : deux lancements identiques sur ces points s'ajoutent."""
    c = run["config"]
    return sha256(
        _canonical(
            {
                "commit": run["synaptane"]["commit"],
                "label": c["label"],
                "provider": c["provider"],
                "model": c["model"],
                "effort": c["effort"],
                "base_url": c["base_url"],
                "prompt": c["prompt"]["sha256"],
                "tools": c["tools_sha256"],
                "price": run["price"],
            }
        )
    )[:12]


def load_runs(paths: Sequence[Path]) -> list[tuple[str, dict[str, Any]]]:
    runs = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ReportError(f"{path.name} : illisible ({error})") from None
        if not isinstance(data, dict) or data.get("format") != RUN_FORMAT:
            raise ReportError(f"{path.name} : pas un lancement du banc ({RUN_FORMAT})")
        if data.get("format_version") != FORMAT_VERSION:
            raise ReportError(
                f"{path.name} : format {data.get('format_version')}, attendu {FORMAT_VERSION}"
            )
        runs.append((path.name, data))
    return runs


def check(
    runs: Sequence[tuple[str, dict[str, Any]]], *, min_trials: int, allow_dirty: bool
) -> None:
    """Refuse ce qui ne se compare pas honnêtement, avec la raison."""
    if not runs:
        raise ReportError("aucun lancement : lancez d'abord `tidy --model …` (docs/dev/eval.md)")
    problems = []
    for name, run in runs:
        if not run["complete"]:
            why = "budget atteint" if run["stopped_by_budget"] else "essais manquants"
            problems.append(f"{name} : lancement incomplet ({why}), à relancer avec plus de budget")
        if run["trials_requested"] < min_trials:
            problems.append(
                f"{name} : {run['trials_requested']} essai(s) par scénario, au moins {min_trials} "
                "pour comparer (un seul tirage ne dit rien de la variance)"
            )
        if run["synaptane"]["dirty"] is not False and not allow_dirty:
            problems.append(
                f"{name} : lancé sur un arbre modifié (ou commit inconnu) : non reproductible "
                "depuis le commit (--allow-dirty pour passer outre)"
            )
    benches = {(run["bench"]["sha256"], _canonical(run["scenarios"])) for _, run in runs}
    if len(benches) > 1:
        problems.append(
            "scénarios ou correcteurs différents d'un lancement à l'autre : comparez des "
            "lancements du même banc"
        )
    if problems:
        raise ReportError("\n".join(problems))


def _configuration(key: str, group: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    first = group[0][1]
    config = first["config"]
    names = [s["name"] for s in first["scenarios"]]
    trials = [t for _, run in group for t in run["trials"]]
    passed = sum(1 for t in trials if t["passed"])
    # Score d'un balayage : part des scénarios réussis à l'essai n d'un lancement.
    sweeps = [
        sum(1 for t in run["trials"] if t["trial"] == n and t["passed"]) / len(names)
        for _, run in group
        for n in range(1, run["trials_requested"] + 1)
    ]
    costs = [float(t["cost"]) for t in trials if t["cost"] is not None]
    known = len(costs) == len(trials)
    total_cost = round(sum(costs), 4) if known else None
    per_scenario = {}
    for name in names:
        mine = [t for t in trials if t["scenario"] == name]
        ok = sum(1 for t in mine if t["passed"])
        graders: dict[str, int] = {}
        for t in mine:
            for grader in t["failures"]:
                graders[grader] = graders.get(grader, 0) + 1
        per_scenario[name] = {
            "passed": ok,
            "total": len(mine),
            "pass_rate": round(ok / len(mine), 4) if mine else None,
            "failures": dict(sorted(graders.items())),
        }
    label = config["label"] or f"{config['model']} · {config['effort']}"
    if config["prompt"]["source"] != "default" and not config["label"]:
        label += f" · prompt {config['prompt']['sha256'][:8]}"
    return {
        "id": key,
        "label": label,
        "provider": config["provider"],
        "model": config["model"],
        "effort": config["effort"],
        "base_url": config["base_url"],
        "prompt": config["prompt"],
        "tools_sha256": config["tools_sha256"],
        "synaptane": {
            "version": first["synaptane"]["version"],
            "commit": first["synaptane"]["commit"],
            "dirty": any(run["synaptane"]["dirty"] is not False for _, run in group),
        },
        "price": first["price"],
        "runs": sorted(name for name, _ in group),
        "dates": {
            "first": min(run["started_at"] for _, run in group),
            "last": max(run["finished_at"] for _, run in group),
        },
        "trials_per_scenario": sum(run["trials_requested"] for _, run in group),
        "passed": passed,
        "total": len(trials),
        "pass_rate": round(passed / len(trials), 4) if trials else None,
        "ci95": list(wilson(passed, len(trials))),
        "sweep_score": _spread(sweeps),
        "cost": {
            "total": total_cost,
            "per_run": _spread(costs) if known else _spread([]),
            "per_passed_run": round(total_cost / passed, 4)
            if total_cost is not None and passed
            else None,
            "currency": first["price"]["currency"],
        },
        "tokens_per_run": {
            kind: _spread([float(t["tokens"][kind]) for t in trials])["mean"]
            for kind in ("input", "output", "cached", "cache_write")
        },
        "turns_per_run": _spread([float(t["turns"]) for t in trials])["mean"],
        "duration_s": {
            "per_run": _spread([float(t["duration_s"]) for t in trials]),
            "total": round(sum(float(run["duration_s"]) for _, run in group), 1),
        },
        "scenarios": per_scenario,
    }


def _order(config: dict[str, Any]) -> tuple[float, float, str, str]:
    """Meilleur taux d'abord, puis le moins cher par passage ; ensuite par nom : stable."""
    mean = config["cost"]["per_run"]["mean"]
    return (-(config["pass_rate"] or 0.0), mean if mean is not None else math.inf,
            config["label"], config["id"])  # fmt: skip


def aggregate(
    runs: Sequence[tuple[str, dict[str, Any]]],
    *,
    min_trials: int = MIN_TRIALS,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    """Le rapport publiable : configurations triées, méthode, provenance."""
    check(runs, min_trials=min_trials, allow_dirty=allow_dirty)
    ordered = sorted(runs, key=lambda r: r[0])
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name, run in ordered:
        groups.setdefault(_key(run), []).append((name, run))
    configurations = sorted(
        (_configuration(key, group) for key, group in groups.items()), key=_order
    )
    first = ordered[0][1]
    report = {
        "format": RESULTS_FORMAT,
        "format_version": FORMAT_VERSION,
        "method": {
            "measured": "outcome of a tidy-up run on a fictional wiki, checked by graders "
            "written in code (docs/dev/eval.md)",
            "pass": "a run passes when no grader fails",
            "interval": "Wilson score interval, 95 %, over all runs of a configuration",
            "sweep_score": "share of scenarios passed in one sweep (trial n of every "
            "scenario); mean and sample standard deviation over sweeps",
            "cost": "estimated from token counts at the price shown, not an invoice",
            "min_trials": min_trials,
        },
        "bench": {
            "sha256": first["bench"]["sha256"],
            "files": first["bench"]["files"],
            "wiki_sha256": first["bench"]["wiki_sha256"],
            "wiki_pages": first["bench"]["wiki_pages"],
            "scenarios": [{"name": s["name"], "sha256": s["sha256"]} for s in first["scenarios"]],
        },
        "configurations": configurations,
    }
    return report


# --- Tableaux Markdown ----------------------------------------------------------------------

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "switch": "English | [Français](results.fr.md)",
        "title": "Tidy-up bench: results",
        "intro": "Generated by `./scripts/eval.sh report` from the run files listed below: "
        "do not edit by hand. How they were produced and how to reproduce them: "
        "[README](README.md).",
        "method": "Method",
        "m1": "Each scenario drops notes (or a human change) in the inbox of a new space, a "
        "copy of a fictional wiki of {pages} pages in French; the agent runs one tidy-up pass, "
        "then graders written in code check the **outcome**: facts filed, scope kept, items "
        "left, pages created, canaries not copied, links, index, deletions, text removed. "
        "A run passes when no grader fails.",
        "m2": "{scenarios} scenarios, {trials} trials per scenario and configuration at least; "
        "every configuration ran the same scenarios (bench `{bench}`).",
        "m3": "Pass rate: runs passed out of runs played, with its 95 % Wilson interval. Sweep "
        "score: share of scenarios passed in one sweep, mean ± standard deviation over sweeps.",
        "m4": "Cost: estimated from the tokens reported by the provider, at the price shown "
        "(public list price on the run date unless stated), not an invoice.",
        "limits": "Limits",
        "l1": "A small sample: {scenarios} short scenarios on one fictional wiki do not cover "
        "a real inbox. Overlapping intervals mean the difference is not established.",
        "l2": "Graders check invariants of the result, not the quality of the writing.",
        "l3": "A model name can point to a different model later at its provider; each row "
        "gives its dates and the Synaptane commit it ran on.",
        "summary": "Summary",
        "h_conf": "Configuration",
        "h_trials": "Trials",
        "h_pass": "Pass rate (95 % CI)",
        "h_sweep": "Sweep score",
        "h_cost": "Cost per run",
        "h_cost_ok": "Cost per passed run",
        "h_tokens": "Tokens per run",
        "h_time": "Time per run",
        "by_scenario": "By scenario",
        "h_scenario": "Scenario",
        "failures": "Graders that failed most",
        "provenance": "Provenance",
        "p_conf": "Configuration",
        "p_model": "Provider / model / effort",
        "p_prompt": "Prompt",
        "p_tools": "Tools",
        "p_commit": "Synaptane",
        "p_dates": "Dates",
        "p_runs": "Run files",
        "p_price": "Price",
        "dirty": "modified tree",
        "price_public": "USD per million tokens, input / output (public list price)",
        "price_entered": "per million tokens, input / output (price entered for the run)",
        "unknown": "unknown",
        "none": "none",
    },
    "fr": {
        "switch": "[English](results.md) | Français",
        "title": "Banc du rangement : résultats",
        "intro": "Produit par `./scripts/eval.sh report` à partir des lancements listés plus "
        "bas : ne pas modifier à la main. Comment ils ont été produits et comment les "
        "reproduire : [README](README.fr.md).",
        "method": "Méthode",
        "m1": "Chaque scénario dépose des notes (ou une modification humaine) dans l'inbox d'un "
        "espace neuf, copie d'un wiki fictif de {pages} pages en français ; l'agent fait un "
        "passage de rangement, puis des correcteurs écrits en code vérifient le **résultat** : "
        "faits rangés, périmètre respecté, éléments laissés, pages créées, canaris non "
        "recopiés, liens, index, suppressions, texte retiré. Un passage réussit quand aucun "
        "correcteur n'échoue.",
        "m2": "{scenarios} scénarios, au moins {trials} essais par scénario et par "
        "configuration ; toutes les configurations ont joué les mêmes scénarios (banc "
        "`{bench}`).",
        "m3": "Taux de réussite : passages réussis sur passages joués, avec son intervalle de "
        "Wilson à 95 %. Score par balayage : part des scénarios réussis en un balayage, "
        "moyenne ± écart-type sur les balayages.",
        "m4": "Coût : estimé à partir des jetons comptés par le fournisseur, au tarif indiqué "
        "(tarif public à la date du lancement, sauf mention), pas une facture.",
        "limits": "Limites",
        "l1": "Un petit échantillon : {scenarios} scénarios courts sur un seul wiki fictif ne "
        "couvrent pas une vraie inbox. Des intervalles qui se chevauchent : l'écart n'est pas "
        "établi.",
        "l2": "Les correcteurs vérifient des invariants du résultat, pas la qualité de l'écriture.",
        "l3": "Un nom de modèle peut désigner plus tard un autre modèle chez son fournisseur ; "
        "chaque ligne donne ses dates et le commit de Synaptane sur lequel elle a tourné.",
        "summary": "Synthèse",
        "h_conf": "Configuration",
        "h_trials": "Essais",
        "h_pass": "Réussite (IC 95 %)",
        "h_sweep": "Score par balayage",
        "h_cost": "Coût par passage",
        "h_cost_ok": "Coût par passage réussi",
        "h_tokens": "Jetons par passage",
        "h_time": "Durée par passage",
        "by_scenario": "Par scénario",
        "h_scenario": "Scénario",
        "failures": "Correcteurs le plus souvent en échec",
        "provenance": "Provenance",
        "p_conf": "Configuration",
        "p_model": "Fournisseur / modèle / effort",
        "p_prompt": "Invite",
        "p_tools": "Outils",
        "p_commit": "Synaptane",
        "p_dates": "Dates",
        "p_runs": "Lancements",
        "p_price": "Tarif",
        "dirty": "arbre modifié",
        "price_public": "$ par million de jetons, entrée / sortie (tarif public)",
        "price_entered": "par million de jetons, entrée / sortie (tarif saisi pour le lancement)",
        "unknown": "inconnu",
        "none": "aucun",
    },
}


def _number(value: float | None, digits: int, lang: str) -> str:
    if value is None:
        return "–"
    text = f"{value:,.{digits}f}"
    if lang == "fr":
        text = text.replace(",", " ").replace(".", ",")
    return text


def _percent(value: float | None, lang: str) -> str:
    if value is None:
        return "–"
    return f"{_number(value * 100, 0, lang)} %"


def _money(value: float | None, unit: str, lang: str) -> str:
    if value is None:
        return "–"
    shown = _number(value, 4, lang)
    if unit == "USD":
        return f"{shown} $" if lang == "fr" else f"${shown}"
    return shown


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def markdown(report: dict[str, Any], lang: str) -> str:
    t = TEXTS[lang]
    configs = report["configurations"]
    scenarios = [s["name"] for s in report["bench"]["scenarios"]]
    least = min(c["trials_per_scenario"] for c in configs)
    values = {
        "pages": report["bench"]["wiki_pages"],
        "scenarios": len(scenarios),
        "trials": least,
        "bench": report["bench"]["sha256"][:12],
    }
    lines = [t["switch"], "", f"# {t['title']}", "", t["intro"], "", f"## {t['method']}", ""]
    lines += [f"- {t[k].format(**values)}" for k in ("m1", "m2", "m3", "m4")]
    lines += ["", f"### {t['limits']}", ""]
    lines += [f"- {t[k].format(**values)}" for k in ("l1", "l2", "l3")]
    lines += ["", f"## {t['summary']}", ""]
    head = ("h_conf", "h_trials", "h_pass", "h_sweep", "h_cost", "h_cost_ok", "h_tokens", "h_time")
    lines.append("| " + " | ".join(t[h] for h in head) + " |")
    lines.append("|---|" + "---:|" * (len(head) - 1))
    for c in configs:
        low, high = c["ci95"]
        sweep = c["sweep_score"]
        unit = c["cost"]["currency"]
        per_run = c["cost"]["per_run"]
        cost = _money(per_run["mean"], unit, lang)
        if per_run["sd"] is not None:
            cost += f" ± {_money(per_run['sd'], unit, lang)}"
        sweep_text = _percent(sweep["mean"], lang)
        if sweep["sd"] is not None:
            sweep_text += f" ± {_percent(sweep['sd'], lang)}"
        tokens = sum(v or 0 for v in c["tokens_per_run"].values())
        row = [
            _cell(c["label"]),
            f"{c['passed']}/{c['total']}",
            f"{_percent(c['pass_rate'], lang)} ({_percent(low, lang)}–{_percent(high, lang)})",
            sweep_text,
            cost,
            _money(c["cost"]["per_passed_run"], unit, lang),
            _number(tokens, 0, lang),
            f"{_number(c['duration_s']['per_run']['mean'], 0, lang)} s",
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", f"## {t['by_scenario']}", ""]
    lines.append("| " + " | ".join([t["h_scenario"]] + [_cell(c["label"]) for c in configs]) + " |")
    lines.append("|---|" + "---:|" * len(configs))
    for name in scenarios:
        cells = []
        for c in configs:
            s = c["scenarios"][name]
            cells.append(f"{s['passed']}/{s['total']}")
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    lines += ["", f"### {t['failures']}", ""]
    for c in configs:
        counts: dict[str, int] = {}
        for s in c["scenarios"].values():
            for grader, n in s["failures"].items():
                counts[grader] = counts.get(grader, 0) + n
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        shown = ", ".join(f"`{g}` ({n})" for g, n in top) or t["none"]
        lines.append(f"- {_cell(c['label'])} : {shown}" if lang == "fr" else
                     f"- {_cell(c['label'])}: {shown}")  # fmt: skip
    lines += ["", f"## {t['provenance']}", ""]
    for c in configs:
        commit = c["synaptane"]["commit"]
        state = f" ({t['dirty']})" if c["synaptane"]["dirty"] else ""
        price = c["price"]
        sep = " : " if lang == "fr" else ": "
        lines += [
            f"### {_cell(c['label'])}",
            "",
            f"- {t['p_model']}{sep}`{c['provider']}` / `{c['model']}` / `{c['effort']}`"
            + (" (base URL: custom)" if c["base_url"] == "custom" else ""),
            f"- {t['p_prompt']}{sep}`{c['prompt']['path']}`, sha256 `{c['prompt']['sha256'][:12]}`",
            f"- {t['p_tools']}{sep}sha256 `{c['tools_sha256'][:12]}`",
            f"- {t['p_commit']}{sep}{c['synaptane']['version']}, commit `{commit[:12]}`{state}",
            f"- {t['p_dates']}{sep}{c['dates']['first']} → {c['dates']['last']}",
            f"- {t['p_price']}{sep}{_number(price['input'], 2, lang)} / "
            f"{_number(price['output'], 2, lang)} "
            + (t["price_public"] if price["public"] else t["price_entered"]),
            f"- {t['p_runs']}{sep}" + ", ".join(f"`runs/{r}`" for r in c["runs"]),
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def write_report(paths: Sequence[Path], out: Path, **options: Any) -> list[Path]:
    """Écrit `results.json`, `results.md` et `results.fr.md` dans `out` ; renvoie leurs
    chemins. Rien n'est écrit si un contrôle échoue."""
    report = aggregate(load_runs(paths), **options)
    files = {
        out / "results.json": dump(report),
        out / "results.md": markdown(report, "en"),
        out / "results.fr.md": markdown(report, "fr"),
    }
    for path, text in files.items():
        if found := leaks(text):
            raise ReportError(f"{path.name} : secret ou chemin à publier : {', '.join(found)}")
    out.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    return list(files)
