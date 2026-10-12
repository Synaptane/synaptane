from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.services import tidy
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job
from synaptane.web import tidy_pages
from tests.integration.conftest import token_for
from tests.integration.test_tidy import Scripted, configure_ai, deposit_note
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki


def test_ai_settings_and_tidy_pages(
    client: TestClient,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(client, "root")
    token = csrf(client, "/o/acme/-/ai")
    client.post(
        "/o/acme/-/ai",
        data={
            "csrf": token,
            "provider": "anthropic",
            "model": "",
            "api_key": "sk-ant-secret",
            "effort": "high",
        },
    )
    page = client.get("/o/acme/-/ai").text
    assert "Anthropic (Claude) · claude-sonnet-5-5" in page and "sk-ant-secret" not in page
    # Subscription sign-in guide, with the command to copy (super-admin).
    assert "claude auth login" in page and "codex login --device-auth" in page

    monkeypatch.setattr(
        tidy_pages, "check_connection", lambda config: f"Connexion réussie ({config.model})."
    )
    assert (
        "Connexion réussie (claude-sonnet-5-5)"
        in client.post("/o/acme/-/ai/test", data={"csrf": token}).text
    )

    client.post(
        "/o/acme/infra/-/tidy-settings",
        data={
            "csrf": token,
            "tidy_enabled": "true",
            "tidy_time": "06:30",
            "tidy_timezone": "Europe/Paris",
            "ai_model": "",
        },
    )
    assert "06:30" in client.get("/o/acme/infra/-/tidy-settings").text
    bad = client.post(
        "/o/acme/infra/-/tidy-settings",
        data={"csrf": token, "tidy_time": "25:00", "tidy_timezone": "Europe/Paris"},
    )
    assert "Heure invalide" in bad.text

    note = deposit_note(db, database, ctx, wiki)
    client.post("/o/acme/infra/-/tidy", data={"csrf": token})
    assert [j.payload["trigger"] for j in db.scalars(select(Job).where(Job.kind == "tidy"))] == [
        "manual"
    ]

    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": note}),
                ("write_page", {"path": "nas.md", "content": "# NAS\n"}),
                ("mark_done", {"item": note, "summary": "ok"}),
            ]
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None
    journal = client.get("/o/acme/infra/tidy").text
    assert "1 élément rangé" in journal and "Annuler ce passage" in journal
    client.post(f"/o/acme/infra/-/tidy/{record.id}/revert", data={"csrf": token})
    assert "annulé" in client.get("/o/acme/infra/tidy").text

    client.cookies.clear()
    login(client, "bob")
    reader = client.get("/o/acme/infra/tidy").text
    assert "Ranger maintenant" not in reader and "Annuler ce passage" not in reader


def test_tidy_rest(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    assert client.get("/api/v1/organizations/acme/ai", headers=root).json() is None
    put: dict[str, Any] = {
        "provider": "openai",
        "model": "gpt-5",
        "api_key": "sk-openai",
        "price_input": 1.25,
        "price_output": 10,
        "monthly_budget": 50,
    }
    saved = client.put("/api/v1/organizations/acme/ai", json=put, headers=root).json()
    assert saved == {
        "provider": "openai",
        "model": "gpt-5",
        "base_url": "",
        "effort": "medium",
        "has_key": True,
        "price_input": "1.25",
        "price_output": "10",
        "monthly_budget": "50",
        "month_spent": "0",
        "tested_at": None,
        "test_ok": None,
        "test_message": "",
        "test_pending": False,
        "metered": True,
    }
    assert client.get("/api/v1/organizations/acme/ai", headers=alice).status_code == 403

    base = "/api/v1/organizations/acme/spaces/infra"
    assert (
        client.patch(
            f"{base}/tidy-settings", json={"time": "03:15", "enabled": False}, headers=root
        ).status_code
        == 204
    )
    assert (
        client.patch(f"{base}/tidy-settings", json={"time": "03:15"}, headers=alice).status_code
        == 403
    )
    assert client.post(f"{base}/tidy", headers=alice).status_code == 202
    configure_ai(db, ctx, wiki)
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=Scripted([]).factory)
    runs = client.get(f"{base}/tidy/runs", headers=alice).json()
    assert [r["status"] for r in runs] == ["nothing"]
