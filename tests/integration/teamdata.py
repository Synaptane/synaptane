"""Test organizations for access rules: Acme (the "wiki" fixture's two spaces, with members of
every kind) and Globex, another organization with its own space and member."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from synaptane.core.access import Action, OrgRole, can
from synaptane.services import accounts, git_sync, load_actor, organizations, spaces
from synaptane.services.extraction import update_index
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Space
from tests.integration.conftest import make_user
from tests.integration.gitutil import Remote
from tests.integration.wikidata import Wiki

DEFAULTS = ("none", "reader", "contributor")
RANK = {None: 0, "reader": 1, "contributor": 2}


@dataclass
class Team:
    wiki: Wiki
    ventes: Space  # space of Globex, an organization whose default role is "contributor"


@pytest.fixture
def team(db: Session, database: Database, ctx: Workspace, tmp_path: Path, wiki: Wiki) -> Team:
    """Acme: Alice (contributor on infra) and Bob (reader on infra) are members through their
    space; Dora is a member added to no space; Carol administers Acme. Globex: Eve, a member
    of Globex only, and its space "ventes"."""
    acme = wiki.infra.organization
    organizations.set_member(db, wiki.root, acme, make_user(db, "dora"), OrgRole.MEMBER)
    organizations.set_member(db, wiki.root, acme, make_user(db, "carol"), OrgRole.ADMIN)
    globex = organizations.create_organization(db, wiki.root, slug="globex", name="Globex")
    remote = Remote(tmp_path / "globex")
    remote.push({"content/plan.md": "# Plan\n\nFusion confidentielle chez Globex.\n"})
    ventes = spaces.create_space(
        db, wiki.root, globex, slug="ventes", name="Ventes", repo_url=remote.url
    )
    organizations.set_member(db, wiki.root, globex, make_user(db, "eve"), OrgRole.MEMBER)
    organizations.update_organization(db, wiki.root, globex, default_space_role="contributor")
    db.commit()
    git_sync.run_sync(database, ctx, ventes.id)
    update_index(ctx, ventes.id, ctx.local_repository(ventes))
    return Team(wiki, ventes)


def configure(db: Session, team: Team, default: str, *, restricted: bool = False) -> None:
    """Acme's default role; `restricted` applies to both its spaces."""
    root = team.wiki.root
    organizations.update_organization(
        db, root, team.wiki.infra.organization, default_space_role=default
    )
    for space in (team.wiki.infra, team.wiki.other):
        spaces.update_space(db, root, space, restricted=restricted)
    db.commit()


def role(db: Session, username: str, space: Space) -> str | None:
    """Effective role, read from the permissions loaded for a request."""
    access = load_actor(db, accounts.get_user(db, username)).access
    where = {"org_id": space.org_id, "space_id": space.id}
    if not can(access, Action.READ_SPACE, **where):
        return None
    return "contributor" if can(access, Action.CONTRIBUTE_SPACE, **where) else "reader"
