"""Alerts to administrators: overdue inbox, failed synchronization."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.git.repo import GitError
from synaptane.services import ai_settings, alerts, deposits, git_sync
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Space
from tests.integration.wikidata import Wiki


def actions(db: Session) -> list[str]:
    db.expire_all()
    return list(db.scalars(select(AuditEvent.action).where(AuditEvent.action.like("alert.%"))))


def test_a_stale_inbox_alerts_once(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_settings.configure(  # tidying enabled: otherwise no alert
        db, wiki.root, ctx.box, wiki.infra.organization, provider="anthropic", api_key="sk-ant-x"
    )
    deposits.deposit(db, database, wiki.alice, ctx, wiki.infra, title="N", body="x", source="web")
    db.commit()
    assert alerts.check_backlogs(database, ctx) == 0  # just deposited
    monkeypatch.setattr(alerts, "BACKLOG_DAYS", -1)
    assert alerts.check_backlogs(database, ctx) == 1
    assert alerts.check_backlogs(database, ctx) == 0  # no new notice before 20 h
    assert actions(db) == ["alert.inbox_backlog"]


def test_a_permanent_sync_error_alerts(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.repo_url = "file:///nulle/part.git"
    db.commit()
    with pytest.raises(GitError):
        git_sync.run_sync(database, ctx, wiki.infra.id)
    assert actions(db) == ["alert.sync_error"]
