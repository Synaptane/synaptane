"""Import of a zip of Markdown files: what the check says, what is written, what an archive
that is not to be trusted cannot do."""

import io
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.services import content, imports
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job
from tests.integration.gitutil import git
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

KEY = "-----BEGIN RSA PRIVATE KEY-----"


def archive(files: dict[str, bytes]) -> io.BytesIO:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in files.items():
            zipped.writestr(name, data)
    out.seek(0)
    return out


VAULT = {
    "Coffre/.obsidian/app.json": b"{}",
    "Coffre/.trash/old.md": b"# Vieux",
    "Coffre/.DS_Store": b"x",
    "__MACOSX/Coffre/._Note.md": b"x",
    "Coffre/Note.md": b"# Note\n\n![[photo.png]]\n",
    "Coffre/sous/Autre.md": b"# Autre\n\n[[Note]]\n",
    "Coffre/pj/photo.png": b"\x89PNG\r\n\x1a\nfake",
}


def test_a_vault_is_checked_then_written_in_one_commit(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    before = content.list_files(wiki.alice, ctx, wiki.infra)
    token = imports.receive(wiki.alice, ctx, wiki.infra, archive(VAULT))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert (plan.pages, plan.attachments, plan.left_out) == (2, 1, 3)
    assert {e.path for e in plan.new} == {"Note.md", "sous/Autre.md", "pj/photo.png"}
    assert content.list_files(wiki.alice, ctx, wiki.infra) == before  # the check writes nothing

    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    assert imports.apply(db, database, wiki.alice, ctx, wiki.infra, token) == 3
    assert git(repo.path, "rev-list", "--count", f"{head}..HEAD").strip() == "1"
    assert git(repo.path, "log", "-1", "--format=%an").strip() == wiki.alice.user.display_name
    page = content.read_file(wiki.alice, ctx, wiki.infra, "Note.md")
    assert page.text == "# Note\n\n![[photo.png]]\n"  # as written
    kinds = set(db.scalars(select(Job.kind).where(Job.space_id == wiki.infra.id)))
    assert {"sync", "index"} <= kinds
    with pytest.raises(SynaptaneError, match="expired"):
        imports.plan(wiki.alice, ctx, wiki.infra, token)


def test_into_a_folder_and_nothing_overwritten(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    files = {"README.md": b"# Importe", "nouveau.md": b"# Nouveau"}
    token = imports.receive(wiki.alice, ctx, wiki.infra, archive(files))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert plan.existing == ["README.md"] and [e.path for e in plan.new] == ["nouveau.md"]
    original = content.read_file(wiki.alice, ctx, wiki.infra, "README.md").text
    imports.apply(db, database, wiki.alice, ctx, wiki.infra, token)
    assert content.read_file(wiki.alice, ctx, wiki.infra, "README.md").text == original

    token = imports.receive(wiki.alice, ctx, wiki.infra, archive(files))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token, "/import/obsidian/")
    assert plan.folder == "import/obsidian" and not plan.existing
    imports.apply(db, database, wiki.alice, ctx, wiki.infra, token, "/import/obsidian/")
    assert content.read_file(wiki.alice, ctx, wiki.infra, "import/obsidian/README.md").text


def test_a_file_written_since_the_check_stays(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    token = imports.receive(wiki.alice, ctx, wiki.infra, archive({"a.md": b"# A", "b.md": b"# B"}))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# Le mien")
    with patch.object(imports, "plan", return_value=plan):
        assert imports.apply(db, database, wiki.alice, ctx, wiki.infra, token) == 1
    assert content.read_file(wiki.alice, ctx, wiki.infra, "a.md").text == "# Le mien"


def test_likely_secrets_are_left_out_unless_kept(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    files = {"cle.md": KEY.encode(), "autre.md": KEY.encode(), "sain.md": b"# Sain"}
    token = imports.receive(wiki.alice, ctx, wiki.infra, archive(files))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert {e.path for e in plan.with_secrets} == {"cle.md", "autre.md"}
    assert plan.with_secrets[0].findings[0].kind == "private key"
    written = imports.apply(
        db, database, wiki.alice, ctx, wiki.infra, token, keep=frozenset({"cle.md"})
    )
    assert written == 2
    paths = {b.path for b in content.list_files(wiki.alice, ctx, wiki.infra)}
    assert {"cle.md", "sain.md"} <= paths and "autre.md" not in paths


def test_an_archive_cannot_leave_the_space_nor_use_links(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, tmp_path: Path
) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zipped:
        zipped.writestr("ok.md", b"# Ok")
        zipped.writestr("../../hors.md", b"# Hors")
        zipped.writestr("dossier/../../hors2.md", b"# Hors")
        zipped.writestr("/etc/passwd", b"root")
        zipped.writestr("ligne\nnouvelle.md", b"x")
        zipped.writestr("README.md/sous.md", b"sous un fichier")
        zipped.writestr("serveurs", b"a la place d'un dossier")
        link = zipfile.ZipInfo("lien.md")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(link, b"/etc/passwd")
    out.seek(0)
    token = imports.receive(wiki.alice, ctx, wiki.infra, out)
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert [e.path for e in plan.new] == ["ok.md"]
    assert len(plan.refused) == 7
    imports.apply(db, database, wiki.alice, ctx, wiki.infra, token)
    repo = ctx.local_repository(wiki.infra)
    assert not (repo.path.parent / "hors.md").exists()
    assert not (repo.path / "content" / "lien.md").exists()


def test_declared_sizes_are_not_trusted(ctx: Workspace, wiki: Wiki) -> None:
    big = {"gros.bin": b"\0" * (content.MAX_UPLOAD + 1), "ok.md": b"# Ok"}
    token = imports.receive(wiki.alice, ctx, wiki.infra, archive(big))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert plan.too_large == ["gros.bin"] and [e.path for e in plan.new] == ["ok.md"]

    with patch.object(imports, "MAX_TOTAL", 100):
        token = imports.receive(wiki.alice, ctx, wiki.infra, archive({"a.md": b"x" * 200}))
        with pytest.raises(SynaptaneError, match="once unpacked"):
            imports.plan(wiki.alice, ctx, wiki.infra, token)


def test_limits_of_an_archive(ctx: Workspace, wiki: Wiki) -> None:
    with pytest.raises(SynaptaneError, match="not a zip"):
        imports.receive(wiki.alice, ctx, wiki.infra, io.BytesIO(b"pas un zip"))
    with patch.object(imports, "MAX_ARCHIVE", 10), pytest.raises(SynaptaneError, match="too large"):
        imports.receive(wiki.alice, ctx, wiki.infra, archive({"a.md": b"# A"}))
    with patch.object(imports, "MAX_FILES", 1):
        token = imports.receive(wiki.alice, ctx, wiki.infra, archive({"a.md": b"", "b.md": b""}))
        with pytest.raises(SynaptaneError, match="Too many"):
            imports.plan(wiki.alice, ctx, wiki.infra, token)


def test_an_import_belongs_to_one_person_and_one_space(ctx: Workspace, wiki: Wiki) -> None:
    token = imports.receive(wiki.alice, ctx, wiki.infra, archive({"a.md": b"# A"}))
    with pytest.raises(SynaptaneError, match="expired"):
        imports.plan(wiki.root, ctx, wiki.infra, token)
    with pytest.raises(SynaptaneError, match="not allowed"):  # a reader
        imports.plan(wiki.bob, ctx, wiki.infra, token)
    with pytest.raises(SynaptaneError):
        imports.plan(wiki.alice, ctx, wiki.infra, "../../" + token)
    second = imports.receive(wiki.alice, ctx, wiki.infra, archive({"b.md": b"# B"}))
    with pytest.raises(SynaptaneError, match="expired"):  # the previous one is gone
        imports.plan(wiki.alice, ctx, wiki.infra, token)
    imports.cancel(wiki.alice, ctx, wiki.infra, second)
    assert not list(ctx.imports_path(wiki.infra.id).iterdir())


def test_the_pages_of_an_import(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    assert "/-/import" in client.get("/o/acme/infra").text
    form = client.get("/o/acme/infra/-/import?folder=guides")
    assert form.status_code == 200 and 'value="guides"' in form.text
    sent = client.post(
        "/o/acme/infra/-/import",
        data={"csrf": csrf(client, "/o/acme/infra/-/import"), "folder": "guides"},
        files={"archive": ("coffre.zip", archive(VAULT), "application/zip")},
        follow_redirects=False,
    )
    assert sent.status_code == 303 and "folder=guides" in sent.headers["location"]
    check = client.get(sent.headers["location"])
    assert "2 pages" in check.text and "guides/sous/Autre.md" in check.text
    assert ".obsidian" in check.text
    url = sent.headers["location"].split("?")[0]
    done = client.post(
        url,
        data={"csrf": csrf(client, sent.headers["location"]), "folder": "guides"},
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == "/o/acme/infra/p/guides"
    page = client.get("/o/acme/infra/p/guides/Note.md").text
    assert "/raw/guides/pj/photo.png" in page or "guides/pj/photo.png" in page


def test_a_reader_cannot_import(client: TestClient, wiki: Wiki) -> None:
    login(client, "bob")
    assert client.get("/o/acme/infra/-/import").status_code in (403, 404)
