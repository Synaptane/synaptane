"""Résultats publiables du banc du rangement : fichier de lancement, puis rapport agrégé.

Deux formats JSON, versionnés (`format`, `format_version`) :
- le **lancement** (`synaptane-tidy-bench-run`), écrit par `tidy --model …` : la version de
  Synaptane (commit, arbre modifié ou non), les dates et la durée, les empreintes du banc et de
  chaque scénario, le modèle, le fournisseur, l'effort demandé et l'effort effectif, l'invite
  (empreinte, chemin, et son texte si elle est hors du dépôt), les outils (empreinte), le tarif
  appliqué, le budget, chaque essai (scénario, numéro, réussite, correcteurs en échec, jetons,
  coût, durée, interruption, modèles qui ont servi, métriques du journal) et les essais écartés
  (erreur passagère du fournisseur, rejoués) ;
- le **rapport** (`synaptane-tidy-bench-results`), écrit par `report` à partir d'un ou
  plusieurs lancements : une configuration par ligne, taux de réussite avec son intervalle de
  Wilson à 95 %, score par balayage (moyenne, écart-type), coût, jetons et durée, le détail par
  scénario, et une comparaison appariée par scénario (test des signes exact) entre
  configurations ; plus deux tableaux Markdown (anglais, français) avec la méthode en tête.

Rien n'est masqué en silence : un lancement ou un rapport qui contiendrait un secret (la clé,
l'adresse du fournisseur, ses identifiants) ou quelque chose qui y ressemble, ou un chemin de la
machine, est refusé. Les erreurs du fournisseur ne sont jamais recopiées en texte libre.

Le rapport est reproductible : il ne dépend que des lancements (aucune date du jour), et le tri
est stable. Il refuse ce qui ne se compare pas honnêtement : un lancement incomplet ou coupé
(budget, fournisseur), des essais en double ou manquants, trop peu d'essais, des scénarios
différents, des commits différents (sauf `--allow-mixed-commits`), un arbre modifié (sauf
`--allow-dirty`), un essai servi par un autre modèle que celui demandé.
"""

import hashlib
import itertools
import json
import math
import os
import re
import statistics
import string
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import synaptane
from synaptane.ai import anthropic_provider, pricing
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
# Tout ce qui change le résultat d'un essai : correcteurs, scénarios, wiki, préparation de
# l'espace, conduite du lancement.
BENCH_FILES = (
    "tests/eval/__main__.py",
    "tests/eval/env.py",
    "tests/eval/scenarios.py",
    "tests/eval/tidy_bench.py",
    "tests/eval/wikidata.py",
    "tests/integration/gitutil.py",
)
DEFAULT_PROMPT = "src/synaptane/services/tidy_tools.py (INSTRUCTIONS)"
MIN_TRIALS = 3
MAX_REPLAYED = 0.05  # au-delà, des essais rejoués peuvent biaiser le résultat (les plus longs)
Z95 = 1.959964  # quantile à 97,5 % de la loi normale : intervalle à 95 %
# Champs de l'invite du rangement (`tidy.tidy_space`) : une autre invite n'en a pas d'autres.
PROMPT_FIELDS = frozenset(
    {"name", "address", "description", "language", "vocation", "custom", "max_deletions"}
)
# Interruptions qui ne disent rien du modèle : l'essai n'a pas été joué jusqu'au bout.
CUT = ("budget", "provider", "error")
NO_EFFORT = "n/a"


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
    """Empreinte du banc : ses fichiers et le wiki lui-même."""
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


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def source_state() -> dict[str, Any]:
    """Version et commit du code qui tourne, lu dans le dépôt (pas `SYNAPTANE_GIT_SHA`, qui
    décrit une image). `dirty` : un fichier suivi modifié ou un fichier non suivi (hors
    `eval/results/`, où s'écrivent les lancements) ; None : inconnu."""
    head = _git("rev-parse", "--verify", "--quiet", "HEAD")
    commit = head.stdout.strip() if head and head.returncode == 0 else ""
    status = _git("status", "--porcelain", "--untracked-files=normal", "--", ".", ":!eval/results")
    dirty = bool(status.stdout.strip()) if status and status.returncode == 0 else None
    return {
        "version": synaptane.__version__,
        "commit": commit or "unknown",
        "dirty": dirty if commit else None,
    }


# --- Invite et modèle -----------------------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    template: str
    source: str  # "default" : INSTRUCTIONS ; "file" : un fichier donné au banc
    path: str  # relatif au dépôt ; hors du dépôt, le nom du fichier seul
    inside: bool = True  # dans le dépôt : son commit la reproduit

    @property
    def sha256(self) -> str:
        return sha256(self.template)

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"source": self.source, "path": self.path, "sha256": self.sha256}
        if not self.inside:
            data["text"] = self.template  # hors du dépôt : le texte, pour la reproduire
        return data


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
    if file.is_relative_to(ROOT):
        return Prompt(template, "file", file.relative_to(ROOT).as_posix())
    return Prompt(template, "file", file.name, inside=False)


@contextmanager
def bench_settings(prompt: Prompt) -> Iterator[None]:
    """Le temps du bloc, pour le banc seulement (jamais l'instance) : cette invite à la place
    de INSTRUCTIONS, et sans repli de Claude vers un autre modèle (`fallbacks`), pour que
    chaque essai mesure le modèle demandé."""
    # `tidy_space` et `request_options` lisent ces noms dans leur module : on les y remplace.
    saved = getattr(tidy, "INSTRUCTIONS"), anthropic_provider.FALLBACK_MODELS  # noqa: B009
    setattr(tidy, "INSTRUCTIONS", prompt.template)  # noqa: B010
    anthropic_provider.FALLBACK_MODELS = ()  # type: ignore[assignment]
    try:
        yield
    finally:
        setattr(tidy, "INSTRUCTIONS", saved[0])  # noqa: B010
        anthropic_provider.FALLBACK_MODELS = saved[1]


def effective_effort(provider: str, model: str, effort: str) -> str:
    """L'effort réellement envoyé : seulement à Claude, et pas aux modèles qui le refusent."""
    if provider != "anthropic" or model.strip().lower().startswith(
        anthropic_provider.NO_EFFORT_MODELS
    ):
        return NO_EFFORT
    return effort


_SNAPSHOT = re.compile(r"(?:-\d{4}-\d{2}-\d{2}|[-@]\d{8}|:latest)$")


def same_model(asked: str, served: str) -> bool:
    """Le modèle qui a répondu est celui demandé, à la casse, au préfixe de routeur et à la
    version datée près (`claude-sonnet-5-5-20261001`, `llama3:latest`)."""

    def bare(name: str) -> str:
        return _SNAPSHOT.sub("", name.strip().lower().rsplit("/", 1)[-1])

    return bare(asked) == bare(served)


# --- Secrets et chemins ---------------------------------------------------------------------

_I = re.IGNORECASE
# Ce qui ressemble à un secret ou à un chemin de la machine qui a lancé le banc.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("clé sk-", re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")),
    ("jeton GitHub", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})")),
    ("jeton GitLab", re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}")),
    ("clé Google", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("jeton Hugging Face", re.compile(r"\bhf_[A-Za-z0-9]{16,}")),
    ("jeton Slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("clé AWS", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("en-tête Bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}", _I)),
    (
        "en-tête ou champ de clé",
        # En-tête, champ JSON ou paramètre suivi d'une valeur qui ressemble à une clé (pas
        # « authorization: none »).
        re.compile(
            r"\b(?:x-api-key|api[_-]?key|api[_-]?token|access[_-]?token|authorization)\b"
            r"[\"']?\s*[:=]\s*[\"']?(?!(?:none|null|n/a)\b)[A-Za-z0-9._~+/=\-]{8,}",
            _I,
        ),
    ),
    (
        "valeur d'un champ secret",
        re.compile(
            r"\b(?:key|token|secret|password|passwd)\b[\"']?\s*[:=]\s*[\"']?"
            r"[A-Za-z0-9._~+/=\-]{16,}",
            _I,
        ),
    ),
    (
        "identifiants dans une adresse",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^/\s:@\"']+(?::[^/\s@\"']*)?@", _I),
    ),
    (
        "chemin de la machine",
        re.compile(
            r"(?<![\w.])(?:/(?:home|Users|root|tmp|private|var/folders|mnt/[a-z]|Volumes"
            r"|opt|srv|workspace)/[^\s\"'`]*|~/[^\s\"'`]+)"
            # Windows : C:\dossier\…, D:\… (doublés dans du JSON), C:/Users/… ; deux niveaux au
            # moins, pour ne pas prendre « B:\n » d'un texte échappé pour un chemin.
            r"|\b[A-Za-z]:(?:\\{1,2}[^\s\"'`\\]+\\{1,2}|/(?:Users|Documents and Settings)/)"
        ),
    ),
)


def run_secrets(api_key: str, base_url: str) -> list[str]:
    """Les valeurs secrètes d'un lancement : la clé, l'adresse du fournisseur, son mot de passe,
    et son hôte s'il ne peut pas être un simple mot (un nom avec un point, une adresse IP :
    `llm.corp.internal`, `10.0.0.5`). « ollama » ou « localhost » ne sont pas des secrets, ni le
    nom d'utilisateur seul. Elles ne sont cherchées que dans le texte libre (`free_text`)."""
    secrets = [api_key, base_url, base_url.rstrip("/")]
    if base_url:
        parts = urlsplit(base_url)
        host = parts.hostname or ""
        secrets += [unquote(parts.password or ""), host if ("." in host or ":" in host) else ""]
    return [s for s in dict.fromkeys(secrets) if s and len(s) >= 4]


def _free(path: tuple[str, ...]) -> bool:
    """Champs de texte libre d'un lancement : ce qu'un modèle, un fournisseur ou une personne
    a pu écrire (le reste, noms de champs et valeurs du format, ne vient que du banc)."""
    head = path[:3]
    return (
        path[:2] == ("config", "label")
        or path[:3] == ("config", "prompt", "text")
        or (path[:1] == ("scenarios",) and path[2:3] == ("summary",))
        or (path[:1] in (("trials",), ("discarded",))
            and head[2:3] in (("failures",), ("metrics",), ("provider_error",)))
    )  # fmt: skip


def _walk(value: Any, path: tuple[str, ...], change: Any) -> Any:
    if isinstance(value, dict):
        return {k: _walk(v, (*path, k), change) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, (*path, "*"), change) for v in value]
    if isinstance(value, str):
        return change(value, _free(path))
    return value


def free_text(record: dict[str, Any]) -> str:
    """Le texte libre d'un lancement, mis bout à bout (où chercher ses secrets propres)."""
    found: list[str] = []
    _walk(record, (), lambda text, free: found.append(text) if free else None)
    return "\n".join(found)


def leaks(text: str, secrets: Iterable[str] = (), free: str | None = None) -> list[str]:
    """Ce qu'un texte à publier contient de secret ou de suspect : la nature, jamais la
    valeur (le message de refus ne doit pas la recopier). Les secrets du lancement sont
    cherchés dans `free` (son texte libre) s'il est donné, sinon dans tout le texte."""
    where = text if free is None else free
    found = ["valeur secrète du lancement"] if any(s and s in where for s in secrets) else []
    found += [name for name, pattern in PATTERNS if pattern.search(text)]
    return found


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Le texte sans les secrets connus ni rien qui leur ressemble : pour une copie privée,
    jamais pour publier (`leaks` refuse avant)."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[secret]")
    for _, pattern in PATTERNS:
        text = pattern.sub("[masqué]", text)
    return text


# --- Lancement ------------------------------------------------------------------------------


@dataclass
class Trial:
    trial: int
    result: Result
    duration_s: float


@dataclass
class Discarded:
    """Essai écarté puis rejoué : le fournisseur a eu une erreur passagère (429, 529…)."""

    trial: int
    result: Result
    duration_s: float


def _tokens(r: Result) -> dict[str, int]:
    return {
        "input": r.usage.input_tokens,  # écritures en cache comprises
        "output": r.usage.output_tokens,
        "cached": r.usage.cached_tokens,
        "cache_write": r.usage.cache_write_tokens,
    }


def trial_json(trial: Trial, model: str) -> dict[str, Any]:
    r = trial.result
    return {
        "scenario": r.scenario,
        "trial": trial.trial,
        "passed": r.passed,
        "failures": dict(sorted(r.failures.items())),
        "interrupted": r.interrupted,
        "provider_error": r.error,
        "served_models": r.served_models,
        "model_mismatch": any(not same_model(model, m) for m in r.served_models),
        "duration_s": round(trial.duration_s, 3),
        "tokens": _tokens(r),
        "turns": r.usage.turns,
        "cost": float(r.cost) if r.cost is not None else None,
        "items_done": r.items_done,
        "metrics": r.metrics,  # métriques du journal (wiki fictif du banc)
    }


def complete(scenarios: Sequence[str], trials: Sequence[dict[str, Any]], requested: int) -> bool:
    """Chaque (scénario, essai n) pour n de 1 à `requested`, exactement une fois, et aucun
    essai coupé par le budget, le fournisseur ou une erreur."""
    expected = sorted((s, n) for s in scenarios for n in range(1, requested + 1))
    played = sorted((t["scenario"], t["trial"]) for t in trials)
    return played == expected and not any(t["interrupted"] in CUT for t in trials)


def run_record(
    *,
    scenarios: Sequence[Scenario],
    trials: Sequence[Trial],
    discarded: Sequence[Discarded] = (),
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
    stopped_by: str | None,
) -> dict[str, Any]:
    """Le fichier d'un lancement (à contrôler par `write_run` avant de l'écrire)."""
    rows = [trial_json(t, model) for t in trials]
    names = [s.name for s in scenarios]
    return {
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
            "effort": effective_effort(provider, model, effort),
            "effort_requested": effort,
            "base_url": "custom" if custom_base_url else "default",
            "fallbacks": False,
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
        "complete": stopped_by is None and complete(names, rows, trials_requested),
        "stopped_by": stopped_by,
        "trials": rows,
        "discarded": [
            {
                "scenario": d.result.scenario,
                "trial": d.trial,
                "provider_error": d.result.error,
                "duration_s": round(d.duration_s, 3),
                "tokens": _tokens(d.result),
                "cost": float(d.result.cost) if d.result.cost is not None else None,
            }
            for d in discarded
        ],
    }


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


def write_run(path: Path, record: dict[str, Any], secrets: Sequence[str] = ()) -> None:
    """Écrit le lancement s'il ne contient rien de secret ni de suspect. Sinon, rien n'est
    écrit dans le dépôt : une copie masquée, privée (créée en 0600, jamais à travers un lien),
    va dans le dossier temporaire pour ne pas perdre un lancement payé, et `ReportError` dit
    ce qui a été trouvé et où est la copie. Les secrets du lancement ne sont cherchés et
    masqués que dans son texte libre : le reste de la copie reste lisible."""
    text = dump(record)
    if found := leaks(text, secrets, free_text(record)):
        masked = _walk(record, (), lambda value, free: scrub(value, secrets if free else ()))
        fd, name = tempfile.mkstemp(prefix=f"synaptane-bench-refused-{path.stem}-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as aside:
            aside.write(dump(masked))
        raise ReportError(
            f"lancement non écrit dans {path.parent.name}/ : {', '.join(found)} ; "
            f"copie masquée, à ne pas publier : {name}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- Statistiques ---------------------------------------------------------------------------


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


def sign_test(better: int, worse: int) -> float:
    """Test des signes exact, bilatéral : probabilité, si les deux configurations se valaient,
    d'un écart au moins aussi net entre scénarios gagnés et perdus (égalités écartées)."""
    n = better + worse
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(better, worse) + 1)) / float(1 << n)
    return round(min(1.0, 2 * tail), 4)


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


# --- Agrégation -----------------------------------------------------------------------------


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


def _run_problems(
    name: str, run: dict[str, Any], min_trials: int, allow_dirty: bool, allow_replays: bool
) -> list[str]:
    problems = []
    share = len(run["discarded"]) / max(1, len(run["trials"]))
    if share > MAX_REPLAYED and not allow_replays:
        problems.append(
            f"{name} : {len(run['discarded'])} essais rejoués après une erreur du fournisseur "
            f"({share:.0%}, plus de {MAX_REPLAYED:.0%}) : les essais longs, plus souvent "
            "touchés, seraient sous-représentés ; relancez plus tard (--allow-replays pour "
            "passer outre, la part est alors affichée en tête)"
        )
    trials = run["trials"]
    scenarios = [s["name"] for s in run["scenarios"]]
    requested = run["trials_requested"]
    played = [(t["scenario"], t["trial"]) for t in trials]
    doubles = sorted({p for p in played if played.count(p) > 1})
    if doubles:
        problems.append(f"{name} : essais en double {doubles}")
    expected = {(s, n) for s in scenarios for n in range(1, requested + 1)}
    if missing := sorted(expected - set(played)):
        problems.append(
            f"{name} : essais manquants {missing[:5]}{' …' if len(missing) > 5 else ''}"
        )
    if strangers := sorted(set(played) - expected):
        problems.append(f"{name} : essais hors des scénarios ou des numéros demandés {strangers}")
    cut = sorted({t["interrupted"] for t in trials if t["interrupted"] in CUT})
    if cut or run["stopped_by"] or not run["complete"]:
        why = ", ".join(cut or [run["stopped_by"] or "incomplet"])
        problems.append(
            f"{name} : lancement incomplet ({why}) : un essai coupé ne dit rien du modèle, "
            "à relancer (plus de budget, ou quand le fournisseur répond)"
        )
    asked = run["config"]["model"]
    served = {m for t in trials for m in t["served_models"]}  # recompté, pas cru sur parole
    if others := sorted(m for m in served if not same_model(asked, m)):
        problems.append(f"{name} : essais servis par un autre modèle que celui demandé {others}")
    if requested < min_trials:
        problems.append(
            f"{name} : {requested} essai(s) par scénario, au moins {min_trials} "
            "pour comparer (un seul tirage ne dit rien de la variance)"
        )
    if run["synaptane"]["dirty"] is not False and not allow_dirty:
        problems.append(
            f"{name} : lancé sur un arbre modifié (ou commit inconnu) : non reproductible "
            "depuis le commit (--allow-dirty pour passer outre)"
        )
    return problems


def check(
    runs: Sequence[tuple[str, dict[str, Any]]],
    *,
    min_trials: int,
    allow_dirty: bool,
    allow_mixed_commits: bool = False,
    allow_replays: bool = False,
) -> None:
    """Refuse ce qui ne se compare pas honnêtement, avec la raison."""
    if not runs:
        raise ReportError("aucun lancement : lancez d'abord `tidy --model …` (docs/dev/eval.md)")
    problems = [
        p
        for name, run in runs
        for p in _run_problems(name, run, min_trials, allow_dirty, allow_replays)
    ]
    benches = {(run["bench"]["sha256"], _canonical(run["scenarios"])) for _, run in runs}
    if len(benches) > 1:
        problems.append(
            "scénarios ou correcteurs différents d'un lancement à l'autre : comparez des "
            "lancements du même banc"
        )
    commits = sorted({run["synaptane"]["commit"] for _, run in runs})
    if len(commits) > 1 and not allow_mixed_commits:
        problems.append(
            f"commits différents ({', '.join(c[:12] for c in commits)}) : le code du rangement "
            "a pu changer entre eux ; relancez sur un même commit (--allow-mixed-commits pour "
            "passer outre, le commit est alors affiché pour chaque ligne)"
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
    discarded = [d for _, run in group for d in run["discarded"]]
    known = len(costs) == len(trials) and all(d["cost"] is not None for d in discarded)
    discarded_cost = round(sum(float(d["cost"] or 0) for d in discarded), 4)
    # Les essais écartés ont été payés : ils comptent dans le coût total et par passage réussi.
    total_cost = round(sum(costs) + discarded_cost, 4) if known else None
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
            "replayed": sum(1 for d in discarded if d["scenario"] == name),
        }
    label = config["label"] or f"{config['model']} · {config['effort']}"
    if config["prompt"]["source"] != "default" and not config["label"]:
        label += f" · prompt {config['prompt']['sha256'][:8]}"
    interruptions: dict[str, int] = {}
    for t in trials:
        if t["interrupted"]:
            interruptions[t["interrupted"]] = interruptions.get(t["interrupted"], 0) + 1
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
        "interruptions": dict(sorted(interruptions.items())),  # du modèle : plafond, refus
        "discarded": len(discarded),
        "replayed_share": round(len(discarded) / len(trials), 4) if trials else 0.0,
        "cost": {
            "total": total_cost,
            "per_run": _spread(costs) if len(costs) == len(trials) else _spread([]),
            "per_passed_run": round(total_cost / passed, 4)
            if total_cost is not None and passed
            else None,
            "discarded": discarded_cost,
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


def _distinct(configurations: list[dict[str, Any]]) -> None:
    """Deux lignes n'ont jamais le même nom : on ajoute ce qui les distingue (effort, invite,
    fournisseur, tarif, commit), et l'identifiant en dernier recours."""
    facets = (
        lambda c: c["effort"],
        lambda c: f"prompt {c['prompt']['sha256'][:8]}",
        lambda c: c["provider"],
        lambda c: f"{c['price']['input']:g}/{c['price']['output']:g}",
        lambda c: f"commit {c['synaptane']['commit'][:7]}",
        lambda c: c["id"],
    )
    by_label: dict[str, list[dict[str, Any]]] = {}
    for c in configurations:
        by_label.setdefault(c["label"], []).append(c)
    for same in by_label.values():
        if len(same) < 2:
            continue
        extra: dict[str, list[str]] = {c["id"]: [] for c in same}
        for facet in facets:
            if len({facet(c) for c in same}) > 1:
                for c in same:
                    extra[c["id"]].append(facet(c))
            if len({" · ".join(extra[c["id"]]) for c in same}) == len(same):
                break
        for c in same:
            c["label"] += " · " + " · ".join(extra[c["id"]])


def _order(config: dict[str, Any]) -> tuple[float, float, float, str, str]:
    """Borne basse de Wilson d'abord (3/3 ne passe pas devant 29/30), puis le taux, puis le
    moins cher par passage ; ensuite par nom : stable."""
    mean = config["cost"]["per_run"]["mean"]
    return (-config["ci95"][0], -(config["pass_rate"] or 0.0),
            mean if mean is not None else math.inf, config["label"], config["id"])  # fmt: skip


def holm(p_values: Sequence[float]) -> list[float]:
    """Correction de Holm-Bonferroni : p ajustés pour des comparaisons multiples (avec
    plusieurs paires, un p < 0,05 isolé arrive par hasard)."""
    order = sorted(range(len(p_values)), key=lambda i: p_values[i])
    adjusted = [1.0] * len(p_values)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(p_values) - rank) * p_values[i]))
        adjusted[i] = round(running, 4)
    return adjusted


def _comparisons(configurations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chaque paire de configurations, scénario par scénario : sur combien de scénarios l'une
    fait mieux que l'autre (taux de réussite), le test des signes exact sur ces écarts, et le
    p ajusté de Holm sur l'ensemble des paires."""
    pairs = []
    for a, b in itertools.combinations(configurations, 2):
        better = worse = ties = 0
        for name, s in a["scenarios"].items():
            ra, rb = s["pass_rate"], b["scenarios"][name]["pass_rate"]
            better, worse, ties = (
                (better + 1, worse, ties)
                if ra > rb
                else (better, worse + 1, ties)
                if ra < rb
                else (better, worse, ties + 1)
            )
        pairs.append(
            {
                "a": a["id"],
                "b": b["id"],
                "a_label": a["label"],
                "b_label": b["label"],
                "a_better": better,
                "b_better": worse,
                "ties": ties,
                "sign_test_p": sign_test(better, worse),
            }
        )
    for pair, adjusted in zip(pairs, holm([p["sign_test_p"] for p in pairs]), strict=True):
        pair["holm_p"] = adjusted
    return pairs


def aggregate(
    runs: Sequence[tuple[str, dict[str, Any]]],
    *,
    min_trials: int = MIN_TRIALS,
    allow_dirty: bool = False,
    allow_mixed_commits: bool = False,
    allow_replays: bool = False,
) -> dict[str, Any]:
    """Le rapport publiable : configurations triées, comparaisons, méthode, provenance."""
    check(
        runs,
        min_trials=min_trials,
        allow_dirty=allow_dirty,
        allow_mixed_commits=allow_mixed_commits,
        allow_replays=allow_replays,
    )
    ordered = sorted(runs, key=lambda r: r[0])
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name, run in ordered:
        groups.setdefault(_key(run), []).append((name, run))
    configurations = [_configuration(key, group) for key, group in groups.items()]
    _distinct(configurations)
    configurations.sort(key=_order)
    first = ordered[0][1]
    return {
        "format": RESULTS_FORMAT,
        "format_version": FORMAT_VERSION,
        "method": {
            "measured": "outcome of a tidy-up run on a fictional wiki, checked by graders "
            "written in code (docs/dev/eval.md)",
            "pass": "a run passes when no grader fails",
            "interval": "Wilson score interval, 95 %, over all runs of a configuration, "
            "conditional on the scenarios of the bench",
            "sweep_score": "share of scenarios passed in one sweep (trial n of every "
            "scenario); mean and sample standard deviation over sweeps",
            "comparison": "paired by scenario: scenarios where one configuration has the "
            "higher pass rate, exact two-sided sign test, Holm-adjusted over all pairs",
            "replays": "trials cut by a passing provider error are discarded and played "
            f"again; refused above {MAX_REPLAYED:.0%} unless allowed",
            "cost_with_discarded": "total cost and cost per passed run include the discarded "
            "trials, which were paid",
            "order": "lower bound of the interval, then pass rate, then cost per run",
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
        "comparisons": _comparisons(configurations),
    }


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
        "every configuration ran the same scenarios (bench `{bench}`). No trial was cut by the "
        "budget or by the provider: a trial hit by a passing provider error (429, 529…) was "
        "discarded and played again ({replayed} out of {played}, {share}).",
        "m3": "Pass rate: runs passed out of runs played, with its 95 % Wilson interval. Rows "
        "are sorted by the lower bound of that interval. Sweep score: share of scenarios "
        "passed in one sweep, mean ± standard deviation over sweeps.",
        "m4": "Cost: estimated from the tokens reported by the provider, at the price shown "
        "(public list price on the run date unless stated), not an invoice; the total and the "
        "cost per passed run include the discarded trials, which were paid. Claude's fallback "
        "to another model is turned off, and every trial whose provider names the model that "
        "served it was served by the model asked for.",
        "limits": "Limits",
        "l1": "A small sample: the intervals are conditional on these {scenarios} short "
        "scenarios on one fictional wiki; they say how a configuration does again on them, not "
        "on a real inbox.",
        "l2": "Separate intervals suggest a real difference; overlapping ones do not rule one "
        "out. The paired comparison by scenario below is the better test between two rows.",
        "l3": "Graders check invariants of the result, not the quality of the writing.",
        "l4": "A model name can point to a different model later at its provider; each row "
        "gives its dates and the Synaptane commit it ran on.",
        "l5": "Replaying the trials cut by the provider can favour the short ones, which an "
        "error hits less often: above {max_replayed} of replays, the report is refused unless "
        "asked for, and the replays are shown per scenario.",
        "replay_warning": "**Warning**: {share} of the trials of {label} were replayed after a "
        "provider error, more than {max_replayed}: read its row with care.",
        "replay_note": "(+n): trials replayed after a passing provider error.",
        "summary": "Summary",
        "h_conf": "Configuration",
        "h_trials": "Trials",
        "h_pass": "Pass rate (95 % CI)",
        "h_sweep": "Sweep score",
        "h_cost": "Cost per run",
        "h_cost_ok": "Cost per passed run",
        "h_tokens": "Tokens per run",
        "h_time": "Time per run",
        "h_commit": "Commit",
        "h_discarded": "Replayed",
        "by_scenario": "By scenario",
        "h_scenario": "Scenario",
        "failures": "Graders that failed most",
        "comparison": "Paired comparison by scenario",
        "c_intro": "For each pair of rows: on how many scenarios each has the higher pass "
        "rate (ties apart), the exact two-sided sign test, and its p adjusted by Holm's method "
        "for the number of pairs compared. A small adjusted p (below 0.05) says the difference "
        "is unlikely to be chance; with {scenarios} scenarios, it takes a clear gap.",
        "c_better": "better on",
        "c_ties": "Ties",
        "c_p": "Sign test p",
        "c_holm": "Holm p",
        "c_none": "A single configuration: nothing to compare.",
        "provenance": "Provenance",
        "p_model": "Provider / model / effort",
        "p_prompt": "Prompt",
        "p_tools": "Tools",
        "p_commit": "Synaptane",
        "p_dates": "Dates",
        "p_runs": "Run files",
        "p_price": "Price",
        "p_stops": "Stopped by the model itself",
        "dirty": "modified tree",
        "price_public": "USD per million tokens, input / output (public list price)",
        "price_entered": "per million tokens, input / output (price entered for the run)",
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
        "`{bench}`). Aucun essai n'a été coupé par le budget ni par le fournisseur : un essai "
        "touché par une erreur passagère du fournisseur (429, 529…) a été écarté puis rejoué "
        "({replayed} sur {played}, {share}).",
        "m3": "Taux de réussite : passages réussis sur passages joués, avec son intervalle de "
        "Wilson à 95 %. Les lignes sont triées par la borne basse de cet intervalle. Score par "
        "balayage : part des scénarios réussis en un balayage, moyenne ± écart-type sur les "
        "balayages.",
        "m4": "Coût : estimé à partir des jetons comptés par le fournisseur, au tarif indiqué "
        "(tarif public à la date du lancement, sauf mention), pas une facture ; le total et le "
        "coût par passage réussi comprennent les essais écartés, qui ont été payés. Le repli de "
        "Claude vers un autre modèle est désactivé, et chaque essai dont le fournisseur nomme "
        "le modèle qui l'a servi a été servi par le modèle demandé.",
        "limits": "Limites",
        "l1": "Un petit échantillon : les intervalles valent pour ces {scenarios} scénarios "
        "courts sur un seul wiki fictif ; ils disent ce qu'une configuration ferait de nouveau "
        "sur eux, pas sur une vraie inbox.",
        "l2": "Des intervalles disjoints suggèrent un vrai écart ; des intervalles qui se "
        "chevauchent ne l'excluent pas. La comparaison appariée par scénario, plus bas, est le "
        "meilleur test entre deux lignes.",
        "l3": "Les correcteurs vérifient des invariants du résultat, pas la qualité de l'écriture.",
        "l4": "Un nom de modèle peut désigner plus tard un autre modèle chez son fournisseur ; "
        "chaque ligne donne ses dates et le commit de Synaptane sur lequel elle a tourné.",
        "l5": "Rejouer les essais coupés par le fournisseur peut favoriser les plus courts, "
        "qu'une erreur touche moins souvent : au-delà de {max_replayed} d'essais rejoués, le "
        "rapport est refusé sauf demande expresse, et les rejoués sont donnés par scénario.",
        "replay_warning": "**Attention** : {share} des essais de {label} ont été rejoués après "
        "une erreur du fournisseur, plus de {max_replayed} : lisez sa ligne avec prudence.",
        "replay_note": "(+n) : essais rejoués après une erreur passagère du fournisseur.",
        "summary": "Synthèse",
        "h_conf": "Configuration",
        "h_trials": "Essais",
        "h_pass": "Réussite (IC 95 %)",
        "h_sweep": "Score par balayage",
        "h_cost": "Coût par passage",
        "h_cost_ok": "Coût par passage réussi",
        "h_tokens": "Jetons par passage",
        "h_time": "Durée par passage",
        "h_commit": "Commit",
        "h_discarded": "Rejoués",
        "by_scenario": "Par scénario",
        "h_scenario": "Scénario",
        "failures": "Correcteurs le plus souvent en échec",
        "comparison": "Comparaison appariée par scénario",
        "c_intro": "Pour chaque paire de lignes : sur combien de scénarios chacune a le "
        "meilleur taux de réussite (égalités à part), le test des signes exact, bilatéral, et "
        "son p ajusté par la méthode de Holm pour le nombre de paires comparées. Un p ajusté "
        "petit (sous 0,05) dit que l'écart a peu de chances d'être dû au hasard ; avec "
        "{scenarios} scénarios, il faut un écart net.",
        "c_better": "meilleure sur",
        "c_ties": "Égalités",
        "c_p": "p (test des signes)",
        "c_holm": "p de Holm",
        "c_none": "Une seule configuration : rien à comparer.",
        "provenance": "Provenance",
        "p_model": "Fournisseur / modèle / effort",
        "p_prompt": "Invite",
        "p_tools": "Outils",
        "p_commit": "Synaptane",
        "p_dates": "Dates",
        "p_runs": "Lancements",
        "p_price": "Tarif",
        "p_stops": "Arrêts du modèle lui-même",
        "dirty": "arbre modifié",
        "price_public": "$ par million de jetons, entrée / sortie (tarif public)",
        "price_entered": "par million de jetons, entrée / sortie (tarif saisi pour le lancement)",
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


def _commit(config: dict[str, Any]) -> str:
    return str(config["synaptane"].get("commit") or "unknown")[:7]


def markdown(report: dict[str, Any], lang: str) -> str:
    t = TEXTS[lang]
    configs = report["configurations"]
    scenarios = [s["name"] for s in report["bench"]["scenarios"]]
    least = min(c["trials_per_scenario"] for c in configs)
    replayed = sum(c["discarded"] for c in configs)
    played = sum(c["total"] for c in configs)
    values = {
        "pages": report["bench"]["wiki_pages"],
        "scenarios": len(scenarios),
        "trials": least,
        "bench": report["bench"]["sha256"][:12],
        "replayed": replayed,
        "played": played,
        "share": _percent(replayed / played if played else 0.0, lang),
        "max_replayed": _percent(MAX_REPLAYED, lang),
    }
    sep = " : " if lang == "fr" else ": "
    lines = [t["switch"], "", f"# {t['title']}", "", t["intro"], ""]
    for c in configs:
        if c["replayed_share"] > MAX_REPLAYED:
            share = _percent(c["replayed_share"], lang)
            lines += [t["replay_warning"].format(**values | {"share": share, "label": c["label"]})]
            lines += [""]
    lines += [f"## {t['method']}", ""]
    lines += [f"- {t[k].format(**values)}" for k in ("m1", "m2", "m3", "m4")]
    lines += ["", f"### {t['limits']}", ""]
    lines += [f"- {t[k].format(**values)}" for k in ("l1", "l2", "l3", "l4", "l5")]
    lines += ["", f"## {t['summary']}", ""]
    head = (
        "h_conf",
        "h_trials",
        "h_pass",
        "h_sweep",
        "h_cost",
        "h_cost_ok",
        "h_tokens",
        "h_time",
        "h_discarded",
        "h_commit",
    )
    lines.append("| " + " | ".join(t[h] for h in head) + " |")
    lines.append("|---|" + "---:|" * (len(head) - 2) + "---|")
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
        tokens = sum(c["tokens_per_run"][k] or 0 for k in ("input", "output", "cached"))
        row = [
            _cell(c["label"]),
            f"{c['passed']}/{c['total']}",
            f"{_percent(c['pass_rate'], lang)} ({_percent(low, lang)}–{_percent(high, lang)})",
            sweep_text,
            cost,
            _money(c["cost"]["per_passed_run"], unit, lang),
            _number(tokens, 0, lang),
            f"{_number(c['duration_s']['per_run']['mean'], 0, lang)} s",
            str(c["discarded"]),
            f"`{_commit(c)}`" + (f" ({t['dirty']})" if c["synaptane"]["dirty"] else ""),
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", f"## {t['by_scenario']}", ""]
    lines.append("| " + " | ".join([t["h_scenario"]] + [_cell(c["label"]) for c in configs]) + " |")
    lines.append("|---|" + "---:|" * len(configs))
    for name in scenarios:
        cells = []
        for c in configs:
            mine = c["scenarios"][name]
            again = f" (+{mine['replayed']})" if mine.get("replayed") else ""
            cells.append(f"{mine['passed']}/{mine['total']}{again}")
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    if replayed:
        lines += ["", t["replay_note"]]
    lines += ["", f"### {t['failures']}", ""]
    for c in configs:
        counts: dict[str, int] = {}
        for s in c["scenarios"].values():
            for grader, n in s["failures"].items():
                counts[grader] = counts.get(grader, 0) + n
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        shown = ", ".join(f"`{g}` ({n})" for g, n in top) or t["none"]
        lines.append(f"- {_cell(c['label'])}{sep}{shown}")
    lines += ["", f"## {t['comparison']}", ""]
    if report["comparisons"]:
        lines += [t["c_intro"].format(**values), ""]
        better = t["c_better"]
        lines.append(
            f"| A | B | A {better} | B {better} | {t['c_ties']} | {t['c_p']} | {t['c_holm']} |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for p in report["comparisons"]:
            lines.append(
                f"| {_cell(p['a_label'])} | {_cell(p['b_label'])} | {p['a_better']} | "
                f"{p['b_better']} | {p['ties']} | {_number(p['sign_test_p'], 3, lang)} | "
                f"{_number(p['holm_p'], 3, lang)} |"
            )
    else:
        lines.append(t["c_none"])
    lines += ["", f"## {t['provenance']}", ""]
    for c in configs:
        commit = str(c["synaptane"].get("commit") or "unknown")
        state = f" ({t['dirty']})" if c["synaptane"]["dirty"] else ""
        price = c["price"]
        stops = ", ".join(f"{k} ({n})" for k, n in c["interruptions"].items()) or t["none"]
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
            f"- {t['p_stops']}{sep}{stops}",
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
