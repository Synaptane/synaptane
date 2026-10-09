"""Demo data set (`synaptane demo`): the fictional wiki of Acme's IT team.

Creates what is missing — the `demo` super-admin, the `acme` organization (of which it is an
administrator, so that its MCP assistants see it), the `acme/it` space — then synchronizes
and indexes the space: the wiki, search, inbox and health are ready without a worker.
Run again, nothing is recreated or overwritten: what changed during the demo is kept.

The space is backed by a **local** bare Git repository, `<data_dir>/demo/acme-it.git`, filled
once from `examples/acme-wiki/` (tree of a space repository). Its address is a `file://` one:
synchronization requires `allow_local_repos` in the command, the server and the worker. This
setting is meant for development and demos only: it lets an organization admin link a space
to any Git directory of the server.

Data: `examples/` next to the Python environment (`<root>/.venv` of a cloned repository,
`/app/.venv` in the Docker image, which copies `examples/`), otherwise next to the sources, or
`--source`.
"""

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.core.access import OrgRole
from synaptane.core.security import SecretBox
from synaptane.git.repo import GitError, Identity, Repository
from synaptane.services import Actor, accounts, git_sync, load_actor, organizations, spaces
from synaptane.services.extraction import update_index
from synaptane.services.inbox import CONTENT_DIR, INBOX_DIR
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database, pending_migrations
from synaptane.storage.models import Organization, OrgMember, Space, User

USERNAME = "demo"
ORG_SLUG, ORG_NAME = "acme", "Acme"
SPACE_SLUG, SPACE_NAME = "it", "IT team"
SPACE_DESCRIPTION = "Fictional wiki of Acme's IT team (Synaptane demo data)."
DATASET = "acme-wiki"
BRANCH = "main"
# Author of the initial commit: fictional, like the whole data set.
AUTHOR = Identity("Acme IT team", "it-team@acme.example")
COMMIT_MESSAGE = "Acme IT wiki: demo data"
# `git init` outside any machine configuration (templates, hooks, default branch).
_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}
LOCAL_REPOS_HINT = (
    "the demo space uses a local Git repository (file://), refused unless "
    "SYNAPTANE_ALLOW_LOCAL_REPOS=true (or allow_local_repos = true in synaptane.toml). Set it "
    "for this command, `synaptane serve` and `synaptane worker`; with Docker Compose, add "
    "COMPOSE_FILE=compose.yaml:examples/compose.demo.yaml to .env and run `docker compose up -d`. "
    "Demo and development only: it lets an organization admin point a space at any Git folder "
    "of the server."
)


class DemoError(Exception):
    """Instance not ready, or demo impossible: message shown as is (English)."""


@dataclass
class DemoResult:
    username: str
    password: str | None  # in clear, only once: None if the account already existed
    address: str  # org/space
    repo: Path
    created: list[str] = field(default_factory=list)  # what this run created
    pages: int = 0
    notes: int = 0


def find_dataset(source: Path | None = None) -> Path:
    """Directory of the data set (with `content/`): `source`, otherwise `examples/acme-wiki`."""
    candidates = (
        [source]
        if source
        else [
            Path(sys.prefix).parent / "examples" / DATASET,
            Path(__file__).resolve().parents[3] / "examples" / DATASET,
        ]
    )
    for candidate in candidates:
        if (candidate / CONTENT_DIR).is_dir():
            return candidate
    raise DemoError(
        f"demo data not found ({', '.join(map(str, candidates))}): pass --source "
        f"path/to/examples/{DATASET}."
    )


def repo_path(settings: Settings) -> Path:
    return settings.data_dir / "demo" / "acme-it.git"


def check_ready(settings: Settings, database: Database) -> None:
    """Refuses before any write: master key, local repositories allowed, git, database current."""
    key = settings.key_file
    if not key.is_file() or key.stat().st_size != SecretBox.KEY_BYTES:
        raise DemoError(f"no master key at {key}: run `synaptane init` first.")
    if not settings.allow_local_repos:
        raise DemoError(LOCAL_REPOS_HINT)
    if shutil.which("git") is None:
        raise DemoError("git is not installed.")
    try:
        pending = pending_migrations(database)
    except SQLAlchemyError as error:
        raise DemoError(f"database unreachable ({type(error).__name__}).") from None
    if pending:
        raise DemoError("the database is not up to date: run `synaptane db upgrade` first.")


def dataset_files(source: Path) -> dict[str, bytes]:
    """Files of the data set, by relative path in the repository (links ignored). The top-level
    README.md presents the data set on GitHub: it does not go into the repository."""
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
            files[path.relative_to(source).as_posix()] = path.read_bytes()
    files.pop("README.md", None)
    return files


def seed(bare: Path, source: Path, committer: Identity) -> bool:
    """Creates the bare repository and pushes the data set. False if it exists (kept as is)."""
    if bare.exists():
        return False
    bare.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".demo-", dir=bare.parent) as scratch:
        staging = Path(scratch) / "bare.git"
        init = subprocess.run(
            ["git", "init", "--quiet", "--bare", "-b", BRANCH, str(staging)],
            env=_GIT_ENV | {"HOME": scratch},
            capture_output=True,
            text=True,
            timeout=60,
        )
        if init.returncode != 0:
            raise GitError(init.stderr.strip() or "git init failed")
        work = Repository(Path(scratch) / "work", staging.as_uri(), BRANCH, committer)
        work.clone()
        sha = work.commit_changes(dataset_files(source), AUTHOR, COMMIT_MESSAGE)
        if sha is None:
            raise DemoError(f"no file to import in {source}.")
        work.push(sha)
        staging.rename(bare)  # whole repository or nothing: a failure leaves no empty one
    return True


def _user(session: Session, password: str | None, settings: Settings) -> tuple[User, str | None]:
    user = session.scalar(select(User).where(User.username == USERNAME))
    if user is not None:
        if not user.is_superadmin:
            raise DemoError(f"an account named “{USERNAME}” exists and is not a super-admin.")
        return user, None
    password = password or secrets.token_urlsafe(12)
    user = accounts.create_user(
        session,
        None,
        username=USERNAME,
        display_name="Demo",
        password=password,
        is_superadmin=True,
        locale=settings.default_locale,
    )
    return user, password


def _organization(session: Session, actor: Actor, created: list[str]) -> Organization:
    org = session.scalar(select(Organization).where(Organization.slug == ORG_SLUG))
    if org is None:
        created.append(f"organization {ORG_SLUG}")
        return organizations.create_organization(
            session, actor, slug=ORG_SLUG, name=ORG_NAME, join=True
        )
    member = session.get(OrgMember, (org.id, actor.user.id))
    if member is None or member.role != OrgRole.ADMIN.value:
        organizations.set_member(session, actor, org, actor.user, OrgRole.ADMIN)
    return org


def _space(
    session: Session, actor: Actor, org: Organization, url: str, created: list[str]
) -> Space:
    space = session.scalar(select(Space).where(Space.org_id == org.id, Space.slug == SPACE_SLUG))
    if space is None:
        created.append(f"space {ORG_SLUG}/{SPACE_SLUG}")
        return spaces.create_space(
            session,
            actor,
            org,
            slug=SPACE_SLUG,
            name=SPACE_NAME,
            description=SPACE_DESCRIPTION,
            repo_url=url,
            branch=BRANCH,
        )
    if space.repo_url != url:
        raise DemoError(
            f"the space {ORG_SLUG}/{SPACE_SLUG} already exists and is not the demo space "
            f"(its repository is {space.repo_url or 'not set'}): nothing was changed."
        )
    return space


def run(
    settings: Settings,
    database: Database,
    ws: Workspace,
    *,
    password: str | None = None,
    source: Path | None = None,
) -> DemoResult:
    """Prepares (or completes) the demo; idempotent."""
    check_ready(settings, database)
    dataset = find_dataset(source)
    bare = repo_path(settings)
    url = bare.as_uri()
    with database.session() as session:
        # Accounts and spaces first: a refusal (foreign account or space) writes nothing.
        user, shown = _user(session, password, settings)
        actor = load_actor(session, user)
        result = DemoResult(USERNAME, shown, f"{ORG_SLUG}/{SPACE_SLUG}", bare)
        if shown is not None:
            result.created.append(f"super-admin {USERNAME}")
        org = _organization(session, actor, result.created)
        session.flush()
        actor = load_actor(session, user)  # administrator of the created organization
        space = _space(session, actor, org, url, result.created)
        space_id = space.id
        try:
            if seed(bare, dataset, ws.committer):
                result.created.append(f"Git repository {bare}")
        except GitError as error:  # the session is rolled back: neither account nor space
            raise DemoError(f"could not create the demo repository: {error}") from None
    try:
        git_sync.run_sync(database, ws, space_id)
    except GitError as error:
        raise DemoError(f"synchronisation failed: {error}") from None
    with database.session() as session:
        synced = session.get(Space, space_id)
        assert synced is not None
        if synced.sync_status != "ok":
            state = synced.sync_error or synced.sync_status
            raise DemoError(f"synchronisation failed: {state}.")
        repo = ws.local_repository(synced)
    update_index(ws, space_id, repo)  # text of attachments and documents: search
    result.pages = sum(1 for blob in repo.files(CONTENT_DIR) if blob.path.endswith(".md"))
    result.notes = sum(
        1
        for blob in repo.files(INBOX_DIR)
        if blob.path.endswith(".md") and blob.path.count("/") == 2
    )
    return result


def summary(settings: Settings, result: DemoResult) -> str:
    """What to know to try it: address, login, MCP assistant."""
    home = f"{settings.public_url}/o/{result.address}"
    if result.password is not None:
        login = (
            f"  Sign in:    {result.username} / {result.password}\n"
            "              (shown only once: write it down)"
        )
    else:
        login = f"  Sign in:    {result.username} (existing account, password unchanged)"
    created = ", ".join(result.created) if result.created else "nothing new, already in place"
    return f"""Synaptane demo ready: {result.address}, {result.pages} pages, {result.notes} \
notes waiting in the inbox.
  Created:    {created}

  Open:       {home}
{login}
  Try:        search for "backup" or "on-call"
              the inbox, notes waiting to be tidied: {home}/inbox
              wiki health, a broken link and a page to review: {home}/health

Connect an AI assistant (MCP): {settings.public_url}/mcp
  In Synaptane: My account → Connect an AI assistant (MCP), create a personal token, then:
    claude mcp add --transport http synaptane {settings.public_url}/mcp \\
      --header "Authorization: Bearer syn_…"
  Claude.ai and ChatGPT need a public HTTPS address.

Keep `synaptane worker` running (with SYNAPTANE_ALLOW_LOCAL_REPOS=true) so that edits reach the
demo repository. Tidying the inbox needs an AI provider: \
{settings.public_url}/o/{ORG_SLUG}/-/ai"""
