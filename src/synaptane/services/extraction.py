"""Text of the non-text files of `content/`, for search (docs/concepts.md).

The cache lives outside the repository, under `extract_dir/<space_id>/`: one `<path>.txt` file
per source, and `manifest.json`, which gives for each path the processed Git digest and its
outcome (`ok`, `empty`, `failed`). The update is incremental; `synaptane reindex` empties the
cache to redo everything, failures included.

The reading itself happens in a bounded child process (`synaptane.extract`): a booby-trapped
document makes that file fail, not the worker. And so that a crash nonetheless (worker stopped
in the middle of a document) does not repeat on every indexing, the file is recorded
`interrupted` **before** being read, in a journal where each line is appended without
rewriting the manifest; its outcome then replaces that line. A file interrupted twice counts
as a failure.
"""

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from synaptane.extract import ExtractionFailed, run_isolated
from synaptane.git.repo import Blob, PathRefused, Repository, safe_path
from synaptane.services.content import kind_of
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.workspace import Workspace

log = logging.getLogger("synaptane.extraction")

MAX_SOURCE = 64 * 1024 * 1024
MANIFEST = "manifest.json"
JOURNAL = "manifest.journal"  # JSON lines appended between two rewrites of the manifest
MAX_ATTEMPTS = 2  # a file interrupted twice (crash, stop) is not retried

OK, EMPTY, FAILED, INTERRUPTED = "ok", "empty", "failed", "interrupted"


# --- JSON files of the cache ------------------------------------------------------------------


def write_json(path: Path, value: object) -> None:
    """Atomic write: a temporary file in the same directory, then `os.replace`. A worker killed
    halfway leaves the old file, never a truncated JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any | None:
    """Content of a JSON file of the cache; None if it is missing or damaged (logged: the cache
    rebuilds itself, it is never a reason to stop indexing)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        log.warning("Extraction cache: %s unreadable (%s), starting from scratch", path, error)
        return None


# --- Manifest ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    sha: str
    status: str
    error: str = ""
    attempts: int = 0

    def settled(self, sha: str) -> bool:
        """Nothing to redo for this digest: extracted, empty, failed, or interrupted too often."""
        if self.sha != sha:
            return False
        return self.status != INTERRUPTED or self.attempts >= MAX_ATTEMPTS

    def as_json(self) -> dict[str, object]:
        data: dict[str, object] = {"sha": self.sha, "status": self.status}
        if self.error:
            data["error"] = self.error
        if self.attempts:
            data["attempts"] = self.attempts
        return data


def _entry(value: object) -> Entry | None:
    if isinstance(value, str):  # old format: path → digest
        return Entry(value, OK)
    if not isinstance(value, dict) or not isinstance(value.get("sha"), str):
        return None
    status = value.get("status")
    attempts = value.get("attempts", 0)
    return Entry(
        value["sha"],
        status if status in (OK, EMPTY, FAILED, INTERRUPTED) else FAILED,
        str(value.get("error", "")),
        attempts if isinstance(attempts, int) else MAX_ATTEMPTS,
    )


def _safe(path: object) -> bool:
    if not isinstance(path, str) or not path.startswith(f"{CONTENT_DIR}/"):
        return False
    try:
        safe_path(path)
    except PathRefused:
        return False
    return True


class Manifest:
    """Path → `Entry`. `manifest.json` is rewritten whole, atomically, at the end of an
    indexing; in the meantime, each change is a line appended to the journal."""

    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.entries: dict[str, Entry] = {}
        self._load()

    @property
    def file(self) -> Path:
        return self.cache / MANIFEST

    @property
    def journal(self) -> Path:
        return self.cache / JOURNAL

    def _load(self) -> None:
        raw = read_json(self.file)
        broken = raw is None and self.file.exists()
        if raw is not None and not isinstance(raw, dict):
            log.warning("Extraction cache: %s is not an object, starting from scratch", self.file)
            broken = True
        if broken:
            self._reset()
            return
        for path, value in (raw or {}).items():
            entry = _entry(value)
            if entry is not None and _safe(path):
                self.entries[path] = entry
        self._replay()

    def _replay(self) -> None:
        """Replays the journal of an indexing that did not finish. A last line cut off by the
        worker stopping is simply ignored."""
        try:
            lines = self.journal.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        except OSError as error:
            log.warning("Extraction cache: journal unreadable (%s), ignored", error)
            return
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or not _safe(record.get("path")):
                continue
            path = record["path"]
            if record.get("removed"):
                self.entries.pop(path, None)
            elif (entry := _entry(record.get("entry"))) is not None:
                self.entries[path] = entry

    def _reset(self) -> None:
        """Damaged manifest: which `.txt` files are up to date is no longer known. The whole cache
        of the space is emptied, and rebuilt from Git."""
        for child in self.cache.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        self.entries = {}

    def get(self, path: str) -> Entry | None:
        return self.entries.get(path)

    def set(self, path: str, entry: Entry) -> None:
        self.entries[path] = entry
        self._append({"path": path, "entry": entry.as_json()})

    def remove(self, path: str) -> None:
        self.entries.pop(path, None)
        self._append({"path": path, "removed": True})

    def _append(self, record: dict[str, object]) -> None:
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save(self) -> None:
        write_json(self.file, {path: e.as_json() for path, e in sorted(self.entries.items())})
        self.journal.unlink(missing_ok=True)


# --- Extraction -------------------------------------------------------------------------------


def extract_text(path: str, data: bytes, ws: Workspace) -> str | None:
    """Text of a file, or None if its type has none (or if OCR is disabled). Read in a bounded
    child process; raises `ExtractionFailed` if reading fails."""
    kind = kind_of(path, data)
    if kind not in ("pdf", "office", "image"):
        return None
    result = run_isolated(
        kind,
        Path(path).suffix.lower(),
        data,
        languages=ws.ocr_languages,
        ocr_images=ws.ocr_images,
    )
    for note in result.notes:
        log.info("Extraction of %s: %s", path, note)
    return result.text


def _target(cache: Path, path: str) -> Path:
    return cache / f"{path}.txt"


def _candidates(repo: Repository) -> dict[str, Blob]:
    """Files to extract. A refused path (booby-trapped Git tree, `..` entry) is logged and
    ignored: it must neither escape the cache nor stop indexing."""
    found = {}
    for blob in repo.files(CONTENT_DIR):
        if kind_of(blob.path) not in ("pdf", "office", "image") or blob.size > MAX_SOURCE:
            continue
        if not _safe(blob.path):
            log.warning("Indexing: path refused in the repository, ignored: %r", blob.path)
            continue
        found[blob.path] = blob
    return found


def update_index(ws: Workspace, space_id: UUID, repo: Repository) -> tuple[int, int]:
    """Updates the cache. Returns (files processed, files removed)."""
    cache = ws.extract_path(space_id)
    cache.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(cache)
    candidates = _candidates(repo)
    extracted = removed = 0
    for path in list(manifest.entries):
        if path not in candidates:
            _target(cache, path).unlink(missing_ok=True)
            manifest.remove(path)
            removed += 1
    for path, blob in candidates.items():
        previous = manifest.get(path)
        if previous is not None and previous.settled(blob.sha):
            continue
        attempts = previous.attempts if previous and previous.sha == blob.sha else 0
        # Recorded before reading: if the worker dies meanwhile, it does not start over endlessly.
        manifest.set(
            path,
            Entry(blob.sha, INTERRUPTED, "interrupted while reading", attempts + 1),
        )
        status, error, text = _extract(ws, repo, path)
        target = _target(cache, path)
        if text:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
        manifest.set(path, Entry(blob.sha, status, error))
        extracted += 1
    manifest.save()
    return extracted, removed


def _extract(ws: Workspace, repo: Repository, path: str) -> tuple[str, str, str | None]:
    """(outcome, reason for the failure, text) for a file."""
    data = repo.blob(path)
    if data is None:
        return EMPTY, "", None
    try:
        text = extract_text(path, data, ws)
    except ExtractionFailed as error:
        log.warning("Extraction failed for %s: %s", path, error)
        return FAILED, str(error)[:500], None
    except Exception as error:  # noqa: BLE001 — an unreadable file does not block the others
        log.warning("Extraction failed for %s: %s", path, error)
        return FAILED, str(error)[:500] or type(error).__name__, None
    if text and text.strip():
        return OK, "", text
    return EMPTY, "", None


def clear_index(ws: Workspace, space_id: UUID) -> None:
    shutil.rmtree(ws.extract_path(space_id), ignore_errors=True)
