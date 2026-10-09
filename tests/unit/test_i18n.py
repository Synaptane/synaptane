"""Interface translation: English source texts, French catalog (`locales/fr.json`)."""

import ast
import json
import re
import string
from pathlib import Path
from typing import cast

from jinja2 import Environment
from jinja2.lexer import TOKEN_NAME, TOKEN_STRING

from synaptane.core.errors import Label, invalid
from synaptane.web.i18n import negotiate, translator

SOURCE = Path(__file__).parents[2] / "src" / "synaptane"
WEB = SOURCE / "web"
CATALOG: dict[str, str | list[str]] = json.loads(
    (WEB / "locales" / "fr.json").read_text(encoding="utf-8")
)
KEYS = set(CATALOG) | {key.rsplit("|", 1)[0] for key in CATALOG}

# Functions whose argument (at this position) is an interface text to translate.
INTERFACE_CALLS = {
    "_": 0,
    "translate": 0,
    "api_text": 1,
    "Label": 0,
    "AIError": 0,
    "SynaptaneError": 1,
    "invalid": 0,
    "forbidden": 0,
    "not_found": 0,
    "conflict": 0,
    "unavailable": 0,
    "too_many": 0,
    "redirect": 2,
    "linked_redirect": 2,
    "error_page": 2,
    "reveal": 3,
    "sign_in": 3,
}


def worded(text: str) -> bool:
    """A text with words, not only values and punctuation ("{kind} ({place})")."""
    return bool(re.search(r"[^\W\d_]", re.sub(r"\{[^}]*\}", "", text)))


def template_strings() -> set[str]:
    """Literal texts of the templates passed to `_()` (first argument) or to `pl()` (the two
    forms to translate)."""
    found: set[str] = set()
    lexer = Environment().lexer
    for template in (WEB / "templates").glob("*.html"):
        tokens = [
            (kind, raw)
            for _, kind, raw in lexer.tokeniter(template.read_text(encoding="utf-8"), "t")
            if kind != "whitespace"
        ]
        for index in range(len(tokens) - 2):
            (kind, name), (_, paren), (next_kind, raw) = tokens[index : index + 3]
            if kind != TOKEN_NAME or paren != "(":
                continue
            if name == "_" and next_kind == TOKEN_STRING:
                found.add(ast.literal_eval(raw))
            elif name == "pl":  # pl(count, "one", "many"): the arguments after a comma
                depth, previous = 1, ""
                for kind, raw in tokens[index + 2 :]:
                    depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(raw, 0)
                    if depth == 0:
                        break
                    if kind == TOKEN_STRING and depth == 1 and previous == ",":
                        found.add(ast.literal_eval(raw))
                    previous = raw
    return {text for text in found if worded(text)}


def python_strings() -> dict[str, str]:
    """Literal texts of the code passed to an interface function: text → where."""
    found: dict[str, str] = {}
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            position = INTERFACE_CALLS.get(name)
            if position is None or len(node.args) <= position:
                continue
            argument = node.args[position]
            text = argument.value if isinstance(argument, ast.Constant) else None
            if isinstance(text, str) and worded(text):
                found[text] = f"{path.relative_to(SOURCE)}:{node.lineno}"
    return found


def test_every_template_string_is_translated_to_french() -> None:
    missing = sorted(template_strings() - KEYS)
    assert missing == []


def test_every_interface_string_of_the_code_is_translated_to_french() -> None:
    missing = sorted(f"{where}: {text}" for text, where in python_strings().items())
    missing = [line for line in missing if line.split(": ", 1)[1] not in KEYS]
    assert missing == []


def test_translations_keep_the_placeholders() -> None:
    def fields(text: str) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}

    for key, value in CATALOG.items():
        for form in value if isinstance(value, list) else [value]:
            assert fields(form) == fields(key.rsplit("|", 1)[0]), key


def test_translation_with_parameters_and_fallback() -> None:
    assert translator("fr")("Hello {name}", name="Ada") == "Bonjour Ada"
    assert translator("en")("Hello {name}", name="Ada") == "Hello Ada"
    # Unknown text: shown as it is, in both languages.
    assert translator("fr")("Unknown text") == "Unknown text"
    assert translator("en")("Unknown text") == "Unknown text"
    # French typography, for the interface texts only (not for the inserted values).
    assert translator("fr")("Page not found.") == "Page introuvable."
    assert translator("fr")("Invalid commit identifier: {sha}.", sha="a : b") == (
        "Identifiant de commit invalide : a : b."
    )


def test_context_and_invariant_plurals() -> None:
    french = translator("fr")
    # Same English text, translated according to where it appears.
    assert french("Enabled") == "Activé" and french("Enabled", context="routine") == "Activée"
    assert translator("en")("Enabled", context="routine") == "Enabled"
    # English invariant with the count, French not: the form follows `n` or `count`.
    assert french("{n} left by tidying", n=1) == "1 laissé par le rangement"
    assert french("{n} left by tidying", n=3) == "3 laissés par le rangement"
    assert french("and {count} more", count=2) == "et 2 autres"


def test_labels_are_translated_with_the_message() -> None:
    """A parameter that is itself an interface text, and a stored message (`Label.dumps`)."""
    error = invalid("{field}: between 1 and 120 characters.", field=Label("Title"))
    assert error.message == "Title: between 1 and 120 characters."
    assert translator("fr")(error.template, **error.params) == (
        "Titre : entre 1 et 120 caractères."
    )
    stored = Label("Connection successful ({model}, {tokens} tokens).", model="m", tokens=12)
    back = Label.loads(stored.dumps())
    assert back == stored and translator("fr")(back) == "Connexion réussie (m, 12 jetons)."
    assert Label.loads("Texte libre {x}") == "Texte libre {x}"  # stored before: as it is


def test_language_negotiation() -> None:
    assert negotiate("en-US,en;q=0.9", "fr") == "en"
    assert negotiate("de-DE,fr;q=0.5", "en") == "fr"  # first supported language
    # No supported language: the instance's default language.
    assert negotiate("de-DE", "en") == "en"
    assert negotiate("de-DE", "fr") == "fr"
    assert negotiate(None, "en") == "en"


def test_labels_computed_by_the_code_are_translated_to_french() -> None:
    """Texts kept in tables or built by the services, shown through the catalog."""
    from synaptane.ai import quota
    from synaptane.core import secret_scan
    from synaptane.git import repo
    from synaptane.services import changes, related, routines, tidy_journal, vocation
    from synaptane.web import pages, routine_pages, tidy_pages

    labels = [
        *pages.EVENT_LABELS.values(),
        *pages.OPERATION_LABELS.values(),
        *cast(dict[str, str], pages.templates.env.globals["provider_labels"]).values(),
        *cast(dict[str, str], pages.templates.env.globals["embedding_labels"]).values(),
        *(label for _, label in pages._SUBJECTS),
        *related.LABELS.values(),
        *quota.LABELS.values(),
        *tidy_pages.NEXT_LABELS.values(),
        *routine_pages.WEEKDAYS,
        *tidy_journal.MESSAGES,
        *changes._TITLES,
        *(message for _, message in repo.EXPLANATIONS),
        repo.LOCAL_REPOS_DISABLED,
        repo.REMOTE_TOO_FAST,
        *(kind for kind, _ in secret_scan._PATTERNS),
        *(text for t in routines.TEMPLATES.values() for text in (t.name, t.description)),
        *(text for t in vocation.TEMPLATES.values() for text in (t.label, t.summary)),
    ]
    missing = sorted({text for text in labels if worded(text)} - KEYS)
    assert missing == []


def test_every_french_entry_is_still_used() -> None:
    """No dead entry: each English text of `fr.json` appears in a template or in the code."""
    used: set[str] = set()
    lexer = Environment().lexer
    for template in (WEB / "templates").glob("*.html"):
        source = template.read_text(encoding="utf-8")
        for _, kind, raw in lexer.tokeniter(source, template.name):
            if kind == TOKEN_STRING:
                used.add(ast.literal_eval(raw))
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                used.add(node.value)
    assert sorted(key for key in CATALOG if key.rsplit("|", 1)[0] not in used) == []
