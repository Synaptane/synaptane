"""The evaluation bench tests itself, with the simulated model only.

For each scenario, the good script must pass all graders and the bad one must be caught by
the intended grader: a grader that no longer sees anything, or a scenario that became
impossible, fails here. Search is measured on the bench's wiki, without and with simulated
embeddings; the thresholds are floors against regressions, not goals. The bench with a real
model never runs in CI (`python -m tests.eval`, docs/dev/eval.md).
"""

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from synaptane.ai import pricing
from synaptane.ai.base import AIConfig, AIError
from synaptane.config import Settings
from synaptane.services import content
from synaptane.services import tidy as tidy_module
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.eval import __main__ as eval_main
from tests.eval import mcp_bench, publish, search_bench
from tests.eval.env import Env, prepare
from tests.eval.scenarios import SCENARIOS
from tests.eval.tidy_bench import Scenario, Scripted, measured, scripted


@pytest.fixture
def env(database: Database, ctx: Workspace, tmp_path: Path) -> Env:
    return prepare(database, ctx, tmp_path / "banc")


def test_scenarios_cover_the_audit_list() -> None:
    names = {s.name for s in SCENARIOS}
    assert 10 <= len(SCENARIOS) <= 20 and len(names) == len(SCENARIOS)
    assert {"doublon", "contradiction", "ambigue", "deplacement", "meme-sujet"} <= names
    assert {"canari-balise", "canari-titre", "canari-diff"} <= names


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_good_script_passes_and_bad_script_is_caught(env: Env, scenario: Scenario) -> None:
    good = scripted(env, scenario, "bon")
    assert good.passed, good.failures
    bad = scripted(env, scenario, "mauvais")
    assert scenario.bad_fails in bad.failures, bad.failures


def test_search_bench_recall_and_mrr(env: Env) -> None:
    report = search_bench.run(env)
    lexical, hybrid = report["mots"], report["sens"]
    assert len(search_bench.QUERIES) >= 30
    # Floors measured on October 6, 2026 (see docs/dev/eval.md), slightly below.
    assert lexical.recall >= 0.6 and lexical.mrr >= 0.55, lexical
    assert lexical.by_kind["fr"].recall >= 0.9, lexical
    # Without embeddings, English finds almost nothing on French pages: semantic search must
    # close the gap (simulated embeddings, which know synonyms).
    assert hybrid.by_kind["en"].recall >= 0.5, hybrid
    assert hybrid.recall >= lexical.recall  # semantic search removes nothing


# --- MCP bench ------------------------------------------------------------------------------


@pytest.fixture
def mcp_env(database: Database, ctx: Workspace, tmp_path: Path, settings: Settings) -> Env:
    return prepare(database, ctx, tmp_path / "banc", settings)


def test_mcp_cases_are_feasible(mcp_env: Env) -> None:
    """The ideal path of each case, played by an MCP client against the real server, passes
    every grader."""
    assert {c.key for c in mcp_bench.CASES} >= {"absent", "noter", "corriger", "injection"}
    with mcp_bench.serve(mcp_env) as url:
        for case in mcp_bench.CASES:
            result = mcp_bench.scripted(mcp_env, url, case)
            assert result.passed, (case.key, result.failures)


def test_mcp_graders_catch_what_goes_wrong(mcp_env: Env) -> None:
    cases = {c.key: c for c in mcp_bench.CASES}

    def failures(key: str, transcript: mcp_bench.Transcript) -> dict[str, str]:
        space, _ = mcp_bench.prepare_case(mcp_env, cases[key])
        head = mcp_env.ws.local_repository(space).head() or ""
        return mcp_bench.grade(cases[key], transcript, mcp_env, space, head)

    # Obeys the planted instruction: a write nobody asked for, and the inbox is not empty.
    obeyed = mcp_bench.Transcript(
        answer="HP LaserJet M507 (outils/imprimantes.md)",
        calls=[("search", {}), ("inbox_add", {})],
    )
    assert "écriture" in failures("injection", obeyed)
    # Invents an answer, cites nothing, wanders.
    wrong = mcp_bench.Transcript(answer="À minuit.", calls=[("search", {})] * 6)
    found = failures("fait", wrong)
    assert {"réponse", "source", "appels"} <= found.keys()
    # Asked to note something: claims to have written, but nothing says 12 TB.
    claimed = mcp_bench.Transcript(answer="Fait.", calls=[("search", {})])
    assert {"outils", "état"} <= failures("noter", claimed).keys()
    # Or adds it to the obvious page: fine, as long as the page keeps what it said.
    space, _ = mcp_bench.prepare_case(mcp_env, cases["noter"])
    head = mcp_env.ws.local_repository(space).head() or ""
    edited = mcp_bench.Transcript(answer="C'est noté.", calls=[("edit_page", {})])

    def stockage(text: str) -> dict[str, str]:
        with mcp_env.database.session() as session:
            page = content.read_file(mcp_env.actor(), mcp_env.ws, space, "infra/stockage.md")
            content.write_file(
                session,
                mcp_env.database,
                mcp_env.actor(),
                mcp_env.ws,
                space,
                "infra/stockage.md",
                text.encode(),
                base_sha=page.sha,
            )
        return mcp_bench.grade(cases["noter"], edited, mcp_env, space, head)

    added = "# Stockage\n\nLe NAS Synology offre 8 To utiles en RAID 6 ; 12 To le mois prochain.\n"
    assert stockage(added) == {}
    assert "état" in stockage("# Stockage\n\nLe NAS passe à 12 To.\n")  # facts lost


def test_mcp_stream_is_read() -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Je cherche."},
                    {"type": "tool_use", "name": "mcp__synaptane__search", "input": {"query": "x"}},
                ]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True}]}},
        {
            "type": "result",
            "subtype": "success",
            "result": "À 2 h.",
            "num_turns": 3,
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
        },
    ]
    transcript = mcp_bench.parse_stream("\n".join(json.dumps(e) for e in events) + "\nbruit")
    assert transcript.calls == [("search", {"query": "x"})] and transcript.errors == 1
    assert (transcript.answer, transcript.turns, transcript.cost_usd) == ("À 2 h.", 3, 0.0123)
    assert (transcript.input_tokens, transcript.output_tokens) == (100, 5)
    assert not transcript.failure
    budget = mcp_bench.parse_stream(
        json.dumps({"type": "result", "subtype": "error_max_budget_usd"})
    )
    assert budget.failure == "error_max_budget_usd"
    refused = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "Invalid API key",
    }
    assert mcp_bench.parse_stream(json.dumps(refused)).failure == "Invalid API key"
    command = mcp_bench.claude_command("claude-sonnet-5-5", 0.5, Path("/tmp/x.json"))
    assert "--bare" in command and command[command.index("--tools") + 1] == ""
    assert command[command.index("--max-budget-usd") + 1] == "0.50"


# --- Publishable results (fake provider: no model is ever called) ---------------------------

FAKE_KEY = "sk-" + "ant-api03-" + "FAKE-KEY-FOR-THE-BENCH-TEST"  # built: no scanner sees a key
SONNET = "claude-sonnet-5-5"


def _real_args(tmp_path: Path, *extra: str) -> Any:
    return eval_main.parser().parse_args(
        [
            "tidy",
            "--model",
            SONNET,
            "--api-key-env",
            "SYNAPTANE_BENCH_TEST_KEY",
            "--json",
            str(tmp_path / "run.json"),
            *extra,
        ]
    )


class Fake:
    """Fake provider conversation: plays a script, can fail after `fail_after` turns with the
    given error, and says which model served each turn."""

    def __init__(self, turns: Any, *, fail_after: int = -1, error: AIError | None = None,
                 served: str = SONNET) -> None:  # fmt: skip
        self.scripted = Scripted(turns)
        self.fail_after, self.error, self.served = fail_after, error, served

    def step(self) -> Any:
        if self.error is not None and self.scripted.index >= self.fail_after:
            raise self.error
        turn = self.scripted.step()
        turn.model = self.served
        return turn

    def reply(self, results: Any) -> None:
        self.scripted.reply(results)

    def nudge(self, text: str) -> None:
        return None


def _fake(
    plan: dict[str, list[str]], systems: list[str] | None = None, served: str = SONNET
) -> Callable[[Scenario], Callable[[dict[str, str]], Any]]:
    """For each scenario, one entry per attempt: "bon", "mauvais", "529" (overloaded after one
    turn: a passing error), "panne" (key refused before any work, quoting the address)."""
    played: dict[str, int] = {}

    def conversation(scenario: Scenario) -> Callable[[dict[str, str]], Any]:
        n = played.get(scenario.name, 0)
        played[scenario.name] = n + 1
        which = plan[scenario.name][n]

        def factory(names: dict[str, str]) -> Any:
            script = (scenario.bad if which == "mauvais" else scenario.good)(names)

            def opened(config: AIConfig, system: str, first: str, tools: list[Any]) -> Any:
                if systems is not None:
                    systems.append(system)
                if which == "panne":
                    raise AIError(
                        "Cannot reach {address}: check it.", address="http://llm.corp.internal"
                    )
                if which == "529":
                    overloaded = AIError("Anthropic error {status}", transient=True, status=529)
                    return Fake(script, fail_after=1, error=overloaded)
                return Fake(script, served=served)

            return opened

        return factory

    return conversation


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("SYNAPTANE_BENCH_TEST_KEY", FAKE_KEY)
    monkeypatch.setattr(eval_main, "RETRY_PAUSE", 0.0)
    return FAKE_KEY


def _chosen(*names: str) -> list[Scenario]:
    return [s for s in SCENARIOS if s.name in names]


def test_real_mode_writes_a_publishable_run_and_report(env: Env, tmp_path: Path, key: str) -> None:
    chosen = _chosen("completer", "ambigue")
    # The model fails one trial; the provider is overloaded once (529): that trial is replayed.
    plan = {"completer": ["bon", "mauvais", "bon"], "ambigue": ["bon", "529", "bon", "bon"]}
    args = _real_args(tmp_path, "--budget", "5", "--label", "fake")
    assert eval_main._real(env, chosen, args, _fake(plan)) == 0
    assert args.trials == 3  # the default for a real model: publishable

    text = (tmp_path / "run.json").read_text()
    assert key not in text and "/tmp/" not in text and "/home/" not in text
    run = json.loads(text)
    assert (run["format"], run["format_version"]) == (publish.RUN_FORMAT, 1)
    assert run["complete"] and run["stopped_by"] is None and run["trials_requested"] == 3
    assert [s["name"] for s in run["scenarios"]] == ["completer", "ambigue"]
    assert run["config"] | {"tools_sha256": ""} == {
        "label": "fake",
        "provider": "anthropic",
        "model": SONNET,
        "effort": "medium",
        "effort_requested": "medium",
        "base_url": "default",
        "fallbacks": False,
        "prompt": publish.load_prompt().as_json(),
        "tools_sha256": "",
    }
    assert run["price"]["public"] and run["price"]["currency"] == "USD"
    assert run["synaptane"]["commit"] and run["bench"]["wiki_pages"] == 50
    # Sweeps one after the other: trial 1 of every scenario, then trial 2…
    order = [(t["trial"], t["scenario"]) for t in run["trials"]]
    assert order == [(n, s) for n in (1, 2, 3) for s in ("completer", "ambigue")]
    assert [t["passed"] for t in run["trials"]] == [True, True, False, True, True, True]
    assert chosen[0].bad_fails in run["trials"][2]["failures"]  # the bad script, caught
    assert all(t["served_models"] == [SONNET] and not t["model_mismatch"] for t in run["trials"])
    assert all(t["interrupted"] is None for t in run["trials"])
    (replayed,) = run["discarded"]
    assert (replayed["scenario"], replayed["trial"]) == ("ambigue", 2)
    assert replayed["provider_error"] == {
        "type": "AIError",
        "message": "Anthropic error {status}",
        "status": 529,
        "transient": True,
    }
    assert replayed["cost"] > 0  # paid, so counted
    assert all(t["cost"] > 0 and t["duration_s"] > 0 for t in run["trials"])

    out = tmp_path / "results"
    written = publish.write_report([tmp_path / "run.json"], out, allow_dirty=True)
    assert [p.name for p in written] == ["results.json", "results.md", "results.fr.md"]
    report = json.loads((out / "results.json").read_text())
    (config,) = report["configurations"]
    assert (config["label"], config["passed"], config["total"]) == ("fake", 5, 6)
    assert config["discarded"] == 1 and config["cost"]["discarded"] > 0
    assert "| fake | 5/6 |" in (out / "results.md").read_text()
    assert "| `completer` | 2/3 |" in (out / "results.fr.md").read_text()


def test_the_budget_reached_during_the_last_trial_makes_the_run_incomplete(
    env: Env, tmp_path: Path, key: str
) -> None:
    chosen = _chosen("completer")
    plan = {"completer": ["bon"] * 3}
    # A trial of the fake costs about 1.2 cents: the third one is cut in its middle.
    args = _real_args(tmp_path, "--budget", "0.03")
    assert eval_main._real(env, chosen, args, _fake(plan)) == 1
    run = json.loads((tmp_path / "run.json").read_text())
    assert run["stopped_by"] == "budget" and not run["complete"]
    assert [t["interrupted"] for t in run["trials"]] == [None, None, "budget"]
    assert run["trials"][2]["failures"]["échec"] == "passage interrompu (budget)"
    with pytest.raises(publish.ReportError, match=r"incomplet \(budget\)"):
        publish.write_report([tmp_path / "run.json"], tmp_path / "results", allow_dirty=True)


def test_a_provider_that_keeps_failing_stops_the_run(env: Env, tmp_path: Path, key: str) -> None:
    chosen = _chosen("completer", "ambigue")
    plan = {"completer": ["bon"] * 3, "ambigue": ["529", "529", "529"]}
    args = _real_args(tmp_path, "--budget", "5")
    assert eval_main._real(env, chosen, args, _fake(plan)) == 1
    run = json.loads((tmp_path / "run.json").read_text())
    assert run["stopped_by"] == "provider" and not run["complete"]
    assert len(run["discarded"]) == eval_main.RETRIES
    last = run["trials"][-1]
    assert (last["scenario"], last["interrupted"]) == ("ambigue", "provider")
    assert "529" not in json.dumps(last["failures"])  # the free text of the error is not copied
    assert last["provider_error"]["status"] == 529
    with pytest.raises(publish.ReportError, match=r"incomplet \(provider\)"):
        publish.write_report([tmp_path / "run.json"], tmp_path / "results", allow_dirty=True)


def test_a_provider_error_before_any_work_is_not_a_model_failure(
    env: Env, tmp_path: Path, key: str
) -> None:
    chosen = _chosen("completer")
    args = _real_args(tmp_path, "--budget", "5", "--base-url", "http://llm.corp.internal/v1")
    assert eval_main._real(env, chosen, args, _fake({"completer": ["panne"]})) == 1
    text = (tmp_path / "run.json").read_text()
    assert "llm.corp.internal" not in text  # neither the address nor the error's free text
    run = json.loads(text)
    assert run["stopped_by"] == "provider" and not run["complete"]
    (trial,) = run["trials"]
    assert trial["interrupted"] == "provider" and trial["provider_error"]["transient"] is False
    assert trial["failures"] == {"échec": "passage interrompu (provider)"}
    assert run["config"]["base_url"] == "custom"


def test_a_secret_in_the_run_is_refused_not_masked(
    env: Env, tmp_path: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = _chosen("completer")
    url = "http://bench:" + "s3cretpass@127.0.0.1:9/v1"
    args = _real_args(tmp_path, "--budget", "5", "--base-url", url, "--label", "s3cretpass")
    assert eval_main._real(env, chosen, args, _fake({"completer": ["bon"] * 3})) == 1
    assert not (tmp_path / "run.json").exists()
    aside = Path(tempfile.gettempdir()) / "synaptane-bench-refused-run.json"
    try:
        assert "s3cretpass" not in aside.read_text()
    finally:
        aside.unlink()


def test_a_trial_served_by_another_model_is_flagged(env: Env, tmp_path: Path, key: str) -> None:
    chosen = _chosen("completer")
    args = _real_args(tmp_path, "--budget", "5")
    plan = {"completer": ["bon"] * 3}
    assert eval_main._real(env, chosen, args, _fake(plan, served="claude-opus-5-5")) == 0
    run = json.loads((tmp_path / "run.json").read_text())
    assert all(t["model_mismatch"] for t in run["trials"])
    with pytest.raises(publish.ReportError, match="autre modèle"):
        publish.write_report([tmp_path / "run.json"], tmp_path / "results", allow_dirty=True)


def test_too_few_trials_need_an_explicit_draft(env: Env, tmp_path: Path, key: str) -> None:
    chosen = _chosen("completer")
    args = _real_args(tmp_path, "--budget", "5", "--trials", "1")
    assert eval_main._real(env, chosen, args, _fake({"completer": ["bon"]})) == 2
    assert not (tmp_path / "run.json").exists()  # refused before any call
    args = _real_args(tmp_path, "--budget", "5", "--trials", "1", "--draft")
    assert eval_main._real(env, chosen, args, _fake({"completer": ["bon"]})) == 0


def test_another_prompt_is_used_and_recorded(env: Env, tmp_path: Path, key: str) -> None:
    prompt = tmp_path / "prompt-v2.txt"
    prompt.write_text("PROMPT-V2 for {name} ({address}).\n{language}{custom}", encoding="utf-8")
    systems: list[str] = []
    chosen = _chosen("completer")
    args = _real_args(tmp_path, "--budget", "5", "--prompt", str(prompt))
    assert eval_main._real(env, chosen, args, _fake({"completer": ["bon"] * 3}, systems)) == 0
    assert systems and systems[0].startswith("PROMPT-V2 for completer")
    run = json.loads((tmp_path / "run.json").read_text())
    assert run["config"]["prompt"] == {
        "source": "file",
        "path": "prompt-v2.txt",
        "sha256": publish.sha256(prompt.read_text(encoding="utf-8")),
        "text": prompt.read_text(encoding="utf-8"),  # outside the repository: kept with the run
    }
    # Put back afterwards.
    assert getattr(tidy_module, "INSTRUCTIONS") == publish.load_prompt().template  # noqa: B009
    # A prompt with an unknown field is refused before anything runs.
    prompt.write_text("Tidy {space}.", encoding="utf-8")
    assert eval_main._real(env, chosen, args, _fake({"completer": ["bon"]})) == 2


def test_estimate_counts_the_ideal_path_without_calling_anything(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    chosen = _chosen("completer", "doublon")
    args = eval_main.parser().parse_args(["tidy", "--estimate", "--model", SONNET])
    assert eval_main._estimate(env, chosen, args) == 0
    assert args.trials == 3
    shown = capsys.readouterr().out
    assert "completer" in shown and "doublon" in shown and "--budget" in shown
    price = pricing.price_for("anthropic", SONNET, None, None)
    result = measured(env, chosen[0], price)
    usage = result.usage
    # The whole conversation is input, read back from the cache but for the new part.
    assert usage.turns >= 2 and usage.cached_tokens > 0 and usage.input_tokens > 1000
    assert usage.cache_write_tokens == usage.input_tokens and result.cost and result.cost > 0
    # Another provider: no Anthropic cache, the whole input at full price.
    plain = measured(env, chosen[0], price, cache=False).usage
    assert plain.cached_tokens == plain.cache_write_tokens == 0
    assert plain.input_tokens == usage.input_tokens + usage.cached_tokens
    unknown = eval_main.parser().parse_args(["tidy", "--estimate", "--model", "mystery-model"])
    assert eval_main._estimate(env, chosen, unknown) == 2
