"""Publishable results of the tidy-up bench (`tests/eval/publish.py`), without any model.

Every run below is a SYNTHETIC EXAMPLE built by `synthetic_run`: invented outcomes and costs,
only to check the format, the aggregation, the sort order and the refusals. None of these
numbers is a measurement, and none is written to `eval/results/`. Fake secrets are built from
pieces, so that no scanner mistakes them for real ones.
"""

import json
import random
import tempfile
from pathlib import Path
from typing import Any

import pytest

from synaptane.ai import anthropic_provider
from synaptane.ai.base import AIConfig
from synaptane.services import tidy
from synaptane.services.tidy_tools import INSTRUCTIONS
from tests.eval import publish
from tests.eval.scenarios import SCENARIOS

SCENARIO_NAMES = ("alpha", "beta", "gamma")
FAKE_KEY = "sk-" + "ant-api03-" + "SYNTHETIC" * 3


def synthetic_run(
    model: str = "model-a",
    effort: str = "medium",
    *,
    outcomes: dict[str, list[bool]] | None = None,
    cost: float | None = 0.01,
    label: str = "",
    prompt_sha: str = "p" * 64,
    commit: str = "c" * 40,
    dirty: bool | None = False,
    bench_sha: str = "b" * 64,
    day: str = "2026-11-01",
    failure: str = "faits",
    provider: str = "anthropic",
    price_input: float = 2.0,
) -> dict[str, Any]:
    """A SYNTHETIC run file (see the module docstring): never a measurement."""
    outcomes = outcomes or {name: [True, True, True] for name in SCENARIO_NAMES}
    trials_requested = len(next(iter(outcomes.values())))
    trials = [
        {
            "scenario": name,
            "trial": n,
            "passed": ok,
            "failures": {} if ok else {failure: "synthetic"},
            "interrupted": None,
            "provider_error": None,
            "served_models": [model],
            "model_mismatch": False,
            "duration_s": 10.0 + n,
            "tokens": {"input": 1000, "output": 100, "cached": 5000, "cache_write": 1000},
            "turns": 4,
            "cost": cost,
            "items_done": 1,
            "metrics": None,
        }
        for n in range(1, trials_requested + 1)
        for name, results in outcomes.items()
        for ok in [results[n - 1]]
    ]
    return {
        "format": publish.RUN_FORMAT,
        "format_version": publish.FORMAT_VERSION,
        "synaptane": {"version": "1.13.0", "commit": commit, "dirty": dirty},
        "started_at": f"{day}T10:00:00+00:00",
        "finished_at": f"{day}T10:30:00+00:00",
        "duration_s": 1800.0,
        "bench": {"sha256": bench_sha, "files": {}, "wiki_sha256": "w" * 64, "wiki_pages": 50},
        "scenarios": [{"name": n, "summary": n, "sha256": n * 4} for n in outcomes],
        "config": {
            "label": label,
            "provider": provider,
            "model": model,
            "effort": effort,
            "effort_requested": effort,
            "base_url": "default",
            "fallbacks": False,
            "prompt": {"source": "default", "path": publish.DEFAULT_PROMPT, "sha256": prompt_sha},
            "tools_sha256": "t" * 64,
        },
        "price": {
            "input": price_input,
            "output": 10.0,
            "cache_read": 0.05,
            "cache_write": 1.25,
            "long_prompt": 0,
            "public": True,
            "currency": "USD",
            "unit": "USD per million tokens (public list price)",
        },
        "budget": 5.0,
        "trials_requested": trials_requested,
        "complete": True,
        "stopped_by": None,
        "trials": trials,
        "discarded": [],
    }


def write(directory: Path, name: str, run: dict[str, Any]) -> Path:
    path = directory / name
    path.write_text(json.dumps(run), encoding="utf-8")
    return path


# --- Statistics -----------------------------------------------------------------------------


def test_wilson_interval() -> None:
    assert publish.wilson(0, 0) == (0.0, 1.0)
    assert publish.wilson(5, 10) == (0.2366, 0.7634)
    low, high = publish.wilson(3, 3)
    assert high == 1.0 and 0.43 < low < 0.44  # three out of three proves little
    low, high = publish.wilson(0, 39)
    assert low == 0.0 and 0.08 < high < 0.1


def test_sign_test() -> None:
    assert publish.sign_test(0, 0) == 1.0
    assert publish.sign_test(13, 0) == pytest.approx(2 / 2**13, abs=1e-4)
    assert publish.sign_test(5, 0) == 0.0625  # five scenarios out of five: not yet enough
    assert publish.sign_test(3, 3) == 1.0
    assert publish.sign_test(1, 9) == publish.sign_test(9, 1) == 0.0215


def test_aggregation_counts_rates_spread_and_costs() -> None:
    run = synthetic_run(
        outcomes={"alpha": [True, False, True], "beta": [True, True, True], "gamma": [False] * 3}
    )
    report = publish.aggregate([("a.json", run)])
    (config,) = report["configurations"]
    assert (config["passed"], config["total"], config["pass_rate"]) == (5, 9, 0.5556)
    assert config["ci95"] == list(publish.wilson(5, 9))
    # Sweeps: trial 1 passes alpha and beta (2/3), trial 2 beta only (1/3), trial 3 2/3.
    sweep = config["sweep_score"]
    assert (sweep["n"], sweep["mean"], sweep["min"], sweep["max"]) == (3, 0.5556, 0.3333, 0.6667)
    assert sweep["sd"] == pytest.approx(0.1925, abs=1e-4)
    assert config["cost"]["total"] == 0.09
    assert config["cost"]["per_passed_run"] == 0.018
    assert config["cost"]["per_run"]["sd"] == 0.0
    assert config["scenarios"]["gamma"] == {
        "passed": 0,
        "total": 3,
        "pass_rate": 0.0,
        "failures": {"faits": 3},
        "replayed": 0,
    }
    assert config["tokens_per_run"]["cache_write"] == 1000.0
    assert config["duration_s"]["per_run"]["mean"] == 12.0
    assert config["label"] == "model-a · medium"
    assert report["method"]["min_trials"] == publish.MIN_TRIALS
    assert report["comparisons"] == []
    # Cache writes are already part of the input: counted once.
    assert "| 6,100 |" in publish.markdown(report, "en")


def test_unknown_cost_is_not_counted_as_zero() -> None:
    report = publish.aggregate([("a.json", synthetic_run(cost=None))])
    cost = report["configurations"][0]["cost"]
    assert cost["total"] is None and cost["per_passed_run"] is None
    assert cost["per_run"]["mean"] is None
    assert "–" in publish.markdown(report, "en")


def test_identical_runs_add_up_and_different_ones_stay_apart() -> None:
    first = synthetic_run(day="2026-11-01")
    second = synthetic_run(day="2026-11-02")
    other = synthetic_run(effort="high")
    report = publish.aggregate([("2.json", second), ("1.json", first), ("3.json", other)])
    merged, alone = sorted(report["configurations"], key=lambda c: c["total"], reverse=True)
    assert merged["runs"] == ["1.json", "2.json"] and merged["total"] == 18
    assert merged["trials_per_scenario"] == 6 and merged["sweep_score"]["n"] == 6
    assert merged["dates"] == {
        "first": "2026-11-01T10:00:00+00:00",
        "last": "2026-11-02T10:30:00+00:00",
    }
    assert alone["runs"] == ["3.json"] and alone["effort"] == "high"
    # Another prompt is another configuration, named after its fingerprint.
    prompt = synthetic_run(prompt_sha="d" * 64)
    prompt["config"]["prompt"]["source"] = "file"
    labels = {c["label"] for c in publish.aggregate([("p.json", prompt)])["configurations"]}
    assert labels == {"model-a · medium · prompt dddddddd"}


def test_two_rows_never_share_a_name() -> None:
    same_label = [
        ("a.json", synthetic_run(label="mine", effort="medium")),
        ("b.json", synthetic_run(label="mine", effort="high")),
        ("c.json", synthetic_run(provider="openai_compatible")),
        ("d.json", synthetic_run(price_input=3.0)),
    ]
    labels = sorted(c["label"] for c in publish.aggregate(same_label)["configurations"])
    assert labels == [
        "mine · high",
        "mine · medium",
        "model-a · medium · anthropic",
        "model-a · medium · openai_compatible",
    ]
    prices = [("a.json", synthetic_run()), ("b.json", synthetic_run(price_input=3.0))]
    assert sorted(c["label"] for c in publish.aggregate(prices)["configurations"]) == [
        "model-a · medium · 2/10",
        "model-a · medium · 3/10",
    ]
    commits = [("a.json", synthetic_run()), ("b.json", synthetic_run(commit="e" * 40))]
    shown = publish.aggregate(commits, allow_mixed_commits=True)["configurations"]
    assert sorted(c["label"] for c in shown) == [
        "model-a · medium · commit ccccccc",
        "model-a · medium · commit eeeeeee",
    ]


def test_sort_is_by_lower_bound_then_rate_then_cost_and_is_stable(tmp_path: Path) -> None:
    worse = {"alpha": [True, False, False], "beta": [True] * 3, "gamma": [True] * 3}
    runs = [
        ("cheap.json", synthetic_run("model-c", cost=0.001)),
        ("dear.json", synthetic_run("model-b", cost=0.5)),
        ("same-b.json", synthetic_run("model-a", cost=0.5)),
        ("worse.json", synthetic_run("model-d", outcomes=worse, cost=0.0001)),
    ]
    report = publish.aggregate(runs)
    order = [c["model"] for c in report["configurations"]]
    assert order == ["model-c", "model-a", "model-b", "model-d"]
    paths = [write(tmp_path, name, run) for name, run in runs]
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    publish.write_report(paths, out_a)
    shuffled = paths[:]
    random.Random(4).shuffle(shuffled)
    publish.write_report(shuffled, out_b)
    for name in ("results.json", "results.md", "results.fr.md"):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes(), name


def test_three_out_of_three_does_not_beat_twenty_nine_out_of_thirty() -> None:
    lucky = synthetic_run("lucky", outcomes={name: [True] for name in SCENARIO_NAMES})
    steady = {name: [True] * 10 for name in SCENARIO_NAMES}
    steady["alpha"][4] = False
    solid = synthetic_run("solid", outcomes=steady)
    report = publish.aggregate([("l.json", lucky), ("s.json", solid)], min_trials=1)
    assert [c["model"] for c in report["configurations"]] == ["solid", "lucky"]


def test_paired_comparison_by_scenario() -> None:
    a = synthetic_run(
        "model-a", outcomes={"alpha": [True] * 3, "beta": [True] * 3, "gamma": [True] * 3}
    )
    b = synthetic_run(
        "model-b", outcomes={"alpha": [False] * 3, "beta": [True, False, True], "gamma": [True] * 3}
    )
    report = publish.aggregate([("a.json", a), ("b.json", b)])
    (pair,) = report["comparisons"]
    assert (pair["a_label"], pair["b_label"]) == ("model-a · medium", "model-b · medium")
    assert (pair["a_better"], pair["b_better"], pair["ties"]) == (2, 0, 1)
    assert pair["sign_test_p"] == 0.5
    english = publish.markdown(report, "en")
    assert "## Paired comparison by scenario" in english
    assert pair["holm_p"] == 0.5  # a single pair: nothing to adjust
    assert "| model-a · medium | model-b · medium | 2 | 0 | 1 | 0.500 | 0.500 |" in english
    assert "Holm" in english
    french = publish.markdown(report, "fr")
    assert "| model-a · medium | model-b · medium | 2 | 0 | 1 | 0,500 | 0,500 |" in french


def test_holm_adjusts_for_several_pairs() -> None:
    assert publish.holm([]) == []
    assert publish.holm([0.01, 0.04, 0.03]) == [0.03, 0.06, 0.06]
    assert publish.holm([0.5, 0.9]) == [1.0, 1.0]
    runs = [(f"{m}.json", synthetic_run(m)) for m in ("model-a", "model-b", "model-c")]
    pairs = publish.aggregate(runs)["comparisons"]
    assert len(pairs) == 3 and all(p["holm_p"] >= p["sign_test_p"] for p in pairs)


def test_markdown_has_the_method_first_then_the_tables() -> None:
    run = synthetic_run(
        outcomes={"alpha": [True, False, True], "beta": [True] * 3, "gamma": [True] * 3},
        label="Sonnet | test",
    )
    report = publish.aggregate([("a.json", run)])
    english, french = publish.markdown(report, "en"), publish.markdown(report, "fr")
    assert english.startswith("English | [Français](results.fr.md)\n\n# Tidy-up bench: results")
    assert english.index("## Method") < english.index("## Summary")
    assert "3 scenarios, 3 trials per scenario" in english and "Wilson" in english
    assert "### Limits" in english and "not an invoice" in english
    assert "conditional on these 3 short scenarios" in english
    assert "overlapping ones do not rule one out" in english
    assert "| Sonnet \\| test | 8/9 | 89 % (56 %–98 %) |" in english
    assert "| 0 | `ccccccc` |" in english  # trials replayed, commit
    assert "| `alpha` | 2/3 |" in english
    assert "- Sonnet \\| test: `faits` (1)" in english
    assert "A single configuration: nothing to compare." in english
    assert "`runs/a.json`" in english and "commit `cccccccccccc`" in english
    assert "2.00 / 10.00 USD per million tokens, input / output (public list price)" in english
    assert french.startswith("[English](results.md) | Français\n\n# Banc du rangement")
    assert "## Méthode" in french and "IC 95 %" in french
    assert "89 % (56 %–98 %)" in french and "0,0100 $" in french
    assert "- Sonnet \\| test : `faits` (1)" in french
    assert "ne l'excluent pas" in french


def test_an_unknown_commit_does_not_break_the_tables() -> None:
    report = publish.aggregate([("a.json", synthetic_run(commit="unknown"))])
    report["configurations"][0]["synaptane"]["commit"] = None
    assert "`unknown`" in publish.markdown(report, "en")


# --- Refusals -------------------------------------------------------------------------------


def _cut(kind: str) -> dict[str, Any]:
    run = synthetic_run()
    run["trials"][-1]["interrupted"] = kind
    run["trials"][-1]["passed"] = False
    return run


def _mismatch() -> dict[str, Any]:
    run = synthetic_run("claude-sonnet-5-5")
    run["trials"][0]["served_models"] = ["claude-opus-5-5-20261001"]
    run["trials"][0]["model_mismatch"] = True
    return run


def _doubled() -> dict[str, Any]:
    run = synthetic_run()
    run["trials"].append(dict(run["trials"][0]))
    return run


def _missing() -> dict[str, Any]:
    run = synthetic_run()
    run["trials"].pop()
    return run


def _strangers() -> dict[str, Any]:
    run = synthetic_run()
    run["trials"][0] = run["trials"][0] | {"trial": 9}
    return run


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (synthetic_run() | {"complete": False, "stopped_by": "budget"}, r"incomplet \(budget\)"),
        (_cut("budget"), r"incomplet \(budget\)"),
        (_cut("provider"), r"incomplet \(provider\)"),
        (_cut("error"), r"incomplet \(error\)"),
        (_mismatch(), "servis par un autre modèle"),
        (_doubled(), "en double"),
        (_missing(), "manquants"),
        (_strangers(), "hors des scénarios"),
        (synthetic_run(outcomes={"alpha": [True]}), r"1 essai\(s\) par scénario"),
        (synthetic_run(dirty=True), "arbre modifié"),
        (synthetic_run(dirty=None), "arbre modifié"),
    ],
)
def test_runs_that_do_not_compare_honestly_are_refused(run: dict[str, Any], message: str) -> None:
    with pytest.raises(publish.ReportError, match=message):
        publish.aggregate([("x.json", run)])


def _replayed(n: int) -> dict[str, Any]:
    run = synthetic_run()
    run["discarded"] = [
        {
            "scenario": "alpha",
            "trial": 1,
            "provider_error": None,
            "duration_s": 1.0,
            "tokens": {},
            "cost": 0.02,
        }  # fmt: skip
    ] * n
    return run


def test_replays_are_counted_published_and_capped() -> None:
    with pytest.raises(publish.ReportError, match="rejoués"):
        publish.aggregate([("x.json", _replayed(1))])  # 1 out of 9: above 5 %
    report = publish.aggregate([("x.json", _replayed(1))], allow_replays=True)
    (config,) = report["configurations"]
    assert config["discarded"] == 1 and config["replayed_share"] == 0.1111
    assert config["scenarios"]["alpha"]["replayed"] == 1
    # The discarded trial was paid: in the total and in the cost per passed run.
    assert config["cost"]["total"] == 0.11 and config["cost"]["discarded"] == 0.02
    assert config["cost"]["per_passed_run"] == round(0.11 / 9, 4)
    english = publish.markdown(report, "en")
    assert english.index("**Warning**: 11 % of the trials") < english.index("## Method")
    assert "| `alpha` | 3/3 (+1) |" in english and "(+n): trials replayed" in english
    assert "(1 out of 9, 11 %)" in english
    many = synthetic_run(outcomes={n: [True] * 10 for n in SCENARIO_NAMES})
    many["discarded"] = _replayed(1)["discarded"]  # 1 out of 30: under 5 %
    assert "Warning" not in publish.markdown(publish.aggregate([("m.json", many)]), "en")


def test_the_served_model_is_checked_again_in_the_report() -> None:
    run = synthetic_run("claude-sonnet-5-5")
    run["trials"][0]["served_models"] = ["claude-haiku-5-5"]  # flag left at False
    with pytest.raises(publish.ReportError, match="autre modèle"):
        publish.aggregate([("x.json", run)])


def test_the_model_stopping_itself_counts_as_its_own_failure() -> None:
    run = _cut("step_cap")
    (config,) = publish.aggregate([("x.json", run)])["configurations"]
    assert config["interruptions"] == {"step_cap": 1} and config["passed"] == 8


def test_refusal_switches() -> None:
    assert publish.aggregate([("x.json", synthetic_run(dirty=True))], allow_dirty=True)
    one = synthetic_run(outcomes={"alpha": [True]})
    assert publish.aggregate([("x.json", one)], min_trials=1)["method"]["min_trials"] == 1
    with pytest.raises(publish.ReportError, match="aucun lancement"):
        publish.aggregate([])
    with pytest.raises(publish.ReportError, match="même banc"):
        publish.aggregate([("a.json", synthetic_run()), ("b.json", synthetic_run(bench_sha="e"))])
    fewer = synthetic_run(outcomes={"alpha": [True] * 3})
    with pytest.raises(publish.ReportError, match="même banc"):
        publish.aggregate([("a.json", synthetic_run()), ("b.json", fewer)])
    mixed = [("a.json", synthetic_run()), ("b.json", synthetic_run("model-b", commit="e" * 40))]
    with pytest.raises(publish.ReportError, match="commits différents"):
        publish.aggregate(mixed)
    assert len(publish.aggregate(mixed, allow_mixed_commits=True)["configurations"]) == 2


def test_files_that_are_not_runs_are_refused(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(publish.ReportError, match="illisible"):
        publish.load_runs([broken])
    other = write(tmp_path, "other.json", {"format": "something"})
    with pytest.raises(publish.ReportError, match="pas un lancement"):
        publish.load_runs([other])
    future = write(tmp_path, "future.json", synthetic_run() | {"format_version": 99})
    with pytest.raises(publish.ReportError, match="format 99"):
        publish.load_runs([future])


def test_completeness_is_counted_not_believed() -> None:
    rows = synthetic_run()["trials"]
    assert publish.complete(SCENARIO_NAMES, rows, 3)
    assert not publish.complete(SCENARIO_NAMES, rows[:-1], 3)
    assert not publish.complete(SCENARIO_NAMES, rows + rows[:1], 3)
    assert not publish.complete(SCENARIO_NAMES, [r | {"interrupted": "provider"} for r in rows], 3)
    assert publish.complete(SCENARIO_NAMES, [r | {"interrupted": "refusal"} for r in rows], 3)


# --- Secrets and paths ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        FAKE_KEY,
        "sk-" + "abcdefgh12",
        "ghp_" + "a" * 30,
        "github_pat_" + "A1" * 12,
        "glpat-" + "A" * 20,
        "AIza" + "B" * 33,
        "hf_" + "c" * 30,
        "xoxb-" + "1234567890-abc",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
        "Bearer " + "abcdef1234567890",
        "x-api-key: " + "whatever",
        "http://bench:" + "s3cretpass@127.0.0.1:9/v1",
        "/home/someone/x/y.json",
        "/tmp/synaptane-eval-1/a",
        "/mnt/c/Users/rene/x",
        "C:\\Users\\rene\\x",
        "/Volumes/Data/x",
        "/opt/bench/x",
        "/srv/x",
        "/workspace/x",
        "~/bench/x",
        '"x-api-key": "' + "0123456789abcdef" + '"',
        "https://bob@" + "llm.corp.internal/v1",
        "D:\\work\\bench\\prompt.txt",
        "/mnt/d/projets/x",
        "key=" + "Zx9Yq8Wv7Ut6Sr5Qp4On3Ml2Kj1Ih0Gf",
        '"password": "' + "correct-horse-battery" + '"',
        "C:/Users/rene/x",
    ],
)
def test_what_looks_like_a_secret_or_a_machine_path_is_caught(text: str) -> None:
    assert publish.leaks(f"refused: {text} (end)"), text
    assert not publish.leaks(publish.scrub(f"refused: {text} (end)")), text


def test_ordinary_bench_text_is_not_mistaken_for_a_secret() -> None:
    ordinary = (
        "infra/stockage.md → ../x.md, 16 To, procedures/risk-management-plan-2026.md, "
        "tests/eval/__main__.py, src/synaptane/services/tidy_tools.py (INSTRUCTIONS), "
        "passage interrompu (provider), authorization: none needed, Plan B:\\nfoo bar"
    )
    assert publish.leaks(ordinary) == [] and publish.scrub(ordinary) == ordinary


def test_the_secrets_of_a_run_include_the_provider_address() -> None:
    secrets = publish.run_secrets(FAKE_KEY, "http://bench:s3cret%21@llm.corp.internal:8000/v1/")
    assert FAKE_KEY in secrets and "llm.corp.internal" in secrets
    assert "s3cret!" in secrets and "bench" not in secrets  # the user alone is not secret
    assert publish.leaks("Cannot reach llm.corp.internal", secrets) == [
        "valeur secrète du lancement"
    ]
    assert publish.run_secrets("", "") == []


def test_a_run_with_a_secret_is_refused_and_kept_aside_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aside_dir = tmp_path / "aside"
    aside_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(aside_dir))
    run = synthetic_run(outcomes={"alpha": [False] * 3}, provider="openai_compatible")
    run["trials"][0]["failures"] = {"échec": "Cannot reach http://llm.corp.internal:8000/v1"}
    secrets = publish.run_secrets(FAKE_KEY, "http://llm.corp.internal:8000/v1")
    with pytest.raises(publish.ReportError, match="valeur secrète du lancement") as error:
        publish.write_run(tmp_path / "run.json", run, secrets)
    assert not (tmp_path / "run.json").exists()
    assert "llm.corp.internal" not in str(error.value)  # the message never repeats the value
    (aside,) = aside_dir.glob("synaptane-bench-refused-run-*.json")
    assert aside.stat().st_mode & 0o777 == 0o600
    copy = json.loads(aside.read_text())
    assert "llm.corp.internal" not in aside.read_text()
    assert copy["trials"][0]["failures"]["échec"].startswith("Cannot reach [secret]")
    assert copy["config"]["provider"] == "openai_compatible"  # the rest stays readable


@pytest.mark.parametrize(
    "base_url",
    [
        "http://ollama:11434/v1",
        "http://localhost:11434/v1",
        "http://bench:" + "pw0rd@127.0.0.1:9/v1",
    ],
)
def test_a_word_of_the_format_is_not_a_secret(tmp_path: Path, base_url: str) -> None:
    run = synthetic_run("llama3", provider="ollama")
    run["bench"]["files"] = {"bench": "x"}  # a key named like the user of the address
    run["trials"][0]["metrics"] = {"note": "served by ollama on localhost"}
    publish.write_run(tmp_path / "run.json", run, publish.run_secrets("", base_url))
    assert json.loads((tmp_path / "run.json").read_text())["config"]["provider"] == "ollama"


def test_only_the_password_key_and_a_dotted_host_are_secrets() -> None:
    assert publish.run_secrets("", "http://ollama:11434/v1") == ["http://ollama:11434/v1"]
    secrets = publish.run_secrets("", "http://bench:" + "pw0rd@127.0.0.1:9/v1")
    assert "bench" not in secrets and "pw0rd" in secrets and "127.0.0.1" in secrets
    run = synthetic_run()
    run["config"]["label"] = "uses pw0rd"
    assert publish.leaks(publish.dump(run), secrets, publish.free_text(run)) == [
        "valeur secrète du lancement"
    ]
    run["config"]["label"] = ""
    run["config"]["model"] = "pw0rd"  # a value of the format: not where secrets are looked for
    assert publish.leaks(publish.dump(run), secrets, publish.free_text(run)) == []


def test_a_report_never_publishes_a_secret(tmp_path: Path) -> None:
    run = synthetic_run(outcomes={"alpha": [False] * 3})
    run["trials"][0]["failures"] = {"échec": "synthetic explanation"}
    # The report only keeps grader names, never their explanations…
    path = write(tmp_path, "ok.json", run)
    publish.write_report([path], tmp_path / "ok")
    assert "explanation" not in "".join(p.read_text() for p in (tmp_path / "ok").iterdir())
    # …and refuses anything it would publish that looks like a secret or a machine path.
    run["config"]["label"] = "run from /home/someone/bench"
    path = write(tmp_path, "path.json", run)
    with pytest.raises(publish.ReportError, match="secret ou chemin"):
        publish.write_report([path], tmp_path / "out")
    assert not (tmp_path / "out").exists()  # nothing written when a check fails


# --- Prompt, model, fingerprints, file names ------------------------------------------------


def test_default_and_custom_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default = publish.load_prompt()
    assert default.template == INSTRUCTIONS and default.source == "default"
    assert default.as_json() == {
        "source": "default",
        "path": publish.DEFAULT_PROMPT,
        "sha256": publish.sha256(INSTRUCTIONS),
    }
    custom = tmp_path / "prompt.txt"
    text = "Tidy {name} ({address}). {language}{vocation}{custom}"
    custom.write_text(text, encoding="utf-8")
    loaded = publish.load_prompt(str(custom))
    # Outside the repository, its commit does not reproduce it: the text goes with the run.
    assert (loaded.source, loaded.path, loaded.inside) == ("file", "prompt.txt", False)
    assert loaded.as_json()["text"] == text
    (tmp_path / "prompts").mkdir()
    custom.rename(tmp_path / "prompts" / "v2.txt")
    monkeypatch.setattr(publish, "ROOT", tmp_path)  # a prompt inside the repository
    inside = publish.load_prompt(str(tmp_path / "prompts" / "v2.txt"))
    assert inside.path == "prompts/v2.txt" and inside.sha256 == loaded.sha256
    assert "text" not in inside.as_json()
    custom.write_text("Tidy {space}.", encoding="utf-8")
    with pytest.raises(ValueError, match="champs inconnus dans l.invite : space"):
        publish.load_prompt(str(custom))
    custom.write_text("A brace { alone", encoding="utf-8")
    with pytest.raises(ValueError, match="accolades"):
        publish.load_prompt(str(custom))


def test_bench_settings_are_put_back() -> None:
    prompt = publish.Prompt("Other {name}.", "file", "x.txt")
    fallbacks = anthropic_provider.FALLBACK_MODELS
    with publish.bench_settings(prompt):
        assert getattr(tidy, "INSTRUCTIONS") == prompt.template  # noqa: B009
        options = anthropic_provider.request_options(
            AIConfig("anthropic", "claude-sonnet-5-5", "k")
        )
        assert "fallbacks" not in options  # the bench measures the model it asked for
    assert getattr(tidy, "INSTRUCTIONS") == INSTRUCTIONS  # noqa: B009
    assert fallbacks == anthropic_provider.FALLBACK_MODELS
    with pytest.raises(RuntimeError), publish.bench_settings(prompt):
        raise RuntimeError
    assert getattr(tidy, "INSTRUCTIONS") == INSTRUCTIONS  # noqa: B009


def test_effective_effort_and_served_model() -> None:
    assert publish.effective_effort("anthropic", "claude-sonnet-5-5", "high") == "high"
    assert publish.effective_effort("anthropic", "claude-haiku-5-5", "high") == "n/a"
    assert publish.effective_effort("openai", "gpt-6.1-sol", "high") == "n/a"
    assert publish.same_model("claude-sonnet-5-5", "claude-sonnet-5-5-20261001")
    assert publish.same_model("llama3", "llama3:latest")
    assert publish.same_model("openai/gpt-5", "gpt-5-2025-08-07")
    assert not publish.same_model("claude-sonnet-5-5", "claude-opus-5-5")


def test_fingerprints_are_stable_and_follow_the_data() -> None:
    first = {s.name: publish.scenario_fingerprint(s) for s in SCENARIOS}
    assert first == {s.name: publish.scenario_fingerprint(s) for s in SCENARIOS}
    assert len(set(first.values())) == len(SCENARIOS)
    bench = publish.bench_fingerprint()
    assert set(bench["files"]) == set(publish.BENCH_FILES) and bench["wiki_pages"] == 50
    assert {"tests/eval/env.py", "tests/integration/gitutil.py"} <= set(bench["files"])
    assert len(publish.tools_fingerprint()) == 64
    state = publish.source_state()
    assert set(state) == {"version", "commit", "dirty"}


def test_source_state_reads_the_repository_not_the_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNAPTANE_GIT_SHA", "0" * 40)
    assert publish.source_state()["commit"] != "0" * 40
    monkeypatch.setattr(publish, "_git", lambda *args: None)  # no Git: unknown, never clean
    assert publish.source_state() | {"version": ""} == {
        "version": "",
        "commit": "unknown",
        "dirty": None,
    }


def test_run_file_names_never_overwrite(tmp_path: Path) -> None:
    first = publish.run_path("claude-sonnet-5-5", "medium", "", "2026-11-08", tmp_path)
    assert first.name == "2026-11-08-claude-sonnet-5-5-medium.json"
    first.write_text("{}")
    second = publish.run_path("claude-sonnet-5-5", "medium", "", "2026-11-08", tmp_path)
    assert second.name == "2026-11-08-claude-sonnet-5-5-medium-2.json"
    named = publish.run_path("openai/gpt-6.1-sol", "high", "My Prompt", "2026-11-08", tmp_path)
    assert named.name == "2026-11-08-openai-gpt-6.1-sol-high-my-prompt.json"


def test_zero_trials_is_refused_with_a_clear_message(capsys: pytest.CaptureFixture[str]) -> None:
    from tests.eval import __main__ as eval_main

    with pytest.raises(SystemExit):
        eval_main.main(["tidy", "--model", "claude-sonnet-5-5", "--trials", "0", "--budget", "1"])
    assert "--trials 0 : au moins 1 essai par scénario" in capsys.readouterr().err


def test_which_provider_errors_are_replayed() -> None:
    from tests.eval import __main__ as eval_main

    def error(status: int | None, message: str = "x", transient: bool = True) -> dict[str, Any]:
        return {"type": "AIError", "message": message, "status": status, "transient": transient}

    assert eval_main.replayable("anthropic", error(529))
    assert eval_main.replayable("anthropic", error(None, "Anthropic unreachable."))
    assert not eval_main.replayable("anthropic", error(401, transient=False))
    assert not eval_main.replayable("anthropic", None)
    # A local server: only saturation and a refused connection; a 500, a time-out or an
    # answer out of format may come from the model itself.
    assert eval_main.replayable("ollama", error(503))
    assert eval_main.replayable("openai_compatible", error(429))
    assert eval_main.replayable("ollama", error(None, "Cannot reach {address} (connection …"))
    assert not eval_main.replayable("ollama", error(500))
    assert not eval_main.replayable("openai_compatible", error(None, "{address} does not answer"))
    assert not eval_main.replayable("ollama", error(None, "Unexpected response from the provider"))
