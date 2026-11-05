"""Import of a zip of Markdown files, an Obsidian vault for instance.

Two steps. `receive` keeps the archive and `plan` says what it holds and what would happen;
once the person has checked, `apply` writes everything in one commit in their name. A commit
that large counts as one item for the tidy-up (`changes.BULK`), not one per page. Pages are
written as they are: wiki links and Obsidian syntax are understood when reading.

The archive is not trusted: reading is bounded whatever its headers declare, symbolic links and
paths that leave the folder are refused, and nothing already in the space is overwritten.
"""

import re
import secrets
import shutil
import stat
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import invalid, not_found
from synaptane.core.secret_scan import Finding, scan_file
from synaptane.git.repo import GitError, safe_path
from synaptane.services import Actor
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


@dataclass
class Entry:
    name: str  # in the archive
    path: str  # in the space, relative to content/
    findings: list[Finding] = field(default_factory=list)


@dataclass
class Plan:
    token: str
    folder: str
    new: list[Entry] = field(default_factory=list)
    with_secrets: list[Entry] = field(default_factory=list)  # left out unless the person keeps it
    existing: list[str] = field(default_factory=list)
    too_large: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # unsafe path, link, encrypted
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
    for path in ws.imports_dir.glob("*/*.zip"):
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
    return token


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
    return bool(info.flag_bits & 1) or stat.S_ISLNK(info.external_attr >> 16)


def _read(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Bounded: `file_size` is only what the archive claims."""
    try:
        with archive.open(info) as source:
            return source.read(MAX_UPLOAD + 1)
    except (zipfile.BadZipFile, EOFError, OSError, NotImplementedError, RuntimeError):
        raise invalid("This archive is damaged: {name}.", name=info.filename) from None


@contextmanager
def _opened(path: Path) -> Iterator[zipfile.ZipFile]:
    try:
        with zipfile.ZipFile(path) as archive:
            yield archive
    except zipfile.BadZipFile:
        raise invalid("This file is not a zip archive.") from None


def plan(actor: Actor, ws: Workspace, space: Space, token: str, folder: str = "") -> Plan:
    """What the archive holds and what would happen, without writing anything."""
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    path = _archive(actor, ws, space, token)
    folder = relative_path(content_path(folder))
    result = Plan(token, folder)
    present = {blob.path for blob in list_files(actor, ws, space)}
    folders = {parent for file in present for parent in _parents(file)}
    with _opened(path) as archive:
        infos = []
        for info in archive.infolist():
            parts = info.filename.replace("\\", "/").split("/")
            if info.is_dir() or parts[0] in LEFT_OUT:
                continue
            infos.append((info, parts))
        if len(infos) > MAX_FILES:
            raise invalid("Too many files in this archive ({max} maximum).", max=MAX_FILES)
        skip = _root([parts for _, parts in infos]) if infos else 0
        total = 0
        taken: set[str] = set()
        for info, parts in sorted(infos, key=lambda item: item[1]):
            parts = parts[skip:]
            written = "/".join(parts)
            if any(p.startswith(".") and p not in (".", "..") or p in LEFT_OUT for p in parts):
                result.left_out += 1
                continue
            relative = f"{folder}/{written}" if folder else written
            try:
                safe_path(f"content/{relative}")
            except GitError:
                result.refused.append(written)
                continue
            under_a_file = any(p in present or p in taken for p in _parents(relative))
            if _unsafe(info) or under_a_file or relative in folders or relative in taken:
                result.refused.append(written)
                continue
            if relative in present:
                result.existing.append(relative)
                continue
            if info.file_size > MAX_UPLOAD:
                result.too_large.append(written)
                continue
            data = _read(archive, info)
            total += len(data)
            if total > MAX_TOTAL:
                raise invalid(
                    "This archive is too large once unpacked ({max} MB maximum).",
                    max=MAX_TOTAL // 1024 // 1024,
                )
            if len(data) > MAX_UPLOAD:
                result.too_large.append(written)
                continue
            taken.add(relative)
            entry = Entry(info.filename, relative, scan_file(relative, data))
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
) -> int:
    """Writes the new files in one commit, those with a likely secret only if in `keep`. Returns
    the number of files written. The archive is then deleted."""
    checked = plan(actor, ws, space, token, folder)
    entries = checked.new + [e for e in checked.with_secrets if e.path in keep]
    if not entries:
        raise invalid("Nothing to import.")
    path = _archive(actor, ws, space, token)
    with _opened(path) as archive:
        files: dict[str, bytes | None] = {
            content_path(e.path): _read(archive, archive.getinfo(e.name)) for e in entries
        }
    with writable(session, database, actor, ws, space) as repo:
        # Written since the check: what is in the space stays, as anywhere else in the import.
        present = {blob.path for blob in repo.files(CONTENT_DIR)}
        files = {p: data for p, data in files.items() if p not in present}
        if not files:
            raise invalid("Nothing to import.")
        commit_files(repo, ws, actor, "web", files, f"Import of {len(files)} files")
    after_write(session, actor, space, "content.imported", content_path(checked.folder))
    path.unlink(missing_ok=True)
    return len(files)


def cancel(actor: Actor, ws: Workspace, space: Space, token: str) -> None:
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    _archive(actor, ws, space, token).unlink(missing_ok=True)
