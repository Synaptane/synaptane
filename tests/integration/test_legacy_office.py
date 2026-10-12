"""Old binary Office files (.doc, .xls, .ppt) in a space: indexed, searchable, shown.

The text of .doc and .xls files is extracted by catdoc: without it, they are kept and listed,
without text and without error (`synaptane doctor` warns). The text of .ppt files is read in
Python, catdoc or not."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane import extract
from synaptane.cli import main
from synaptane.config import Settings
from synaptane.services import content, search
from synaptane.services.extraction import EMPTY, FAILED, OK, Manifest, update_index
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.test_offsite import write_config
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy"
WITNESSES = {  # scripts/legacy_files.py
    "archives/printer-memo.doc": "Zanzibar",
    "archives/spare-parts.xls": "Okavango",
    "archives/kickoff-slides.ppt": "Kilimanjaro",
}
# Words of the .ppt found in a text box, a table and the speaker notes, not only in titles.
SLIDE_WORDS = ("Kilimanjaro", "Serengeti", "Kalahari", "Zambezi")
CATDOC = extract.legacy_available()


def deposit(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> None:
    for path in WITNESSES:
        data = (FIXTURES / Path(path).name).read_bytes()
        content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, data)
    # A damaged file: its failure is recorded, the others are read.
    broken = (FIXTURES / "printer-memo.doc").read_bytes()[:3000]
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "archives/broken.doc", broken)
    db.commit()


def index(ctx: Workspace, wiki: Wiki) -> Manifest:
    update_index(ctx, wiki.infra.id, ctx.local_repository(wiki.infra))
    return Manifest(ctx.extract_path(wiki.infra.id))


@pytest.mark.skipif(not CATDOC, reason="catdoc absent (apt install catdoc)")
def test_old_office_files_are_searchable(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    deposit(db, database, ctx, wiki)
    manifest = index(ctx, wiki)
    for path, word in WITNESSES.items():
        entry = manifest.get(f"content/{path}")
        assert entry is not None and entry.status == OK, path
        hits = search.search(db, wiki.alice, ctx, word, addresses=["acme/infra"])
        assert [(hit.path, hit.extracted) for hit in hits] == [(path, True)], word
    for word in SLIDE_WORDS:
        hits = search.search(db, wiki.alice, ctx, word, addresses=["acme/infra"])
        assert [hit.path for hit in hits] == ["archives/kickoff-slides.ppt"], word
    broken = manifest.get("content/archives/broken.doc")
    assert broken is not None and broken.status == FAILED
    assert broken.error.startswith("catdoc: ")


@pytest.mark.skipif(not CATDOC, reason="catdoc absent (apt install catdoc)")
def test_old_office_file_page_shows_its_text_and_a_download(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, client: TestClient
) -> None:
    deposit(db, database, ctx, wiki)
    index(ctx, wiki)
    login(client, "alice")
    page = client.get("/o/acme/infra/p/archives/spare-parts.xls")
    assert page.status_code == 200 and "Okavango" in page.text and "download=1" in page.text
    page = client.get("/o/acme/infra/p/archives/kickoff-slides.ppt")
    assert page.status_code == 200 and "download=1" in page.text
    for word in (*SLIDE_WORDS, "Kalahari\t12\t75 inch"):
        assert word in page.text, word
    file = client.get("/o/acme/infra/raw/archives/spare-parts.xls", params={"download": 1})
    assert file.content == (FIXTURES / "spare-parts.xls").read_bytes()
    assert file.headers["content-disposition"].startswith("attachment")


def test_without_catdoc_old_word_and_excel_files_are_kept_without_text(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deposit(db, database, ctx, wiki)
    # Git, but neither catdoc nor xls2csv.
    (tmp_path / "git").symlink_to(shutil.which("git") or "git")
    with monkeypatch.context() as patch:
        patch.setenv("PATH", str(tmp_path))
        manifest = index(ctx, wiki)
    for path in ("archives/printer-memo.doc", "archives/spare-parts.xls", "archives/broken.doc"):
        entry = manifest.get(f"content/{path}")
        assert entry is not None and entry.status == EMPTY and not entry.error, path
    files = {f.path for f in content.list_files(wiki.alice, ctx, wiki.infra)}
    assert set(WITNESSES) <= files
    assert search.search(db, wiki.alice, ctx, "Okavango", addresses=["acme/infra"]) == []
    # The .ppt does not need catdoc.
    slides = manifest.get("content/archives/kickoff-slides.ppt")
    assert slides is not None and slides.status == OK
    hits = search.search(db, wiki.alice, ctx, "Zambezi", addresses=["acme/infra"])
    assert [hit.path for hit in hits] == ["archives/kickoff-slides.ppt"]


def test_doctor_reports_catdoc(
    settings: Settings,
    database: Database,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.key_file.chmod(0o600)
    settings.repos_dir.mkdir()
    config = write_config(settings, tmp_path / "synaptane.toml")
    main(["--config", str(config), "doctor"])
    out = capsys.readouterr().out
    if CATDOC:
        assert "[ok] catdoc installed" in out
    monkeypatch.setattr(extract, "legacy_available", lambda: False)
    main(["--config", str(config), "doctor"])
    out = capsys.readouterr().out
    assert "[warning] catdoc installed — install catdoc (text of the old Word and Excel" in out
