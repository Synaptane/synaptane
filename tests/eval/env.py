"""Environnement du banc : base PostgreSQL, espace de travail Git, compte et organisation.

Deux façons de l'obtenir :
- dans pytest, `prepare()` sur les fixtures habituelles (`database`, `ctx`, `tmp_path`) ;
- en ligne de commande, `temporary()` crée une base jetable sur le PostgreSQL de
  développement (`compose.dev.yaml`, ou `SYNAPTANE_TEST_DATABASE_URL`) et la supprime à la fin.

Chaque scénario part d'un espace neuf (`new_space`) : un dépôt copié du modèle, synchronisé et
indexé, pour que deux passages ne se voient jamais.
"""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from synaptane.config import Settings
from synaptane.core.security import SecretBox
from synaptane.services import Actor, accounts, git_sync, load_actor, organizations, spaces
from synaptane.services.extraction import update_index
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database, upgrade
from synaptane.storage.models import Organization, Space
from tests.eval.wikidata import PAGES
from tests.integration.gitutil import Remote, git, workspace_for

ADMIN_URL = os.environ.get(
    "SYNAPTANE_TEST_DATABASE_URL",
    "postgresql+psycopg://synaptane:synaptane@127.0.0.1:5433/synaptane",
)


@dataclass
class Env:
    database: Database
    ws: Workspace
    root_dir: Path
    org_id: UUID
    template: Path  # dépôt nu modèle (le wiki du banc), copié pour chaque espace
    count: int = field(default=0)
    settings: Settings | None = None  # réglages de l'application (banc MCP : serveur réel)

    def actor(self) -> Actor:
        with self.database.session() as session:
            return load_actor(session, accounts.get_user(session, "eval"))


def prepare(
    database: Database, ws: Workspace, root_dir: Path, settings: Settings | None = None
) -> Env:
    """Compte super-administrateur et organisation du banc, plus le dépôt modèle."""
    with database.session() as session:
        user = accounts.create_user(
            session,
            None,
            username="eval",
            display_name="Banc d'évaluation",
            password="mot-de-passe-du-banc",
            is_superadmin=True,
            locale="fr",
        )
        actor = load_actor(session, user)
        org = organizations.create_organization(session, actor, slug="eval", name="Banc")
        org_id = org.id
    remote = Remote(root_dir / "modele")
    files: dict[str, str | None] = dict(PAGES)
    remote.push(files, "Wiki du banc d'évaluation")
    return Env(database, ws, root_dir, org_id, remote.bare, settings=settings)


def new_space(env: Env, slug: str = "") -> Space:
    """Espace neuf sur une copie du dépôt modèle, synchronisé et indexé."""
    env.count += 1
    slug = slug or f"s{env.count}"
    bare = env.root_dir / "depots" / f"{slug}-{env.count}.git"
    shutil.copytree(env.template, bare)
    actor = env.actor()
    with env.database.session() as session:
        org = session.get(Organization, env.org_id)
        assert org is not None
        space = spaces.create_space(
            session, actor, org, slug=f"{slug}-{env.count}", name=slug, repo_url=f"file://{bare}"
        )
        space_id = space.id
    git_sync.run_sync(env.database, env.ws, space_id)
    with env.database.session() as session:
        found = session.get(Space, space_id)
        assert found is not None
        _ = found.organization
        session.expunge(found)
    update_index(env.ws, space_id, env.ws.local_repository(found))
    return found


@contextmanager
def temporary(admin_url: str = ADMIN_URL) -> Iterator[Env]:
    """Base et dossiers jetables, pour lancer le banc hors de pytest."""
    name = f"synaptane_eval_{uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    root = Path(tempfile.mkdtemp(prefix="synaptane-eval-"))
    database: Database | None = None
    try:
        upgrade(url)
        settings = Settings(
            public_url="http://localhost",
            database_url=url,
            data_dir=root / "data",
            allow_local_repos=True,
            default_locale="fr",
        )
        settings.data_dir.mkdir()
        settings.key_file.write_bytes(SecretBox.generate_key())
        database = Database(url)
        yield prepare(database, workspace_for(settings), root, settings)
    finally:
        if database is not None:
            database.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
        shutil.rmtree(root, ignore_errors=True)


__all__ = ["Env", "git", "new_space", "prepare", "temporary"]
