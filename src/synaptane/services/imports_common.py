"""What the readers of an export share (`imports_notion`, `imports_confluence`): the files they
lay out, names made unique, relative links."""

import posixpath
import re
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field

from synaptane.core.errors import Label

Read = Callable[[zipfile.ZipInfo], bytes]  # bounded read of an archive entry
MAX_BROKEN = 1000  # broken links listed by the check; beyond that, only counted
NAME_LENGTH = 100  # characters of a file or folder name made from a title
# Control characters, separators, and the bidirectional controls that would show a name the
# wrong way round (`Report\u202egnp.md`, read as "Reportdm.png").
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]+")
# Names Windows keeps for its devices, with or without an extension: a repository holding
# `con.md` or `aux/` cannot be cloned there.
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{kind}{n}" for kind in ("COM", "LPT") for n in range(1, 10)
}


@dataclass
class Item:
    """One file to write. `info` is its entry in the archive, None for a page made by the
    import (`made`, the index page of a Notion database); `convert` turns the entry into
    what is written (None: as it is)."""

    name: str  # entry in the archive, or the one the page is made from
    path: str  # relative to the import folder
    info: zipfile.ZipInfo | None = None
    convert: Callable[[bytes], bytes] | None = None
    made: Callable[[], bytes] | None = None


@dataclass
class Links:
    """Links of the converted pages: rewritten to the new paths, or leading nowhere."""

    rewritten: int = 0
    broken: list[tuple[str, str]] = field(default_factory=list)  # (page, link as written)
    broken_count: int = 0

    def broke(self, page: str, target: str) -> None:
        self.broken_count += 1
        if len(self.broken) < MAX_BROKEN:
            self.broken.append((page, target))


@dataclass
class Layout:
    items: list[Item] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # entries of the export not imported
    links: Links = field(default_factory=Links)
    reasons: dict[str, Label] = field(default_factory=dict)  # why, for some of the skipped


def windows_safe(name: str) -> str:
    """A file or folder name usable on Windows: `CON`, `con.md`, `Aux.tar.gz` get a `_` after
    the reserved word (`CON_`, `con_.md`, `Aux_.tar.gz`)."""
    stem = name.split(".", 1)[0]
    if stem.rstrip(" ").upper() in RESERVED:
        return stem + "_" + name[len(stem) :]
    return name


def windows_path(path: str) -> str:
    return "/".join(windows_safe(part) for part in path.split("/"))


def clean_name(title: str, fallback: str = "Untitled") -> str:
    """A file or folder name from a title: no slash nor control character (bidirectional ones
    included), no leading dot, at most NAME_LENGTH characters."""
    name = unicodedata.normalize("NFC", _UNSAFE_NAME.sub("-", title))
    name = " ".join(name.split()).strip(" .-") or fallback
    if len(name) > NAME_LENGTH:
        name = name[:NAME_LENGTH].rstrip(" .-")
    return name


class Names:
    """Names given in one folder: a second "Notes" becomes "Notes (2)". Case is ignored, so
    that the repository can be cloned on macOS or Windows, and a folder `Notes/` and a page
    `Notes.md` count as the same name: `[[Notes]]` would not know which to choose."""

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.tried: dict[tuple[str, str], int] = {}  # last number given to a name

    @staticmethod
    def _keys(name: str, suffix: str) -> set[str]:
        key = (name + suffix).lower()
        if suffix.lower() == ".md":
            return {key, name.lower()}
        return {key, key + ".md"} if not suffix else {key}

    def take(self, name: str, suffix: str = "") -> str:
        """`suffix`: the extension of a file, "" for a folder."""
        safe = windows_safe(name + suffix)
        name = safe[: len(safe) - len(suffix)]
        # The numbers already given to this name are taken: not tried again (a thousand
        # "Untitled" would otherwise take a time quadratic in their number).
        number = self.tried.get((name.lower(), suffix.lower()), 1)
        candidate = name if number == 1 else f"{name} ({number})"
        while self._keys(candidate, suffix) & self.used:
            number += 1
            candidate = f"{name} ({number})"
        self.tried[name.lower(), suffix.lower()] = number
        self.used |= self._keys(candidate, suffix)
        return candidate + suffix


def relative(page: str, target: str) -> str:
    """Address of `target` from the page `page` (both relative to the import folder), as
    written in a link: what would cut it (`%`, `#`, `?`) is encoded, spaces are kept."""
    path = posixpath.relpath(target, posixpath.dirname(page) or ".")
    return path.replace("%", "%25").replace("#", "%23").replace("?", "%3F")


def split_name(name: str) -> tuple[str, str]:
    """("photo", ".png"); a name without extension, or starting with a dot: ("name", "")."""
    stem, ext = posixpath.splitext(name)
    if not stem or len(ext) > 12 or " " in ext:
        return name, ""
    return stem, ext
