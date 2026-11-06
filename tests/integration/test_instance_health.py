"""/admin/health: for super admins only, what works, what does not, spaces and system alerts."""

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.server import create_app
from synaptane.services import alerts, audit, instance, jobs
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, TidyRun
from tests.integration.conftest import make_user
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki, wiki  # noqa: F401 — shared fixture


def states(settings: Settings, db: Session, database: Database, ctx: Workspace) -> dict[str, str]:
    found = instance.report(db, database, ctx, settings)
    return {check.title: check.state for check in found.checks}


def age(path: Path, delta: timedelta) -> None:
    moment = time.time() - delta.total_seconds()
    os.utime(path, (moment, moment))


def test_only_super_admins_see_the_page(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    make_user(db, "root", superadmin=True)
    db.commit()
    assert client.get("/admin/health", follow_redirects=False).status_code == 303
    login(client, "alice")
    assert client.get("/admin/health").status_code == 403
    client.cookies.clear()
    login(client, "root")
    page = client.get("/admin/health")
    assert page.status_code == 200
    assert '<h2 id="health-state">État de l' in page.text
    assert 'href="/admin/health"' in page.text and "Aucune alerte." in page.text


def test_the_page_lists_spaces_and_their_trouble_first(
    settings: Settings,
    database: Database,
    db: Session,
    ctx: Workspace,
    wiki: Wiki,  # noqa: F811
) -> None:
    wiki.infra.sync_status = "conflict"
    db.add(
        TidyRun(
            space_id=wiki.infra.id,
            trigger="manual",
            status="failed",
            finished_at=datetime.now(UTC),
        )
    )
    db.commit()
    found = instance.report(db, database, ctx, settings)
    assert found.state == instance.ERROR
    assert found.spaces[0].space.id == wiki.infra.id
    assert found.spaces[0].state == instance.ERROR
    assert found.spaces[0].last_tidy is not None


def test_worker_state_follows_its_mark(
    settings: Settings, database: Database, db: Session, ctx: Workspace
) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    assert states(settings, db, database, ctx)["Worker"] == instance.ERROR  # never seen
    instance.mark(settings, instance.WORKER_ALIVE)
    assert states(settings, db, database, ctx)["Worker"] == instance.OK
    age(settings.data_dir / instance.WORKER_ALIVE, instance.WORKER_SILENT + timedelta(minutes=1))
    assert states(settings, db, database, ctx)["Worker"] == instance.ERROR


def test_backup_state_follows_the_last_archive(
    settings: Settings,
    database: Database,
    db: Session,
    ctx: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYNAPTANE_BACKUP_DIR", raising=False)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    assert states(settings, db, database, ctx)["Backup"] == instance.ERROR
    instance.mark(settings, instance.BACKUP_DONE)
    assert states(settings, db, database, ctx)["Backup"] == instance.OK
    done = settings.data_dir / instance.BACKUP_DONE
    age(done, instance.BACKUP_LATE + timedelta(hours=1))
    assert states(settings, db, database, ctx)["Backup"] == instance.WARN
    age(done, instance.BACKUP_LOST + timedelta(hours=1))
    assert states(settings, db, database, ctx)["Backup"] == instance.ERROR


def test_an_archive_in_the_backup_folder_counts(settings: Settings, tmp_path: Path) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    folder = tmp_path / "backups"
    folder.mkdir()
    assert instance.last_backup(settings, str(folder)) is None
    (folder / "synaptane-2026-11-06-030000.tar.gz").touch()
    assert instance.last_backup(settings, str(folder)) is not None


def test_offsite_needs_a_destination_then_a_complete_copy(
    settings: Settings, database: Database, db: Session, ctx: Workspace
) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    assert states(settings, db, database, ctx)["Off-site copy"] == instance.WARN
    configured = settings.model_copy(update={"backup_remotes": ["s3:bucket"]})
    assert states(configured, db, database, ctx)["Off-site copy"] == instance.WARN
    instance.mark(configured, instance.OFFSITE_DONE)
    assert states(configured, db, database, ctx)["Off-site copy"] == instance.OK


def test_a_late_queue_and_a_given_up_task_are_reported(
    settings: Settings,
    database: Database,
    db: Session,
    ctx: Workspace,
    wiki: Wiki,  # noqa: F811
) -> None:
    title = "Background tasks"
    assert states(settings, db, database, ctx)[title] == instance.OK
    jobs.enqueue(db, "index", wiki.infra.id)
    db.commit()
    assert states(settings, db, database, ctx)[title] == instance.OK  # just queued
    job = db.scalars(select(Job)).one()
    job.run_after = datetime.now(UTC) - instance.QUEUE_LATE - timedelta(minutes=1)
    db.commit()
    assert states(settings, db, database, ctx)[title] == instance.WARN
    job.status, job.finished_at = "failed", datetime.now(UTC)
    job.error = f"{jobs.ABANDONED}: no network"
    db.commit()
    assert states(settings, db, database, ctx)[title] == instance.WARN


def test_a_given_up_sync_says_nothing(
    settings: Settings,
    database: Database,
    db: Session,
    ctx: Workspace,
    wiki: Wiki,  # noqa: F811
) -> None:
    """It starts again by itself, and has its own alert (`alerts.ABANDON_IGNORED`)."""
    jobs.enqueue(db, "sync", wiki.infra.id)
    db.commit()
    job = db.scalars(select(Job)).one()
    job.status, job.finished_at = "failed", datetime.now(UTC)
    job.error = f"{jobs.ABANDONED}: no network"
    db.commit()
    assert states(settings, db, database, ctx)["Background tasks"] == instance.OK


def test_every_alert_has_a_title() -> None:
    assert set(instance.ALERT_TITLES) == set(alerts.KINDS) | set(alerts.ORG_KINDS)


def test_system_alerts_are_listed_with_their_space(
    settings: Settings,
    database: Database,
    db: Session,
    ctx: Workspace,
    wiki: Wiki,  # noqa: F811
) -> None:
    audit(db, None, "alert.sync_error", space=wiki.infra, error="repository gone")
    audit(db, None, "content.imported", space=wiki.infra)  # not an alert
    db.commit()
    found = instance.report(db, database, ctx, settings).alerts
    assert [(a.title, a.space, a.error) for a in found] == [
        ("Git sync failed", wiki.infra, "repository gone")
    ]


def test_the_page_shows_an_alert_and_its_state_in_french(
    settings: Settings,
    database: Database,
    db: Session,
    wiki: Wiki,  # noqa: F811
) -> None:
    audit(db, None, "alert.sync_error", space=wiki.infra, error="repository gone")
    wiki.infra.sync_status = "error"
    db.commit()
    with TestClient(create_app(settings, database), base_url="http://localhost") as client:
        login(client, "root")
        page = client.get("/admin/health").text
    assert "Synchronisation Git en échec" in page and "repository gone" in page
    assert f"/o/{wiki.infra.organization.slug}/{wiki.infra.slug}" in page
    assert "status status-error" in page
