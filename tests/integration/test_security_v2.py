"""Security: secrets bound to their destination, logins, zip bomb."""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole
from synaptane.core.errors import SynaptaneError
from synaptane.core.throttle import MAX_PER_ACCOUNT_AND_ADDRESS
from synaptane.extract import MAX_PART, extract_office
from synaptane.git.credentials import GitCredentials
from synaptane.services import Actor, ai_settings, git_sync, organizations, semantic, spaces
from synaptane.services.workspace import Workspace
from synaptane.storage.models import Space
from tests.integration.conftest import actor_of, make_user
from tests.integration.wikidata import Wiki


def org_admin(db: Session, wiki: Wiki) -> Actor:
    carla = make_user(db, "carla")
    organizations.set_member(db, wiki.root, wiki.infra.organization, carla, OrgRole.ADMIN)
    db.commit()
    return actor_of(db, carla)


def test_a_key_never_follows_to_another_destination(
    db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    org = wiki.infra.organization
    carla = org_admin(db, wiki)
    ai_settings.configure(db, carla, ctx.box, org, provider="anthropic", api_key="sk-ant-1")
    # An organization admin cannot point the API to an address of their choice…
    with pytest.raises(SynaptaneError, match="super-admin"):
        ai_settings.configure(
            db,
            carla,
            ctx.box,
            org,
            provider="openai_compatible",
            model="m",
            base_url="http://10.0.0.5/v1",
        )
    # … nor change provider while keeping the stored key.
    with pytest.raises(SynaptaneError, match="Enter the API key again"):
        ai_settings.configure(db, carla, ctx.box, org, provider="openai", model="gpt-5")
    ai_settings.configure(
        db,
        carla,
        ctx.box,
        org,
        provider="openai",
        model="gpt-5",
        api_key="sk-openai",
    )
    # The super-admin, however, can use a custom address (Ollama, gateway).
    ai_settings.configure(
        db,
        wiki.root,
        ctx.box,
        org,
        provider="ollama",
        model="qwen3",
        base_url="http://ollama:11434/v1",
        api_key="",  # no key to the new destination
    )
    with pytest.raises(SynaptaneError, match="super-admin"):
        semantic.configure(
            db,
            carla,
            ctx.box,
            org,
            provider="openai_compatible",
            model="e5",
            base_url="http://x/v1",
        )


def test_git_credentials_are_dropped_when_the_repository_changes(
    db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.repo_url = "https://forge.example.com/acme/infra.git"
    db.flush()
    git_sync.set_credentials(db, wiki.root, ctx.box, space, GitCredentials.https("ghp_" + "a" * 36))
    db.commit()
    assert git_sync.credential_kind(db, ctx.box, space) == "https"
    spaces.update_space(db, wiki.root, space, repo_url="https://evil.example.com/x.git")
    db.commit()
    assert git_sync.credential_kind(db, ctx.box, space) is None


def test_login_variants_share_one_counter(client: TestClient, db: Session) -> None:
    make_user(db, "alice")
    db.commit()
    variants = ["alice", " alice", "ALICE ", "\talice"]
    for n in range(MAX_PER_ACCOUNT_AND_ADDRESS):
        client.post("/login", data={"username": variants[n % 4], "password": "faux"})
    blocked = client.post("/login", data={"username": "alice", "password": "faux"})
    assert blocked.status_code == 429


def test_a_zip_bomb_is_not_expanded() -> None:
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<w>" + b" " * (MAX_PART + 10) + b"</w>")
    assert len(bomb.getvalue()) < 100_000  # a few tens of KB compressed
    assert extract_office(bomb.getvalue(), ".docx") == ""
