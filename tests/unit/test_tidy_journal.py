"""Summarized tidying log: errors explained, retries grouped, next run."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from synaptane.ai.base import AIConfig, AIError
from synaptane.services import tidy_journal
from synaptane.services.tidy_journal import entries, explain, next_run, operation
from synaptane.storage.models import Space, TidyRun
from synaptane.web.tidy_pages import NEXT_LABELS

OLLAMA = AIConfig(provider="ollama", model="qwen3:8b", base_url="http://127.0.0.1:9/v1")
PARIS = ZoneInfo("Europe/Paris")
REFUSED = "Fournisseur injoignable : [Errno 111] Connection refused"


def test_a_raw_connection_error_becomes_an_address_to_check() -> None:
    found = explain(REFUSED, OLLAMA)
    assert found.code == "unreachable" and found.action == "test"
    assert found.params == {"address": "http://127.0.0.1:9/v1"}
    assert "{address}" in found.message and "Errno" not in found.message
    assert found.show_detail and found.detail == REFUSED  # kept for administrators
    assert "{address}" not in explain(REFUSED).message  # unknown address: message without it


def test_the_address_cited_by_the_error_wins_over_the_configuration() -> None:
    message = (
        "Impossible de joindre http://ollama:11434/v1 (connexion refusée ou adresse inconnue) : "
        "vérifiez l'adresse et que le service tourne."
    )
    assert explain(message, OLLAMA).params == {"address": "http://ollama:11434/v1"}


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("http://x/v1 ne répond pas à temps : le service est-il surchargé ?", "timeout"),
        ("Clé refusée par le fournisseur.", "auth"),
        ("Clé Anthropic refusée : invalid x-api-key", "auth"),
        ("Modèle introuvable (claude-x) : not found", "model"),
        ("Limite de débit Anthropic atteinte.", "rate_limit"),
        ("Fournisseur indisponible (503).", "unavailable"),
        ("Erreur Anthropic 529", "unavailable"),
        ("Anthropic injoignable.", "unreachable"),
        ("Réponse inattendue du fournisseur (pas au format OpenAI)", "bad_response"),
        ("Le modèle répond mais n'appelle pas les outils : il ne pourra pas ranger.", "no_tools"),
        ("Requête refusée (400) : bad request", "refused"),
        ("Le programme « claude » n'est pas installé sur le serveur.", "cli_missing"),
        ("Claude Code n'est pas connecté sur le serveur : lancez « … »", "cli_login"),
        ("Le passage a dépassé le temps maximal.", "run_timeout"),
        ("L'espace est occupé : passage reporté.", "busy"),
        ("Des fichiers ont été modifiés pendant le rangement : a.md.", "conflict"),
        ("Passage interrompu (worker arrêté pendant le rangement).", "interrupted"),
        ("Budget mensuel d'IA atteint : passage refusé.", "budget"),
        ("Erreur interne : KeyError: 'x'", "internal"),
    ],
)
def test_known_errors_get_a_code(error: str, code: str) -> None:
    assert explain(error, OLLAMA).code == code


def test_an_outdated_message_is_replaced_not_repeated() -> None:
    """The hint "(page de l'organisation)" pointed to a screen that no longer exists:
    Synaptane's message replaces the stored text, which is no longer shown."""
    found = explain(
        "Aucun fournisseur d'IA configuré pour l'organisation (page de l'organisation)."
    )
    assert found.code == "no_provider" and not found.show_detail
    assert "page de l'organisation" not in found.message


def test_an_unknown_error_is_shown_as_is() -> None:
    found = explain("Quelque chose d'imprévu")
    assert found.code == "other" and found.message == "{detail}"
    assert found.params == {"detail": "Quelque chose d'imprévu"} and not found.show_detail
    assert explain(None).code == "unknown" and explain("  ").code == "unknown"


def test_every_message_is_translated_to_french() -> None:
    locales = Path(tidy_journal.__file__).parents[1] / "web" / "locales" / "fr.json"
    catalog = json.loads(locales.read_text(encoding="utf-8"))
    wanted = set(tidy_journal.MESSAGES) - {"{detail}"} | set(NEXT_LABELS.values())
    assert sorted(wanted - catalog.keys()) == []


def test_operations_link_pages_and_list_where_an_item_went() -> None:
    filed = operation(
        {"op": "rangé", "path": "Nouveau NAS", "detail": "Ajouté → serveurs/nas.md, a b.md"}
    )
    assert filed.pages == ("serveurs/nas.md", "a b.md") and filed.detail == "Ajouté"
    assert not filed.linked  # title of an inbox item
    lost = operation({"op": "rangé", "path": "Doublon", "detail": "Déjà là — aucune page indiquée"})
    assert lost.no_page and lost.detail == "Déjà là" and lost.pages == ()
    reduced = operation({"op": "réduit", "path": "serveurs/nas.md", "detail": "−40 %"})
    assert reduced.linked and reduced.detail == "−40 %"
    assert not operation({"op": "supprimé", "path": "vieux.md"}).linked
    explicit = operation({"op": "rangé", "path": "X", "detail": "ok", "pages": ["p.md"]})
    assert explicit.pages == ("p.md",) and explicit.detail == "ok"


def _run(
    at: datetime, status: str = "failed", trigger: str = "schedule", error: str = REFUSED
) -> TidyRun:
    return TidyRun(
        trigger=trigger,
        status=status,
        started_at=at,
        error=error if status == "failed" else None,
        operations=[],
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
    )


def test_retries_of_the_same_night_are_one_entry() -> None:
    night = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)  # 02:00 in Paris
    runs = [
        _run(night + timedelta(minutes=73)),
        _run(night + timedelta(minutes=12)),
        _run(night + timedelta(minutes=2)),
        _run(night),
        _run(night - timedelta(hours=1), error="Clé refusée par le fournisseur."),  # autre cause
        _run(night - timedelta(hours=2), trigger="manual"),  # other trigger
        _run(night - timedelta(hours=3), status="done"),
        _run(night - timedelta(days=1)),  # autre nuit
    ]
    found = entries(runs, PARIS, OLLAMA)
    assert [len(e.attempts) for e in found] == [4, 1, 1, 1, 1]
    assert found[0].grouped and found[0].run is runs[0]
    assert found[0].explanation is not None and found[0].explanation.code == "unreachable"
    assert found[3].explanation is None  # passage abouti


def _space(**values: object) -> Space:
    defaults: dict[str, object] = {
        "tidy_enabled": True,
        "tidy_time": "02:00",
        "tidy_timezone": "Europe/Paris",
        "tidy_last_date": None,
        "repo_url": "https://git.example/infra.git",
    }
    return Space(**(defaults | values))


def test_next_run_is_tonight_at_the_configured_time() -> None:
    afternoon = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # 16:00 in Paris, done at 02:00
    upcoming = next_run(_space(tidy_last_date=date(2026, 10, 6)), afternoon)
    assert upcoming is not None and not upcoming.imminent
    assert upcoming.at == datetime(2026, 10, 7, 2, 0, tzinfo=PARIS)
    assert upcoming.day(afternoon) == "tonight"
    late = next_run(_space(tidy_time="20:30", tidy_last_date=date(2026, 10, 5)), afternoon)
    assert late is not None and late.day(afternoon) == "today"
    morning = next_run(_space(tidy_time="09:00", tidy_last_date=date(2026, 10, 6)), afternoon)
    assert morning is not None and morning.day(afternoon) == "tomorrow"


def test_next_run_is_imminent_when_due_and_absent_when_off() -> None:
    afternoon = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
    due = next_run(_space(tidy_last_date=date(2026, 10, 5)), afternoon)
    assert due is not None and due.imminent  # the scheduler starts it on the next round
    assert next_run(_space(tidy_enabled=False), afternoon) is None
    assert next_run(_space(repo_url=None), afternoon) is None


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AIError("Anthropic key refused: {detail}", detail="invalid x-api-key"), "auth"),
        (AIError("Key refused by the provider."), "auth"),
        (AIError("Model not found ({model}): {detail}", model="m", detail="?"), "model"),
        (AIError("Anthropic rate limit reached."), "rate_limit"),
        (AIError("Anthropic error {status}", status=529), "unavailable"),
        (AIError("Provider unavailable ({status}).", status=503), "unavailable"),
        (
            AIError("{address} does not answer in time: is the service overloaded?", address="x"),
            "timeout",
        ),
        (AIError("Claude Code does not answer (timed out)."), "timeout"),
        (
            AIError(
                "Cannot reach {address} (connection refused or unknown address): check the "
                "address and that the service is running.",
                address="http://h",
            ),
            "unreachable",
        ),
        (
            AIError(
                "Unexpected response from the provider (not in the OpenAI format): check the "
                "API address and the model."
            ),
            "bad_response",
        ),
        (AIError("The model answers but does not call the tools: it cannot tidy."), "no_tools"),
        (AIError("The CLI answers but does not call the tools of Synaptane."), "no_tools"),
        (
            AIError("The program “{name}” is not installed on the server.", name="claude"),
            "cli_missing",
        ),
        (
            AIError(
                "Claude Code is not logged in on the server: run “{command}” (steps on this page).",
                command="c",
            ),
            "cli_login",
        ),
        (AIError("The run exceeded the maximum time."), "run_timeout"),
    ],
)
def test_the_providers_errors_are_recognized(error: AIError, code: str) -> None:
    """The errors are recorded in English now (French before: see the other tests)."""
    assert explain(str(error)).code == code
