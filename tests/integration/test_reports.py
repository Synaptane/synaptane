"""Pages reported as outdated by their readers: the report, its life (withdrawn, dismissed,
answered by a verification, following a move) and where it shows."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.services import content, freshness, reports
from synaptane.services import search as search_service
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.conftest import token_for
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

PAGE = "serveurs/proxmox.md"
QUOTED = "«\u202fPlus à 2h.\u202f»"  # French quotes, narrow no-break spaces


def _open(db: Session, wiki: Wiki, path: str = PAGE) -> list[tuple[str, str]]:
    return [(r.by, r.reason) for r in reports.open_reports(db, wiki.bob, wiki.infra, path)]


def test_a_reader_reports_a_page_until_it_is_verified(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    reports.report(db, wiki.bob, ctx, wiki.infra, PAGE, "La sauvegarde tourne à 3h.")
    reports.report(db, wiki.bob, ctx, wiki.infra, PAGE, "  Elle tourne à 4h.  ")  # updated
    reports.report(db, wiki.alice, ctx, wiki.infra, PAGE, "Le serveur a changé.")
    db.commit()
    assert _open(db, wiki) == [("Bob", "Elle tourne à 4h."), ("Alice", "Le serveur a changé.")]
    for path, reason, expected in (
        ("serveurs/notes.txt", "x", "Only Markdown pages"),
        (PAGE, "   ", "Say what is outdated"),
        (PAGE, "x" * 501, "Reason too long"),
        ("absent.md", "x", "Page not found"),
    ):
        with pytest.raises(SynaptaneError, match=expected):
            reports.report(db, wiki.bob, ctx, wiki.infra, path, reason)
    with pytest.raises(SynaptaneError):  # a space Bob cannot read
        reports.report(db, wiki.bob, ctx, wiki.other, "secret.md", "x")

    [hit] = search_service.search(db, wiki.bob, ctx, "nocturne")
    assert hit.reported == 2

    # A moved page keeps its reports.
    content.move_file(db, database, wiki.alice, ctx, wiki.infra, PAGE, "pve.md")
    db.commit()
    assert _open(db, wiki) == [] and len(_open(db, wiki, "pve.md")) == 2

    assert reports.resolve(db, wiki.bob, wiki.infra, "pve.md", reports.WITHDRAWN) == 1
    with pytest.raises(SynaptaneError, match="No open report"):
        reports.resolve(db, wiki.bob, wiki.infra, "pve.md", reports.WITHDRAWN)
    with pytest.raises(SynaptaneError) as reader:
        reports.resolve(db, wiki.bob, wiki.infra, "pve.md", reports.DISMISSED)
    assert reader.value.code == "forbidden"
    assert reports.resolve(db, wiki.alice, wiki.infra, "pve.md", reports.DISMISSED) == 1
    assert _open(db, wiki, "pve.md") == []

    # Reread and verified: the reports are answered.
    reports.report(db, wiki.bob, ctx, wiki.infra, "pve.md", "Toujours faux.")
    db.commit()
    freshness.verify_page(db, database, wiki.alice, ctx, wiki.infra, "pve.md")
    db.commit()
    assert _open(db, wiki, "pve.md") == []


def test_reports_in_the_web(client: TestClient, wiki: Wiki) -> None:
    login(client, "bob")
    page = client.get(f"/o/acme/infra/p/{PAGE}").text
    assert 'data-test="report-page"' in page and 'data-test="page-reports"' not in page
    token = csrf(client, f"/o/acme/infra/p/{PAGE}")
    reported = client.post(
        "/o/acme/infra/-/report", data={"csrf": token, "path": PAGE, "reason": "Plus à 2h."}
    )
    assert "Page signalée comme obsolète" in reported.text
    assert QUOTED in reported.text and 'value="withdraw"' in reported.text
    assert 'data-test="dismiss-reports"' not in reported.text  # Bob is a reader
    health = client.get("/o/acme/infra/health").text
    assert "Signalée comme obsolète" in health and QUOTED in health
    assert "signalée obsolète" in client.get("/search", params={"q": "nocturne"}).text

    login(client, "alice")
    token = csrf(client, f"/o/acme/infra/p/{PAGE}")
    seen = client.get(f"/o/acme/infra/p/{PAGE}").text
    assert 'data-test="dismiss-reports"' in seen and 'value="withdraw"' not in seen
    dismissed = client.post(
        "/o/acme/infra/-/report/close", data={"csrf": token, "path": PAGE, "action": "dismiss"}
    )
    assert (
        "Signalement écarté" in dismissed.text and 'data-test="page-reports"' not in dismissed.text
    )


def test_reports_over_rest(client: TestClient, database: Database, wiki: Wiki) -> None:
    bob = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    url = f"/api/v1/organizations/acme/spaces/infra/reports/{PAGE}"
    assert client.post(url, json={"reason": "Plus à 2h."}, headers=bob).status_code == 201
    [found] = client.get(url, headers=alice).json()
    assert (found["by"], found["reason"]) == ("Bob", "Plus à 2h.")
    hits = client.get("/api/v1/search", params={"q": "nocturne"}, headers=bob).json()
    assert hits[0]["reported_outdated"] == 1
    assert client.delete(url, params={"all": "true"}, headers=bob).status_code == 403
    assert client.delete(url, params={"all": "true"}, headers=alice).status_code == 204
    assert client.get(url, headers=bob).json() == []
