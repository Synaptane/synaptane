"""Links follow moves (web, REST, agent), end-of-run check, wiki health."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.services import content, deposits, health, tidy
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.conftest import token_for
from tests.integration.gitutil import git
from tests.integration.test_tidy import Scripted, configure_ai
from tests.integration.test_tidy_robustness import note
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

CEPH = "# Ceph\n\n[Proxmox](../serveurs/proxmox.md) · [[serveurs/proxmox]] · [vieux](../vieux.md)\n"


def test_moving_a_page_rewrites_links_in_the_same_commit(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "stockage/ceph.md", CEPH.encode())
    updated = content.move_file(
        db, database, wiki.alice, ctx, wiki.infra, "serveurs/proxmox.md", "infra/proxmox.md"
    )
    db.commit()
    assert updated == ["README.md", "stockage/ceph.md"]
    repo = ctx.local_repository(wiki.infra)
    assert repo.blob("content/README.md") == b"# Infra\n\nVoir [serveurs](infra/proxmox.md).\n"
    assert repo.blob("content/stockage/ceph.md") == (
        b"# Ceph\n\n[Proxmox](../infra/proxmox.md) \xc2\xb7 [[infra/proxmox]] \xc2\xb7 "
        b"[vieux](../vieux.md)\n"
    )
    moved = repo.blob("content/infra/proxmox.md") or b""
    assert "Retour à l'[accueil](../README.md).".encode() in moved  # same depth
    log = git(repo.path, "log", "-1", "--format=%s", "--name-only")
    assert log.startswith("serveurs/proxmox.md moved to infra/proxmox.md")
    assert {"content/README.md", "content/stockage/ceph.md", "content/infra/proxmox.md"} <= set(
        log.split()
    )


def test_the_web_and_the_rest_api_move_with_links(
    client: TestClient, database: Database, wiki: Wiki
) -> None:
    login(client, "alice")
    page = client.get("/o/acme/infra/p/serveurs/proxmox.md").text
    assert "Les liens vers cette page, et les siens, sont mis à jour en même temps." in page
    response = client.post(
        "/o/acme/infra/-/move",
        data={
            "csrf": csrf(client, "/o/acme/infra"),
            "path": "serveurs/proxmox.md",
            "target": "hyperviseurs/pve/proxmox.md",
        },
    )
    assert "Page déplacée. Liens mis à jour dans 1 page\xa0: README.md." in response.text
    readme = client.get("/o/acme/infra/p/README.md").text
    assert 'href="/o/acme/infra/p/hyperviseurs/pve/proxmox.md"' in readme
    moved = client.get("/o/acme/infra/p/hyperviseurs/pve/proxmox.md").text
    assert 'href="/o/acme/infra/p/README.md"' in moved  # ../../README.md, recomputed

    headers = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    answer = client.post(
        "/api/v1/organizations/acme/spaces/infra/move",
        json={"from": "hyperviseurs/pve/proxmox.md", "to": "proxmox.md"},
        headers=headers,
    )
    assert answer.status_code == 204
    readme_file = client.get(
        "/api/v1/organizations/acme/spaces/infra/files/README.md", headers=headers
    ).json()
    assert "[serveurs](proxmox.md)" in readme_file["text"]


def test_agent_move_rewrites_links_and_the_end_check_asks_once(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "stockage/ceph.md", CEPH.encode())
    db.commit()
    [change] = [i.name for i in deposits.inbox(db, wiki.alice, ctx, wiki.infra)]
    name = note(db, database, ctx, wiki, "Réorganiser")
    target = "infra/hyperviseurs/proxmox.md"
    neuve = "# Neuve\n\n[x](../absent.md)\n"
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": name}),
                ("read_inbox_item", {"item": change}),
                ("read_page", {"path": "stockage/ceph.md"}),
                ("move", {"source": "serveurs/proxmox.md", "target": target}),
                ("write_page", {"path": "stockage/ceph.md", "content": CEPH}),  # outdated: refused
                ("write_page", {"path": "notes/neuve.md", "content": neuve}),
                ("mark_done", {"item": change, "summary": "vu"}),
                ("mark_done", {"item": name, "summary": "ok", "pages": ["notes/neuve.md"]}),
            ],
            [
                ("edit_page", {"path": "notes/neuve.md", "old": "absent", "new": "README"}),
                ("finish", {"summary": "Réorganisé"}),
            ],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit, record
    _, _, _, moved, stale, _, _, closed, _, finished = (r.content for r in script.results)
    assert "Its links are recomputed" in moved
    assert "Links updated in: README.md, stockage/ceph.md" in moved
    assert "read it in full first" in stale  # its links changed since it was read
    assert "End-of-run check" in closed
    assert "notes/neuve.md: broken link ../absent.md (not found)" in closed
    assert "notes/neuve.md: page created without an incoming link" in closed
    assert "vieux.md" not in closed  # already broken before the run
    assert finished == "Run finished."  # check sent back only once
    attention = [op for op in record.operations if op["op"] == "attention"]
    assert attention == [
        {
            "op": "attention",
            "path": "notes/neuve.md",
            "detail": "page created without an incoming link",
        }
    ]
    assert {"op": "modifié", "path": "README.md", "detail": f"links to {target} updated"} in (
        record.operations
    )
    repo = ctx.local_repository(wiki.infra)
    assert repo.blob("content/README.md") == f"# Infra\n\nVoir [serveurs]({target}).\n".encode()
    assert b"[[infra/hyperviseurs/proxmox]]" in (repo.blob("content/stockage/ceph.md") or b"")
    assert b"[accueil](../../README.md)" in (repo.blob(f"content/{target}") or b"")


def test_the_end_check_sees_links_broken_by_a_deletion(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    name = note(db, database, ctx, wiki, "Ménage")
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": name}),
                ("delete", {"path": "serveurs/proxmox.md"}),
                ("mark_done", {"item": name, "summary": "supprimée"}),
            ]
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.commit
    assert "README.md: broken link serveurs/proxmox.md" in script.results[-1].content
    # The model stops without fixing: only one more turn, then the log keeps the signal.
    assert script.index == 1 and record.input_tokens == 1010
    assert any(op["op"] == "attention" and op["path"] == "README.md" for op in record.operations)


def test_wiki_health_on_a_small_space(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    pages = {
        "a.md": "# A\n\n[B](b.md) · [cassé](serveur/proxmox.md) · [[absente]]\n",
        "b.md": "Sans titre, mais [retour](a.md).\n\n```\n# pas un titre\n```\n",
        "vide.md": "---\ntags: [x]\n---\n\n",
        "titre.md": "---\ntitle: Titre\n---\nTexte.\n",
    }
    for path, text in pages.items():
        content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, text.encode())
    db.commit()
    repo = ctx.local_repository(wiki.infra)
    report = health.space_health(wiki.bob, ctx, wiki.infra)
    assert report.commit == repo.head() and report.pages == 6
    assert report.broken == [
        health.BrokenLink("a.md", "serveur/proxmox.md", "serveurs/proxmox.md"),
        health.BrokenLink("a.md", "[[absente]]", None),
    ]
    assert report.orphans == ["titre.md", "vide.md"]  # README.md: home page
    assert (report.empty, report.untitled) == (["vide.md"], ["b.md"])
    assert report.stale == [] and report.problems == 6
    later = health.analyze(repo, 12, now=datetime(2040, 1, 1, tzinfo=UTC))
    assert len(later.stale) == 6 and later.stale[0].date <= later.stale[-1].date

    # A new commit: only the changed page is read again, the result follows.
    content.write_file(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        "titre.md",
        b"# Titre\n\n[A](a.md) [V](vide.md)\n",
    )
    db.commit()
    assert health.space_health(wiki.bob, ctx, wiki.infra).orphans == ["titre.md"]

    headers = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    api = client.get(
        "/api/v1/organizations/acme/spaces/infra/health", params={"limit": 1}, headers=headers
    )
    assert api.status_code == 200, api.text
    body = api.json()
    assert body["counts"] == {
        "broken": 2,
        "orphans": 1,
        "empty": 1,
        "untitled": 1,
        "stale": 0,
        "due": 0,
        "unowned": 0,
        "verified": 0,
    }
    assert body["broken"] == [
        {"page": "a.md", "link": "serveur/proxmox.md", "suggestion": "serveurs/proxmox.md"}
    ]

    login(client, "bob")
    shown = client.get("/o/acme/infra/health").text
    assert "Santé du wiki" in shown and 'href="/o/acme/infra/p/a.md"' in shown
    assert "<code>serveur/proxmox.md</code>" in shown and "/edit/" not in shown  # reader
    assert 'href="/o/acme/infra/health"' in client.get("/o/acme/infra/activity").text
    client.post("/logout", data={"csrf": csrf(client, "/o/acme/infra")})
    login(client, "alice")
    assert 'href="/o/acme/infra/edit/a.md"' in client.get("/o/acme/infra/health?months=3").text
