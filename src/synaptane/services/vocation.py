"""Vocation of a space and tidy-up rules: two Markdown files of the repository.

- `.synaptane/vocation.md` says **what** the space **is**: what it is for and for whom, what
  goes there (and what does not), the folder organization, the conventions, what matters
  most, the vocabulary. It is served to the tidy-up, to routines and to assistants (MCP,
  REST).
- `.synaptane/prompts/tidy.md` says **how to tidy up**: precise rules for the agent. Its
  former name, `.synaptane/prompts/rangement.md`, is still read when it is alone (spaces
  created before the rename); a write from Synaptane moves to the new name.

Git remains the source: both files can also be changed by a push. From Synaptane, a write is
a commit in the person's name, under the space lock, followed by a sync (like
`content.write_file`). A space not synced yet (repository not cloned yet, right after its
creation) keeps the text **pending**, in a file next to its local copy: the first sync writes
it to the repository, unless the repository already has its own (see `apply_pending`).

The vocation templates offered when a space is created exist in English (`<key>.md`) and in
French (`<key>.fr.md`): `Template.text_for` picks the one for a language. This module also
detects the language of a space's pages (`space_language`), so that the agents write in it.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError, conflict, invalid, not_found
from synaptane.core.secret_scan import added_findings, refuse_secrets
from synaptane.git.repo import Commit, Identity, Repository
from synaptane.services import Actor, audit, jobs
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.workspace import Workspace, author_identity, trailers
from synaptane.storage.db import Database, try_space_lock
from synaptane.storage.models import Space

VOCATION_FILE = ".synaptane/vocation.md"
RULES_FILE = ".synaptane/prompts/tidy.md"  # tidy-up rules
LEGACY_RULES_FILE = ".synaptane/prompts/rangement.md"  # former name, read if alone
MAX_LENGTH = 20_000  # characters, for each of the two files
EXCERPT_LENGTH = 600  # excerpt of the vocation in `list_spaces` (MCP)

log = logging.getLogger("synaptane.vocation")


@dataclass(frozen=True)
class Kind:
    key: str
    path: str
    commit: str  # commit subject
    removed: str  # commit subject when the text is emptied
    action: str  # audit
    legacy: str | None = None  # former file name, read if alone


VOCATION = Kind(
    "vocation",
    VOCATION_FILE,
    "Space purpose updated",
    "Space purpose removed",
    "space.vocation_updated",
)
RULES = Kind(
    "tidy-rules",
    RULES_FILE,
    "Tidy-up rules updated",
    "Tidy-up rules removed",
    "space.tidy_rules_updated",
    legacy=LEGACY_RULES_FILE,
)
KINDS = {k.key: k for k in (VOCATION, RULES)}


def kind_of(key: str) -> Kind:
    try:
        return KINDS[key]
    except KeyError:
        raise not_found("Page not found.") from None


def current_path(repo: Repository, kind: Kind, rev: str = "HEAD") -> str:
    """File that is authoritative at commit `rev`: the current name, unless only the old one
    exists."""
    if kind.legacy and not repo.exists_at(kind.path, rev) and repo.exists_at(kind.legacy, rev):
        return kind.legacy
    return kind.path


def read_rules(repo: Repository, rev: str = "HEAD") -> bytes | None:
    """Tidy-up rules of the space at commit `rev`, or None."""
    return repo.blob(current_path(repo, RULES, rev), rev)


# --- Templates --------------------------------------------------------------------------------


# Languages a template exists in; any other language gets the English one.
TEMPLATE_LANGUAGES = ("en", "fr")


@dataclass(frozen=True)
class Template:
    """A vocation template. `label` and `summary` are interface texts, translated by the
    web catalog; the text itself exists in each of `TEMPLATE_LANGUAGES`."""

    key: str
    label: str
    summary: str

    def text_for(self, locale: str) -> str:
        """Text of the template in a language (`fr`, `en`…); English for any other one."""
        return _template_text(self.key, locale if locale in TEMPLATE_LANGUAGES else "en")


TEMPLATES = {
    t.key: t
    for t in (
        Template("infra", "Infrastructure / ops team", "Servers, procedures, incidents."),
        Template("marketing", "Marketing team", "Brand, campaigns, market watch."),
        Template("product", "Product team", "Features, decisions, feedback."),
        Template("client", "Client project", "Scope, meetings, deliverables."),
        Template("personal", "Personal second brain", "Reading, topics, projects."),
        Template("family", "Family", "Paperwork, health, home, holidays."),
        Template("job-search", "Job search", "Companies, applications, interviews."),
        Template("blank", "Blank", "Just the sections, to fill in."),
    )
}


# First line of the templates, in each language (and the former English title): left out of
# `excerpt`.
TITLES = frozenset({"# Space purpose", "# Space vocation", "# Vocation de l'espace"})


@cache
def _template_text(key: str, locale: str) -> str:
    name = f"{key}.md" if locale == "en" else f"{key}.{locale}.md"
    return (Path(__file__).parent / "vocation_templates" / name).read_text("utf-8")


def template(key: str) -> Template:
    found = TEMPLATES.get(key.strip())
    if found is None:
        raise invalid("Unknown purpose template: {key}.", key=key[:40])
    return found


# --- Language of the pages ----------------------------------------------------------------------

# Most common words of each language, enough to tell them apart on a sample of pages.
_FRENCH = frozenset(
    "le la les des une est et pour dans sur avec pas qui que du au aux ce cette sont nous "  # noqa: SIM905
    "vous il elle ou mais par plus être été fait faut".split()
)
_ENGLISH = frozenset(
    "the and is of to for with on in that this are not be it as by from or we you was "  # noqa: SIM905
    "have has should".split()
)
_WORD = re.compile(r"[a-zà-ÿœæ']+")
LANGUAGE_NAMES = {"fr": "French", "en": "English"}
LANGUAGE_SAMPLE = 6  # pages read to detect the language of a space
LANGUAGE_CHARS = 4_000  # characters read from each


def language_of(texts: list[str]) -> str:
    """Dominant language of a sample of texts: "fr", "en", "fr, en" (mixed), or "" if the
    sample is too thin. The most common words are enough: the point is only to tell an agent
    or an assistant which language to write or search in."""
    french = english = 0
    for text in texts:
        for word in _WORD.findall(text.lower()):
            french += word in _FRENCH
            english += word in _ENGLISH
    if french + english < 15:
        return ""
    if french >= 2 * english:
        return "fr"
    if english >= 2 * french:
        return "en"
    return "fr, en"


def space_language(repo: Repository, rev: str, purpose: str | None = None) -> str:
    """Language of a space's pages at commit `rev`, from its home page and its longest pages;
    failing that (no page, or too little text), the language of its vocation `purpose`.
    Same codes as `language_of`."""
    prefix = CONTENT_DIR + "/"
    pages = [b for b in repo.files(CONTENT_DIR, rev) if b.path.endswith(".md")]
    chosen = sorted(pages, key=lambda b: (b.path != prefix + "README.md", -b.size))
    data = repo.read_many([b.path for b in chosen[:LANGUAGE_SAMPLE]], rev)
    found = language_of([d[:LANGUAGE_CHARS].decode("utf-8", "replace") for d in data.values()])
    if not found and purpose:
        found = language_of([for_prompt(purpose)])
    return found


def language_note(code: str) -> str:
    """Sentence telling an agent the detected language of the space's pages ("" if unknown)."""
    names = [LANGUAGE_NAMES.get(c.strip(), c.strip()) for c in code.split(",") if c.strip()]
    if not names:
        return ""
    if len(names) > 1:
        return (
            f" The existing pages of this space mix {' and '.join(names)}: keep the language "
            "of the page or folder you write in."
        )
    return f" The existing pages of this space are written in {names[0]}."


# --- Reading ----------------------------------------------------------------------------------


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    text = data.decode("utf-8", "replace").strip()
    return text or None


def read(repo: Repository, rev: str | None = None) -> str | None:
    """Vocation of the space at commit `rev` (HEAD by default), or None if it has none.

    The text is as written in the repository (even beyond MAX_LENGTH, possible through a
    push): `for_prompt` prepares it for a model."""
    return _decode(repo.blob(VOCATION_FILE, rev or "HEAD"))


_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def for_prompt(text: str) -> str:
    """Text to give to a model: without the templates' HTML comments (writing help), extra
    blank lines removed, capped at MAX_LENGTH."""
    clean = re.sub(r"\n{3,}", "\n\n", _COMMENT.sub("", text)).strip()
    if len(clean) > MAX_LENGTH:
        clean = clean[:MAX_LENGTH] + "\n[… vocation truncated]"
    return clean


def excerpt(text: str, limit: int = EXCERPT_LENGTH) -> str:
    """Beginning of the vocation, on one line: enough to choose a space without reading it
    all."""
    flat = " ".join(
        line.strip().lstrip("#").strip()
        for line in for_prompt(text).splitlines()
        if line.strip() and line.strip() not in TITLES
    )
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _synced(ws: Workspace, space: Space) -> Repository | None:
    try:
        return ws.local_repository(space)
    except SynaptaneError:
        return None  # not synced yet


def space_vocation(actor: Actor, ws: Workspace, space: Space) -> str | None:
    """Vocation read by a member of the space (assistants, REST)."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    repo = _synced(ws, space)
    return read(repo) if repo is not None else None


@dataclass(frozen=True)
class Document:
    """One of the two files, for the settings page and the REST API."""

    kind: Kind
    path: str  # file read: `kind.path`, or its former name if alone
    text: str  # empty if it does not exist
    sha: str | None  # hash of the file (None: missing), to send back when saving
    pending: bool = False  # waiting for the first sync
    synced: bool = True
    history: list[Commit] = field(default_factory=list)


def document(actor: Actor, ws: Workspace, space: Space, kind: Kind) -> Document:
    """Current text and latest changes. Reserved for administrators, except the vocation,
    readable by any member of the space."""
    if kind is VOCATION:
        require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    else:
        require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    repo = _synced(ws, space)
    if repo is None:
        waiting = _load_pending(pending_file(ws.clone_path(space.id)))
        text = waiting.files.get(kind.path) if waiting else None
        return Document(kind, kind.path, text or "", None, pending=text is not None, synced=False)
    path = current_path(repo, kind)
    data = repo.blob(path)
    return Document(
        kind,
        path,
        (data or b"").decode("utf-8", "replace"),
        repo.blob_sha(path),
        history=repo.history(path, limit=5),
    )


# --- Writing ---------------------------------------------------------------------------------


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return text + "\n" if text else ""


def save(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    kind: Kind,
    text: str,
    *,
    base_sha: str | None = None,
    allow_secrets: bool = False,
    source: str = "web",
) -> str | None:
    """Saves the vocation or the rules; an empty text removes the file.

    Reserved for the organization's administrators. `base_sha`: hash read before editing
    ("": the file did not exist); conflict if the file changed in the meantime (a push,
    another administrator). A likely secret is refused, unless `allow_secrets`.

    Returns the commit created; None if nothing changed, or if the space is not synced yet
    (the text is then kept pending for the first sync)."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    clean = _normalize(text)
    if len(clean) > MAX_LENGTH:
        raise invalid(
            "Text too long: {length} characters, {limit} at most.",
            limit=f"{MAX_LENGTH:,}".replace(",", "\u202f"),
            length=f"{len(clean):,}".replace(",", "\u202f"),
        )
    data = clean.encode() if clean else None
    if data is not None and not allow_secrets:  # scanned outside the space lock
        repo = _synced(ws, space)
        old = repo.blob(current_path(repo, kind)) if repo is not None else None
        refuse_secrets(added_findings(kind.path, data, old))
    author = author_identity(actor.user, ws.committer)
    # The request's connection goes back to the pool during git (like `content.writable`).
    session.commit()
    with try_space_lock(database.engine, space.id, timeout=30) as acquired:
        if not acquired:
            raise conflict("The space is busy (synchronization in progress). Try again.")
        repo = _synced(ws, space)  # read again under the lock: a sync may have happened
        if repo is None:
            _save_pending(pending_file(ws.clone_path(space.id)), kind.path, clean, author)
            sha = None
        else:
            if repaired := repo.heal():
                log.warning("Space %s: local copy repaired (%s)", space.id, ", ".join(repaired))
            if base_sha is not None and repo.blob_sha(current_path(repo, kind)) != (
                base_sha or None
            ):
                raise conflict(
                    "The file {path} has changed since you opened it (push or another "
                    "administrator): reload the page before saving.",
                    path=kind.path,
                )
            subject = kind.commit if data is not None else kind.removed
            subject += trailers(source, actor.via)
            changes: dict[str, bytes | None] = {kind.path: data}
            if kind.legacy and repo.exists_at(kind.legacy):
                changes[kind.legacy] = None  # moves to the new name (or removes the old file)
            sha = repo.commit_changes(changes, author, subject)
    if sha:
        jobs.enqueue(session, "sync", space.id)
    if sha or repo is None:
        audit(
            session,
            actor,
            kind.action,
            space=space,
            chars=len(clean),
            pending=repo is None,
            source=source,
        )
    return sha


# --- Pending until the first sync ----------------------------------------------------------------


def start_with(
    session: Session, actor: Actor, ws: Workspace, space: Space, chosen: Template
) -> None:
    """Vocation of a space just created, from a template in the creator's language: kept
    pending, then written by the first sync (the repository is not cloned yet, and no sync
    can have started: no need for the space lock)."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    author = author_identity(actor.user, ws.committer)
    text = chosen.text_for(actor.user.locale)
    _save_pending(pending_file(ws.clone_path(space.id)), VOCATION_FILE, text, author)
    audit(session, actor, VOCATION.action, space=space, template=chosen.key, pending=True)


def pending(ws: Workspace, space: Space) -> bool:
    """True if the space has a vocation or rules waiting for its first sync."""
    return pending_file(ws.clone_path(space.id)).is_file()


PENDING_SUFFIX = ".attente.json"


def pending_file(clone: Path) -> Path:
    """Pending texts of a space, next to its local copy (`<id>.attente.json`)."""
    return clone.with_name(clone.name + PENDING_SUFFIX)


@dataclass
class _Pending:
    files: dict[str, str]
    author: Identity


def _load_pending(path: Path) -> _Pending | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text("utf-8"))
        files = {
            (RULES_FILE if p == LEGACY_RULES_FILE else p): t
            for p, t in raw["files"].items()
            if p in (VOCATION_FILE, RULES_FILE, LEGACY_RULES_FILE) and t.strip()
        }
        return _Pending(files, Identity(str(raw["author"]["name"]), str(raw["author"]["email"])))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        log.warning("Unreadable pending texts (%s): %s", path, error)
        return None


def _save_pending(path: Path, repo_path: str, text: str, author: Identity) -> None:
    current = _load_pending(path)
    files = dict(current.files) if current else {}
    if text:
        files[repo_path] = text
    else:
        files.pop(repo_path, None)
    if not files:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"author": {"name": author.name, "email": author.email}, "files": files}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False), "utf-8")
    temporary.replace(path)


def apply_pending(repo: Repository) -> str | None:
    """Writes to the repository the texts kept pending before its first sync, under the
    space lock (called by `git_sync`). A file the repository already has (an existing
    repository that already has a vocation) is not replaced: Git remains the source.

    Returns the commit created, or None. The pending file is removed in every case."""
    path = pending_file(repo.path)
    waiting = _load_pending(path)
    if waiting is None:
        path.unlink(missing_ok=True)
        return None
    present = {kind.path for kind in KINDS.values() if repo.exists_at(current_path(repo, kind))}
    changes: dict[str, bytes | None] = {
        p: t.encode() for p, t in sorted(waiting.files.items()) if p not in present
    }
    if skipped := sorted(set(waiting.files) - set(changes)):
        log.info("Pending texts ignored, already in the repository: %s", ", ".join(skipped))
    sha = None
    if changes:
        what = " and ".join("vocation" if p == VOCATION_FILE else "tidy-up rules" for p in changes)
        subject = f"Synaptane: {what}, saved before the first sync"
        sha = repo.commit_changes(changes, waiting.author, subject)
    path.unlink(missing_ok=True)
    return sha
