import base64

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.core.access import SpaceRole
from synaptane.server import create_app
from synaptane.services import spaces
from synaptane.storage.db import Database
from tests.integration.conftest import make_user, token_for
from tests.integration.wikidata import Wiki


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_requires_a_valid_token(client: TestClient) -> None:
    response = client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"
    assert client.get("/api/v1/me", headers=bearer("syn_nope")).status_code == 401


def test_admin_builds_orgs_spaces_and_members(
    client: TestClient, database: Database, db: Session
) -> None:
    make_user(db, "root", superadmin=True)
    make_user(db, "alice")
    db.commit()
    root = bearer(token_for(database, "root"))

    assert (
        client.post(
            "/api/v1/organizations", json={"slug": "acme", "name": "Acme"}, headers=root
        ).status_code
        == 201
    )
    created = client.post(
        "/api/v1/organizations/acme/spaces",
        json={"slug": "infra", "name": "Infra", "repo_url": "https://gitlab.com/acme/infra.git"},
        headers=root,
    )
    assert created.status_code == 201
    assert created.json()["organization"] == "acme"
    response = client.put(
        "/api/v1/organizations/acme/spaces/infra/members/alice",
        json={"role": "reader"},
        headers=root,
    )
    assert response.json()["role"] == "reader"

    alice = bearer(token_for(database, "alice"))
    mine = client.get("/api/v1/spaces", headers=alice).json()
    assert [(s["organization"], s["slug"], s["role"]) for s in mine] == [
        ("acme", "infra", "reader")
    ]
    denied = client.patch(
        "/api/v1/organizations/acme/spaces/infra", json={"name": "Piraté"}, headers=alice
    )
    assert denied.status_code == 403
    assert client.get("/api/v1/users", headers=alice).status_code == 403
    hidden = client.get("/api/v1/organizations/acme/spaces/other", headers=alice)
    assert hidden.status_code == 404


def test_validation_error_is_400_and_nothing_is_written(
    client: TestClient, database: Database, db: Session
) -> None:
    make_user(db, "root", superadmin=True)
    db.commit()
    root = bearer(token_for(database, "root"))
    bad = client.post("/api/v1/organizations", json={"slug": "Bad Slug", "name": "X"}, headers=root)
    assert bad.status_code == 400
    assert client.get("/api/v1/organizations", headers=root).json() == []


def test_token_lifecycle(client: TestClient, database: Database, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    alice = bearer(token_for(database, "alice"))
    created = client.post(
        "/api/v1/me/tokens", json={"name": "Claude Code", "days": 30}, headers=alice
    )
    assert created.status_code == 201
    new = created.json()
    assert new["token"].startswith("syn_") and new["hint"] == new["token"][-4:]
    assert len(client.get("/api/v1/me/tokens", headers=alice).json()) == 2
    assert client.delete(f"/api/v1/me/tokens/{new['id']}", headers=alice).status_code == 204
    assert client.get("/api/v1/me", headers=bearer(new["token"])).status_code == 401


def test_openapi_is_published_only_on_demand(
    client: TestClient, settings: Settings, database: Database
) -> None:
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/v1/openapi.json").status_code == 404
    documented_settings = settings.model_copy(update={"api_docs": True})
    with TestClient(create_app(documented_settings, database)) as documented:
        assert documented.get("/api/docs").status_code == 200
        schema = documented.get("/api/v1/openapi.json").json()
    assert "/api/v1/organizations/{org}/spaces/{space}/members/{username}" in schema["paths"]
    tags = {
        tag
        for path in schema["paths"].values()
        for op in path.values()
        for tag in op.get("tags", [])
    }
    assert tags == {
        *("account", "content", "git", "inbox", "invitations", "organizations", "routines"),
        *("search", "spaces", "sso", "tidy", "users", "webhooks"),
    }


def test_an_unreachable_database_gives_a_clean_503(settings: Settings) -> None:
    """Database outage → 503 in Synaptane's format, with Retry-After."""
    down = Database("postgresql+psycopg://synaptane:x@127.0.0.1:1/synaptane")
    try:
        with TestClient(create_app(settings, down), raise_server_exceptions=False) as client:
            api = client.get("/api/v1/spaces", headers={"Authorization": "Bearer syn_x"})
            assert api.status_code == 503 and api.headers["retry-after"] == "10"
            assert api.json()["error"] == "unavailable"
            web = client.post("/login", data={"username": "a", "password": "b"})
            assert web.status_code == 503 and "injoignable" in web.text
    finally:
        down.dispose()


def test_an_html_file_is_never_served_as_a_page(
    client: TestClient, database: Database, wiki: Wiki
) -> None:
    """A .html file from the repository is downloaded, under a sandbox CSP: it never runs in
    Synaptane's origin, neither through the web nor through the API."""
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    base = "/api/v1/organizations/acme/spaces/infra"
    written = client.put(
        f"{base}/files/decks/plan.html",
        json={"content": "<script>alert(1)</script>"},
        headers=alice,
    )
    assert written.status_code in (200, 201), written.text
    raw = client.get(f"{base}/raw/decks/plan.html", headers=alice)
    assert raw.headers["content-security-policy"].startswith("sandbox")
    assert raw.headers["content-disposition"] == "attachment"


# --- REST API on par with the web and MCP ---------------------------------------------------------


def test_rest_never_replaces_a_file_blindly(
    client: TestClient, database: Database, wiki: Wiki
) -> None:
    """Without `base_sha`, an existing file is not overwritten (409); `force` overwrites it
    deliberately."""
    alice = bearer(token_for(database, "alice"))
    url = "/api/v1/organizations/acme/spaces/infra/files/serveurs/proxmox.md"
    blind = client.put(url, json={"content": "# Écrasé\n"}, headers=alice)
    assert blind.status_code == 409 and "base_sha" in blind.json()["message"]
    assert "force: true" in blind.json()["message"]
    assert "Écrasé" not in client.get(url, headers=alice).json()["text"]
    sha = client.get(url, headers=alice).json()["sha"]
    read_first = client.put(url, json={"content": "# Lu\n", "base_sha": sha}, headers=alice)
    assert read_first.status_code == 200 and read_first.json()["commit"]
    forced = client.put(url, json={"content": "# Forcé\n", "force": True}, headers=alice)
    assert forced.status_code == 200 and client.get(url, headers=alice).json()["text"] == (
        "# Forcé\n"
    )
    created = client.put(url.replace("proxmox", "neuf"), json={"content": "x"}, headers=alice)
    assert created.status_code == 200  # a creation needs nothing


def test_rest_search_signals_pending_notes(
    client: TestClient, database: Database, wiki: Wiki
) -> None:
    """REST search stays a list (existing clients), counts pending notes in
    `X-Pending-Count` and lists them on a dedicated route."""
    alice = bearer(token_for(database, "alice"))
    inbox = "/api/v1/organizations/acme/spaces/infra/inbox"
    note = {"title": "Routeur principal remplacé", "content": "Nouveau modèle le 3 octobre."}
    assert client.post(inbox, json=note, headers=alice).status_code == 201
    found = client.get("/api/v1/search", params={"q": "routeur"}, headers=alice)
    assert found.json() == [] and found.headers["x-pending-count"] == "1"
    [pending] = client.get("/api/v1/search/pending", params={"q": "routeur"}, headers=alice).json()
    assert (pending["space"], pending["title"]) == ("acme/infra", "Routeur principal remplacé")
    assert "@" not in pending["deposited_by"]
    item = client.get(f"{inbox}/{pending['name']}", headers=alice).json()
    assert item["body"].strip() == "Nouveau modèle le 3 octobre." and item["type"] == "note"
    elsewhere = client.get(
        "/api/v1/search/pending", params={"q": "routeur", "spaces": "acme/autre"}, headers=alice
    )
    assert elsewhere.json() == []  # invisible space: nothing, no error


def test_rest_manages_inbox_items(
    client: TestClient, db: Session, database: Database, wiki: Wiki
) -> None:
    """Parity with the web: read, fix, move and remove a note, mark a changed page as seen,
    download an attachment."""
    spaces.set_member(db, wiki.root, wiki.other, wiki.alice.user, SpaceRole.CONTRIBUTOR)
    db.commit()
    alice = bearer(token_for(database, "alice"))
    bob = bearer(token_for(database, "bob"))
    inbox = "/api/v1/organizations/acme/spaces/infra/inbox"
    attachment = {"name": "devis.txt", "content_base64": base64.b64encode(b"12 To").decode()}
    body = {
        "title": "Brouillon",
        "content": "Faux.",
        "hint": "stockage",
        "attachments": [attachment],
    }
    path = client.post(inbox, json=body, headers=alice).json()["path"]
    name = path.rsplit("/", 1)[-1]

    patched = client.patch(f"{inbox}/{name}", json={"title": "Corrigé"}, headers=alice)
    assert patched.status_code == 200, patched.text
    assert (patched.json()["title"], patched.json()["hint"]) == ("Corrigé", "stockage")
    assert patched.json()["body"].strip() == "Faux."  # missing field: unchanged
    assert client.patch(f"{inbox}/{name}", json={"title": "x"}, headers=bob).status_code == 403
    raw = client.get(f"{inbox}/{name}/files/devis.txt", headers=bob)
    assert raw.content == b"12 To" and raw.headers["content-security-policy"].startswith("sandbox")
    assert client.get(f"{inbox}/{name}/files/autre.txt", headers=bob).status_code == 404

    moved = client.post(f"{inbox}/{name}/move", json={"space": "acme/autre"}, headers=alice)
    assert moved.status_code == 200 and moved.json()["space"] == "acme/autre"
    assert client.get(f"{inbox}/{name}", headers=alice).status_code == 404
    other = "/api/v1/organizations/acme/spaces/autre/inbox"
    assert client.get(f"{other}/{name}", headers=alice).json()["title"] == "Corrigé"
    assert client.delete(f"{other}/{name}", headers=alice).status_code == 204
    assert client.get(f"{other}/{name}", headers=alice).status_code == 404

    page = "/api/v1/organizations/acme/spaces/infra/files/journal.md"
    assert client.put(page, json={"content": "# Journal\n"}, headers=alice).status_code == 200
    [change] = client.get(inbox, params={"kind": "change"}, headers=alice).json()
    detail = client.get(f"{inbox}/{change['name']}", headers=alice).json()
    assert detail["type"] == "change" and "journal.md" in detail["body"]
    assert client.post(f"{inbox}/{change['name']}/seen", headers=bob).status_code == 403
    assert client.post(f"{inbox}/{change['name']}/seen", headers=alice).status_code == 204
    assert client.get(inbox, params={"kind": "change"}, headers=alice).json() == []
    assert client.get(inbox, params={"kind": "tout"}, headers=alice).status_code == 422
