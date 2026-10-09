"""Interface translation. Source texts are in English; `locales/<language>.json` translates them
(`fr.json` for French).

A text missing from a catalog is shown in English, on purpose: nothing disappears.

A catalog maps an English text to its translation, with two refinements:

- `"Enabled|routine": "Activée"`: the same English text translated differently depending on
  where it appears (gender, noun or verb…), chosen with `_("Enabled", context="routine")`;
- `"{n} left by tidying": ["{n} laissé par le rangement", "{n} laissés par le rangement"]`:
  an English text that does not change with the count, unlike its translation; the form is
  chosen by the `n` (or `count`) value, with the language's rule (`plural`).
"""

import json
import re
from collections.abc import Callable
from functools import cache
from pathlib import Path

from synaptane.core.errors import Label

SOURCE = "en"  # language of the texts in the code
LOCALES = ("fr", "en")
_DIR = Path(__file__).parent / "locales"

Translate = Callable[..., str]
Entry = str | list[str]


@cache
def _catalog(locale: str) -> dict[str, Entry]:
    path = _DIR / f"{locale}.json"
    if locale == SOURCE or not path.exists():
        return {}
    data: dict[str, Entry] = json.loads(path.read_text(encoding="utf-8"))
    return data


_THIN = " "  # narrow no-break space
_NBSP = " "  # no-break space


def french_typography(text: str) -> str:
    """French no-break spaces: before "; ? ! :" and inside guillemets (« »). Applied to
    interface texts only (never to page content)."""
    text = re.sub(r" ([;?!])", _THIN + r"\1", text)
    text = re.sub(r" :(?=\s|$)", _NBSP + ":", text)
    return text.replace("« ", "«" + _THIN).replace(" »", _THIN + "»")


def translator(locale: str) -> Translate:
    catalog = _catalog(locale)

    def translate(text: str, context: str = "", **values: object) -> str:
        if isinstance(text, Label):  # a message with its own values
            return translate(text.template, context, **{**text.params, **values})
        entry = catalog.get(f"{text}|{context}") if context else None
        if entry is None:
            entry = catalog.get(text, text)
        if isinstance(entry, list):
            count = values.get("n", values.get("count", 0))
            entry = plural(locale, int(str(count)), entry[0], entry[1])
        if locale == "fr":
            entry = french_typography(entry)  # before inserting values
        if not values:
            return entry
        # A value that is itself an interface text ("Title", "attachment x.pdf") is translated.
        return entry.format(
            **{
                k: translate(v.template, **v.params) if isinstance(v, Label) else v
                for k, v in values.items()
            }
        )

    return translate


def plural(locale: str, count: int, one: str, many: str) -> str:
    """Form to translate depending on the count: in French, 0 and 1 are singular."""
    singular = count <= 1 if locale == "fr" else count == 1
    return one if singular else many


def negotiate(accept_language: str | None, default: str) -> str:
    """First supported language of the `Accept-Language` header, otherwise `default` (the
    instance's default language, `Settings.default_locale`)."""
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LOCALES:
            return code
    return default
