"""`synaptane demo`: demo data (examples/acme-wiki), ready to use and idempotent."""

import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane import extract
from synaptane.cli import main
from synaptane.config import Settings
from synaptane.core.access import OrgRole
from synaptane.services import (
    accounts,
    content,
    deposits,
    freshness,
    git_sync,
    health,
    load_actor,
    search,
)
from synaptane.services import demo as demo_service
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Organization, OrgMember, Space, User
from tests.integration.conftest import actor_of, make_user
from tests.integration.gitutil import Remote
from tests.integration.test_web import login

PASSWORD = "demo-password-for-tests"
# Fixed verification dates in the dataset: the test does not age with them.
TODAY = date(2026, 10, 9)
# Sample files that are not Markdown (scripts/demo_files.py), each with a word found nowhere
# else in the dataset: read as text (HTML), from their text layer (PDF, Office) or by OCR.
WITNESSES = {
    "infra/documents/brightrack-support-contract.pdf": "Lindqvist",
    "infra/documents/hardware-inventory.xlsx": "docking",
    "infra/documents/status-report-2026-w40.html": "uptime",
    "team/documents/onboarding-checklist.docx": "lanyard",
    "team/documents/2026-q3-infrastructure-review.pptx": "MTTR",
}
SCANNED = {  # no text layer: OCR only
    "infra/documents/2026-09-delivery-note.pdf": "Kestrel",
    "infra/documents/rack-b-elevation.png": "Annapurna",
}
LEGACY = {  # old binary Office format (scripts/legacy_files.py): read by catdoc
    "infra/documents/archive/hardware-inventory-2019.xls": "NetWare",
}
OCR = shutil.which("tesseract") is not None
CATDOC = extract.legacy_available()


def _config(settings: Settings, tmp_path: Path, *, local: bool = True, url: str = "") -> Path:
    config = tmp_path / "synaptane.toml"
    config.write_text(
        f'public_url = "{settings.public_url}"\n'
        f'database_url = "{url or settings.database_url}"\n'
        f'data_dir = "{settings.data_dir}"\n'
        f"allow_local_repos = {'true' if local else 'false'}\n",
        encoding="utf-8",
    )
    return config


def _demo(config: Path, *extra: str) -> int:
    return main(["--config", str(config), "demo", *extra])


def _space(db: Session) -> Space:
    space = db.scalar(select(Space).join(Space.organization).where(Organization.slug == "acme"))
    assert space is not None
    return space


def test_demo_sets_up_a_searchable_space_once(
    settings: Settings,
    database: Database,
    db: Session,
    ctx: Workspace,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(settings, tmp_path)
    monkeypatch.setenv("SYNAPTANE_DEMO_PASSWORD", PASSWORD)
    monkeypatch.setattr(freshness, "today_for", lambda space, now=None: TODAY)
    assert _demo(config) == 0
    out = capsys.readouterr().out
    assert f"demo / {PASSWORD}" in out and "shown only once" in out
    assert "http://localhost/o/acme/it" in out and "http://localhost/mcp" in out
    assert "50 pages, 6 notes waiting in the inbox" in out
    assert "super admin demo, organization acme, space acme/it, Git repository" in out

    user = accounts.authenticate(db, "demo", PASSWORD)
    assert user is not None and user.is_superadmin
    space = _space(db)
    assert space.address == "acme/it" and space.sync_status == "ok"
    assert space.repo_url == demo_service.repo_path(settings).as_uri()
    # Organization administrator: their assistants (MCP) see the space.
    member = db.get(OrgMember, (space.org_id, user.id))
    assert member is not None and member.role == OrgRole.ADMIN.value

    actor = load_actor(db, user)
    hits = search.search(db, actor, ctx, "vzdump", addresses=["acme/it"])
    assert {"infra/backups.md", "infra/proxmox.md"} <= {hit.path for hit in hits}
    backups = next(hit for hit in hits if hit.path == "infra/backups.md")
    assert backups.verified and not backups.stale
    firewall = search.search(db, actor, ctx, "OPNsense")[0]
    assert firewall.path == "network/firewall.md" and firewall.stale

    notes = deposits.list_items(actor, ctx, space)
    assert len(notes) == 6 and all(note.type == "note" for note in notes)
    with_file = next(note for note in notes if note.attachments)
    assert with_file.hint == "network/topology.md"
    csv = deposits.read_attachment(actor, ctx, space, with_file.name, "switch-inventory.csv")
    assert b"core-a,core,rack A" in csv

    report = health.space_health(actor, ctx, space)
    assert [(b.page, b.link) for b in report.broken] == [
        ("team/onboarding.md", "../procedures/security-training.md")
    ]
    assert [page.path for page in report.due] == ["network/firewall.md"]
    assert report.verified == 4
    repo = ctx.local_repository(space)
    assert (repo.blob(".synaptane/vocation.md") or b"").startswith(b"# Space purpose")
    assert "acme.example" in (repo.blob("content/team/members.md") or b"").decode()

    # A page written during the demo, then the command run again: nothing is recreated
    # or overwritten, the password stays the original one.
    content.write_file(db, database, actor, ctx, space, "notes.md", b"# Notes\n\nKept.\n")
    db.commit()
    git_sync.run_sync(database, ctx, space.id)
    monkeypatch.setenv("SYNAPTANE_DEMO_PASSWORD", "another-password-ignored")
    assert _demo(config) == 0
    out = capsys.readouterr().out
    assert "existing account, password unchanged" in out
    assert "nothing new, already in place" in out and "51 pages" in out
    db.expire_all()
    assert accounts.authenticate(db, "demo", PASSWORD) is not None
    assert len(db.scalars(select(Space)).all()) == 1
    assert ctx.local_repository(_space(db)).blob("content/notes.md") == b"# Notes\n\nKept.\n"


def test_demo_completes_an_existing_organization(
    settings: Settings, db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Organization `acme` already there, without the demo account: it becomes an admin."""
    from synaptane.services import organizations

    root = actor_of(db, make_user(db, "root", superadmin=True))
    organizations.create_organization(db, root, slug="acme", name="Acme")
    db.commit()
    assert _demo(_config(settings, tmp_path), "--password", PASSWORD) == 0
    out = capsys.readouterr().out
    assert "super admin demo, space acme/it, Git repository" in out
    user = accounts.get_user(db, "demo")
    member = db.get(OrgMember, (_space(db).org_id, user.id))
    assert member is not None and member.role == OrgRole.ADMIN.value


def _nothing_created(db: Session) -> None:
    db.expire_all()
    assert db.scalar(select(User).where(User.username == "demo")) is None
    assert db.scalars(select(Space)).all() == []


def test_demo_refuses_an_instance_that_is_not_ready(
    settings: Settings,
    db: Session,
    tmp_path: Path,
    new_database: Callable[[], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Local repositories refused: the command says what to set, for itself, the server and worker.
    assert _demo(_config(settings, tmp_path, local=False)) == 1
    err = capsys.readouterr().err
    assert "SYNAPTANE_ALLOW_LOCAL_REPOS=true" in err and "examples/compose.demo.yaml" in err
    _nothing_created(db)
    # Database without migrations.
    assert _demo(_config(settings, tmp_path, url=new_database())) == 1
    assert "synaptane db upgrade" in capsys.readouterr().err
    # Unreachable database.
    assert _demo(_config(settings, tmp_path, url="postgresql+psycopg://x:y@127.0.0.1:1/z")) == 1
    assert "database unreachable" in capsys.readouterr().err
    # Dataset not found.
    assert _demo(_config(settings, tmp_path), "--source", str(tmp_path / "absent")) == 1
    assert "demo data not found" in capsys.readouterr().err
    _nothing_created(db)
    # No master key: `synaptane init` first.
    settings.key_file.unlink()
    assert _demo(_config(settings, tmp_path)) == 1
    assert "synaptane init" in capsys.readouterr().err


def test_demo_leaves_foreign_accounts_and_spaces_alone(
    settings: Settings, db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from synaptane.services import organizations, spaces

    config = _config(settings, tmp_path)
    make_user(db, "demo")  # a regular account named demo
    db.commit()
    assert _demo(config) == 1
    assert "is not a super admin" in capsys.readouterr().err
    db.delete(accounts.get_user(db, "demo"))
    root = actor_of(db, make_user(db, "root", superadmin=True))
    acme = organizations.create_organization(db, root, slug="acme", name="Acme")
    other = Remote(tmp_path / "other")
    spaces.create_space(db, root, acme, slug="it", name="IT", repo_url=other.url)
    db.commit()
    assert _demo(config) == 1
    assert "already exists and is not the demo space" in capsys.readouterr().err
    assert _space(db).repo_url == other.url
    db.expire_all()
    assert db.scalar(select(User).where(User.username == "demo")) is None  # everything rolled back
    assert not demo_service.repo_path(settings).exists()


def test_dataset_is_found_from_the_source_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tesseract on one thread, as in the extraction child (extract._child_env): with one
    # thread per core in each pytest process, the OCR of the scans ran past its timeout.
    monkeypatch.setenv("OMP_THREAD_LIMIT", "1")
    dataset = demo_service.find_dataset()
    assert (dataset / "content" / "README.md").is_file()
    assert (dataset / ".synaptane" / "vocation.md").is_file()
    files = demo_service.dataset_files(dataset)
    assert {f"content/{path}" for path in (*WITNESSES, *SCANNED, *LEGACY)} <= set(files)
    # Reserved domains only (RFC 2606, .internal): no real address, documents included (their
    # extracted text; the scans and images only where Tesseract is installed, the old Office
    # files where catdoc is).
    for path, data in files.items():
        kind = content.kind_of(path, data)
        if kind in ("pdf", "office", "image"):
            suffix = PurePosixPath(path).suffix
            budget = extract.Budget()
            text = extract.extract_direct(kind, suffix, data, "eng", True, budget) or ""
            unread = (not OCR and path[len("content/") :] in SCANNED) or (
                not CATDOC and path[len("content/") :] in LEGACY
            )
            assert text.strip() or unread, (path, budget.notes)
        else:
            text = data.decode()
        for word in text.replace("(", " ").replace(")", " ").replace("`", " ").split():
            if "@" in word or word.endswith((".com", ".fr", ".lan", ".org", ".net")):
                assert word.rstrip('.,>"]').endswith((".example", ".internal")), (path, word)


def test_demo_pages_render_in_the_web_interface(
    settings: Settings, database: Database, ctx: Workspace, client: TestClient
) -> None:
    result = demo_service.run(settings, database, ctx, password=PASSWORD)
    assert result.password == PASSWORD and result.notes == 6
    login(client, "demo", PASSWORD)
    home = client.get("/o/acme/it")
    assert home.status_code == 200 and "IT team wiki" in home.text
    page = client.get("/o/acme/it/p/procedures/restore.md")
    assert page.status_code == 200 and "qmrestore" in page.text
    inbox = client.get("/o/acme/it/inbox")
    assert inbox.status_code == 200
    assert "Team meeting moves to Tuesday" in inbox.text and "Old server?" in inbox.text
    assert "security-training.md" in client.get("/o/acme/it/health").text
    # Search: the pages, and the notes not tidied yet.
    found = client.get("/search", params={"q": "WireGuard"})
    assert "tools/vpn.md" in found.text or "VPN" in found.text
    found = client.get("/search", params={"q": "firmware"})
    assert "2026-10-08T10-05-48Z-note-switch-inventory-b82c.md" in found.text
    # Files other than Markdown: an image in a page, an HTML page in an isolated frame, the
    # text extracted from an Office document under its download link.
    page = client.get("/o/acme/it/p/infra/servers.md")
    assert "documents/rack-b-elevation.png" in page.text and "<img" in page.text
    page = client.get("/o/acme/it/p/infra/documents/status-report-2026-w40.html")
    assert page.status_code == 200 and "?preview=1" in page.text and "sandbox=" in page.text
    page = client.get("/o/acme/it/p/team/documents/onboarding-checklist.docx")
    assert page.status_code == 200 and "lanyard" in page.text and "download=1" in page.text


def _searched(db: Session, ctx: Workspace, files: dict[str, str]) -> None:
    """Each witness word leads to its file, and to it alone."""
    actor = load_actor(db, accounts.get_user(db, "demo"))
    for path, word in files.items():
        hits = search.search(db, actor, ctx, word, addresses=["acme/it"])
        assert [hit.path for hit in hits] == [path], word
        assert hits[0].extracted == (not path.endswith(".html")), path


def test_demo_files_other_than_markdown_are_searchable(
    settings: Settings, database: Database, db: Session, ctx: Workspace
) -> None:
    demo_service.run(settings, database, ctx, password=PASSWORD)
    _searched(db, ctx, WITNESSES)


@pytest.mark.skipif(not OCR, reason="Tesseract absent")
def test_demo_scans_are_read_by_ocr(
    settings: Settings, database: Database, db: Session, ctx: Workspace
) -> None:
    demo_service.run(settings, database, ctx, password=PASSWORD)
    _searched(db, ctx, SCANNED)


@pytest.mark.skipif(not CATDOC, reason="catdoc absent")
def test_demo_old_office_files_are_read_by_catdoc(
    settings: Settings, database: Database, db: Session, ctx: Workspace
) -> None:
    demo_service.run(settings, database, ctx, password=PASSWORD)
    _searched(db, ctx, LEGACY)


def test_demo_reports_a_broken_demo_repository(
    settings: Settings, db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Repository folder present but empty (data deleted by hand): kept as is, and the failed
    synchronization is reported instead of an empty space announced as "ready"."""
    demo_service.repo_path(settings).mkdir(parents=True)
    assert _demo(_config(settings, tmp_path), "--password", PASSWORD) == 1
    assert "synchronization failed" in capsys.readouterr().err
    assert _space(db).sync_status == "error"


def test_demo_summary_names_the_worker_to_keep_running(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a container, the worker is the Compose service, already started; elsewhere, the
    `synaptane worker` command to start with the setting."""
    result = demo_service.DemoResult("demo", None, "acme/it", tmp_path / "acme-it.git")
    docker = demo_service.summary(settings, result, container=True)
    assert "`worker` service" in docker and "synaptane worker" not in docker
    local = demo_service.summary(settings, result, container=False)
    assert "Keep `synaptane worker` running (with SYNAPTANE_ALLOW_LOCAL_REPOS=true)" in local
    for text in (docker, local):
        assert "AI provider: http://localhost/o/acme/-/ai" in text

    markers: set[str] = set()
    monkeypatch.setattr(Path, "exists", lambda path: str(path) in markers)
    assert not demo_service.in_container()
    assert "synaptane worker" in demo_service.summary(settings, result)
    markers.add("/.dockerenv")
    assert demo_service.in_container()
    assert "`worker` service" in demo_service.summary(settings, result)
