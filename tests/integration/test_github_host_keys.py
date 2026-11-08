"""GitHub changes its SSH server keys: the sync reads them again from GitHub's API over HTTPS
(never trusted on first SSH connection), saves them and tries once more."""

from typing import Any
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.git.credentials import GitCredentials
from synaptane.git.repo import GitError, Repository
from synaptane.services import forges, git_sync, organizations, spaces
from synaptane.services.secrets import get_space_secret, put_space_secret
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent
from tests.integration.conftest import actor_of, make_user

OLD = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOldKeyOldKeyOldKeyOldKeyOldKeyOldKey\n"
NEW_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINewKeyNewKeyNewKeyNewKeyNewKeyNewKey"
PRIVATE = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----"
REFUSED = "Host key verification failed.\nfatal: Could not read from remote repository."


def space_on(db: Session, ctx: Workspace, remote: str, known_hosts: str = OLD) -> UUID:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    org = organizations.create_organization(db, root, slug="acme", name="Acme")
    space = spaces.create_space(db, root, org, slug="infra", name="Infra", repo_url=remote)
    credentials = GitCredentials.ssh(PRIVATE, known_hosts)
    put_space_secret(db, ctx.box, space.id, git_sync.GIT_CREDENTIALS, credentials.to_json(), None)
    db.commit()
    return space.id


@pytest.fixture
def exchanges(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`_exchange` fails on the host key while the old key is saved: the known_hosts each
    attempt was given."""
    seen: list[str] = []

    def exchange(_db: Any, _ctx: Any, _id: Any, _repo: Any, snapshot: Any) -> None:
        seen.append(snapshot.credentials.known_hosts)
        if snapshot.credentials.known_hosts == OLD:
            raise GitError(REFUSED)

    monkeypatch.setattr(git_sync, "_exchange", exchange)
    monkeypatch.setattr(Repository, "gc", lambda self: None)
    return seen


def meta(monkeypatch: pytest.MonkeyPatch, answer: httpx2.Response) -> list[httpx2.Request]:
    asked: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return answer

    monkeypatch.setattr(forges, "TRANSPORT", httpx2.MockTransport(handle))
    return asked


def saved(database: Database, ctx: Workspace, space_id: UUID) -> str:
    with database.session() as session:
        raw = get_space_secret(session, ctx.box, space_id, git_sync.GIT_CREDENTIALS)
    assert raw is not None
    return GitCredentials.from_json(raw).known_hosts


def test_new_github_keys_are_read_from_its_api_and_the_sync_tries_again(
    db: Session,
    database: Database,
    ctx: Workspace,
    exchanges: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    space_id = space_on(db, ctx, "git@github.com:acme/infra.git")
    asked = meta(monkeypatch, httpx2.Response(200, json={"ssh_keys": [NEW_KEY]}))
    git_sync.run_sync(database, ctx, space_id)
    assert [str(r.url) for r in asked] == ["https://api.github.com/meta"]
    assert "authorization" not in asked[0].headers
    assert exchanges == [OLD, f"github.com {NEW_KEY}\n"]
    assert saved(database, ctx, space_id) == f"github.com {NEW_KEY}\n"
    with database.session() as session:
        found = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "space.git_host_keys_refreshed")
        )
        assert found is not None and found.actor_id is None
        # The private key is kept: the deploy key registered on GitHub still works.
        raw = get_space_secret(session, ctx.box, space_id, git_sync.GIT_CREDENTIALS)
        assert raw is not None and GitCredentials.from_json(raw).private_key.startswith(PRIVATE)


def test_same_keys_the_error_stays(
    db: Session,
    database: Database,
    ctx: Workspace,
    exchanges: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    space_id = space_on(db, ctx, "git@github.com:acme/infra.git")
    meta(monkeypatch, httpx2.Response(200, json={"ssh_keys": [OLD.split(" ", 1)[1].strip()]}))
    with pytest.raises(GitError):
        git_sync.run_sync(database, ctx, space_id)
    assert exchanges == [OLD]


@pytest.mark.parametrize(
    ("remote", "answer"),
    [
        ("git@gitlab.com:acme/infra.git", httpx2.Response(200, json={"ssh_keys": [NEW_KEY]})),
        ("git@github.com:acme/infra.git", httpx2.Response(503)),
        ("git@github.com:acme/infra.git", httpx2.Response(200, json={"ssh_keys": "nope"})),
    ],
)
def test_other_hosts_and_unreadable_keys_change_nothing(
    db: Session,
    database: Database,
    ctx: Workspace,
    exchanges: list[str],
    monkeypatch: pytest.MonkeyPatch,
    remote: str,
    answer: httpx2.Response,
) -> None:
    space_id = space_on(db, ctx, remote)
    asked = meta(monkeypatch, answer)
    with pytest.raises(GitError):
        git_sync.run_sync(database, ctx, space_id)
    assert exchanges == [OLD]
    assert saved(database, ctx, space_id) == OLD
    if remote.startswith("git@gitlab"):
        assert asked == []
