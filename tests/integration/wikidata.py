"""Test "wiki" space: a repository with pages, text, HTML, PDF and a Word document."""

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from synaptane.core.access import SpaceRole
from synaptane.services import Actor, git_sync, organizations, spaces
from synaptane.services.extraction import update_index
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Space
from tests.integration.conftest import actor_of, make_user
from tests.integration.gitutil import Remote, git


def native_pdf(text: str) -> bytes:
    """One-page PDF with a real text layer (no dependency)."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def docx(text: str) -> bytes:
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:p><w:r><w:t>Fin</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


@dataclass
class Wiki:
    root: Actor
    alice: Actor  # contributor on infra
    bob: Actor  # reader on infra
    infra: Space
    other: Space  # another Acme space, invisible to Alice and Bob
    remote: Remote


@pytest.fixture
def wiki(db: Session, database: Database, ctx: Workspace, tmp_path: Path) -> Wiki:
    remote = Remote(tmp_path / "infra")
    remote.push(
        {
            "content/README.md": "# Infra\n\nVoir [serveurs](serveurs/proxmox.md).\n",
            "content/serveurs/proxmox.md": "# Proxmox\n\nLa sauvegarde nocturne tourne à 2h.\n\n"
            "Retour à l'[accueil](../README.md).\n",
            "content/serveurs/notes.txt": "Le mot de passe est dans le coffre.\n",
            "content/page.html": "<script>alert(1)</script>\n",
        }
    )
    files = {
        "content/rapport.pdf": native_pdf("Rapport Kubernetes annuel"),
        "content/specs.docx": docx("Cahier Terraform"),
    }
    for path, data in files.items():
        (remote.work / path).write_bytes(data)
    git(remote.work, "add", "-A")
    git(remote.work, "commit", "-qm", "Binaires")
    git(remote.work, "push", "-q", "origin", "HEAD:main")
    other_remote = Remote(tmp_path / "other")
    other_remote.push({"content/secret.md": "# Secret\n\nSauvegarde confidentielle.\n"})

    root = actor_of(db, make_user(db, "root", superadmin=True))
    acme = organizations.create_organization(db, root, slug="acme", name="Acme")
    infra = spaces.create_space(db, root, acme, slug="infra", name="Infra", repo_url=remote.url)
    other = spaces.create_space(
        db, root, acme, slug="autre", name="Autre", repo_url=other_remote.url
    )
    alice, bob = make_user(db, "alice"), make_user(db, "bob")
    spaces.set_member(db, root, infra, alice, SpaceRole.CONTRIBUTOR)
    spaces.set_member(db, root, infra, bob, SpaceRole.READER)
    db.commit()
    for space in (infra, other):
        git_sync.run_sync(database, ctx, space.id)
        update_index(ctx, space.id, ctx.local_repository(space))
    return Wiki(root, actor_of(db, alice), actor_of(db, bob), infra, other, remote)
