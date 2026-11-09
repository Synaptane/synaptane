"""Lancer le banc à la main : `uv run python -m tests.eval {tidy,search,mcp} [options]`.

    tidy                      scénarios avec le modèle simulé (bons et mauvais scripts)
    tidy --model M --budget B scénarios avec un vrai modèle, budget en euros fixé d'avance ;
                              écrit un lancement publiable (eval/results/runs/)
    tidy --estimate --model M ordre de grandeur du coût, sans aucun appel
    report [LANCEMENT…]       agrège les lancements : eval/results/results.{json,md,fr.md}
    search                    rappel@5 et MRR, mots seuls puis hybride (embeddings simulés)
    mcp                       outils MCP : script idéal de chaque cas (gratuit)
    mcp --model M --budget-usd B   un vrai assistant (Claude Code, clé d'API), budget en $

Le PostgreSQL de développement doit tourner (`docker compose -f compose.dev.yaml up -d`) : une
base jetable y est créée, puis supprimée. Avec un vrai modèle, la clé est lue dans une variable
d'environnement (`ANTHROPIC_API_KEY` par défaut), jamais en argument. Mode d'emploi complet et
lecture des résultats : docs/dev/eval.md.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any

from synaptane.ai import pricing
from synaptane.ai.base import API_PROVIDERS, EFFORTS, AIConfig, Usage
from synaptane.ai.providers import open_conversation
from synaptane.services.tidy import ConversationFactory
from tests.eval import mcp_bench, publish, search_bench
from tests.eval.env import Env, temporary
from tests.eval.scenarios import SCENARIOS
from tests.eval.tidy_bench import (
    BUDGET_REACHED,
    CHARS_PER_TOKEN,
    Result,
    Scenario,
    measured,
    run_scenario,
    scripted,
)


def _line(result: Result, trial: str = "") -> str:
    mark = "ok    " if result.passed else "FAILED"
    cost = f" · {result.cost} €" if result.cost is not None else ""
    tokens = result.usage.input_tokens + result.usage.output_tokens + result.usage.cached_tokens
    head = f"{mark} {result.scenario:<15} {result.mode:<8}{trial} {tokens:>8} tokens{cost}"
    problems = "".join(f"\n        {name} : {why}" for name, why in result.failures.items())
    return head + problems


def _simulated(env: Env, chosen: list[Scenario]) -> int:
    """Bons scripts réussis, mauvais attrapés par le correcteur prévu : sinon, code 1."""
    broken = 0
    for scenario in chosen:
        good, bad = scripted(env, scenario, "bon"), scripted(env, scenario, "mauvais")
        print(_line(good))
        print(_line(bad))
        if not good.passed or scenario.bad_fails not in bad.failures:
            broken += 1
            print(f"      → banc à corriger : attendu « {scenario.bad_fails} » sur le mauvais")
    print(f"\n{len(chosen) - broken}/{len(chosen)} scénarios cohérents.")
    return 1 if broken else 0


def _conversation(_scenario: Scenario) -> Callable[[dict[str, str]], ConversationFactory]:
    return lambda _names: open_conversation


def _price(args: argparse.Namespace) -> pricing.Price | None:
    price = pricing.price_for(args.provider, args.model, args.price_input, args.price_output)
    if price is None:
        print(
            "Tarif inconnu pour ce modèle : indiquez --price-input et --price-output (€ par "
            "million de jetons), sans quoi le budget ne peut pas être tenu.",
            file=sys.stderr,
        )
    return price


def _prompt(args: argparse.Namespace) -> publish.Prompt | None:
    try:
        return publish.load_prompt(args.prompt)
    except (OSError, ValueError) as error:
        print(f"Invite refusée : {error}", file=sys.stderr)
        return None


RETRIES = 2  # essais rejoués au plus après une erreur passagère du fournisseur (429, 529…)
RETRY_PAUSE = 30.0  # secondes avant de rejouer
# Serveurs compatibles OpenAI et Ollama : un 500, un délai dépassé ou une réponse hors format
# peuvent venir du modèle lui-même (appel d'outil mal formé, raisonnement trop long). Seuls la
# saturation (429, 503) et le refus de connexion y sont rejoués ; le reste est son échec.
LOCAL_PROVIDERS = ("openai_compatible", "ollama")


def _models_fault(provider: str, result: Result) -> bool:
    """Erreur passagère d'un serveur local qui ne se rejoue pas : celle du modèle."""
    error = result.error
    return (
        provider in LOCAL_PROVIDERS
        and error is not None
        and error["transient"]
        and not replayable(provider, error)
    )


def replayable(provider: str, error: dict[str, Any] | None) -> bool:
    """L'erreur du fournisseur justifie-t-elle d'écarter l'essai et de le rejouer ?"""
    if error is None or not error["transient"]:
        return False
    if provider in LOCAL_PROVIDERS:
        return error["status"] in (429, 503) or str(error["message"]).startswith("Cannot reach")
    return True


def _real(
    env: Env,
    chosen: list[Scenario],
    args: argparse.Namespace,
    conversation: Callable[[Scenario], Callable[[dict[str, str]], ConversationFactory]] = (
        _conversation
    ),
) -> int:
    """Un vrai modèle : `--trials` balayages de tous les scénarios, puis le fichier du
    lancement (format publiable, `publish.py`). Les balayages passent l'un après l'autre.

    Un essai coupé ne mesure pas le modèle : une erreur passagère du fournisseur fait écarter
    l'essai (gardé dans `discarded`, son coût compté) et le rejouer, `RETRIES` fois au plus ;
    une erreur durable du fournisseur ou le budget arrêtent le lancement, qui est alors
    incomplet (et refusé par le rapport)."""
    if args.trials is None:
        args.trials = publish.MIN_TRIALS  # défaut : de quoi publier
    key = os.environ.get(args.api_key_env, "")
    if not key and args.provider not in ("ollama", "openai_compatible"):
        print(f"Clé absente : définissez {args.api_key_env}.", file=sys.stderr)
        return 2
    if args.trials < publish.MIN_TRIALS and not args.draft:
        print(
            f"{args.trials} essai(s) par scénario : moins de {publish.MIN_TRIALS}, le lancement "
            "ne sera pas publiable. Ajoutez --draft pour le lancer quand même.",
            file=sys.stderr,
        )
        return 2
    price, prompt = _price(args), _prompt(args)
    if price is None or prompt is None:
        return 2
    config = AIConfig(args.provider, args.model, key, args.base_url, args.effort)
    secrets = publish.run_secrets(key, args.base_url)
    budget = Decimal(str(args.budget))
    spent = Decimal(0)
    trials: list[publish.Trial] = []
    discarded: list[publish.Discarded] = []
    stopped_by: str | None = None
    started = datetime.now(UTC)
    with publish.bench_settings(prompt):
        for trial in range(1, args.trials + 1):
            for scenario in chosen:
                for attempt in range(RETRIES + 1):
                    if spent >= budget:
                        stopped_by = "budget"
                        break

                    def limit(usage: Usage, already: Decimal = spent) -> str | None:
                        cost = pricing.cost(usage, price) or Decimal(0)
                        return BUDGET_REACHED if already + cost >= budget else None

                    clock = time.monotonic()
                    result = run_scenario(
                        env,
                        scenario,
                        mode=args.model,
                        config=config,
                        factory=conversation(scenario),
                        price=price,
                        limit=limit,
                    )
                    duration = time.monotonic() - clock
                    spent += result.cost or Decimal(0)
                    print(_line(result, f" #{trial}"), flush=True)
                    passing = replayable(args.provider, result.error)
                    if result.interrupted == "provider" and passing and attempt < RETRIES:
                        discarded.append(publish.Discarded(trial, result, duration))
                        why = result.detail
                        print(f"        erreur passagère du fournisseur, essai rejoué : {why}")
                        time.sleep(RETRY_PAUSE)
                        continue
                    trials.append(publish.Trial(trial, result, duration))
                    if result.interrupted == "budget":
                        stopped_by = "budget"
                    elif result.interrupted == "provider" and _models_fault(args.provider, result):
                        # Serveur local : 500, délai, réponse hors format comptent au modèle.
                        result.interrupted = "model_error"
                        result.failures["échec"] = "passage interrompu (model_error)"
                    elif result.interrupted == "provider":
                        stopped_by = "provider"
                        print(f"        erreur du fournisseur : {result.detail}", file=sys.stderr)
                    elif result.interrupted == "error":
                        stopped_by = "error"  # erreur du banc ou de Git : rien ne sert de continuer
                        print(f"        erreur du banc : {result.detail}", file=sys.stderr)
                    break
                if stopped_by:
                    break
            if stopped_by:
                break
    results = [t.result for t in trials]
    passed = [r for r in results if r.passed]
    print(f"\n{len(passed)}/{len(results)} passages réussis ; coût ≈ {spent} € / {budget} €.")
    if passed:
        unit = Decimal("0.0001")
        items = sum(r.items_done for r in passed) or 1
        print(
            f"Coût par passage réussi ≈ {(spent / len(passed)).quantize(unit)} € ; par élément "
            f"rangé dans un passage réussi ≈ {(spent / items).quantize(unit)} €."
        )
    if stopped_by == "budget":
        print("Budget atteint : lancement incomplet, non publiable (relancez avec plus de budget).")
    elif stopped_by == "provider":
        print("Le fournisseur a échoué : lancement incomplet, non publiable.")
    elif stopped_by == "error":
        print("Erreur du banc : lancement arrêté, incomplet, non publiable.")
    record = publish.run_record(
        scenarios=chosen,
        trials=trials,
        discarded=discarded,
        trials_requested=args.trials,
        provider=args.provider,
        model=args.model,
        effort=args.effort,
        custom_base_url=bool(args.base_url),
        prompt=prompt,
        price=price,
        budget=args.budget,
        label=args.label,
        started=started,
        finished=datetime.now(UTC),
        stopped_by=stopped_by,
    )
    day = started.date().isoformat()
    path = publish.run_path(args.model, args.effort, args.label, day)
    if args.json:
        path = Path(args.json)
    try:
        publish.write_run(path, record, secrets)
    except publish.ReportError as error:
        print(f"Lancement refusé : {error}", file=sys.stderr)
        return 1
    print(f"Lancement écrit dans {path} ; pour publier : ./scripts/eval.sh report")
    return 0 if record["complete"] else 1


HIGH_FACTOR = 5  # haut de la fourchette : un vrai modèle cherche, relit et raisonne davantage


def _estimate(env: Env, chosen: list[Scenario], args: argparse.Namespace) -> int:
    """Ordre de grandeur du coût d'un lancement, sans aucun appel : le chemin idéal de chaque
    scénario (son bon script), compté comme le facturerait le fournisseur. Le cache relu à
    chaque tour n'est compté que pour Anthropic, dont le fournisseur le demande ; ailleurs,
    toute l'entrée au plein tarif (haut de fourchette)."""
    if args.trials is None:
        args.trials = publish.MIN_TRIALS
    price, prompt = _price(args), _prompt(args)
    if price is None or prompt is None:
        return 2
    cache = args.provider == "anthropic"
    total, calls = Decimal(0), 0
    with publish.bench_settings(prompt):
        for scenario in chosen:
            result = measured(env, scenario, price, cache)
            usage = result.usage
            print(
                f"{scenario.name:<15} {usage.turns:>3} appels · "
                f"{usage.input_tokens + usage.cached_tokens:>7} jetons en entrée (dont "
                f"{usage.cached_tokens} relus du cache) · {usage.output_tokens:>5} en sortie · "
                f"{result.cost} €"
            )
            total += result.cost or Decimal(0)
            calls += usage.turns
    cent = Decimal("0.01")
    low = (total * args.trials).quantize(cent)
    high = (total * args.trials * HIGH_FACTOR).quantize(cent)
    print(
        f"\nPar balayage, chemin idéal : {calls} appels au modèle, ≈ {total.quantize(cent)} € "
        f"({args.model} : {price.input} / {price.output} par million de jetons, tarif public en "
        "dollars compté un pour un comme pour --budget"
        + ("" if cache else " ; sans cache : toute l'entrée au plein tarif")
        + ")."
        f"\n{args.trials} essai(s) par scénario : {args.trials * calls} appels, de {low} € "
        f"(chemin idéal) à ≈ {high} € ({HIGH_FACTOR} fois : un vrai modèle cherche et relit "
        "davantage, et son raisonnement n'est pas compté ici)."
        f"\nEstimation, pas un devis : jetons ≈ caractères / {CHARS_PER_TOKEN}. Budget "
        f"conseillé : --budget {high.quantize(Decimal(1), ROUND_UP)} (un lancement arrêté par "
        "le budget n'est pas publiable)."
    )
    return 0


def _report(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.runs] or sorted(publish.RUNS_DIR.glob("*.json"))
    try:
        written = publish.write_report(
            paths,
            Path(args.out),
            min_trials=args.min_trials,
            allow_dirty=args.allow_dirty,
            allow_mixed_commits=args.allow_mixed_commits,
            allow_replays=args.allow_replays,
        )
    except publish.ReportError as error:
        print(f"Rapport refusé :\n{error}", file=sys.stderr)
        return 1
    print("Écrit : " + ", ".join(str(p) for p in written))
    return 0


def _mcp(env: Env, chosen: list[mcp_bench.Case], args: argparse.Namespace) -> int:
    """Banc MCP : script idéal (simulé) ou vrai assistant, cas par cas."""
    if args.model and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Clé absente : définissez ANTHROPIC_API_KEY (jamais l'abonnement).", file=sys.stderr)
        return 2
    results: list[mcp_bench.Result] = []
    spent = 0.0
    stopped = False
    with mcp_bench.serve(env) as url:
        for case in chosen:
            for trial in range(1, (args.trials if args.model else 1) + 1):
                if args.model and spent >= args.budget_usd:
                    stopped = True
                    break
                if args.model:
                    left = min(args.budget_usd - spent, args.per_case_usd)
                    result = mcp_bench.real(env, url, case, args.model, left)
                else:
                    result = mcp_bench.scripted(env, url, case)
                spent += result.transcript.cost_usd
                results.append(result)
                print(mcp_bench.line(result, f" #{trial}" if args.model else ""), flush=True)
    passed = [r for r in results if r.passed]
    calls = sum(len(r.transcript.calls) for r in results)
    print(f"\n{len(passed)}/{len(results)} cas réussis ; {calls} appels d'outils", end="")
    print(f" ; coût ≈ {spent:.2f} $ / {args.budget_usd:.2f} $." if args.model else ".")
    if stopped:
        print("Budget atteint : cas suivants non joués.")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        "case": r.case,
                        "mode": r.mode,
                        "passed": r.passed,
                        "failures": r.failures,
                        "calls": [name for name, _ in r.transcript.calls],
                        "errors": r.transcript.errors,
                        "input_tokens": r.transcript.input_tokens,
                        "output_tokens": r.transcript.output_tokens,
                        "cost_usd": r.transcript.cost_usd,
                        "turns": r.transcript.turns,
                        "answer": r.transcript.answer,
                    }
                    for r in results
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
    return 0 if args.model or len(passed) == len(results) else 1


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.eval", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="bench", required=True)
    tidy = sub.add_parser("tidy", help="scénarios du rangement")
    tidy.add_argument(
        "--scenario", action="append", default=[], help="nom (répétable) ; défaut : tous"
    )
    tidy.add_argument("--model", default="", help="vrai modèle ; sans lui, modèle simulé")
    tidy.add_argument("--provider", default="anthropic", choices=API_PROVIDERS)
    tidy.add_argument("--effort", default="medium", choices=EFFORTS)
    tidy.add_argument("--base-url", default="")
    tidy.add_argument(
        "--budget", type=float, default=0.0, help="€ au plus, obligatoire avec --model"
    )
    tidy.add_argument(
        "--trials", type=int, default=None, help="essais par scénario, vrai modèle ; défaut : 3"
    )
    tidy.add_argument(
        "--draft", action="store_true", help="vrai modèle avec moins de 3 essais (non publiable)"
    )
    tidy.add_argument("--api-key-env", default="ANTHROPIC_API_KEY")
    tidy.add_argument("--price-input", type=Decimal, default=None)
    tidy.add_argument("--price-output", type=Decimal, default=None)
    tidy.add_argument(
        "--json", default="", help="fichier du lancement ; défaut : eval/results/runs/<date>-…"
    )
    tidy.add_argument(
        "--prompt", default="", help="invite à essayer à la place de INSTRUCTIONS (fichier)"
    )
    tidy.add_argument("--label", default="", help="nom de la configuration dans le rapport")
    tidy.add_argument(
        "--estimate", action="store_true", help="avec --model : coût estimé, sans aucun appel"
    )
    sub.add_parser("search", help="rappel@5 et MRR de la recherche")
    report = sub.add_parser("report", help="agréger des lancements en résultats publiables")
    report.add_argument("runs", nargs="*", help="lancements ; défaut : eval/results/runs/*.json")
    report.add_argument("--out", default=str(publish.RESULTS_DIR), help="dossier des résultats")
    report.add_argument(
        "--min-trials", type=int, default=publish.MIN_TRIALS, help="essais par scénario, au moins"
    )
    report.add_argument(
        "--allow-dirty", action="store_true", help="accepter un lancement sur un arbre modifié"
    )
    report.add_argument(
        "--allow-mixed-commits", action="store_true", help="accepter des commits différents"
    )
    report.add_argument(
        "--allow-replays",
        action="store_true",
        help=f"accepter plus de {publish.MAX_REPLAYED:.0%} d'essais rejoués",
    )
    mcp = sub.add_parser("mcp", help="outils MCP, vus par un assistant")
    mcp.add_argument("--case", action="append", default=[], help="cas (répétable) ; défaut : tous")
    mcp.add_argument("--model", default="", help="vrai modèle (Claude Code) ; sans lui, simulé")
    mcp.add_argument(
        "--budget-usd", type=float, default=0.0, help="$ au plus, obligatoire avec --model"
    )
    mcp.add_argument("--per-case-usd", type=float, default=0.5, help="$ au plus par passage")
    mcp.add_argument("--trials", type=int, default=1, help="essais par cas (variance)")
    mcp.add_argument("--json", default="", help="fichier où écrire les résultats détaillés")
    return parser


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.bench == "report":
        return _report(args)

    if args.bench == "mcp":
        known_cases = {c.key: c for c in mcp_bench.CASES}
        if unknown := [n for n in args.case if n not in known_cases]:
            cli.error(f"cas inconnu : {', '.join(unknown)} (connus : {', '.join(known_cases)})")
        if args.model and args.budget_usd <= 0:
            cli.error("--budget-usd est obligatoire avec un vrai modèle")
        with temporary() as env:
            return _mcp(env, [known_cases[n] for n in args.case] or list(mcp_bench.CASES), args)

    if args.bench == "tidy":
        known = {s.name: s for s in SCENARIOS}
        if unknown := [n for n in args.scenario if n not in known]:
            cli.error(f"scénario inconnu : {', '.join(unknown)} (connus : {', '.join(known)})")
        chosen = [known[n] for n in args.scenario] or SCENARIOS
        if args.trials is not None and args.trials < 1:
            cli.error(f"--trials {args.trials} : au moins 1 essai par scénario")
        if args.estimate:
            if not args.model:
                cli.error("--estimate demande --model (le tarif du modèle)")
            with temporary() as env:
                return _estimate(env, chosen, args)
        if args.model and args.budget <= 0:
            cli.error("--budget (en €) est obligatoire avec un vrai modèle")
        with temporary() as env:
            return _real(env, chosen, args) if args.model else _simulated(env, chosen)
    with temporary() as env:
        print(search_bench.report(search_bench.run(env)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
