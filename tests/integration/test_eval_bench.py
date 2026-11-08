"""The evaluation bench tests itself, with the simulated model only.

For each scenario, the good script must pass all graders and the bad one must be caught by
the intended grader: a grader that no longer sees anything, or a scenario that became
impossible, fails here. Search is measured on the bench's wiki, without and with simulated
embeddings; the thresholds are floors against regressions, not goals. The bench with a real
model never runs in CI (`python -m tests.eval`, docs/dev/eval.md).
"""

import json
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

FAKE_KEY = "sk-ant-api03-FAKE-KEY-FOR-THE-BENCH-TEST-0000"


def _real_args(tmp_path: Path, *extra: str) -> Any:
    return eval_main.parser().parse_args(
        [
            "tidy",
            "--model",
            "claude-sonnet-5-5",
            "--api-key-env",
            "SYNAPTANE_BENCH_TEST_KEY",
            "--json",
            str(tmp_path / "run.json"),
            *extra,
        ]
    )


def _fake(
    plan: dict[str, list[str]], systems: list[str] | None = None
) -> Callable[[Scenario], Callable[[dict[str, str]], Any]]:
    """Fake provider: for each scenario, the good or bad script, trial after trial ("panne":
    the provider fails and quotes the key). Records the system prompt it was given."""
    played: dict[str, int] = {}

    def conversation(scenario: Scenario) -> Callable[[dict[str, str]], Any]:
        n = played.get(scenario.name, 0)
        played[scenario.name] = n + 1
        which = plan[scenario.name][n]

        def factory(names: dict[str, str]) -> Any:
            if which == "panne":

                def broken(*_args: Any) -> Any:
                    raise AIError("Anthropic key refused: {detail}", detail=f"bad key {FAKE_KEY}")

                return broken
            fake = Scripted((scenario.good if which == "bon" else scenario.bad)(names))

            def record(config: AIConfig, system: str, first: str, tools: list[Any]) -> Any:
                if systems is not None:
                    systems.append(system)
                return fake.factory(config, system, first, tools)

            return record

        return factory

    return conversation


def test_real_mode_writes_a_publishable_run_and_report(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPTANE_BENCH_TEST_KEY", FAKE_KEY)
    chosen = [s for s in SCENARIOS if s.name in ("completer", "ambigue")]
    plan = {"completer": ["bon", "mauvais", "bon"], "ambigue": ["bon", "bon", "panne"]}
    args = _real_args(tmp_path, "--budget", "5", "--trials", "3", "--label", "fake")
    assert eval_main._real(env, chosen, args, _fake(plan)) == 0

    text = (tmp_path / "run.json").read_text()
    assert FAKE_KEY not in text and "/tmp/" not in text and "/home/" not in text
    run = json.loads(text)
    assert (run["format"], run["format_version"]) == (publish.RUN_FORMAT, 1)
    assert run["complete"] and not run["stopped_by_budget"] and run["trials_requested"] == 3
    assert [s["name"] for s in run["scenarios"]] == ["completer", "ambigue"]
    assert run["config"] | {"tools_sha256": ""} == {
        "label": "fake",
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "effort": "medium",
        "base_url": "default",
        "prompt": publish.load_prompt().as_json(),
        "tools_sha256": "",
    }
    assert run["price"]["public"] and run["price"]["currency"] == "USD"
    assert run["synaptane"]["commit"] and run["bench"]["wiki_pages"] == 50
    # Sweeps one after the other: trial 1 of every scenario, then trial 2…
    order = [(t["trial"], t["scenario"]) for t in run["trials"]]
    assert order == [(n, s) for n in (1, 2, 3) for s in ("completer", "ambigue")]
    assert [t["passed"] for t in run["trials"]] == [True, True, False, True, True, False]
    assert chosen[0].bad_fails in run["trials"][2]["failures"]  # the bad script, caught
    assert "[secret]" in run["trials"][5]["failures"]["échec"]
    assert all(t["cost"] > 0 and t["duration_s"] > 0 for t in run["trials"][:5])

    out = tmp_path / "results"
    written = publish.write_report([tmp_path / "run.json"], out, allow_dirty=True)
    assert [p.name for p in written] == ["results.json", "results.md", "results.fr.md"]
    report = json.loads((out / "results.json").read_text())
    (config,) = report["configurations"]
    assert (config["label"], config["passed"], config["total"]) == ("fake", 4, 6)
    assert config["scenarios"]["ambigue"]["failures"] == {"échec": 1}
    assert "| fake | 4/6 |" in (out / "results.md").read_text()
    assert "| `completer` | 2/3 |" in (out / "results.fr.md").read_text()


def test_a_run_stopped_by_the_budget_is_not_published(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPTANE_BENCH_TEST_KEY", FAKE_KEY)
    chosen = [s for s in SCENARIOS if s.name in ("completer", "ambigue")]
    plan = {"completer": ["bon"] * 3, "ambigue": ["bon"] * 3}
    args = _real_args(tmp_path, "--budget", "0.001", "--trials", "3")
    assert eval_main._real(env, chosen, args, _fake(plan)) == 0
    run = json.loads((tmp_path / "run.json").read_text())
    assert run["stopped_by_budget"] and not run["complete"] and len(run["trials"]) < 6
    with pytest.raises(publish.ReportError, match="incomplet"):
        publish.write_report([tmp_path / "run.json"], tmp_path / "results", allow_dirty=True)


def test_another_prompt_is_used_and_recorded(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPTANE_BENCH_TEST_KEY", FAKE_KEY)
    prompt = tmp_path / "prompt-v2.txt"
    prompt.write_text("PROMPT-V2 for {name} ({address}).\n{language}{custom}", encoding="utf-8")
    systems: list[str] = []
    chosen = [s for s in SCENARIOS if s.name == "completer"]
    args = _real_args(tmp_path, "--budget", "5", "--prompt", str(prompt))
    assert eval_main._real(env, chosen, args, _fake({"completer": ["bon"]}, systems)) == 0
    assert systems and systems[0].startswith("PROMPT-V2 for completer")
    run = json.loads((tmp_path / "run.json").read_text())
    assert run["config"]["prompt"] == {
        "source": "file",
        "path": "prompt-v2.txt",
        "sha256": publish.sha256(prompt.read_text(encoding="utf-8")),
    }
    # Put back afterwards.
    assert getattr(tidy_module, "INSTRUCTIONS") == publish.load_prompt().template  # noqa: B009
    # A prompt with an unknown field is refused before anything runs.
    prompt.write_text("Tidy {space}.", encoding="utf-8")
    assert eval_main._real(env, chosen, args, _fake({"completer": ["bon"]})) == 2


def test_estimate_counts_the_ideal_path_without_calling_anything(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    chosen = [s for s in SCENARIOS if s.name in ("completer", "doublon")]
    args = eval_main.parser().parse_args(
        ["tidy", "--estimate", "--model", "claude-sonnet-5-5", "--trials", "3"]
    )
    assert eval_main._estimate(env, chosen, args) == 0
    shown = capsys.readouterr().out
    assert "completer" in shown and "doublon" in shown and "--budget" in shown
    price = pricing.price_for("anthropic", "claude-sonnet-5-5", None, None)
    result = measured(env, chosen[0], price)
    usage = result.usage
    # The whole conversation is input, read back from the cache but for the new part.
    assert usage.turns >= 2 and usage.cached_tokens > 0 and usage.input_tokens > 1000
    assert usage.cache_write_tokens == usage.input_tokens and result.cost and result.cost > 0
    unknown = eval_main.parser().parse_args(["tidy", "--estimate", "--model", "mystery-model"])
    assert eval_main._estimate(env, chosen, unknown) == 2
