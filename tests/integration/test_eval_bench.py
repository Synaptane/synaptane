"""The evaluation bench tests itself, with the simulated model only.

For each scenario, the good script must pass all graders and the bad one must be caught by
the intended grader: a grader that no longer sees anything, or a scenario that became
impossible, fails here. Search is measured on the bench's wiki, without and with simulated
embeddings; the thresholds are floors against regressions, not goals. The bench with a real
model never runs in CI (`python -m tests.eval`, docs/dev/eval.md).
"""

import json
from pathlib import Path

import pytest

from synaptane.config import Settings
from synaptane.services import content
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.eval import mcp_bench, search_bench
from tests.eval.env import Env, prepare
from tests.eval.scenarios import SCENARIOS
from tests.eval.tidy_bench import Scenario, scripted


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
