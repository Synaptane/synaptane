"""The evaluation bench tests itself, with the simulated model only.

For each scenario, the good script must pass all graders and the bad one must be caught by
the intended grader: a grader that no longer sees anything, or a scenario that became
impossible, fails here. Search is measured on the bench's wiki, without and with simulated
embeddings; the thresholds are floors against regressions, not goals. The bench with a real
model never runs in CI (`python -m tests.eval`, docs/dev/eval.md).
"""

from pathlib import Path

import pytest

from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.eval import search_bench
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
