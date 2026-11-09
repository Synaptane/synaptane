"""Nightly maintenance: what the agent says of each problem, and the log in French."""

from typing import Any

from synaptane.services import maintenance
from synaptane.services.maintenance import Problem
from synaptane.web.i18n import translator


def test_reasons_are_read_per_problem_number() -> None:
    summary = (
        "Liens réparés. P2: aucune page sur ce sujet; P10 — à trancher par l'équipe\n"
        "- p3) page de brouillon; P: sans numéro; P4 sans séparateur; P5:"
    )
    assert maintenance.reasons(summary) == {
        2: "aucune page sur ce sujet",
        10: "à trancher par l'équipe",
        3: "page de brouillon",
    }
    assert maintenance.reasons("") == {}
    assert maintenance.reasons("P1: " + "x" * 1000)[1] == "x" * 300


def test_problems_in_words_and_their_translation() -> None:
    french: Any = translator("fr")
    link = Problem("wiki", "broken_link", "a.md", "b.md", "../b.md")
    assert link.describe() == "broken link b.md (did you mean ../b.md?)"
    assert maintenance.localize(link.describe() + " — à voir", french) == (
        "lien cassé b.md (vouliez-vous dire ../b.md\u202f?) — à voir"
    )
    index = Problem("wiki", "not_in_index", "infra/a.md", "infra/README.md")
    assert maintenance.localize(index.describe(), french) == (
        "absente de l'index du dossier infra/README.md"
    )
    assert maintenance.localize("Wiki maintenance (3)", french) == "Entretien du wiki (3)"
    assert maintenance.localize("empty page — vide exprès", french) == "page vide — vide exprès"
    assert maintenance.localize("Some note title", french) == "Some note title"
    assert Problem("team", "custom", "a.md", "dates out of order").describe() == (
        "dates out of order"
    )
