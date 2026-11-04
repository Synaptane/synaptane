"""/metrics for Prometheus: absent without a token, bearer token required, what it reports on
the instance and the requests, and still an answer when the database is down."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane import __version__
from synaptane.config import Settings
from synaptane.server import create_app
from synaptane.services import jobs
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import TidyRun
from tests.integration.test_tidy import deposit_note
from tests.integration.wikidata import Wiki, wiki  # noqa: F401 — shared fixture

TOKEN = "m" * 40


def browser(settings: Settings, database: Database, **changes: object) -> TestClient:
    return TestClient(
        create_app(settings.model_copy(update=changes), database), base_url="http://localhost"
    )


def scrape(client: TestClient) -> dict[str, float]:
    response = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    samples = {}
    for line in response.text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            samples[name] = float(value)
    return samples


def test_without_a_token_metrics_do_not_exist(client: TestClient) -> None:
    assert client.get("/metrics").status_code == 404


def test_the_bearer_token_is_required(settings: Settings, database: Database) -> None:
    with browser(settings, database, metrics_token=TOKEN) as client:
        missing = client.get("/metrics")
        assert missing.status_code == 401 and missing.headers["www-authenticate"] == "Bearer"
        wrong = client.get("/metrics", headers={"Authorization": "Bearer " + "x" * 40})
        assert wrong.status_code == 401
        basic = client.get("/metrics", headers={"Authorization": f"Basic {TOKEN}"})
        assert basic.status_code == 401
        assert "synaptane_database_up 1" not in wrong.text


def test_metrics_report_the_instance_and_the_requests(
    settings: Settings,
    database: Database,
    db: Session,
    ctx: Workspace,
    wiki: Wiki,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deposit_note(db, database, ctx, wiki)
    jobs.enqueue(db, "sync", wiki.infra.id)
    now = datetime.now(UTC)
    db.add(TidyRun(space_id=wiki.infra.id, trigger="manual", status="failed", finished_at=now))
    db.commit()
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "synaptane-2026-11-04-020000.tar.gz").write_bytes(b"x")
    (backups / "notes.txt").write_bytes(b"not a backup")
    monkeypatch.setenv("SYNAPTANE_BACKUP_DIR", str(backups))

    with browser(settings, database, metrics_token=TOKEN) as client:
        client.get("/login")
        client.get("/api/v1/me")  # 401: counted as 4xx
        first = scrape(client)
        samples = scrape(client)

    assert any(k.startswith('synaptane_info{version="' + __version__) for k in samples)
    assert samples['synaptane_users{state="active"}'] == 3
    assert samples['synaptane_users{state="disabled"}'] == 0
    assert samples['synaptane_space_sync_status{space="acme/infra",status="ok"}'] == 1
    assert samples['synaptane_space_sync_status{space="acme/infra",status="error"}'] == 0
    assert samples['synaptane_space_last_sync_timestamp_seconds{space="acme/infra"}'] > 0
    assert samples['synaptane_space_inbox_items{space="acme/infra"}'] == 1
    assert samples['synaptane_space_inbox_items{space="acme/autre"}'] == 0
    assert samples['synaptane_space_last_tidy_failed{space="acme/infra"}'] == 1
    assert 'synaptane_space_last_tidy_failed{space="acme/autre"}' not in samples
    assert samples['synaptane_jobs{kind="sync",status="queued"}'] >= 1
    assert samples["synaptane_jobs_waiting_seconds"] >= 0
    assert samples['synaptane_ai_spend_month_euros{organization="acme"}'] == 0
    assert samples["synaptane_backup_last_timestamp_seconds"] > 0
    assert samples["synaptane_database_up"] == 1
    assert samples["process_cpu_seconds_total"] > 0
    # The requests: /metrics is not counted, the scrapes do not count each other.
    assert samples['synaptane_http_requests_total{area="web",code="2xx"}'] == 1
    assert samples['synaptane_http_requests_total{area="api",code="4xx"}'] == 1
    assert samples['synaptane_http_request_duration_seconds_count{area="web"}'] == 1
    assert samples['synaptane_http_request_duration_seconds_bucket{area="web",le="+Inf"}'] == 1
    assert first['synaptane_http_requests_total{area="web",code="2xx"}'] == 1


def test_metrics_answer_when_the_database_is_down(settings: Settings, tmp_path: Path) -> None:
    down = Database("postgresql+psycopg://synaptane:x@127.0.0.1:9/synaptane")
    with browser(settings, down, metrics_token=TOKEN) as client:
        samples = scrape(client)
    assert samples["synaptane_database_up"] == 0
    assert not any(k.startswith("synaptane_users") for k in samples)
    assert any(k.startswith("synaptane_info") for k in samples)
