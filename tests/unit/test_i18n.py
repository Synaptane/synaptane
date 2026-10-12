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


def template_calls() -> set[tuple[str, str]]:
    """Literal texts of the templates passed to `_()` (first argument, with its `context`) or
    to `pl()` (the two forms to translate, without context): (text, context) pairs."""
    found: set[tuple[str, str]] = set()
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
                context = ""
                following = [value for _, value in tokens[index + 3 : index + 7]]
                if following[:3] == [",", "context", "="]:
                    context = ast.literal_eval(following[3])
                found.add((ast.literal_eval(raw), context))
            elif name == "pl":  # pl(count, "one", "many"): the arguments after a comma
                depth, previous = 1, ""
                for kind, raw in tokens[index + 2 :]:
                    depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(raw, 0)
                    if depth == 0:
                        break
                    if kind == TOKEN_STRING and depth == 1 and previous == ",":
                        found.add((ast.literal_eval(raw), ""))
                    previous = raw
    return {(text, context) for text, context in found if worded(text)}


def translated(text: str, context: str = "") -> bool:
    """The catalog has an entry for this call: `"X|context"` or `"X"` with a context (the
    translator falls back to it), only `"X"` without one."""
    return (bool(context) and f"{text}|{context}" in CATALOG) or text in CATALOG


def python_calls() -> dict[tuple[str, str], str]:
    """Literal texts of the code passed to an interface function, with their `context`:
    (text, context) → where."""
    found: dict[tuple[str, str], str] = {}
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
            context = next(
                (
                    k.value.value
                    for k in node.keywords
                    if k.arg == "context" and isinstance(k.value, ast.Constant)
                ),
                "",
            )
            if isinstance(text, str) and worded(text):
                found[(text, str(context))] = f"{path.relative_to(SOURCE)}:{node.lineno}"
    return found


def test_every_template_string_is_translated_to_french() -> None:
    missing = sorted(call for call in template_calls() if not translated(*call))
    assert missing == []


def test_every_interface_string_of_the_code_is_translated_to_french() -> None:
    missing = sorted(
        f"{where}: {text}"
        for (text, context), where in python_calls().items()
        if not translated(text, context)
    )
    assert missing == []


def test_a_text_without_context_needs_its_own_entry() -> None:
    """`_("X")` is not translated by the `"X|context"` entry alone: it falls back to English."""
    assert translated("Search", "noun") and not translated("Unlisted", "noun")
    contextual = {key.rsplit("|", 1)[0] for key in CATALOG if "|" in key}
    plain = {text for text, context in template_calls() if not context}
    plain |= {text for text, context in python_calls() if not context}
    assert sorted(text for text in plain & contextual if not translated(text)) == []


# Translations identical to their English text, on purpose: names (products, providers) and
# words or abbreviations written the same way in French.
SAME_IN_FRENCH = {
    # Names
    "Anthropic (Claude)",
    "ChatGPT",
    "Claude Code",
    "Google Gemini",
    "Mistral",
    "Ollama",
    "OpenAI",
    "Synaptane",
    "Voyage AI",
    # Same word, or a term kept in French
    "5 h",
    "Actions",
    "Administration",
    "Application",
    "application",
    "Client",
    "Inbox",
    "Invitation",
    "Note",
    "Notes",
    "{n} note",
    "{n} notes",
    "Options",
    "Page",
    "Pages",
    "Port",
    "Routines",
    "Secret",
    "SSO",
    "Suggestion",
    "Type",
    "URL",
    "Webhook",
    "Webhooks",
}


def test_catalog_entries_are_well_formed() -> None:
    """No empty translation, two forms for a plural entry, and a real translation (not the
    English text copied), apart from words that are the same in French."""
    problems = []
    for key, value in CATALOG.items():
        text = key.rsplit("|", 1)[0]
        forms = value if isinstance(value, list) else [value]
        if isinstance(value, list) and (
            len(value) != 2 or not all(isinstance(form, str) for form in value)
        ):
            problems.append(f"plural entry with {len(value)} forms: {key}")
        if any(not form.strip() for form in forms):
            problems.append(f"empty translation: {key}")
        copied = not isinstance(value, list) and value == text and worded(text)
        if copied and text not in SAME_IN_FRENCH:
            problems.append(f"translation identical to the English text: {key}")
    # Each exception is still needed: an entry of the catalog, translated by itself.
    problems += [
        f"needless exception: {text}" for text in SAME_IN_FRENCH if CATALOG.get(text) != text
    ]
    assert problems == []


# French words that should not appear in a literal attribute of a template (the attribute must
# go through `_()`).
_FRENCH_WORDS = re.compile(
    r"[àâçéèêëîïôûùüÿœ]|\b(?:le|la|les|des|du|un|une|et|ou|pour|avec|dans|sur|par|vous|votre|"
    r"vos|nouveau|nouvelle|rechercher|ajouter|fermer|ouvrir|supprimer|modifier|enregistrer|"
    r"annuler|dossier|espace|fichier|serveurs?|rapports?|ma|mon|mes)\b",
    re.IGNORECASE,
)
_ATTRIBUTE = re.compile(r'\b(placeholder|title|aria-label|alt)="([^"]*)"')


def test_template_attributes_are_not_left_in_french() -> None:
    """A user-visible attribute written in French in a template: shown in French to everyone."""
    assert _FRENCH_WORDS.search("vous@example.com") and _FRENCH_WORDS.search("Résumé")
    assert not _FRENCH_WORDS.search("you@example.com") and not _FRENCH_WORDS.search("my-page.md")
    found = []
    for template in sorted((WEB / "templates").glob("*.html")):
        for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
            literal = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", line)  # Jinja: checked elsewhere
            for attribute, value in _ATTRIBUTE.findall(literal):
                if _FRENCH_WORDS.search(value):
                    found.append(f"{template.name}:{number}: {attribute}={value!r}")
    assert found == []


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
    assert french("{n} left by the tidy-up", n=1) == "1 laissé par le rangement"
    assert french("{n} left by the tidy-up", n=3) == "3 laissés par le rangement"
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
    missing = sorted({text for text in labels if worded(text)} - set(CATALOG))
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
