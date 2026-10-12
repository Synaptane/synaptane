"""Indexing of extracted text, robust to booby-trapped documents and crashes.

Giant PDF (failure recorded before reading), atomic manifest (tolerant reading), files
without chunks not re-read, and booby-trapped Git tree (path ignored, not fatal).
"""

import json
import logging
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from synaptane.extract import MAX_PIXELS, ExtractionFailed
from synaptane.git.repo import Repository
from synaptane.services import content, extraction, semantic
from synaptane.services.extraction import (
    EMPTY,
    FAILED,
    INTERRUPTED,
    MANIFEST,
    OK,
    Manifest,
    clear_index,
    update_index,
    write_json,
)
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.test_semantic import ConceptEmbedder, enable
from tests.integration.wikidata import Wiki
from tests.unit.test_extract import REPORT_SIZE, blank_pdf, fake_tesseract


def manifest(ctx: Workspace, wiki: Wiki) -> Manifest:
    return Manifest(ctx.extract_path(wiki.infra.id))


def run(ctx: Workspace, wiki: Wiki) -> tuple[int, int]:
    return update_index(ctx, wiki.infra.id, ctx.local_repository(wiki.infra))


def put(db: Session, database: Database, ctx: Workspace, wiki: Wiki, path: str, data: bytes) -> str:
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, data)
    db.commit()
    blob = ctx.local_repository(wiki.infra).blob_entry(f"content/{path}")
    assert blob is not None
    return blob[0]


# --- Giant PDF, failure recorded before reading -----------------------------------------------


def test_giant_pdf_is_indexed_and_the_worker_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
) -> None:
    """The 432-byte PDF that got the worker killed in a loop (2.4 GB when rendered)."""
    fake_tesseract(tmp_path, monkeypatch, REPORT_SIZE)
    sha = put(db, database, ctx, wiki, "plan.pdf", blank_pdf(4500))
    assert run(ctx, wiki) == (1, 0)
    assert manifest(ctx, wiki).get("content/plan.pdf") == extraction.Entry(sha, OK)
    text = ctx.extracted_text(wiki.infra.id, "content/plan.pdf") or ""
    width, height = (int(n) for n in text.split("image de ")[1].split(" pixels")[0].split(" x "))
    assert width * height <= MAX_PIXELS
    assert run(ctx, wiki) == (0, 0)  # nothing to redo


def test_failure_is_recorded_before_reading(
    monkeypatch: pytest.MonkeyPatch, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """A worker that dies while reading a file does not re-read it endlessly: the file is
    recorded beforehand, and counted as a failure after two interruptions."""
    sha = put(db, database, ctx, wiki, "piege.pdf", blank_pdf(600))
    calls: list[str] = []

    class WorkerKilled(BaseException):
        pass

    def crash(ws: Workspace, repo: Repository, path: str) -> tuple[str, str, str | None]:
        calls.append(path)
        raise WorkerKilled

    monkeypatch.setattr(extraction, "_extract", crash)
    for attempt in (1, 2):
        with pytest.raises(WorkerKilled):
            run(ctx, wiki)
        entry = manifest(ctx, wiki).get("content/piege.pdf")
        assert entry == extraction.Entry(sha, INTERRUPTED, "interrupted while reading", attempt)
    assert run(ctx, wiki) == (0, 0) and calls == ["content/piege.pdf"] * 2
    # The rewritten manifest keeps the failure; `reindex` (cache cleared) retries everything.
    saved = json.loads((ctx.extract_path(wiki.infra.id) / MANIFEST).read_text())
    assert saved["content/piege.pdf"]["status"] == INTERRUPTED
    monkeypatch.undo()
    clear_index(ctx, wiki.infra.id)
    run(ctx, wiki)
    assert manifest(ctx, wiki).get("content/piege.pdf") == extraction.Entry(sha, EMPTY)


def test_extraction_failure_is_not_retried_until_the_file_changes(
    monkeypatch: pytest.MonkeyPatch, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    put(db, database, ctx, wiki, "lent.pdf", blank_pdf(600))
    calls: list[str] = []

    def failing(*args: object, **kwargs: object) -> object:
        calls.append("x")
        raise ExtractionFailed("délai dépassé (600 s)")

    monkeypatch.setattr(extraction, "run_isolated", failing)
    assert run(ctx, wiki) == (1, 0)
    entry = manifest(ctx, wiki).get("content/lent.pdf")
    assert entry is not None and entry.status == FAILED and entry.error.startswith("délai")
    assert run(ctx, wiki) == (0, 0) and len(calls) == 1
    put(db, database, ctx, wiki, "lent.pdf", blank_pdf(700))
    assert run(ctx, wiki) == (1, 0) and len(calls) == 2


# --- Atomic manifest, tolerant reading --------------------------------------------------------


def test_broken_manifest_starts_over(
    caplog: pytest.LogCaptureFixture, ctx: Workspace, wiki: Wiki
) -> None:
    cache = ctx.extract_path(wiki.infra.id)
    (cache / MANIFEST).write_text('{"content/rapport.pdf": {"sha": "ab')  # cut off abruptly
    orphan = cache / "content" / "disparu.pdf.txt"
    orphan.write_text("texte d'un fichier supprimé du dépôt")
    with caplog.at_level(logging.WARNING, "synaptane.extraction"):
        assert run(ctx, wiki) == (2, 0)  # everything redone, without error
    assert "unreadable" in caplog.text
    assert not orphan.exists()
    assert "Kubernetes" in (cache / "content" / "rapport.pdf.txt").read_text()
    assert set(json.loads((cache / MANIFEST).read_text())) == {
        "content/rapport.pdf",
        "content/specs.docx",
    }


def test_truncated_journal_line_is_ignored(ctx: Workspace, wiki: Wiki) -> None:
    cache = ctx.extract_path(wiki.infra.id)
    (cache / extraction.JOURNAL).write_text('{"path": "content/rapport.pdf", "entry": {"sh')
    assert run(ctx, wiki) == (0, 0)
    assert not (cache / extraction.JOURNAL).exists()


def test_old_manifest_format_is_still_read(ctx: Workspace, wiki: Wiki) -> None:
    cache = ctx.extract_path(wiki.infra.id)
    current = json.loads((cache / MANIFEST).read_text())
    (cache / MANIFEST).write_text(json.dumps({p: e["sha"] for p, e in current.items()}))
    assert run(ctx, wiki) == (0, 0)


def test_manifest_is_replaced_atomically(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / MANIFEST
    write_json(target, {"avant": 1})

    def interrupted(source: str, destination: str) -> None:
        raise OSError("worker tué")

    monkeypatch.setattr("synaptane.services.extraction.os.replace", interrupted)
    with pytest.raises(OSError):
        write_json(target, {"après": 2})
    assert json.loads(target.read_text()) == {"avant": 1}
    assert [p.name for p in tmp_path.iterdir()] == [MANIFEST]  # no temporary file left


# --- Booby-trapped Git tree: path ignored, not fatal ------------------------------------------


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, input=stdin, capture_output=True, check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "X", "GIT_AUTHOR_EMAIL": "x@example.com",
             "GIT_COMMITTER_NAME": "X", "GIT_COMMITTER_EMAIL": "x@example.com"},
    )  # fmt: skip
    return result.stdout.decode().strip()


def trap(clone: Path) -> None:
    """Commit whose tree contains `content/../evil.pdf` (a `..` entry), as a push to a bare
    repository without fsck accepts it."""
    evil = _git(clone, "hash-object", "-w", "--stdin", stdin=blank_pdf(600))
    inner = _git(clone, "mktree", stdin=f"100644 blob {evil}\tevil.pdf\n".encode())
    content_tree = _git(clone, "rev-parse", "HEAD:content")
    entries = _git(clone, "ls-tree", content_tree) + f"\n040000 tree {inner}\t..\n"
    trapped = _git(clone, "mktree", stdin=entries.encode())
    root = "".join(
        f"040000 tree {trapped}\tcontent\n" if line.endswith("\tcontent") else f"{line}\n"
        for line in _git(clone, "ls-tree", "HEAD").splitlines()
    )
    tree = _git(clone, "mktree", stdin=root.encode())
    commit = _git(clone, "commit-tree", tree, "-p", "HEAD", "-m", "Arbre piégé")
    _git(clone, "update-ref", "HEAD", commit)


def test_trapped_tree_is_ignored_not_fatal(
    caplog: pytest.LogCaptureFixture, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    repo = ctx.local_repository(wiki.infra)
    trap(repo.path)
    assert "content/../evil.pdf" in {b.path for b in repo.files("content")}
    with caplog.at_level(logging.WARNING):
        assert run(ctx, wiki) == (0, 0)
        enable(db, ctx, wiki)
        config = semantic.org_config(db, ctx.box, wiki.infra.org_id)
        assert config is not None
        embedded, _ = semantic.index_space(
            database, ctx, wiki.infra.id, repo, config, ConceptEmbedder()
        )
    assert embedded >= 4
    assert caplog.text.count("path refused") == 2
    assert not (ctx.extract_dir / str(wiki.infra.id) / "evil.pdf.txt").exists()


# --- Files without chunks not re-read ---------------------------------------------------------


def test_files_without_pieces_are_not_read_again(
    monkeypatch: pytest.MonkeyPatch, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    put(db, database, ctx, wiki, "vide.md", b"# Vide\n")
    put(db, database, ctx, wiki, "outil.bin", b"\x7fELF\0\0binaire")
    enable(db, ctx, wiki)
    config = semantic.org_config(db, ctx.box, wiki.infra.org_id)
    assert config is not None
    read: list[str] = []
    original = Repository.blob

    def counting(self: Repository, path: str, rev: str = "HEAD") -> bytes | None:
        read.append(path)
        return original(self, path, rev)

    monkeypatch.setattr(Repository, "blob", counting)

    def index() -> int:
        repo = ctx.local_repository(wiki.infra)
        return semantic.index_space(database, ctx, wiki.infra.id, repo, config, ConceptEmbedder())[
            0
        ]

    first = index()
    assert {"content/vide.md", "content/outil.bin"} <= set(read)
    files, _ = semantic.stats(db, wiki.infra.id)
    assert first == files  # only the files actually embedded are counted
    read.clear()
    assert index() == 0 and read == []
    put(db, database, ctx, wiki, "vide.md", b"# Vide\n\nPlus maintenant.\n")
    read.clear()
    assert index() == 1 and read == ["content/vide.md"]
