"""Import of a zip: an Obsidian vault, Markdown files, a Notion or a Confluence export.

Two steps. `receive` keeps the archive and `plan` says what it holds and what would happen;
once the person has checked, `apply` writes everything in one commit in their name. A commit
that large counts as one item for the tidy-up (`changes.BULK`), not one per page.

The format is recognized from the archive's names (`detect`), and can be forced. Markdown
files and Obsidian vaults are written as they are: wiki links and Obsidian syntax are
understood when reading. A Notion export (`imports_notion`) or a Confluence one in HTML
(`imports_confluence`) is laid out and converted: clean names, Markdown pages, links rewritten
to the new paths. What is converted goes through the same checks, secrets included.

The archive is not trusted: reading is bounded whatever its headers declare (a zip of zips, as
Notion makes for a large workspace, is unpacked once within the same limits), symbolic links
and paths that leave the folder are refused, and nothing already in the space is overwritten.
"""

import lzma
import re
import secrets
import shutil
import stat
import time
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import Label, SynaptaneError, invalid, not_found
from synaptane.core.secret_scan import Finding, scan_file
from synaptane.git.repo import GitError, safe_path
from synaptane.services import Actor
from synaptane.services import checks as team_checks
from synaptane.services.content import (
    MARKDOWN,
    MAX_UPLOAD,
    after_write,
    commit_files,
    content_path,
    list_files,
    relative_path,
    writable,
)
from synaptane.services.imports_common import Item, Layout, Links, Read, windows_path
from synaptane.services.imports_confluence import Confluence, is_confluence, is_confluence_xml
from synaptane.services.imports_notion import Notion, is_notion
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Space

MAX_ARCHIVE = 100 * 1024 * 1024  # what nginx accepts in front of the application
MAX_FILES = 10_000
MAX_TOTAL = 400 * 1024 * 1024  # once unpacked
KEPT_FOR = 24 * 3600  # seconds: an archive never confirmed
TOKEN = re.compile(r"[0-9a-f]{32}")
# System leftovers; anything starting with a dot is left out too (.obsidian, .trash, .git).
LEFT_OUT = {"__MACOSX", "Thumbs.db", "desktop.ini"}
CHUNK = 1024 * 1024
HEAD = 4096  # bytes read to find the title of a Notion page
# Formats of an archive, recognized or chosen at the check.
FORMATS = {
    "obsidian": Label("Obsidian vault"),
    "markdown": Label("Markdown files"),
    "notion": Label("Notion export"),
    "confluence": Label("Confluence export (HTML)"),
}
CONVERTED = {"notion", "confluence"}
# What reading a damaged entry raises: a corrupt compressed stream is not an OSError.
DAMAGED = (
    zipfile.BadZipFile,
    EOFError,
    OSError,
    NotImplementedError,
    RuntimeError,
    zlib.error,
    lzma.LZMAError,
)


@dataclass
class Entry:
    name: str  # in the archive
    path: str  # in the space, relative to content/
    findings: list[Finding] = field(default_factory=list)
    data: bytes | None = field(default=None, repr=False)  # what is written (`apply` only)


@dataclass
class Plan:
    token: str
    folder: str
    format: str = "markdown"  # how the archive is read
    detected: str = "markdown"  # what it looks like
    new: list[Entry] = field(default_factory=list)
    with_secrets: list[Entry] = field(default_factory=list)  # left out unless the person keeps it
    existing: list[str] = field(default_factory=list)
    too_large: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # unsafe path, link, encrypted
    skipped: list[str] = field(default_factory=list)  # files of the export not imported
    reasons: dict[str, Label] = field(default_factory=dict)  # why, for some of the skipped
    links: Links = field(default_factory=Links)  # links of the converted pages
    left_out: int = 0

    @property
    def pages(self) -> int:
        return sum(PurePosixPath(e.path).suffix.lower() in MARKDOWN for e in self.new)

    @property
    def attachments(self) -> int:
        return len(self.new) - self.pages


def _archive(actor: Actor, ws: Workspace, space: Space, token: str) -> Path:
    path = ws.imports_path(space.id) / f"{actor.user.id}-{token}.zip"
    if not TOKEN.fullmatch(token) or not path.is_file():
        raise not_found("This import has expired. Send the archive again.")
    return path


def _purge(ws: Workspace, space: Space, actor: Actor) -> None:
    """Archives left behind, and this person's previous one in this space."""
    limit = time.time() - KEPT_FOR
    for pattern in ("*/*.zip", "*/*.flat", "*/*.part"):  # .flat, .part: an unpacking cut short
        for path in ws.imports_dir.glob(pattern):
            if path.stat().st_mtime < limit:
                path.unlink(missing_ok=True)
    for path in ws.imports_path(space.id).glob(f"{actor.user.id}-*.zip"):
        path.unlink(missing_ok=True)


def receive(actor: Actor, ws: Workspace, space: Space, upload: BinaryIO) -> str:
    """Keeps the archive for the check that follows. Returns its token."""
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    ws.local_repository(space)
    ws.imports_path(space.id).mkdir(parents=True, exist_ok=True)
    _purge(ws, space, actor)
    token = secrets.token_hex(16)
    path = ws.imports_path(space.id) / f"{actor.user.id}-{token}.zip"
    with path.open("wb") as out:
        shutil.copyfileobj(upload, out)
    if path.stat().st_size > MAX_ARCHIVE:
        path.unlink()
        raise invalid("Archive too large ({max} MB maximum).", max=MAX_ARCHIVE // 1024 // 1024)
    if not zipfile.is_zipfile(path):
        path.unlink()
        raise invalid("This file is not a zip archive.")
    try:
        _unnest(path)
        with _opened(path) as archive:
            paths = [p for _, p in _entries(archive)[0]]
        if is_confluence_xml(paths):
            raise invalid(
                "This is a Confluence XML export. Export the space in HTML instead: "
                "Space settings, Export space, HTML."
            )
    except SynaptaneError:
        path.unlink(missing_ok=True)
        raise
    return token


def _hidden(parts: list[str]) -> bool:
    return any(p.startswith(".") and p not in (".", "..") or p in LEFT_OUT for p in parts)


def _unnest(path: Path) -> None:
    """An archive made only of archives (a large Notion workspace: `Export-…-Part-1.zip`…)
    becomes one archive of what they hold, within the same limits as any other. The first of
    two files with the same path is kept."""
    with _opened(path) as archive:
        parts = [
            info
            for info in archive.infolist()
            if not info.is_dir() and not _hidden(info.filename.replace("\\", "/").split("/"))
        ]
        if not parts or not all(info.filename.lower().endswith(".zip") for info in parts):
            return
        if len(parts) > MAX_FILES:  # each part is unpacked: as many as files at most
            raise invalid("Too many files in this archive ({max} maximum).", max=MAX_FILES)
        flat, inner = path.with_suffix(".flat"), path.with_suffix(".part")
        try:
            _flatten(archive, sorted(parts, key=lambda i: i.filename), flat, inner)
            flat.replace(path)
        finally:
            flat.unlink(missing_ok=True)
            inner.unlink(missing_ok=True)


def _flatten(
    archive: zipfile.ZipFile, parts: list[zipfile.ZipInfo], flat: Path, inner: Path
) -> None:
    unpacked = held_bytes = files = 0  # bytes of the parts, bytes of the files they hold
    seen: set[str] = set()

    def too_large() -> SynaptaneError:
        return invalid(
            "This archive is too large once unpacked ({max} MB maximum).",
            max=MAX_TOTAL // 1024 // 1024,
        )

    with zipfile.ZipFile(flat, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as out:
        for part in parts:
            if _unsafe(part):
                raise invalid("This archive is damaged: {name}.", name=part.filename)
            try:
                with archive.open(part) as source, inner.open("wb") as copy:
                    while chunk := source.read(CHUNK):
                        unpacked += len(chunk)
                        if unpacked > MAX_TOTAL:
                            raise too_large()
                        copy.write(chunk)
                held = zipfile.ZipFile(inner)
            except DAMAGED:
                raise invalid("This archive is damaged: {name}.", name=part.filename) from None
            with held:
                for info in held.infolist():
                    if info.is_dir() or info.filename in seen:
                        continue
                    seen.add(info.filename)
                    files += 1
                    if files > MAX_FILES:
                        raise invalid(
                            "Too many files in this archive ({max} maximum).", max=MAX_FILES
                        )
                    copied = zipfile.ZipInfo(info.filename, info.date_time)
                    copied.compress_type = zipfile.ZIP_DEFLATED
                    copied.external_attr = info.external_attr
                    if _unsafe(info):  # kept as a link: the check refuses it, unread
                        copied.external_attr = (stat.S_IFLNK | 0o777) << 16
                        out.writestr(copied, b"")
                        continue
                    written = 0
                    try:
                        with held.open(info) as source, out.open(copied, "w") as target:
                            # Beyond the limit of a file, the rest is not needed: the check
                            # sets it aside as too large.
                            while written <= MAX_UPLOAD and (chunk := source.read(CHUNK)):
                                chunk = chunk[: MAX_UPLOAD + 1 - written]
                                written += len(chunk)
                                held_bytes += len(chunk)
                                if held_bytes > MAX_TOTAL:
                                    raise too_large()
                                target.write(chunk)
                    except DAMAGED:
                        raise invalid(
                            "This archive is damaged: {name}.", name=info.filename
                        ) from None


def _root(names: list[list[str]]) -> int:
    """1 if every file is in one top folder (a vault zipped from outside): it is left out."""
    tops = {parts[0] for parts in names}
    return 1 if len(tops) == 1 and all(len(parts) > 1 for parts in names) else 0


def _parents(path: str) -> Iterator[str]:
    parent = PurePosixPath(path).parent
    while parent.name:
        yield parent.as_posix()
        parent = parent.parent


def _unsafe(info: zipfile.ZipInfo) -> bool:
    """Encrypted, or a symbolic link."""
    return bool(info.flag_bits & 1) or stat.S_ISLNK(info.external_attr >> 16)


def _read(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Bounded: `file_size` is only what the archive claims."""
    try:
        with archive.open(info) as source:
            return source.read(MAX_UPLOAD + 1)
    except DAMAGED:
        raise invalid("This archive is damaged: {name}.", name=info.filename) from None


def _plain(path: str) -> bool:
    """A path inside the export: no empty part (absolute path, `a//b`), no `.` nor `..`."""
    return all(part not in ("", ".", "..") for part in path.split("/"))


@contextmanager
def _opened(path: Path) -> Iterator[zipfile.ZipFile]:
    try:
        with zipfile.ZipFile(path) as archive:
            yield archive
    except zipfile.BadZipFile:
        raise invalid("This file is not a zip archive.") from None


def _entries(archive: zipfile.ZipFile) -> tuple[list[tuple[zipfile.ZipInfo, str]], int, bool]:
    """Files of the archive with their path in the export (the top folder of a folder zipped
    from outside left out), the number of hidden or system files left out, and whether it
    holds an Obsidian vault."""
    infos = []
    for info in archive.infolist():
        parts = info.filename.replace("\\", "/").split("/")
        if info.is_dir() or parts[0] in LEFT_OUT:
            continue
        infos.append((info, parts))
    if len(infos) > MAX_FILES:
        raise invalid("Too many files in this archive ({max} maximum).", max=MAX_FILES)
    skip = _root([parts for _, parts in infos]) if infos else 0
    entries, left_out, obsidian = [], 0, False
    for info, parts in sorted(infos, key=lambda item: item[1]):
        parts = parts[skip:]
        if _hidden(parts):
            left_out += 1
            obsidian = obsidian or ".obsidian" in parts
            continue
        entries.append((info, "/".join(parts)))
    return entries, left_out, obsidian


def detect(paths: list[str], obsidian: bool = False) -> str:
    """Format of an archive, from its paths."""
    if is_confluence(paths):
        return "confluence"
    if is_notion(paths):
        return "notion"
    return "obsidian" if obsidian else "markdown"


class _Reader:
    """Bounded reads; what is unpacked is counted once per entry, however often it is read
    (an export is read once to lay it out, then to convert it)."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.total = 0
        self.counted: set[str] = set()

    def read(self, info: zipfile.ZipInfo) -> bytes:
        data = _read(self.archive, info)
        if info.filename not in self.counted:
            self.counted.add(info.filename)
            self.total += len(data)
            if self.total > MAX_TOTAL:
                raise invalid(
                    "This archive is too large once unpacked ({max} MB maximum).",
                    max=MAX_TOTAL // 1024 // 1024,
                )
        return data

    def head(self, info: zipfile.ZipInfo) -> bytes:
        """The first bytes of an entry (a page's title), "" if it cannot be read. Not counted:
        HEAD bytes at most per entry."""
        try:
            with self.archive.open(info) as source:
                return source.read(HEAD)
        except DAMAGED:
            return b""


def _layout(
    kind: str, entries: list[tuple[zipfile.ZipInfo, str]], read: Read, head: Read
) -> Layout:
    if kind == "notion":
        return Notion(entries, read, head).layout()
    if kind == "confluence":
        return Confluence(entries, read).layout()
    # Kept as they are, but for the names Windows refuses (`con.md` becomes `con_.md`).
    return Layout([Item(info.filename, windows_path(path), info) for info, path in entries])


def plan(
    actor: Actor, ws: Workspace, space: Space, token: str, folder: str = "", format: str = ""
) -> Plan:
    """What the archive holds and what would happen, without writing anything. `format`: one
    of FORMATS, or recognized from the archive."""
    return _check(actor, ws, space, token, folder, format, collect=False)


def _check(
    actor: Actor, ws: Workspace, space: Space, token: str, folder: str, format: str, collect: bool
) -> Plan:
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    path = _archive(actor, ws, space, token)
    folder = relative_path(content_path(folder))
    result = Plan(token, folder)
    present = {blob.path for blob in list_files(actor, ws, space)}
    folders = {parent for file in present for parent in _parents(file)}
    checks = team_checks.load(ws.local_repository(space))
    with _opened(path) as archive:
        entries, result.left_out, obsidian = _entries(archive)
        result.detected = detect([p for _, p in entries], obsidian)
        result.format = format if format in FORMATS else result.detected
        # A link, an encrypted file or a path that leaves the export (absolute, `..`) is never
        # read, not even to lay out an export.
        result.refused = [path for info, path in entries if _unsafe(info) or not _plain(path)]
        entries = [(info, path) for info, path in entries if not _unsafe(info) and _plain(path)]
        reader = _Reader(archive)
        layout = _layout(result.format, entries, reader.read, reader.head)
        result.links, result.reasons = layout.links, layout.reasons
        # With a reason (too large, cells shortened) first: never hidden past the list's end.
        result.skipped = sorted(layout.skipped, key=lambda name: name not in layout.reasons)
        taken: set[str] = set()
        for item in sorted(layout.items, key=lambda item: item.path.split("/")):
            written = item.path
            relative = f"{folder}/{written}" if folder else written
            try:
                safe_path(f"content/{relative}")
            except GitError:
                result.refused.append(written)
                continue
            under_a_file = any(p in present or p in taken for p in _parents(relative))
            if under_a_file or relative in folders or relative in taken:
                result.refused.append(written)
                continue
            if relative in present:
                result.existing.append(relative)
                continue
            if item.info is not None and item.info.file_size > MAX_UPLOAD:
                result.too_large.append(written)
                continue
            data = reader.read(item.info) if item.info is not None else b""
            if item.made is not None:
                data = item.made()
            if item.convert is not None and len(data) <= MAX_UPLOAD:
                data = item.convert(data)
            if len(data) > MAX_UPLOAD:
                result.too_large.append(written)
                continue
            if blocked := [v for v in checks.introduced(relative, data, None) if v.blocks]:
                # A team's `block` check: the file is not imported (`services.checks`).
                result.skipped.append(item.name)
                result.reasons[item.name] = Label(
                    "refused by the team check “{rule}”: {message}",
                    rule=blocked[0].rule,
                    message=blocked[0].message or blocked[0].rule,
                )
                continue
            taken.add(relative)
            entry = Entry(item.name, relative, scan_file(relative, data), data if collect else None)
            (result.with_secrets if entry.findings else result.new).append(entry)
    return result


def apply(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    token: str,
    folder: str = "",
    keep: frozenset[str] = frozenset(),
    format: str = "",
) -> int:
    """Writes the new files in one commit, those with a likely secret only if in `keep`. Returns
    the number of files written. The archive is then deleted."""
    checked = _check(actor, ws, space, token, folder, format, collect=True)
    entries = checked.new + [e for e in checked.with_secrets if e.path in keep]
    if not entries:
        raise invalid("Nothing to import.")
    path = _archive(actor, ws, space, token)
    files: dict[str, bytes | None] = {content_path(e.path): e.data for e in entries}
    with writable(session, database, actor, ws, space) as repo:
        # Written since the check: what is in the space stays, as anywhere else in the import.
        present = {blob.path for blob in repo.files(CONTENT_DIR)}
        files = {p: data for p, data in files.items() if p not in present}
        if not files:
            raise invalid("Nothing to import.")
        subject = f"Import of {len(files)} files"
        if checked.format in CONVERTED:
            subject += f" ({FORMATS[checked.format]})"
        commit_files(repo, ws, actor, "web", files, subject)
    after_write(session, actor, space, "content.imported", content_path(checked.folder))
    path.unlink(missing_ok=True)
    return len(files)


def cancel(actor: Actor, ws: Workspace, space: Space, token: str) -> None:
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    _archive(actor, ws, space, token).unlink(missing_ok=True)
