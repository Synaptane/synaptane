"""Lancer le banc à la main : `uv run python -m tests.eval {tidy,search} [options]`.

    tidy                      scénarios avec le modèle simulé (bons et mauvais scripts)
    tidy --model M --budget B scénarios avec un vrai modèle, budget en euros fixé d'avance
    search                    rappel@5 et MRR, mots seuls puis hybride (embeddings simulés)

Le PostgreSQL de développement doit tourner (`docker compose -f compose.dev.yaml up -d`) : une
base jetable y est créée, puis supprimée. Avec un vrai modèle, la clé est lue dans une variable
d'environnement (`ANTHROPIC_API_KEY` par défaut), jamais en argument. Mode d'emploi complet et
lecture des résultats : docs/dev/eval.md.
"""

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from synaptane.ai import pricing
from synaptane.ai.base import API_PROVIDERS, EFFORTS, AIConfig, Usage
from synaptane.ai.providers import open_conversation
from tests.eval import search_bench
from tests.eval.env import Env, temporary
from tests.eval.scenarios import SCENARIOS
from tests.eval.tidy_bench import Result, Scenario, run_scenario, scripted


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


def _real(env: Env, chosen: list[Scenario], args: argparse.Namespace) -> int:
    key = os.environ.get(args.api_key_env, "")
    if not key and args.provider not in ("ollama", "openai_compatible"):
        print(f"Clé absente : définissez {args.api_key_env}.", file=sys.stderr)
        return 2
    config = AIConfig(args.provider, args.model, key, args.base_url, args.effort)
    price = pricing.price_for(args.provider, args.model, args.price_input, args.price_output)
    if price is None:
        print(
            "Tarif inconnu pour ce modèle : indiquez --price-input et --price-output (€ par "
            "million de jetons), sans quoi le budget ne peut pas être tenu.",
            file=sys.stderr,
        )
        return 2
    budget = Decimal(str(args.budget))
    spent = Decimal(0)
    results: list[Result] = []
    stopped = False
    for scenario in chosen:
        for trial in range(1, args.trials + 1):
            if spent >= budget:
                stopped = True
                break

            def limit(usage: Usage, already: Decimal = spent) -> str | None:
                cost = pricing.cost(usage, price) or Decimal(0)
                return "Budget du banc atteint." if already + cost >= budget else None

            result = run_scenario(
                env,
                scenario,
                mode=args.model,
                config=config,
                factory=lambda _names: open_conversation,
                price=price,
                limit=limit,
            )
            spent += result.cost or Decimal(0)
            results.append(result)
            print(_line(result, f" #{trial}"), flush=True)
    passed = [r for r in results if r.passed]
    print(f"\n{len(passed)}/{len(results)} passages réussis ; coût ≈ {spent} € / {budget} €.")
    if passed:
        unit = Decimal("0.0001")
        items = sum(r.items_done for r in passed) or 1
        print(
            f"Coût par passage réussi ≈ {(spent / len(passed)).quantize(unit)} € ; par élément "
            f"rangé dans un passage réussi ≈ {(spent / items).quantize(unit)} €."
        )
    if stopped:
        print("Budget atteint : scénarios suivants non joués.")
    if args.json:
        Path(args.json).write_text(
            json.dumps([_as_json(r) for r in results], indent=2, ensure_ascii=False)
        )
    return 0


def _as_json(result: Result) -> dict[str, Any]:
    return {
        "scenario": result.scenario,
        "mode": result.mode,
        "passed": result.passed,
        "failures": result.failures,
        "cost": str(result.cost) if result.cost is not None else None,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "cached_tokens": result.usage.cached_tokens,
        "cache_write_tokens": result.usage.cache_write_tokens,
        "turns": result.usage.turns,
        "metrics": result.metrics,
    }


def main(argv: list[str] | None = None) -> int:
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
    tidy.add_argument("--trials", type=int, default=1, help="essais par scénario (variance)")
    tidy.add_argument("--api-key-env", default="ANTHROPIC_API_KEY")
    tidy.add_argument("--price-input", type=Decimal, default=None)
    tidy.add_argument("--price-output", type=Decimal, default=None)
    tidy.add_argument("--json", default="", help="fichier où écrire les résultats détaillés")
    sub.add_parser("search", help="rappel@5 et MRR de la recherche")
    args = parser.parse_args(argv)

    if args.bench == "tidy":
        known = {s.name: s for s in SCENARIOS}
        if unknown := [n for n in args.scenario if n not in known]:
            parser.error(f"scénario inconnu : {', '.join(unknown)} (connus : {', '.join(known)})")
        chosen = [known[n] for n in args.scenario] or SCENARIOS
        if args.model and args.budget <= 0:
            parser.error("--budget (en €) est obligatoire avec un vrai modèle")
        with temporary() as env:
            return _real(env, chosen, args) if args.model else _simulated(env, chosen)
    with temporary() as env:
        print(search_bench.report(search_bench.run(env)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
