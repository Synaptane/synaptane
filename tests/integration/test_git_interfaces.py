import hashlib
import hmac
import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import SpaceRole
from synaptane.services import organizations, spaces
from synaptane.storage.db import Database
from synaptane.storage.models import Job, Space
from tests.integration.conftest import actor_of, make_user, token_for
from tests.integration.test_web import csrf, login

BASE = "/api/v1/organizations/acme/spaces/infra/git"


def setup_space(db: Session) -> Space:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    org = organizations.create_organization(db, root, slug="acme", name="Acme")
    space = spaces.create_space(
        db, root, org, slug="infra", name="Infra", repo_url="https://github.com/acme/infra.git"
    )
    spaces.set_member(db, root, space, make_user(db, "reader"), SpaceRole.READER)
    spaces.set_member(db, root, space, make_user(db, "writer"), SpaceRole.CONTRIBUTOR)
    db.commit()
    return space


def queued(database: Database) -> list[tuple[str, dict[str, object]]]:
    with database.session() as session:
        return [
            (j.kind, j.payload) for j in session.scalars(select(Job).where(Job.status == "queued"))
        ]


def auth(database: Database, username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(database, username)}"}


def test_credentials_status_and_sync_permissions(
    client: TestClient, database: Database, db: Session
) -> None:
    setup_space(db)
    root, reader, writer = (auth(database, u) for u in ("root", "reader", "writer"))

    response = client.put(
        f"{BASE}/credentials", json={"kind": "https", "token": "ghp_x"}, headers=root
    )
    assert response.status_code == 204
    assert queued(database) == [("sync", {})]  # new credentials trigger a new sync
    assert client.get(BASE, headers=root).json()["credentials"] == "https"
    reader_view = client.get(BASE, headers=reader).json()
    assert reader_view["credentials"] is None and reader_view["status"] == "never"
    assert "ghp_x" not in json.dumps(client.get(BASE, headers=root).json())

    wrong_kind = {"kind": "ssh", "private_key": "x", "known_hosts": "y"}
    assert client.put(f"{BASE}/credentials", json=wrong_kind, headers=root).status_code == 400
    assert (
        client.put(
            f"{BASE}/credentials", json={"kind": "https", "token": "t"}, headers=writer
        ).status_code
        == 403
    )
    assert client.post(f"{BASE}/sync", headers=reader).status_code == 403
    assert client.post(f"{BASE}/sync", headers=writer).status_code == 202
    assert (
        client.post(f"{BASE}/conflicts/resolve", json={"choices": {}}, headers=root).status_code
        == 400
    )


def test_webhook_triggers_a_sync_only_when_signed(
    client: TestClient, database: Database, db: Session
) -> None:
    setup_space(db)
    secret = client.post(f"{BASE}/webhook-secret", headers=auth(database, "root")).json()
    assert secret["url"] == "http://localhost/hooks/git/acme/infra"
    body = b'{"ref":"refs/heads/main"}'
    signature = "sha256=" + hmac.new(secret["secret"].encode(), body, hashlib.sha256).hexdigest()

    assert (
        client.post(
            "/hooks/git/acme/infra", content=body, headers={"X-Hub-Signature-256": "sha256=0"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/hooks/git/acme/nope", content=body, headers={"X-Hub-Signature-256": signature}
        ).status_code
        == 401
    )
    assert queued(database) == []
    ok = client.post(
        "/hooks/git/acme/infra", content=body, headers={"X-Hub-Signature-256": signature}
    )
    assert ok.status_code == 202
    gitlab = client.post(
        "/hooks/git/acme/infra", content=body, headers={"X-Gitlab-Token": secret["secret"]}
    )
    assert gitlab.status_code == 202
    assert queued(database) == [("sync", {})]


def test_space_page_git_blocks_and_conflict_resolution(
    client: TestClient, database: Database, db: Session
) -> None:
    space = setup_space(db)
    login(client, "reader")
    page = client.get("/o/acme/infra/-/settings").text
    assert "Jamais synchronisé" in page and "Accès au dépôt" not in page
    assert "Synchroniser maintenant" not in page

    client.cookies.clear()
    login(client, "root")
    token = csrf(client, "/o/acme/infra/-/git")
    client.post("/o/acme/infra/-/git/https", data={"csrf": token, "token": "ghp_secret"})
    page = client.get("/o/acme/infra/-/git").text
    assert "accès HTTPS enregistré." in page
    assert "ghp_secret" not in page

    with database.session() as session:
        found = session.get(Space, space.id)
        assert found is not None
        found.sync_status, found.conflict_paths = "conflict", ["content/a.md", "content/b.md"]
    page = client.get("/o/acme/infra/-/git").text
    assert "Conflits à résoudre" in page and "/-/compare?path=content/b.md" in page
    assert (
        client.get("/o/acme/infra/-/compare", params={"path": "x.md"}).status_code == 400
        and "content/b.md" in page
    )
    incomplete = client.post(
        "/o/acme/infra/-/conflicts",
        data={"csrf": token, "path": ["content/a.md", "content/b.md"], "choice": ["local", ""]},
    )
    assert "pour chaque fichier" in incomplete.text
    client.post(
        "/o/acme/infra/-/conflicts",
        data={
            "csrf": token,
            "path": ["content/a.md", "content/b.md"],
            "choice": ["local", "remote"],
        },
    )
    [resolve] = [payload for kind, payload in queued(database) if kind == "resolve"]
    assert resolve["choices"] == {"content/a.md": "local", "content/b.md": "remote"}
    assert resolve["by"]  # who decided: author of the resolution commit
    assert re.search(r"Résolution demandée", client.get("/o/acme/infra/-/git").text) is None
