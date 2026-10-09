"""Linking a space to its Git repository and keeping it synchronized (docs/admin/git.md).

Request side: credentials and webhook settings, synchronization and resolution requests.
Worker side: `run_sync` and `run_resolution`. Network operations (clone, fetch, push) happen
outside the space lock, which is only taken for local steps: a silent forge never blocks
writes.
"""

import hashlib
import hmac
import logging
import secrets
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import invalid
from synaptane.core.security import SecretBox
from synaptane.git.credentials import GitCredentials
from synaptane.git.repo import (
    LOCAL_REPOS_DISABLED,
    REMOTE_TOO_FAST,
    GitError,
    Identity,
    PushRejected,
    Repository,
    describe_error,
)
from synaptane.services import Actor, alerts, audit, changes, jobs, vocation, webhooks
from synaptane.services.inbox import CONTENT_DIR, INBOX_DIR, parse_front_matter, skeleton
from synaptane.services.secrets import delete_space_secret, get_space_secret, put_space_secret
from synaptane.services.workspace import Workspace, author_identity
from synaptane.storage.db import Database, space_lock, sync_lock
from synaptane.storage.models import Job, Organization, Space, User

GIT_CREDENTIALS = "git"
WEBHOOK_SECRET = "webhook"
SYNC, RESOLVE = "sync", "resolve"
# Author of the commits reapplying local changes after a forced recovery: distinct from
# Synaptane, so the recovered pages are still reported to the tidy run. Older versions wrote
# "Synaptane – reprise"; nothing reads this name back (only Synaptane's address is compared).
RECOVERY_AUTHOR = "Synaptane recovery"
# A transient error (network, forge unavailable) notifies no one… as long as it does not last
# longer than this (since the first error of the series, `Space.sync_error_since`).
TRANSIENT_ALERT_AFTER = timedelta(hours=6)


# --- Settings (web / REST requests) -----------------------------------------------------------


def credential_kind(session: Session, box: SecretBox, space: Space) -> str | None:
    value = get_space_secret(session, box, space.id, GIT_CREDENTIALS)
    return GitCredentials.from_json(value).kind if value else None


def set_credentials(
    session: Session, actor: Actor, box: SecretBox, space: Space, credentials: GitCredentials
) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    if space.repo_url and not credentials.matches(space.repo_url):
        raise invalid(
            "These credentials do not match the repository URL: a token for an https:// URL, "
            "an SSH key for an ssh:// or git@… URL."
        )
    put_space_secret(session, box, space.id, GIT_CREDENTIALS, credentials.to_json(), actor.user.id)
    audit(session, actor, "space.git_credentials_set", space=space, kind=credentials.kind)
    jobs.enqueue(session, SYNC, space.id)


def clear_credentials(session: Session, actor: Actor, space: Space) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    if delete_space_secret(session, space.id, GIT_CREDENTIALS):
        audit(session, actor, "space.git_credentials_cleared", space=space)


def compare_with_remote(actor: Actor, ws: Workspace, space: Space, path: str) -> str:
    """Diff of a file between the local version (Synaptane) and the remote version."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    if path not in space.conflict_paths:
        raise invalid("This file is not in conflict.")
    repo = ws.local_repository(space)
    return repo.compare(path, repo.remote_ref)


def webhook_secret(session: Session, actor: Actor, box: SecretBox, space: Space) -> str | None:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    return get_space_secret(session, box, space.id, WEBHOOK_SECRET)


def rotate_webhook_secret(session: Session, actor: Actor, box: SecretBox, space: Space) -> str:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    value = secrets.token_urlsafe(32)
    put_space_secret(session, box, space.id, WEBHOOK_SECRET, value, actor.user.id)
    audit(session, actor, "space.webhook_secret_rotated", space=space)
    return value


def request_sync(session: Session, actor: Actor, space: Space) -> None:
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    if not space.repo_url:
        raise invalid("No Git repository is configured for this space.")
    # Explicit request: a synchronization already waiting after failures (growing delay, up to
    # nearly an hour) starts again right away, attempts reset to zero.
    jobs.enqueue(session, SYNC, space.id, replace=True)


# --- Synchronization requested or running: what the "Git repository" page says about it -----

SOON = timedelta(seconds=90)  # a sync due by then is "queued": the page waits for it
STALLED = timedelta(minutes=5)  # due for longer than that: is the worker running?


@dataclass(frozen=True)
class SyncActivity:
    """Synchronization requested or running. `state`: "queued" (waiting for the worker),
    "running" (taken by the worker), "retry" (automatic new attempt planned at `at`, after
    `failures` failures)."""

    state: str
    at: datetime | None = None
    failures: int = 0
    stalled: bool = False

    @property
    def poll(self) -> bool:
        """The page polls the server as long as the result may arrive at any moment (not for a
        new attempt planned in an hour)."""
        return self.state in ("queued", "running")


def sync_activity(
    session: Session, space_id: UUID, now: datetime | None = None
) -> SyncActivity | None:
    now = now or datetime.now(UTC)
    rows = session.execute(
        select(Job.status, Job.run_after, Job.attempts).where(
            Job.kind == SYNC, Job.space_id == space_id, Job.status.in_(("queued", "running"))
        )
    ).all()
    if any(status == "running" for status, _, _ in rows):
        return SyncActivity("running")
    if not rows:
        return None
    _, run_after, attempts = min(rows, key=lambda row: row[1])
    if run_after > now + SOON:
        return SyncActivity("retry", at=run_after, failures=attempts)
    return SyncActivity("queued", at=run_after, stalled=now - run_after > STALLED)


def sync_version(session: Session, space: Space) -> str:
    """Fingerprint of the sync state: the "Git repository" page reloads when it changes."""
    current = sync_activity(session, space.id)
    return ":".join(
        [
            space.sync_status,
            space.synced_at.isoformat() if space.synced_at else "-",
            space.sync_error or "-",
            current.state if current else "idle",
        ]
    )


def request_resolution(
    session: Session, actor: Actor, space: Space, choices: Mapping[str, str]
) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    if space.sync_status != "conflict":
        raise invalid("This space is not in conflict.")
    unknown = set(choices) - set(space.conflict_paths)
    missing = set(space.conflict_paths) - set(choices)
    if unknown or missing or any(v not in ("local", "remote") for v in choices.values()):
        raise invalid("Choose “local” or “remote” for each conflicting file.")
    jobs.replace_payload(
        session, RESOLVE, space.id, {"choices": dict(choices), "by": str(actor.user.id)}
    )
    audit(session, actor, "space.conflict_resolution_requested", space=space, files=len(choices))


def verify_webhook(secret: str, headers: Mapping[str, str], body: bytes) -> bool:
    """GitHub and Gitea sign the body (HMAC-SHA256); GitLab sends the secret as is."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if signature := headers.get("x-hub-signature-256"):
        return hmac.compare_digest(signature, "sha256=" + digest)
    if signature := headers.get("x-gitea-signature"):
        return hmac.compare_digest(signature, digest)
    if token := headers.get("x-gitlab-token"):
        return hmac.compare_digest(token, secret)
    return False


def accept_webhook(
    session: Session,
    box: SecretBox,
    org_slug: str,
    space_slug: str,
    headers: Mapping[str, str],
    body: bytes,
) -> bool:
    """True if the call is authenticated; a synchronization is then requested."""
    space = session.scalar(
        select(Space)
        .join(Space.organization)
        .where(Organization.slug == org_slug, Space.slug == space_slug)
    )
    if space is None:
        return False
    secret = get_space_secret(session, box, space.id, WEBHOOK_SECRET)
    if not secret or not verify_webhook(secret, headers, body):
        return False
    jobs.enqueue(session, SYNC, space.id)
    return True


# --- Execution (worker) -----------------------------------------------------------------------


@dataclass
class Outcome:
    status: str  # "ok" or "conflict"
    conflicts: list[str] = field(default_factory=list)
    remote_head: str | None = None
    external_paths: list[str] = field(default_factory=list)  # content/ changed outside Synaptane
    reset_watermark: bool = False  # history changed: watermark of changes to reset…
    watermark: str | None = None  # … to this commit (before the recovered changes)
    recovery: "Recovery | None" = None


@dataclass
class Recovery:
    """Local changes recovered after a repository change or a rewrite."""

    reason: str
    bundle: str | None  # full backup of the old local branch
    reapplied: list[str] = field(default_factory=list)
    kept_aside: list[str] = field(default_factory=list)  # changed on both sides


@dataclass(frozen=True)
class _Snapshot:
    remote: str
    branch: str
    name: str
    description: str
    credentials: GitCredentials | None
    integrated_head: str | None  # last remote commit integrated and reported
    change_watermark: str | None = None  # watermark of the changes to tidy


def _snapshot(database: Database, ctx: Workspace, space_id: UUID) -> _Snapshot | None:
    with database.session() as session:
        space = session.get(Space, space_id)
        if space is None or not space.repo_url:
            return None
        raw = get_space_secret(session, ctx.box, space.id, GIT_CREDENTIALS)
        return _Snapshot(
            remote=space.repo_url,
            branch=space.branch,
            name=space.name,
            description=space.description,
            credentials=GitCredentials.from_json(raw) if raw else None,
            integrated_head=space.remote_head,
            change_watermark=space.change_watermark,
        )


def _repository(
    ctx: Workspace, space_id: UUID, snapshot: _Snapshot, path: Path | None = None
) -> Repository:
    if snapshot.remote.startswith("file://") and not ctx.allow_local_repos:
        raise GitError(LOCAL_REPOS_DISABLED)
    return Repository(
        path or ctx.clone_path(space_id),
        snapshot.remote,
        snapshot.branch,
        ctx.committer,
        snapshot.credentials,
        ctx.network_timeout,
    )


def _record(
    database: Database,
    space_id: UUID,
    outcome: Outcome | None,
    error: str | None,
    ctx: Workspace | None = None,
    transient: bool = True,
) -> None:
    """Records what was integrated (`outcome`), then the error that interrupted the
    synchronization if there is one (failed fetch or push): a rewritten history already
    adopted, or external commits already reported, must not be so twice."""
    with database.session() as session:
        space = session.get(Space, space_id)
        if space is None:
            return
        if outcome is not None:
            _apply(session, space, outcome)
        now = datetime.now(UTC)
        if error is None:
            space.sync_error_since = None
            if outcome is not None and outcome.status == "ok":
                space.synced_at = now
            return
        space.sync_status, space.sync_error = "error", error
        space.sync_error_since = space.sync_error_since or now
        lasting = now - space.sync_error_since
        if ctx is not None and not transient:  # error that will not go away on its own
            alerts.notify(
                session, ctx.box, space, "sync_error", ctx.public_url, error=alert_text(error)
            )
        elif ctx is not None and lasting >= TRANSIENT_ALERT_AFTER:
            # Transient, but lasting (forge unreachable for hours): notify.
            hours = int(lasting.total_seconds() // 3600)
            alerts.notify(
                session,
                ctx.box,
                space,
                "sync_error",
                ctx.public_url,
                error=alert_text(error, hours),
            )


def alert_text(error: str, hours: int | None = None) -> Callable[[str], str]:
    """The error of an alert e-mail, in each recipient's language (`mail.localized`)."""

    def text(locale: str) -> str:
        from synaptane.web.i18n import translator

        translate = translator(locale)
        described = describe_error(error, translate)
        if hours is None:
            return described
        return translate("{error} (transient error for {hours} h)", error=described, hours=hours)

    return text


def _apply(session: Session, space: Space, outcome: Outcome) -> None:
    if outcome.status == "conflict" and space.sync_status != "conflict":
        webhooks.emit(session, space, "sync.conflict", {"paths": outcome.conflicts})
    if outcome.external_paths:
        webhooks.emit(
            session,
            space,
            "content.changed",
            {
                "paths": outcome.external_paths,
                "action": "pushed",
                "by": "git",
                "source": "git",
            },
        )
    space.sync_status, space.sync_error = outcome.status, None
    space.conflict_paths = outcome.conflicts
    space.remote_head = outcome.remote_head
    if outcome.reset_watermark:
        changes.reset(session, space, outcome.watermark)
    if outcome.recovery is not None:
        _report_recovery(session, space, outcome.recovery)
    if outcome.status == "ok":
        # Watermark of the changes for the tidy run to review (first clone).
        changes.start(session, space, outcome.remote_head)


log = logging.getLogger("synaptane.git")
# Files carried over after a repository change or a rewritten history.
CARRIED = (
    CONTENT_DIR,
    INBOX_DIR,
    vocation.VOCATION_FILE,
    vocation.RULES_FILE,
    vocation.LEGACY_RULES_FILE,
)


def _report_recovery(session: Session, space: Space, recovery: Recovery) -> None:
    audit(
        session,
        None,
        "space.local_changes_recovered",
        space=space,
        reason=recovery.reason,
        bundle=recovery.bundle or "",
        reapplied=len(recovery.reapplied),
        kept_aside=", ".join(recovery.kept_aside[:50]),
    )
    log.warning(
        "Space %s: %s; %d file(s) reapplied, %d set aside (backup: %s)",
        space.id,
        recovery.reason,
        len(recovery.reapplied),
        len(recovery.kept_aside),
        recovery.bundle,
    )


def _set_aside(
    repo: Repository, recovery_dir: Path, base: str | None, reason: str
) -> tuple[dict[str, tuple[bytes | None, bytes | None]], Recovery]:
    """Backs up the local branch (bundle) and lists the files changed since `base`."""
    carried = repo.local_changes(base, CARRIED)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle = repo.bundle(recovery_dir / f"{stamp}.bundle") if carried else None
    return carried, Recovery(reason, str(bundle) if bundle else None)


def _reapply(
    repo: Repository,
    carried: dict[str, tuple[bytes | None, bytes | None]],
    recovery: Recovery,
    committer: Identity,
) -> None:
    """Reapplies the files changed locally, except those also changed on the remote side."""
    if not carried:
        return
    current = repo.read_many(list(carried))
    files: dict[str, bytes | None] = {}
    for path, (before, mine) in sorted(carried.items()):
        theirs = current.get(path)
        if theirs == mine:
            continue  # already identical
        if theirs == before:
            files[path] = mine
            recovery.reapplied.append(path)
        else:
            recovery.kept_aside.append(path)  # changed on both sides: kept in the bundle
    author = Identity(RECOVERY_AUTHOR, committer.email.replace("@", "+recovery@", 1))
    if files:
        repo.commit_changes(files, author, f"Synaptane: recover local changes ({recovery.reason})")


def _legacy_changes(repo: Repository) -> list[str]:
    """Inbox `change` files from before version 0.9: changed pages are now computed from the
    history; these files would be duplicates."""
    if repo.head() is None:
        return []
    names = [
        b.path
        for b in repo.files(INBOX_DIR)
        if b.path.endswith(".md") and "-change-" in b.path.rsplit("/", 1)[-1]
    ]
    if not names:
        return []
    found = []
    for path, data in repo.read_many(names).items():
        fields, _ = parse_front_matter(data.decode("utf-8", "replace"))
        if fields.get("type") == "change":
            found.append(path)
    return sorted(found)


@dataclass
class _Progress:
    """What a synchronization keeps from one round to the next (fetch, integrate, push)."""

    integrated_head: str | None  # last remote commit integrated (changes after a force-push)
    started: bool = False
    previous: str | None = None  # starting point of the external commits to report
    external_paths: set[str] = field(default_factory=set)
    carried: dict[str, tuple[bytes | None, bytes | None]] = field(default_factory=dict)
    recovery: Recovery | None = None
    reset_watermark: bool = False
    watermark: str | None = None


class _Skip(Exception):
    """Nothing left to do (resolution already done, duplicate request): stop quietly."""


def _install(
    repo: Repository,
    staged: Repository,
    snapshot: _Snapshot,
    recovery_dir: Path,
    progress: _Progress,
) -> None:
    """Replaces the local copy with the clone made outside the lock (new repository, URL or
    branch changed), after setting the old local branch aside.

    The watermark and decisions survive if the new history contains the watermark: same
    repository under another address (https → ssh), or a branch that contains it. Otherwise,
    they are reset."""
    replaced = repo.exists()
    if replaced:
        base = repo.merge_base("HEAD", repo.remote_ref) if repo.remote_head() else None
        progress.carried, progress.recovery = _set_aside(
            repo, recovery_dir, base, "repository changed"
        )
    repo.discard()
    staged.path.rename(repo.path)
    if replaced:
        progress.reset_watermark = not changes.survives(repo, snapshot.change_watermark)


def _integrate(
    repo: Repository,
    staged: Repository | None,
    snapshot: _Snapshot,
    committer: Identity,
    recovery_dir: Path,
    progress: _Progress,
) -> tuple[Outcome, str | None]:
    """One integration round, under the space lock and **without network**: the fetch (or the
    clone, in `staged`) is already done. Returns the outcome and the commit to push (None:
    nothing to push).

    Nothing local is ever discarded without a backup: after a URL or branch change, or a
    rewritten remote history (force-push), the local branch is kept in a bundle and its file
    changes are reapplied on top of the new remote state."""
    if repaired := repo.heal():
        log.warning("Local copy repaired before synchronization: %s", ", ".join(repaired))
    if staged is not None:
        _install(repo, staged, snapshot, recovery_dir, progress)
    if not progress.started:
        progress.started = True
        repo.remove_symlinks()  # copies made before core.symlinks=false
        # Starting point of the external commits to report: the last remote commit integrated,
        # kept in the database (the local ref moves on fetch, even in case of conflict).
        previous = progress.integrated_head
        progress.previous = previous if previous and repo.rev(previous) else None
    current = repo.remote_head()
    integrated = progress.integrated_head
    if (
        current
        and integrated
        and repo.rev(integrated)
        and not repo.is_ancestor(integrated, current)
    ):
        # Remote history rewritten (force-push, branch recreated): adopt it. The removed
        # commits must not come back; only ours since the last integration are carried
        # over.
        progress.carried, progress.recovery = _set_aside(
            repo, recovery_dir, integrated, "history rewritten"
        )
        repo.reset_to_remote()
        progress.integrated_head = current
        progress.previous, progress.reset_watermark = current, True
    ahead, behind = repo.ahead_behind()
    if behind:
        if ahead == 0:
            repo.fast_forward()
        else:
            conflicts = repo.merge_remote("Synaptane: merge remote changes")
            if conflicts:
                outcome = Outcome(
                    "conflict",
                    conflicts,
                    progress.previous,
                    reset_watermark=progress.reset_watermark,
                    recovery=progress.recovery,
                )
                return outcome, None
    if current and progress.previous is None and progress.integrated_head is None:
        progress.previous = current  # first clone: the existing history is not "new"
    if progress.previous and current and progress.previous != current:
        progress.external_paths.update(
            p[len(CONTENT_DIR) + 1 :]
            for c in repo.commits_between(progress.previous, current)
            if c.committer_email != committer.email
            for p in c.paths
            if p.startswith(CONTENT_DIR + "/")
        )
    progress.previous = current
    existing = {d for d in (CONTENT_DIR, INBOX_DIR) if (repo.path / d).is_dir()}
    if files := skeleton(snapshot.name, snapshot.description, existing):
        repo.commit_changes(files, committer, "Synaptane: initialize space")
    # Purpose (and rules) chosen before the repository was cloned: written now.
    vocation.apply_pending(repo)
    if legacy := _legacy_changes(repo):
        repo.commit_changes(
            dict.fromkeys(legacy),
            committer,
            "Synaptane: remove old inbox format (changes are computed from Git)",
        )
    if progress.recovery is not None and progress.carried:
        progress.watermark = repo.head()  # if the watermark is reset: recovered pages to review
        _reapply(repo, progress.carried, progress.recovery, committer)
        progress.carried = {}
    if snapshot.change_watermark and repo.rev(snapshot.change_watermark) is None:
        progress.reset_watermark = True  # watermark gone (purge, other repository): reset
    if progress.reset_watermark and progress.watermark is None:
        progress.watermark = repo.head()
    outcome = Outcome(
        "ok",
        [],
        repo.remote_head(),
        sorted(progress.external_paths),
        reset_watermark=progress.reset_watermark,
        watermark=progress.watermark,
        recovery=progress.recovery,
    )
    return outcome, repo.head() if repo.ahead_behind()[0] else None


Prepare = Callable[[Repository], Outcome | None]


def _exchange(
    database: Database,
    ctx: Workspace,
    space_id: UUID,
    repo: Repository,
    snapshot: _Snapshot,
    prepare: Prepare | None = None,
) -> None:
    """Synchronizes in rounds: network outside the lock, integration under the lock, push
    outside the lock. No network access under the space lock: a forge that stops answering
    blocks neither web, REST and MCP writes, nor the commit of a tidy run.

    `prepare`: local step of the first round, under the lock, before integration (conflict
    resolution). It may return an outcome that stops there.

    A network error stops the synchronization: it is recorded (with what was already
    integrated) and the job will be retried. Never a new attempt under the lock."""
    progress = _Progress(snapshot.integrated_head)
    outcome: Outcome | None = None
    staging = ctx.clone_path(space_id).with_name(f"{space_id}.clone")
    try:
        for attempt in range(3):
            # 1. Network, outside the lock: `fetch` only touches `origin/<branch>`, the clone
            #    goes to a separate directory, put in place under the lock.
            staged = None
            if attempt == 0 and (not repo.exists() or not repo.matches_configuration()):
                staged = _repository(ctx, space_id, snapshot, staging)
                staged.clone()
            else:
                repo.fetch()
            # 2. Local integration, under the lock.
            with space_lock(database.engine, space_id):
                # No resolution on a copy being replaced: its conflicts go away.
                first = attempt == 0 and staged is None
                early = prepare(repo) if prepare is not None and first else None
                if early is not None:
                    outcome = early
                    break
                outcome, to_push = _integrate(
                    repo, staged, snapshot, ctx.committer, ctx.recovery_path(space_id), progress
                )
            if to_push is None:
                break
            # 3. Push, outside the lock: `push` only reads objects. HEAD may move meanwhile
            #    (web write): the integrated commit is pushed, the rest will go with the
            #    synchronization that this write requested.
            try:
                repo.push(to_push)
            except PushRejected:
                continue  # the remote moved on: new round, starting with a fetch
            with space_lock(database.engine, space_id):
                repo.pushed(to_push)
            outcome.remote_head = to_push
            break
        else:
            raise GitError(REMOTE_TOO_FAST, transient=True)
    except GitError as error:
        _record(database, space_id, outcome, str(error), ctx, error.transient)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)  # interrupted clone
    _record(database, space_id, outcome, None)


def run_sync(database: Database, ctx: Workspace, space_id: UUID) -> None:
    # One synchronization at a time per space: the network happens outside the space lock,
    # two synchronizations must not fetch, clone or push at the same time.
    with sync_lock(database.engine, space_id):
        snapshot = _snapshot(database, ctx, space_id)
        if snapshot is None:
            return
        try:
            repo = _repository(ctx, space_id, snapshot)
        except GitError as error:
            _record(database, space_id, None, str(error), ctx, error.transient)
            raise
        _exchange(database, ctx, space_id, repo, snapshot)
        repo.gc()  # space lock released: a long gc blocks no one


RESOLVED_AS = {"local": "Synaptane's version", "remote": "repository's version"}


def _resolution_commit(
    database: Database, ctx: Workspace, choices: Mapping[str, str], by: UUID | None
) -> tuple[str, Identity | None]:
    """Message (choice file by file) and author (who decided) of the resolution commit."""
    with database.session() as session:
        user = session.get(User, by) if by else None
        author = author_identity(user, ctx.committer) if user else None
        name = user.display_name if user else ""
    lines = [f"- {path}: {RESOLVED_AS.get(side, side)}" for path, side in sorted(choices.items())]
    body = (f"Decided by {name}.\n\n" if name else "") + "\n".join(lines)
    return f"Synaptane: resolve conflicts\n\n{body}", author


def _in_conflict(database: Database, space_id: UUID) -> bool:
    with database.session() as session:
        current = session.get(Space, space_id)
        return current is not None and current.sync_status == "conflict"


def run_resolution(
    database: Database,
    ctx: Workspace,
    space_id: UUID,
    choices: Mapping[str, str],
    by: UUID | None = None,
) -> None:
    with sync_lock(database.engine, space_id):
        snapshot = _snapshot(database, ctx, space_id)
        if snapshot is None or not _in_conflict(database, space_id):
            return  # duplicate request, or repository changed in the meantime
        message, author = _resolution_commit(database, ctx, choices, by)
        try:
            repo = _repository(ctx, space_id, snapshot)
        except GitError as error:
            _record(database, space_id, None, str(error), ctx, error.transient)
            raise

        def resolve(repo: Repository) -> Outcome | None:
            if not _in_conflict(database, space_id):  # read again under the lock
                raise _Skip
            missing = repo.merge_with_choices(choices, message, author)
            # Incomplete resolution: the remote head is not integrated yet.
            return Outcome("conflict", missing, snapshot.integrated_head) if missing else None

        try:
            _exchange(database, ctx, space_id, repo, snapshot, resolve)
        except _Skip:
            return
