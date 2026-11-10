"""Memory of an agent routine: what it wants to remember from one run to the next.

A routine starts each run afresh: the activity since its last run and its own instructions.
With `memory: true` in its file, it also keeps notes for itself: topics already reported,
pages it follows, decisions noted, what remains to watch. Without them, a weekly check
reports the same contradiction every week, and a quiet week rereads the same old pages.

Where: `.synaptane/memory/<name>.md`, a file of the repository (`routine_files.memory_path`).
- Git stays the source of truth: the team sees the memory, its history says what the
  routine noted and when, and anyone who can push can correct or delete it, as anything
  else in `.synaptane/`.
- A file of its own, never the routine's file: a run does not rewrite what the team wrote
  (a changed routine file would also conflict with an administrator's edit, `base_sha`).
- Under `.synaptane/`, outside `content/`: not a page of the wiki (neither search, nor the
  tidy-up, nor the contributors' write paths, which are confined to `content/` and the
  inbox, reach it).
- The memory follows the routine's name: removed with the routine from Synaptane, and by the
  next synchronization when a push removes or renames the routine without moving it
  (`routine_files._drop_orphan_memories`).

Opt-in: a memory is read at every run, so it costs tokens every time; existing routines keep
their behavior and nothing is written in the repository unless the routine's file asks for
it. The provided consistency check asks for one.

Reading (`recall`): at the start of the run, at the commit the run reads, capped at
MAX_MEMORY characters. It derives from texts of the wiki and anyone who can push can edit it:
the agent receives it framed as untrusted content (`tidy_tools.untrusted`), never as
instructions; it changes neither the routine's tools, nor its scope, nor its outputs.

Writing (`routine_agent`, tool `remember`): the agent rewrites it in full, MAX_MEMORY characters
at most (a summary, not a log that grows without end). The secret detector applies, and so do
the team's `block` checks that concern the text of every page (`refusal`). The memory goes in
the run's single commit, with the report and the inbox items: an interrupted run leaves it as
it was. If the memory changed during the run (a push, an administrator clearing it), that
change wins and the run's version is dropped, which the run's log says: the agent never saw
that change, and a clearing must not be undone by a run that started before it.

Rights: only the routine's run writes it from Synaptane. The space's contributors, who see the
routines, read it; the organization's administrators also clear it (a commit in their name).
"""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.checks import Checks
from synaptane.core.errors import SynaptaneError, conflict, invalid
from synaptane.git.repo import PathRefused, Repository
from synaptane.services import Actor, audit, jobs
from synaptane.services import checks as team_checks
from synaptane.services.routine_files import memory_enabled, memory_path
from synaptane.services.workspace import Workspace, author_identity, trailers
from synaptane.storage.db import Database, try_space_lock
from synaptane.storage.models import Routine, Space

log = logging.getLogger("synaptane.routines")

MAX_MEMORY = 4_000  # characters: what the agent writes, and what it is given
MAX_FILE = 64_000  # bytes of the file read at most (a push may bring more)


@dataclass(frozen=True)
class Recalled:
    """The memory as a run receives it."""

    text: str  # "": none, or empty
    sha: str | None  # of the file at the run's commit (None: no file)
    note: str = ""  # for the model: truncated, or too large to be read


def recall(repo: Repository, name: str, rev: str) -> Recalled:
    """The memory of the routine `name` at commit `rev`, capped for the model."""
    path = memory_path(name)
    entry = repo.blob_entry(path, rev)
    if entry is None:
        return Recalled("", None)
    sha, size = entry
    if size > MAX_FILE:
        return Recalled(
            "",
            sha,
            f"The memory file is too large ({size} bytes): it was not read. Rewrite it with "
            "remember, as a short summary.",
        )
    text = (repo.blob(path, rev) or b"").decode("utf-8", "replace").strip()
    if len(text) > MAX_MEMORY:
        return Recalled(
            text[:MAX_MEMORY],
            sha,
            f"Truncated: the memory is longer than {MAX_MEMORY} characters (edited by hand?). "
            "Rewrite it shorter with remember.",
        )
    return Recalled(text, sha)


def refusal(repo: Repository, rev: str, name: str, text: str) -> str | None:
    """Why the team's checks refuse this memory, for the model (None: accepted).

    Only the `block` rules on the text of every page apply (a pattern, or personal data such
    as email addresses): the memory is no page, so the rules on a front matter field or
    restricted to folders of the wiki do not concern it. As for a page, a violation already
    in the memory may stay."""
    checks = team_checks.load(repo, rev)
    rules = tuple(
        rule
        for rule in checks.rules
        if rule.blocks and not rule.field_name and not rule.paths and not rule.skip
    )
    if not rules:
        return None
    path = memory_path(name)
    found = Checks(rules=rules).introduced(path, text.encode(), repo.blob(path, rev))
    return team_checks.tool_refusal(found) if found else None


# --- Shown to people ---------------------------------------------------------------------------


@dataclass(frozen=True)
class View:
    """The memory of a routine, as Settings → Routines and the REST API show it."""

    enabled: bool  # the routine's file asks for a memory
    path: str
    text: str  # "": none
    sha: str | None
    size: int  # bytes of the file
    too_large: bool = False  # not shown (MAX_FILE)


def view(session: Session, actor: Actor, ws: Workspace, space: Space, routine: Routine) -> View:
    """The routine's memory in the local copy now; contributors read it, as the routines."""
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    path = memory_path(routine.name)
    enabled = memory_enabled(ws, space, routine.name)
    try:
        repo = ws.local_repository(space)
    except SynaptaneError:
        return View(enabled, path, "", None, 0)  # not synced yet
    entry = repo.blob_entry(path)
    if entry is None:
        return View(enabled, path, "", None, 0)
    sha, size = entry
    if size > MAX_FILE:
        return View(enabled, path, "", sha, size, too_large=True)
    text = (repo.blob(path) or b"").decode("utf-8", "replace")
    return View(enabled, path, text, sha, size)


def clear(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    routine: Routine,
    *,
    source: str = "web",
) -> bool:
    """Clears the routine's memory (removes its file): a commit in the administrator's name,
    under the space lock. A run in progress keeps it cleared (see the module). Returns False
    if there was nothing to clear."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    name, title = routine.name, routine.title
    path = memory_path(name)
    author = author_identity(actor.user, ws.committer)
    session.commit()  # the connection goes back to the pool during git
    with try_space_lock(database.engine, space.id, timeout=30) as acquired:
        if not acquired:
            raise conflict("The space is busy (synchronization in progress). Try again.")
        try:
            repo = ws.local_repository(space)
        except SynaptaneError:
            return False  # not synced yet: no memory
        if repaired := repo.heal():
            log.warning("Space %s: local copy repaired (%s)", space.id, ", ".join(repaired))
        subject = f"Routine memory cleared: {name}" + trailers(source, actor.via)
        try:
            committed = repo.commit_changes({path: None}, author, subject)
        except PathRefused as refused:
            raise invalid("Write refused: {detail}", detail=str(refused)) from None
    if committed is None:
        return False
    jobs.enqueue(session, "sync", space.id)
    audit(session, actor, "routine.memory_cleared", space=space, routine=title, file=path)
    return True
