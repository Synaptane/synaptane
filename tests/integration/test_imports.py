"""Import of a zip of Markdown files: what the check says, what is written, what an archive
that is not to be trusted cannot do."""

import io
import stat
import zipfile
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.services import content, imports, imports_confluence
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job
from tests.exports import TASKS, WIKI, confluence_export, confluence_page, notion_export, zipped
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
    before = content.list_files(wiki.alice, ctx, wiki.infra)
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# Le mien")
    with patch.object(imports, "list_files", return_value=before):  # as seen by the check
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
    with patch.object(imports, "MAX_FILES", 1), pytest.raises(SynaptaneError, match="Too many"):
        imports.receive(wiki.alice, ctx, wiki.infra, archive({"a.md": b"", "b.md": b""}))
    assert not list(ctx.imports_path(wiki.infra.id).iterdir())  # nothing kept


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


# --- Notion and Confluence exports ----------------------------------------------------------


def sent(
    files: Mapping[str, bytes | str], nested: Mapping[str, Mapping[str, bytes | str]] | None = None
) -> io.BytesIO:
    return io.BytesIO(zipped(files, nested))


def test_a_notion_export_is_converted_then_written_in_one_commit(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent(notion_export()))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token, "notion")
    assert (plan.format, plan.detected) == ("notion", "notion")
    assert (plan.pages, plan.attachments) == (7, 1)
    assert plan.links.rewritten == 7 and plan.links.broken_count == 2
    assert plan.skipped == [f"Wiki {WIKI}/Tâches {TASKS}.csv"]
    assert all(entry.data is None for entry in plan.new)  # the check keeps nothing in memory

    repo = ctx.local_repository(wiki.infra)
    head = repo.head()
    assert imports.apply(db, database, wiki.alice, ctx, wiki.infra, token, "notion") == 8
    assert git(repo.path, "rev-list", "--count", f"{head}..HEAD").strip() == "1"
    subject = git(repo.path, "log", "-1", "--format=%s").strip()
    assert subject == "Import of 8 files (Notion export)"
    task = content.read_file(wiki.alice, ctx, wiki.infra, "notion/Wiki/Tâches/Écrire la doc.md")
    assert (task.text or "").startswith('---\nStatus: "Done"\n')
    image = content.read_file(wiki.alice, ctx, wiki.infra, "notion/Wiki/Untitled.png")
    assert image.data == b"\x89PNG\r\n\x1a\nfake"


def test_a_large_notion_workspace_comes_as_a_zip_of_zips(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    export = notion_export()
    names = sorted(export)
    first = {name: export[name] for name in names[:4]}
    second = {name: export[name] for name in names[4:]}
    second[names[0]] = "# Doublon d'une autre partie\n"  # the first one is kept
    parts = {"Export-1234-Part-1.zip": first, "Export-1234-Part-2.zip": second}
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent({"__MACOSX/._x": b""}, parts))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert plan.format == "notion" and (plan.pages, plan.attachments) == (7, 1)
    imports.apply(db, database, wiki.alice, ctx, wiki.infra, token)
    home = content.read_file(wiki.alice, ctx, wiki.infra, "Wiki/README.md").text or ""
    assert home.startswith("# Wiki\n\nVoir [Projets](Projets/README.md)")
    assert sorted(p.name for p in ctx.imports_path(wiki.infra.id).iterdir()) == []


def test_the_parts_of_a_zip_of_zips_are_not_trusted(ctx: Workspace, wiki: Wiki) -> None:
    def receive(**parts: Mapping[str, bytes | str]) -> str:
        return imports.receive(wiki.alice, ctx, wiki.infra, sent({}, parts))

    with pytest.raises(SynaptaneError, match="damaged: a.zip"):
        imports.receive(wiki.alice, ctx, wiki.infra, sent({"a.zip": b"pas un zip"}))
    with patch.object(imports, "MAX_TOTAL", 100), pytest.raises(SynaptaneError, match="unpacked"):
        receive(**{"a.zip": {"x.md": "x" * 50}, "b.zip": {"y.md": "y" * 50}})
    with patch.object(imports, "MAX_TOTAL", 300), pytest.raises(SynaptaneError, match="unpacked"):
        receive(**{"a.zip": {"x.md": "x" * 200}, "b.zip": {"y.md": "y" * 200}})  # what they hold
    with patch.object(imports, "MAX_FILES", 2), pytest.raises(SynaptaneError, match="Too many"):
        receive(**{"a.zip": {"x.md": "", "y.md": ""}, "b.zip": {"z.md": ""}})
    assert not list(ctx.imports_path(wiki.infra.id).iterdir())  # nothing left behind

    # A file larger than the limit is not copied whole: it is set aside as too large.
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as part:
        part.writestr("gros.bin", b"\0" * (content.MAX_UPLOAD + 5))
        part.writestr("ok.md", "# Ok")
        link = zipfile.ZipInfo("lien.md")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        part.writestr(link, "/etc/passwd")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zipped_parts:
        zipped_parts.writestr("Part-1.zip", inner.getvalue())
    outer.seek(0)
    token = imports.receive(wiki.alice, ctx, wiki.infra, outer)
    stored = ctx.imports_path(wiki.infra.id) / f"{wiki.alice.user.id}-{token}.zip"
    with zipfile.ZipFile(stored) as flat:
        assert flat.getinfo("gros.bin").file_size == content.MAX_UPLOAD + 1
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert plan.too_large == ["gros.bin"] and plan.refused == ["lien.md"]
    assert [e.path for e in plan.new] == ["ok.md"]
    # Zips next to other files are attachments, kept as they are.
    token = imports.receive(
        wiki.alice, ctx, wiki.infra, sent({"a.zip": zipped({"x.md": ""}), "b.md": ""})
    )
    assert {e.path for e in imports.plan(wiki.alice, ctx, wiki.infra, token).new} == {
        "a.zip",
        "b.md",
    }


def test_a_confluence_export_is_converted(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent(confluence_export()))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token, "wiki/ops")
    assert plan.format == "confluence" and (plan.pages, plan.attachments) == (4, 3)
    assert "OPS/styles/site.css" in plan.skipped
    assert ("Ops Home/README.md", "Disparue_99999.html") in plan.links.broken
    imports.apply(db, database, wiki.alice, ctx, wiki.infra, token, "wiki/ops")
    backup = "wiki/ops/Ops Home/Procédures/Backup/README.md"
    assert "> [!danger]" in (content.read_file(wiki.alice, ctx, wiki.infra, backup).text or "")
    assert (
        content.read_file(
            wiki.alice, ctx, wiki.infra, "wiki/ops/Ops Home/Procédures/Backup/runbook.pdf"
        ).data
        == b"%PDF-1.4 fake"
    )


def test_a_confluence_xml_export_is_refused(ctx: Workspace, wiki: Wiki) -> None:
    xml = {"export/entities.xml": "<hibernate-generic/>", "export/exportDescriptor.properties": ""}
    with pytest.raises(SynaptaneError, match="Confluence XML export"):
        imports.receive(wiki.alice, ctx, wiki.infra, sent(xml))
    assert not list(ctx.imports_path(wiki.infra.id).iterdir())


def test_converted_pages_go_through_the_secret_check(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    files = confluence_export()
    files["OPS/FAQ_65542.html"] = confluence_page(
        "FAQ", [("index.html", "Ops")], f"<pre>{KEY}\nMIIE…</pre>"
    )
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent(files))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert [e.path for e in plan.with_secrets] == ["Ops Home/FAQ.md"]
    assert plan.with_secrets[0].findings[0].kind == "private key"
    imports.apply(db, database, wiki.alice, ctx, wiki.infra, token)
    paths = {b.path for b in content.list_files(wiki.alice, ctx, wiki.infra)}
    assert "Ops Home/README.md" in paths and "Ops Home/FAQ.md" not in paths

    notion = {f"Base {TASKS}.csv": "Name,Clé\nServeur,ghp_" + "a" * 36 + "\n"}
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent(notion))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token, "base")
    assert [e.path for e in plan.with_secrets] == ["base/Base.md"]
    written = imports.apply(
        db, database, wiki.alice, ctx, wiki.infra, token, "base", keep=frozenset({"base/Base.md"})
    )
    assert written == 1


def test_a_format_can_be_forced_at_the_check(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent(notion_export()))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token, format="markdown")
    assert (plan.format, plan.detected) == ("markdown", "notion") and not plan.skipped
    assert plan.links.rewritten == 0
    assert imports.plan(wiki.alice, ctx, wiki.infra, token, format="inconnu").format == "notion"
    imports.apply(db, database, wiki.alice, ctx, wiki.infra, token, format="markdown")
    raw = content.read_file(wiki.alice, ctx, wiki.infra, f"Wiki {WIKI}.md").text or ""
    assert raw.startswith("# Wiki\n\nVoir [Projets](Wiki%20")


def test_the_check_page_of_an_export(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    form = client.get("/o/acme/infra/-/import")
    assert "Export Notion" in form.text and "Détecter automatiquement" in form.text
    response = client.post(
        "/o/acme/infra/-/import",
        data={"csrf": csrf(client, "/o/acme/infra/-/import")},
        files={"archive": ("export.zip", sent(notion_export()), "application/zip")},
        follow_redirects=False,
    )
    check_url = response.headers["location"]
    check = client.get(check_url).text
    assert "Format reconnu\u00a0: Export Notion." in check
    assert "7 liens réécrits" in check and "Wiki/Tâches/README.md" in check
    assert "Liens qui ne mènent nulle part" in check and "../../hors.md" in check
    assert f"Tâches {TASKS}.csv" in check  # not imported as it is

    raw = client.get(check_url + "?format=markdown").text
    assert "Format reconnu\u00a0: Export Notion, lu comme\u00a0:" in raw
    assert '<option value="markdown" selected>' in raw
    assert 'name="format" value="markdown"' in raw  # carried to the confirmation
    done = client.post(
        check_url,
        data={"csrf": csrf(client, check_url), "format": "markdown"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert client.get(f"/o/acme/infra/p/Wiki {WIKI}.md").status_code == 200


def test_the_format_can_be_chosen_when_sending(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    response = client.post(
        "/o/acme/infra/-/import",
        data={
            "csrf": csrf(client, "/o/acme/infra/-/import"),
            "format": "confluence",
            "folder": "ops",
        },
        files={"archive": ("ops.zip", sent(confluence_export()), "application/zip")},
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert "format=confluence" in location and "folder=ops" in location
    check = client.get(location).text
    assert "Format reconnu\u00a0: Export Confluence (HTML)." in check
    url = location.split("?")[0]
    done = client.post(
        url,
        data={"csrf": csrf(client, location), "folder": "ops", "format": "confluence"},
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == "/o/acme/infra/p/ops"
    page = client.get("/o/acme/infra/p/ops/Ops Home/Procédures/Backup/README.md").text
    assert 'class="callout callout-danger"' in page and "Jamais le vendredi." in page
    assert "<script>alert" not in page

    xml = {"entities.xml": "<x/>", "exportDescriptor.properties": ""}
    refused = client.post(
        "/o/acme/infra/-/import",
        data={"csrf": csrf(client, "/o/acme/infra/-/import")},
        files={"archive": ("xml.zip", sent(xml), "application/zip")},
    )
    assert "export XML de Confluence" in refused.text


def test_a_reader_cannot_send_check_nor_confirm_an_export(
    client: TestClient, ctx: Workspace, wiki: Wiki
) -> None:
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent(notion_export()))
    login(client, "bob")
    url = f"/o/acme/infra/-/import/{token}"
    assert client.get(url, follow_redirects=False).status_code in (303, 403, 404)
    for target in ("/o/acme/infra/-/import", url, url + "/cancel"):
        response = client.post(
            target,
            data={"csrf": csrf(client, "/o/acme"), "format": "notion"},
            files={"archive": ("x.zip", sent(notion_export()), "application/zip")}
            if target.endswith("import")
            else None,
            follow_redirects=False,
        )
        assert response.status_code in (303, 403, 404)
    assert ctx.imports_path(wiki.infra.id).joinpath(f"{wiki.alice.user.id}-{token}.zip").exists()
    with pytest.raises(SynaptaneError, match="not allowed"):
        imports.receive(wiki.bob, ctx, wiki.infra, sent(notion_export()))


def test_an_export_without_csrf_is_refused(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    response = client.post(
        "/o/acme/infra/-/import",
        data={"format": "notion"},
        files={"archive": ("x.zip", sent(notion_export()), "application/zip")},
        follow_redirects=False,
    )
    assert response.status_code in (400, 403, 422)


def encrypted(archive: bytes, name: str) -> io.BytesIO:
    """The archive, with `name` marked as encrypted in its central directory (zipfile cannot
    write an encrypted entry)."""
    data = bytearray(archive)
    position = data.find(b"PK\x01\x02")
    while position >= 0:
        length = int.from_bytes(data[position + 28 : position + 30], "little")
        if data[position + 46 : position + 46 + length] == name.encode():
            data[position + 8] |= 1
        position = data.find(b"PK\x01\x02", position + 4)
    return io.BytesIO(bytes(data))


def test_an_encrypted_page_of_an_export_is_refused_unread(ctx: Workspace, wiki: Wiki) -> None:
    files = {**confluence_export(), "OPS/Secret_70000.html": "<p>chiffré</p>"}
    sealed = encrypted(zipped(files), "OPS/Secret_70000.html")
    token = imports.receive(wiki.alice, ctx, wiki.infra, sealed)
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert plan.format == "confluence" and plan.refused == ["Secret_70000.html"]
    assert plan.pages == 4


def test_encrypted_parts_of_a_zip_of_zips(ctx: Workspace, wiki: Wiki) -> None:
    sealed_part = encrypted(zipped({"Part-1.zip": zipped({"a.md": ""})}), "Part-1.zip")
    with pytest.raises(SynaptaneError, match="damaged: Part-1.zip"):
        imports.receive(wiki.alice, ctx, wiki.infra, sealed_part)
    inner = encrypted(zipped({"a.md": "# A", "b.md": "# B"}), "a.md").getvalue()
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent({"Part-1.zip": inner}))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert plan.refused == ["a.md"] and [e.path for e in plan.new] == ["b.md"]


def corrupted(archive: bytes, name: str) -> io.BytesIO:
    """The archive, with the compressed data of `name` damaged (an invalid deflate block)."""
    data = bytearray(archive)
    position = data.find(b"PK\x03\x04")
    while position >= 0:
        length = int.from_bytes(data[position + 26 : position + 28], "little")
        extra = int.from_bytes(data[position + 28 : position + 30], "little")
        if data[position + 30 : position + 30 + length] == name.encode():
            data[position + 30 + length + extra] = 0xFF  # block type 3: does not exist
        position = data.find(b"PK\x03\x04", position + 4)
    return io.BytesIO(bytes(data))


def test_a_damaged_page_or_part_is_reported_not_an_error(ctx: Workspace, wiki: Wiki) -> None:
    files = notion_export()
    page = next(name for name in files if name.endswith(".md"))
    token = imports.receive(wiki.alice, ctx, wiki.infra, corrupted(zipped(files), page))
    with pytest.raises(SynaptaneError, match="damaged"):
        imports.plan(wiki.alice, ctx, wiki.infra, token)
    token = imports.receive(wiki.alice, ctx, wiki.infra, corrupted(zipped({"a.md": "# A"}), "a.md"))
    with pytest.raises(SynaptaneError, match="damaged: a.md"):
        imports.plan(wiki.alice, ctx, wiki.infra, token)
    inner = corrupted(zipped({"a.md": "# A " * 100}), "a.md").getvalue()
    with pytest.raises(SynaptaneError, match="damaged: a.md"):
        imports.receive(wiki.alice, ctx, wiki.infra, sent({"Part-1.zip": inner}))


def test_paths_that_leave_an_export_are_refused_before_it_is_laid_out(
    ctx: Workspace, wiki: Wiki
) -> None:
    # An absolute path once made the layout of a Notion export loop forever.
    files = {**notion_export(), "/etc/evil.md": "# x", "a//b.md": "# x", "c/../d.md": "# x"}
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent(files))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert plan.format == "notion"
    assert sorted(plan.refused) == ["/etc/evil.md", "a//b.md", "c/../d.md"]
    assert plan.pages == 7
    files = {**confluence_export(), "OPS//x.html": "<p>x</p>", "OPS/attachments/../../y.png": "y"}
    token = imports.receive(wiki.alice, ctx, wiki.infra, sent(files))
    plan = imports.plan(wiki.alice, ctx, wiki.infra, token)
    assert plan.format == "confluence"
    assert sorted(plan.refused) == ["/x.html", "attachments/../../y.png"]


def test_a_zip_of_too_many_zips_is_refused(ctx: Workspace, wiki: Wiki) -> None:
    parts: dict[str, dict[str, str]] = {f"Part-{n}.zip": {} for n in range(3)}
    with patch.object(imports, "MAX_FILES", 2), pytest.raises(SynaptaneError, match="Too many"):
        imports.receive(wiki.alice, ctx, wiki.infra, sent({}, parts))


def test_the_check_says_why_a_file_of_an_export_is_left_out(client: TestClient, wiki: Wiki) -> None:
    files = {
        **confluence_export(),
        "OPS/Huge_70000.html": confluence_page("Énorme", [("index.html", "Ops")], "x" * 20_000),
    }
    login(client, "alice")
    response = client.post(
        "/o/acme/infra/-/import",
        data={"csrf": csrf(client, "/o/acme/infra/-/import")},
        files={"archive": ("ops.zip", sent(files), "application/zip")},
        follow_redirects=False,
    )
    with patch.object(imports_confluence, "MAX_PAGE_HTML", 15_000):
        check = client.get(response.headers["location"]).text
    assert "<code>OPS/Huge_70000.html</code>" in check
    assert "trop grande pour être convertie (0 Mo maximum)" in check or (
        "trop grande pour être convertie (0 Mo maximum)" in check
    )
    assert '<details class="card section-gap-small" open>' in check
