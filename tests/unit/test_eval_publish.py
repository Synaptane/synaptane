"""Publishable results of the tidy-up bench (`tests/eval/publish.py`), without any model.

Every run below is a SYNTHETIC EXAMPLE built by `synthetic_run`: invented outcomes and costs,
only to check the format, the aggregation, the sort order and the refusals. None of these
numbers is a measurement, and none is written to `eval/results/`.
"""

import json
import random
from pathlib import Path
from typing import Any

import pytest

from synaptane.services import tidy
from synaptane.services.tidy_tools import INSTRUCTIONS
from tests.eval import publish
from tests.eval.scenarios import SCENARIOS

SCENARIO_NAMES = ("alpha", "beta", "gamma")


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
    complete: bool = True,
    day: str = "2026-11-01",
    failure: str = "faits",
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
            "provider": "anthropic",
            "model": model,
            "effort": effort,
            "base_url": "default",
            "prompt": {"source": "default", "path": publish.DEFAULT_PROMPT, "sha256": prompt_sha},
            "tools_sha256": "t" * 64,
        },
        "price": {
            "input": 2.0,
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
        "complete": complete,
        "stopped_by_budget": not complete,
        "trials": trials,
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
    }
    assert config["tokens_per_run"] == {
        "input": 1000.0,
        "output": 100.0,
        "cached": 5000.0,
        "cache_write": 1000.0,
    }
    assert config["duration_s"]["per_run"]["mean"] == 12.0
    assert config["label"] == "model-a · medium"
    assert report["method"]["min_trials"] == publish.MIN_TRIALS


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


def test_sort_is_by_pass_rate_then_cost_then_label_and_is_stable(tmp_path: Path) -> None:
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
    assert "| Sonnet \\| test | 8/9 | 89 % (56 %–98 %) |" in english
    assert "| `alpha` | 2/3 |" in english
    assert "- Sonnet \\| test: `faits` (1)" in english
    assert "`runs/a.json`" in english and "commit `cccccccccccc`" in english
    assert "2.00 / 10.00 USD per million tokens, input / output (public list price)" in english
    assert french.startswith("[English](results.md) | Français\n\n# Banc du rangement")
    assert "## Méthode" in french and "IC 95 %" in french
    assert "89 % (56 %–98 %)" in french and "0,0100 $" in french
    assert "- Sonnet \\| test : `faits` (1)" in french


# --- Refusals -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (synthetic_run(complete=False), r"incomplet \(budget atteint\)"),
        (synthetic_run(outcomes={"alpha": [True]}), r"1 essai\(s\) par scénario"),
        (synthetic_run(dirty=True), "arbre modifié"),
        (synthetic_run(dirty=None), "arbre modifié"),
    ],
)
def test_runs_that_do_not_compare_honestly_are_refused(run: dict[str, Any], message: str) -> None:
    with pytest.raises(publish.ReportError, match=message):
        publish.aggregate([("x.json", run)])


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


# --- Secrets and paths ----------------------------------------------------------------------


def test_secrets_and_machine_paths_are_scrubbed() -> None:
    key = "sk-ant-api03-SYNTHETICFAKEKEY0123456789"
    text = (
        f"refused {key} in /home/someone/x/y.json and /tmp/synaptane-eval-1/a, "
        "Bearer abcdef1234567890"
    )
    clean = publish.scrub(text, [key])
    assert key not in clean and "/home/" not in clean and "/tmp/" not in clean
    assert "Bearer abc" not in clean and publish.leaks(clean) == []
    assert publish.scrub("custom-secret-value", ["custom-secret-value"]) == "[secret]"
    assert publish.leaks("glpat-ABCDEFGHIJKLMNOPQRST ghp_" + "a" * 30) != []
    # Ordinary bench text is left alone.
    ordinary = "infra/stockage.md → ../x.md, 16 To, procedures/risk-management-plan-2026.md"
    assert publish.scrub(ordinary) == ordinary


def test_a_report_never_publishes_a_secret(tmp_path: Path) -> None:
    run = synthetic_run(outcomes={"alpha": [False] * 3})
    run["trials"][0]["failures"] = {"échec": "passage échoué : sk-ant-api03-LEAKEDLEAKEDLEAKED"}
    with pytest.raises(publish.ReportError, match="secret"):
        publish.write_run(tmp_path / "run.json", run)
    # The report only keeps grader names, never their explanations…
    path = write(tmp_path, "leak.json", run)
    publish.write_report([path], tmp_path / "ok")
    assert "LEAKED" not in "".join(p.read_text() for p in (tmp_path / "ok").iterdir())
    # …and refuses anything it would publish that looks like a secret or a machine path.
    run["config"]["label"] = "run from /home/someone/bench"
    path = write(tmp_path, "path.json", run)
    with pytest.raises(publish.ReportError, match="secret ou chemin"):
        publish.write_report([path], tmp_path / "out")
    assert not (tmp_path / "out").exists()  # nothing written when a check fails


# --- Prompt, fingerprints, file names -------------------------------------------------------


def test_default_and_custom_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default = publish.load_prompt()
    assert default.template == INSTRUCTIONS and default.source == "default"
    assert default.as_json() == {
        "source": "default",
        "path": publish.DEFAULT_PROMPT,
        "sha256": publish.sha256(INSTRUCTIONS),
    }
    custom = tmp_path / "prompt.txt"
    custom.write_text("Tidy {name} ({address}). {language}{vocation}{custom}", encoding="utf-8")
    loaded = publish.load_prompt(str(custom))
    assert (loaded.source, loaded.path) == ("file", "prompt.txt")  # outside the repository
    (tmp_path / "prompts").mkdir()
    custom.rename(tmp_path / "prompts" / "v2.txt")
    monkeypatch.setattr(publish, "ROOT", tmp_path)  # a prompt inside the repository
    inside = publish.load_prompt(str(tmp_path / "prompts" / "v2.txt"))
    assert inside.path == "prompts/v2.txt" and inside.sha256 == loaded.sha256
    custom.write_text("Tidy {space}.", encoding="utf-8")
    with pytest.raises(ValueError, match="champs inconnus dans l.invite : space"):
        publish.load_prompt(str(custom))
    custom.write_text("A brace { alone", encoding="utf-8")
    with pytest.raises(ValueError, match="accolades"):
        publish.load_prompt(str(custom))
    with publish.instructions(loaded):
        assert getattr(tidy, "INSTRUCTIONS") == loaded.template  # noqa: B009
    assert getattr(tidy, "INSTRUCTIONS") == INSTRUCTIONS  # noqa: B009


def test_fingerprints_are_stable_and_follow_the_data() -> None:
    first = {s.name: publish.scenario_fingerprint(s) for s in SCENARIOS}
    assert first == {s.name: publish.scenario_fingerprint(s) for s in SCENARIOS}
    assert len(set(first.values())) == len(SCENARIOS)
    bench = publish.bench_fingerprint()
    assert set(bench["files"]) == set(publish.BENCH_FILES) and bench["wiki_pages"] == 50
    assert len(publish.tools_fingerprint()) == 64
    state = publish.source_state()
    assert set(state) == {"version", "commit", "dirty"}


def test_run_file_names_never_overwrite(tmp_path: Path) -> None:
    first = publish.run_path("claude-sonnet-5-5", "medium", "", "2026-11-08", tmp_path)
    assert first.name == "2026-11-08-claude-sonnet-5-5-medium.json"
    first.write_text("{}")
    second = publish.run_path("claude-sonnet-5-5", "medium", "", "2026-11-08", tmp_path)
    assert second.name == "2026-11-08-claude-sonnet-5-5-medium-2.json"
    named = publish.run_path("openai/gpt-6.1-sol", "high", "My Prompt", "2026-11-08", tmp_path)
    assert named.name == "2026-11-08-openai-gpt-6.1-sol-high-my-prompt.json"
