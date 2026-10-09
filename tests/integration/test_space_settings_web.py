"""A space's Git settings in the browser: repository URL and SSH key.

`space_pages.py` was only 58% covered: changing the repository URL must forget the old
repository's credentials and queue a sync; the SSH key is read then saved without ever being
shown again; none of this is allowed outside the organization's admins.
"""

import html
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole, SpaceRole
from synaptane.core.security import SecretBox
from synaptane.services import git_sync, organizations, spaces
from synaptane.services.secrets import get_space_secret
from synaptane.storage.db import Database
from synaptane.storage.models import Job, Space
from synaptane.web import space_pages
from tests.integration.conftest import actor_of, make_user
from tests.integration.test_web import csrf, login

GIT = "/o/acme/infra/-/git"
HTTPS_URL = "https://forge.example.com/acme/infra.git"
SSH_URL = "git@forge.example.com:acme/infra.git"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\ncle-secrete-du-test\n-----END OPENSSH PRIVATE KEY-----"
HOSTS = "forge.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEmpreinteDeTest"


@pytest.fixture
def infra(db: Session, client: TestClient) -> Space:
    """Acme/Infra on an HTTPS repository: carla administers the organization (without being
    super-admin), writer contributes, reader reads."""
    root = actor_of(db, make_user(db, "root", superadmin=True))
    acme = organizations.create_organization(db, root, slug="acme", name="Acme")
    organizations.set_member(db, root, acme, make_user(db, "carla"), OrgRole.ADMIN)
    space = spaces.create_space(db, root, acme, slug="infra", name="Infra", repo_url=HTTPS_URL)
    spaces.set_member(db, root, space, make_user(db, "writer"), SpaceRole.CONTRIBUTOR)
    spaces.set_member(db, root, space, make_user(db, "reader"), SpaceRole.READER)
    db.commit()
    return space


def box_of(client: TestClient) -> SecretBox:
    return client.app.state.workspace.box  # type: ignore[attr-defined,no-any-return]


def flash(page: str) -> tuple[str, str] | None:
    """Message shown after a redirect: (kind, text)."""
    found = re.search(r'data-flash="(\w+)">([^<]*)<', page)
    return (found.group(1), html.unescape(found.group(2))) if found else None


def state(database: Database, space: Space) -> tuple[str | None, str, str, list[str]]:
    """The space's URL, branch, sync state and queued jobs."""
    with database.session() as session:
        found = session.get(Space, space.id)
        assert found is not None
        jobs = session.scalars(
            select(Job.kind).where(Job.space_id == space.id, Job.status == "queued")
        )
        return found.repo_url, found.branch, found.sync_status, sorted(jobs)


def forget_jobs(database: Database) -> None:
    """Empty the queue: a job already pending would hide the one we expect to arrive."""
    with database.session() as session:
        session.execute(delete(Job))


def kind(database: Database, client: TestClient, space: Space) -> str | None:
    with database.session() as session:
        found = session.get(Space, space.id)
        assert found is not None
        return git_sync.credential_kind(session, box_of(client), found)


def test_admin_changes_the_repository_and_a_sync_is_queued(
    client: TestClient, database: Database, infra: Space
) -> None:
    login(client, "carla")
    token = csrf(client, GIT)
    client.post(f"{GIT}/https", data={"csrf": token, "token": "glpat-ancien-depot"})
    assert kind(database, client, infra) == "https"
    with database.session() as session:  # last known state of the old repository
        found = session.get(Space, infra.id)
        assert found is not None
        found.sync_status, found.remote_head = "ok", "a" * 40
    forget_jobs(database)

    page = client.post(
        "/o/acme/infra/-/repository",
        data={"csrf": token, "repo_url": f"  {SSH_URL} ", "branch": "prod"},
    )
    assert page.url.path == GIT
    assert flash(page.text) == ("ok", "Adresse du dépôt enregistrée.")
    assert state(database, infra) == (SSH_URL, "prod", "never", ["sync"])
    with database.session() as session:
        found = session.get(Space, infra.id)
        assert found is not None and found.remote_head is None  # nothing merged from the repository
    # The old repository's token is not presented to the new one: it must be re-entered.
    assert kind(database, client, infra) is None
    assert "Aucun identifiant" in page.text
    assert "/-/git/scan" in page.text and 'name="private_key"' in page.text  # SSH offered
    assert 'name="token"' not in page.text

    forget_jobs(database)  # same URL: the sync is restarted anyway
    client.post("/o/acme/infra/-/repository", data={"csrf": token, "repo_url": SSH_URL})
    assert state(database, infra) == (SSH_URL, "main", "never", ["sync"])


@pytest.mark.parametrize(
    ("repo_url", "branch", "message"),
    [
        ("ftp://forge.example.com/infra.git", "main", "URL de dépôt invalide"),
        ("https://forge.example.com/a b.git", "main", "URL de dépôt invalide"),
        ("https://bot:glpat-x@forge.example.com/infra.git", "main", "Ne mettez pas d'identifiants"),
        (SSH_URL, "../main", "Nom de branche invalide"),
    ],
)
def test_invalid_repository_settings_change_nothing(
    client: TestClient, database: Database, infra: Space, repo_url: str, branch: str, message: str
) -> None:
    login(client, "carla")
    token = csrf(client, GIT)
    client.post(f"{GIT}/https", data={"csrf": token, "token": "glpat-garde"})
    forget_jobs(database)
    page = client.post(
        "/o/acme/infra/-/repository", data={"csrf": token, "repo_url": repo_url, "branch": branch}
    )
    shown = flash(page.text)
    assert shown is not None and shown[0] == "error" and message in shown[1], shown
    assert state(database, infra) == (HTTPS_URL, "main", "never", [])  # nothing partial
    assert kind(database, client, infra) == "https"


@pytest.mark.parametrize("username", ["writer", "reader"])
def test_non_admins_cannot_change_git_settings(
    client: TestClient, database: Database, infra: Space, username: str
) -> None:
    """Contributor or reader: the repository state is readable, but neither the URL, nor the
    credentials, nor the SSH key scan are within reach, even when posting by hand."""
    login(client, "carla")
    admin_token = csrf(client, GIT)
    client.post(f"{GIT}/https", data={"csrf": admin_token, "token": "glpat-admin"})
    client.cookies.clear()
    forget_jobs(database)

    login(client, username)
    page = client.get(GIT)
    assert page.status_code == 200 and "Jamais synchronisé" in page.text
    assert 'data-test="repo-form"' not in page.text and "/-/git/https" not in page.text
    token = csrf(client, GIT)

    moved = client.post(
        "/o/acme/infra/-/repository",
        data={"csrf": token, "repo_url": "https://evil.example.com/x.git", "branch": "main"},
    )
    assert flash(moved.text) == ("error", "Action non autorisée.")
    replaced = client.post(f"{GIT}/https", data={"csrf": token, "token": "glpat-pirate"})
    assert flash(replaced.text) == ("error", "Action non autorisée.")
    keyed = client.post(
        f"{GIT}/ssh", data={"csrf": token, "private_key": KEY, "known_hosts": HOSTS}
    )
    assert flash(keyed.text) == ("error", "Action non autorisée.")
    assert client.post(f"{GIT}/scan", data={"csrf": token}).status_code == 403
    assert client.post(f"{GIT}/clear", data={"csrf": token}).status_code == 403

    assert state(database, infra) == (HTTPS_URL, "main", "never", [])
    with database.session() as session:
        found = session.get(Space, infra.id)
        assert found is not None
        raw = get_space_secret(session, box_of(client), found.id, git_sync.GIT_CREDENTIALS)
    assert raw is not None and "glpat-admin" in raw  # the admin's token is intact


def test_ssh_key_is_scanned_saved_and_never_shown_again(
    client: TestClient, database: Database, infra: Space, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full SSH flow without network: host key scan (ssh-keyscan replaced), fingerprints to
    compare, prefilled keys, encrypted storage, queueing."""
    scanned: list[str] = []

    def fake_scan(remote: str, timeout: int = 10) -> tuple[str, list[str]]:
        scanned.append(remote)
        return HOSTS + "\n", ["256 SHA256:EmpreinteDeTest forge.example.com (ED25519)"]

    monkeypatch.setattr(space_pages, "scan_host_keys", fake_scan)
    login(client, "carla")
    token = csrf(client, GIT)
    client.post("/o/acme/infra/-/repository", data={"csrf": token, "repo_url": SSH_URL})
    forget_jobs(database)

    page = client.post(f"{GIT}/scan", data={"csrf": token})
    assert page.status_code == 200 and scanned == [SSH_URL]  # the saved URL
    assert "SHA256:EmpreinteDeTest" in page.text
    assert re.search(rf'name="known_hosts"[^>]*>{re.escape(HOSTS)}\s*</textarea>', page.text)

    refused = client.post(
        f"{GIT}/ssh", data={"csrf": token, "private_key": "pas une clé", "known_hosts": HOSTS}
    )
    assert flash(refused.text) == (
        "error",
        "Clé privée SSH invalide (format OpenSSH ou PEM attendu).",
    )
    no_hosts = client.post(f"{GIT}/ssh", data={"csrf": token, "private_key": KEY})
    shown = flash(no_hosts.text)
    assert shown is not None and shown[0] == "error" and "known_hosts" in shown[1]
    assert kind(database, client, infra) is None and state(database, infra)[3] == []

    saved = client.post(
        f"{GIT}/ssh", data={"csrf": token, "private_key": KEY, "known_hosts": HOSTS}
    )
    assert flash(saved.text) == ("ok", "Accès au dépôt enregistré.")
    assert "Clé SSH enregistrée." in saved.text
    assert "cle-secrete-du-test" not in saved.text  # never shown again
    assert kind(database, client, infra) == "ssh"
    assert state(database, infra)[3] == ["sync"]

    cleared = client.post(f"{GIT}/clear", data={"csrf": token})
    assert flash(cleared.text) == ("ok", "Accès au dépôt supprimé.")
    assert kind(database, client, infra) is None


def test_scan_of_an_https_repository_is_refused(client: TestClient, infra: Space) -> None:
    """The real `scan_host_keys`: refused before any network call for an HTTPS URL."""
    login(client, "carla")
    page = client.post(f"{GIT}/scan", data={"csrf": csrf(client, GIT)})
    assert flash(page.text) == ("error", "Cette URL de dépôt n'utilise pas SSH.")


def test_a_bad_ssh_port_is_refused_or_still_renders(
    client: TestClient, database: Database, infra: Space
) -> None:
    login(client, "carla")
    page = client.post(
        "/o/acme/infra/-/repository",
        data={"csrf": csrf(client, GIT), "repo_url": "ssh://git@forge.example.com:abc/infra.git"},
    )
    assert page.status_code == 200


def test_sync_state_is_shown_and_sync_now_restarts_a_waiting_retry(
    client: TestClient, database: Database, infra: Space
) -> None:
    """After failures (DNS outage when the server restarts, for example), the page says when
    the sync will be retried; "Sync now" restarts it right away, and the page follows the
    request (state route polled while it is pending or running)."""
    forget_jobs(database)
    with database.session() as session:
        found = session.get(Space, infra.id)
        assert found is not None
        found.sync_status, found.sync_error = "error", "Hôte introuvable"
        later = datetime.now(UTC) + timedelta(minutes=45)
        session.add(Job(kind="sync", space_id=infra.id, attempts=4, run_after=later))
    login(client, "writer")
    page = client.get(GIT).text
    assert "Nouvel essai automatique le" in page and "après 4 échecs" in page
    assert "data-tidy-state" not in page  # retry in 45 minutes: the page does not poll
    assert client.get(f"{GIT}/state").json()["poll"] is False

    client.post("/o/acme/infra/-/sync", data={"csrf": csrf(client, GIT)})
    page = client.get(GIT).text
    assert "elle démarre dès que le worker est libre" in page
    assert 'data-tidy-state="/o/acme/infra/-/git/state"' in page
    polled = client.get(f"{GIT}/state").json()
    assert polled["poll"] is True and polled["version"].endswith(":queued")
    with database.session() as session:
        job = session.scalars(select(Job).where(Job.space_id == infra.id)).one()
        assert job.attempts == 0 and job.run_after <= datetime.now(UTC)
        job.status = "running"
    assert "Synchronisation en cours…" in client.get(GIT).text
