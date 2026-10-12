"""A space's SSH key encrypted in the database, then read back.

Full round trip from the service that saves it (`git_sync.set_credentials`) to the read done
by the sync (`git_sync._snapshot`): only an HTTPS token had been tested end to end.
"""

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.git.credentials import GitCredentials
from synaptane.services import Actor, git_sync, organizations, spaces
from synaptane.services.secrets import get_space_secret
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, Secret, Space
from tests.integration.conftest import actor_of, make_user

MARKER = "cle-de-deploiement-tres-secrete"
KEY = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{MARKER}\n-----END OPENSSH PRIVATE KEY-----"
HOSTS = "forge.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEmpreinteDeTest"


def setup(db: Session, repo_url: str) -> tuple[Actor, Space]:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    acme = organizations.create_organization(db, root, slug="acme", name="Acme")
    space = spaces.create_space(db, root, acme, slug="infra", name="Infra", repo_url=repo_url)
    db.commit()
    return root, space


@pytest.mark.parametrize(
    "repo_url",
    ["git@forge.example.com:acme/infra.git", "ssh://git@forge.example.com:2222/acme/infra.git"],
)
def test_ssh_key_is_encrypted_in_database_and_read_back_by_the_sync(
    db: Session, database: Database, ctx: Workspace, repo_url: str
) -> None:
    root, space = setup(db, repo_url)
    credentials = GitCredentials.ssh(KEY, HOSTS)
    git_sync.set_credentials(db, root, ctx.box, space, credentials)
    db.commit()

    [row] = db.scalars(select(Secret).where(Secret.space_id == space.id))
    assert row.purpose == git_sync.GIT_CREDENTIALS
    for clear in (MARKER, "PRIVATE KEY", "forge.example.com", '"ssh"'):
        assert clear.encode() not in row.ciphertext  # nothing readable in the database
    assert git_sync.credential_kind(db, ctx.box, space) == "ssh"
    raw = get_space_secret(db, ctx.box, space.id, git_sync.GIT_CREDENTIALS)
    assert raw is not None and GitCredentials.from_json(raw) == credentials

    snapshot = git_sync._snapshot(database, ctx, space.id)  # what the sync reads
    assert snapshot is not None and snapshot.remote == repo_url
    assert snapshot.credentials == credentials
    assert snapshot.credentials.private_key.endswith(
        f"{MARKER}\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    # New credentials restart the sync.
    assert [(j.kind, j.space_id) for j in db.scalars(select(Job))] == [("sync", space.id)]


def test_ssh_ciphertext_is_bound_to_its_space(db: Session, ctx: Workspace) -> None:
    """The encryption context binds the key to its space: a ciphertext copied to another
    space (SQL injection, makeshift restore) does not decrypt."""
    root, space = setup(db, "git@forge.example.com:acme/infra.git")
    git_sync.set_credentials(db, root, ctx.box, space, GitCredentials.ssh(KEY, HOSTS))
    other = spaces.create_space(db, root, space.organization, slug="autre", name="Autre")
    [row] = db.scalars(select(Secret).where(Secret.space_id == space.id))
    db.add(Secret(space_id=other.id, purpose=row.purpose, ciphertext=row.ciphertext))
    db.commit()
    with pytest.raises(InvalidTag):
        get_space_secret(db, ctx.box, other.id, git_sync.GIT_CREDENTIALS)


@pytest.mark.parametrize(
    ("repo_url", "credentials"),
    [
        ("https://forge.example.com/acme/infra.git", GitCredentials.ssh(KEY, HOSTS)),
        ("git@forge.example.com:acme/infra.git", GitCredentials.https("glpat-secret")),
        ("ssh://git@forge.example.com/acme/infra.git", GitCredentials.https("glpat-secret")),
    ],
)
def test_credentials_of_the_wrong_kind_are_not_stored(
    db: Session, ctx: Workspace, repo_url: str, credentials: GitCredentials
) -> None:
    root, space = setup(db, repo_url)
    with pytest.raises(SynaptaneError) as error:
        git_sync.set_credentials(db, root, ctx.box, space, credentials)
    assert "do not match the repository URL" in error.value.message
    db.rollback()
    assert git_sync.credential_kind(db, ctx.box, space) is None


def test_ssh_key_does_not_follow_the_space_to_another_repository(
    db: Session, ctx: Workspace
) -> None:
    """Changing SSH host forgets the key: it is never presented to another server."""
    root, space = setup(db, "git@forge.example.com:acme/infra.git")
    git_sync.set_credentials(db, root, ctx.box, space, GitCredentials.ssh(KEY, HOSTS))
    db.commit()
    spaces.update_space(db, root, space, repo_url="git@forge.example.com:acme/infra.git")
    db.commit()
    assert git_sync.credential_kind(db, ctx.box, space) == "ssh"  # same URL: nothing changes
    spaces.update_space(db, root, space, repo_url="git@ailleurs.example.net:acme/infra.git")
    db.commit()
    assert git_sync.credential_kind(db, ctx.box, space) is None
