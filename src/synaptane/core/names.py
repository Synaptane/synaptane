"""Readable identifiers: organization and space slugs, usernames."""

import re
import unicodedata

from synaptane.core.errors import Label, invalid

_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_USERNAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
RESERVED_SLUGS = frozenset({"admin", "api", "account", "login", "logout", "mcp", "static", "oauth"})


_LIGATURES = str.maketrans({"œ": "oe", "æ": "ae", "ß": "ss", "ø": "o", "đ": "d", "ł": "l"})


def ascii_text(value: str) -> str:
    """Lowercase text without accents or ligatures: "Retour arrière" → "retour arriere"
    (file names, suggested identifiers)."""
    folded = unicodedata.normalize("NFKD", value.lower().translate(_LIGATURES))
    return "".join(c for c in folded if not unicodedata.combining(c))


def slug(value: str) -> str:
    """Validate a slug (`acme`, `infra-2026`) used in URLs and `org/space` addressing."""
    value = value.strip()
    if not _SLUG.fullmatch(value) or "--" in value:
        raise invalid(
            "Invalid identifier: lowercase letters, digits and hyphens, 64 characters max."
        )
    if value in RESERVED_SLUGS:
        raise invalid("The identifier “{value}” is reserved.", value=value)
    return value


def username(value: str) -> str:
    value = value.strip().lower()
    if not _USERNAME.fullmatch(value):
        raise invalid(
            "Invalid username: lowercase letters, digits, dots and hyphens, 64 characters max."
        )
    return value


def display_name(value: str, *, field: str = "Name") -> str:
    value = " ".join(value.split())
    if not value or len(value) > 120:
        raise invalid("{field}: between 1 and 120 characters.", field=Label(field))
    return value


def without_email(value: str) -> str:
    """ "Ada Admin <ada@acme.test>" → "Ada Admin": the address of whoever dropped a note is
    shown to administrators (accounts), not to every reader of a space."""
    return re.sub(r"\s*<[^>]*>", "", value).strip()
