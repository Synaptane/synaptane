"""Putting back a past version of a file: service, wiki, REST (MCP: test_mcp.py)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.services import content
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.conftest import token_for
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

PAGE = "serveurs/proxmox.md"
VERIFIED = "---\nowner: alice\nverified: 2026-10-01\n---\n"


def _write(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, path: str, text: str
) -> None:
    try:
        sha = content.read_file(wiki.alice, ctx, wiki.infra, path).sha
    except SynaptaneError:
        sha = ""
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, text.encode(), base_sha=sha)


def test_restore_follows_renames_and_keeps_verification(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    _write(db, database, ctx, wiki, "guides/a.md", "# A\n\nPremière version.\n")
    [first] = content.history(wiki.alice, ctx, wiki.infra, "guides/a.md")
    content.move_file(db, database, wiki.alice, ctx, wiki.infra, "guides/a.md", "guides/b.md")
    _write(db, database, ctx, wiki, "guides/b.md", VERIFIED + "# B\n\nRéécrite par l'IA.\n")

    # The version before the rename is read under the name it had then.
    commits = content.history(wiki.alice, ctx, wiki.infra, "guides/b.md")
    assert [c.paths[0] for c in commits] == [
        "content/guides/b.md",
        "content/guides/b.md",
        "content/guides/a.md",
    ]
    old = content.read_version(wiki.bob, ctx, wiki.infra, "guides/b.md", first.sha[:8])
    assert old.path == "guides/b.md" and old.text == "# A\n\nPremière version.\n"

    commit = content.restore_version(
        db, database, wiki.alice, ctx, wiki.infra, "guides/b.md", first.sha
    )
    assert commit
    restored = content.read_file(wiki.alice, ctx, wiki.infra, "guides/b.md").text or ""
    # The old text, with the current verification fields: going back does not verify.
    assert restored.startswith(VERIFIED) and restored.endswith("Première version.\n")
    latest = content.history(wiki.alice, ctx, wiki.infra, "guides/b.md")[0]
    assert latest.subject == f"guides/b.md: restored to version {first.sha[:8]}"
    # Already the current version: nothing to commit.
    assert (
        content.restore_version(
            db, database, wiki.alice, ctx, wiki.infra, "guides/b.md", latest.sha
        )
        is None
    )

    with pytest.raises(SynaptaneError) as reader:
        content.restore_version(db, database, wiki.bob, ctx, wiki.infra, "guides/b.md", first.sha)
    assert reader.value.code == "forbidden"
    for bad, code in (("--all", "invalid"), ("abc", "invalid"), ("0" * 40, "not_found")):
        with pytest.raises(SynaptaneError) as refused:
            content.read_version(wiki.alice, ctx, wiki.infra, "guides/b.md", bad)
        assert refused.value.code == code


def test_a_deleted_page_comes_back_and_a_moved_one_points_to_its_address(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.delete_file(db, database, wiki.alice, ctx, wiki.infra, PAGE)
    removal, moved = content.gone(wiki.bob, ctx, wiki.infra, PAGE) or (None, "x")
    assert removal and moved == ""
    deletion, last = content.history(wiki.alice, ctx, wiki.infra, PAGE)[:2]
    with pytest.raises(SynaptaneError, match="deleted it"):
        content.read_version(wiki.alice, ctx, wiki.infra, PAGE, deletion.sha)
    # A deleted file: the restore is a creation (a base digest would be a conflict).
    with pytest.raises(SynaptaneError) as stale:
        content.restore_version(
            db, database, wiki.alice, ctx, wiki.infra, PAGE, last.sha, base_sha="abc"
        )
    assert stale.value.code == "conflict"
    content.restore_version(db, database, wiki.alice, ctx, wiki.infra, PAGE, last.sha, base_sha="")
    assert "sauvegarde" in (content.read_file(wiki.bob, ctx, wiki.infra, PAGE).text or "")
    assert content.gone(wiki.bob, ctx, wiki.infra, "guides/never.md") is None

    content.move_file(db, database, wiki.alice, ctx, wiki.infra, PAGE, "serveurs/pve.md")
    assert content.gone(wiki.bob, ctx, wiki.infra, PAGE)[1] == "serveurs/pve.md"  # type: ignore[index]


def test_wiki_history_restores_a_version(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    token = csrf(client, f"/o/acme/infra/edit/{PAGE}")
    form = client.get(f"/o/acme/infra/edit/{PAGE}").text
    base = form.split('name="base_sha" value="')[1].split('"')[0]
    client.post(
        f"/o/acme/infra/edit/{PAGE}",
        data={"csrf": token, "text": "# Proxmox\n\nTout effacé.\n", "base_sha": base},
    )
    history = client.get(f"/o/acme/infra/history/{PAGE}").text
    assert "version actuelle" in history and history.count('data-test="restore-version"') == 1
    sha = history.split('name="sha" value="')[1].split('"')[0]
    current = history.split('name="base_sha" value="')[1].split('"')[0]
    # What restoring would change: the current text removed, the old one added.
    compare = client.get(f"/o/acme/infra/diff/{sha}/{PAGE}", params={"current": 1}).text
    assert "-Tout effacé." in compare and "+La sauvegarde nocturne" in compare

    stale = client.post(
        "/o/acme/infra/-/restore",
        data={"csrf": token, "path": PAGE, "sha": sha, "base_sha": base},
    )
    assert stale.status_code == 400 and "depuis que vous avez ouvert son historique" in stale.text
    done = client.post(
        "/o/acme/infra/-/restore",
        data={"csrf": token, "path": PAGE, "sha": sha, "base_sha": current},
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == f"/o/acme/infra/p/{PAGE}"
    assert "sauvegarde nocturne" in client.get(f"/o/acme/infra/p/{PAGE}").text

    client.post("/o/acme/infra/-/delete-file", data={"csrf": token, "path": PAGE})
    deleted = client.get(f"/o/acme/infra/p/{PAGE}")
    assert deleted.status_code == 404 and "Cette page a été supprimée" in deleted.text
    assert "Voir son historique pour la restaurer" in deleted.text
    gone = client.get(f"/o/acme/infra/history/{PAGE}").text
    assert "Ce fichier a été supprimé" in gone and 'name="base_sha" value=""' in gone

    login(client, "bob")  # a reader sees the history, without restoring
    assert 'data-test="restore-version"' not in client.get("/o/acme/infra/history/README.md").text
    assert "Voir son historique" in client.get(f"/o/acme/infra/p/{PAGE}").text


def test_moved_page_gives_its_new_address(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    token = csrf(client, f"/o/acme/infra/edit/{PAGE}")
    client.post("/o/acme/infra/-/move", data={"csrf": token, "path": PAGE, "target": "pve.md"})
    moved = client.get(f"/o/acme/infra/p/{PAGE}").text
    assert "Cette page a été déplacée" in moved and 'href="/o/acme/infra/p/pve.md"' in moved


def test_rest_restore(client: TestClient, database: Database, wiki: Wiki) -> None:
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    bob = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    base = "/api/v1/organizations/acme/spaces/infra"
    first = client.get(f"{base}/history/{PAGE}", headers=bob).json()[-1]["sha"]
    client.put(f"{base}/files/{PAGE}", json={"content": "x", "force": True}, headers=alice)
    body = {"path": PAGE, "commit": first}
    assert client.post(f"{base}/restore", json=body, headers=bob).status_code == 403
    restored = client.post(f"{base}/restore", json=body, headers=alice)
    assert restored.status_code == 200 and restored.json()["commit"]
    assert "sauvegarde" in client.get(f"{base}/files/{PAGE}", headers=bob).json()["text"]
