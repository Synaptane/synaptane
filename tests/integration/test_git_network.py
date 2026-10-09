"""Network and space lock: no network command under the lock, silent forge, push outside the
lock, same repository under another URL, marker compared before writing."""

import dataclasses
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from synaptane.git.repo import GitError, Identity, Repository
from synaptane.services import changes, content, git_sync, organizations, spaces, webhooks
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database, space_lock, sync_lock, try_space_lock
from synaptane.storage.models import ChangeMark, Space
from tests.integration.conftest import actor_of, make_user
from tests.integration.gitutil import Remote, git
from tests.integration.wikidata import Wiki


def fresh(db: Session, space_id: object) -> Space:
    db.expire_all()
    found = db.get(Space, space_id)
    assert found is not None
    return found


def pending_titles(db: Session, ctx: Workspace, space_id: object) -> list[str]:
    found = fresh(db, space_id)
    return sorted(f"{i.status}:{i.title}" for i, _ in changes.pending(db, ctx, found))


# --- Silent forge --------------------------------------------------------------------------------


@pytest.fixture
def silent_forge() -> Iterator[str]:
    """A server that accepts connections but never answers (TLS blocked, load balancer under
    maintenance, half-open connection)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except TimeoutError:
                continue
            except OSError:
                return

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{server.getsockname()[1]}/acme/brain.git"
    stop.set()
    thread.join()
    for connection in held:
        connection.close()
    server.close()


@pytest.mark.parametrize("step", ["fetch", "clone"])
def test_a_silent_forge_never_blocks_writes(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, silent_forge: str, step: str
) -> None:
    """While a sync waits on a silent forge, web writes go through.

    Before the fix, a failed fetch outside the lock was retried **under** the lock: writes
    refused (409 after 30 s) and tidying discarded for the whole network timeout."""
    stalled = dataclasses.replace(ctx, network_timeout=3)
    if step == "fetch":  # copy up to date, the forge no longer answers
        git(ctx.clone_path(wiki.infra.id), "remote", "set-url", "origin", silent_forge)
    # Otherwise, new URL: the clone, done separately, waits on the forge.
    database_space = fresh(db, wiki.infra.id)
    database_space.repo_url = silent_forge
    db.commit()

    failure: list[BaseException] = []

    def sync() -> None:
        try:
            git_sync.run_sync(database, stalled, wiki.infra.id)
        except BaseException as error:  # noqa: BLE001 — read back by the test
            failure.append(error)

    thread = threading.Thread(target=sync)
    started = time.monotonic()
    thread.start()
    time.sleep(0.5)  # the sync waits on the forge
    writes = 0
    while thread.is_alive() and writes < 5:
        before = time.monotonic()
        content.write_file(
            db, database, wiki.alice, ctx, fresh(db, wiki.infra.id), f"panne-{writes}.md", b"# x\n"
        )
        db.commit()
        assert time.monotonic() - before < 2  # never the 30 s lock wait
        writes += 1
    thread.join()
    assert writes == 5 and time.monotonic() - started < 20
    assert len(failure) == 1 and isinstance(failure[0], GitError) and failure[0].transient
    failed = fresh(db, wiki.infra.id)
    assert failed.sync_status == "error" and "timed out" in (failed.sync_error or "")
    assert not ctx.clone_path(wiki.infra.id).with_name(f"{wiki.infra.id}.clone").exists()
    assert (ctx.clone_path(wiki.infra.id) / "content" / "panne-4.md").is_file()


# --- No network command under the lock -----------------------------------------------------------


@pytest.fixture
def network_probe(monkeypatch: pytest.MonkeyPatch, database: Database) -> list[tuple[str, bool]]:
    """Records each network command, and whether its space's lock was free at that moment."""
    calls: list[tuple[str, bool]] = []
    original = Repository._git

    def probe(self: Repository, *args: str, **kwargs: Any) -> Any:
        if kwargs.get("network"):
            space_id = next(
                s.id for s in _spaces(database) if self.path.name in (str(s.id), f"{s.id}.clone")
            )
            with try_space_lock(database.engine, space_id, timeout=0) as free:
                calls.append((args[0], free))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Repository, "_git", probe)
    return calls


def _spaces(database: Database) -> list[Space]:
    with database.session() as session:
        return list(session.scalars(select(Space)))


@pytest.fixture
def space(db: Session, remote: Remote) -> Space:
    root = actor_of(db, make_user(db, "root", superadmin=True))
    org = organizations.create_organization(db, root, slug="acme", name="Acme")
    space = spaces.create_space(db, root, org, slug="infra", name="Infra", repo_url=remote.url)
    db.commit()
    return space


def test_no_network_command_runs_under_the_space_lock(
    db: Session,
    database: Database,
    ctx: Workspace,
    space: Space,
    remote: Remote,
    tmp_path: Path,
    network_probe: list[tuple[str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule, checked on every path: first clone, push, merge, push rejected then retried,
    URL change, conflict resolution."""
    root = actor_of(db, make_user(db, "chef", superadmin=True))
    git_sync.run_sync(database, ctx, space.id)  # first clone (empty repository), skeleton pushed
    content.write_file(db, database, root, ctx, fresh(db, space.id), "a.md", b"# A\n")
    db.commit()
    remote.push({"content/b.md": "# B\n"})
    git_sync.run_sync(database, ctx, space.id)  # merge, then push

    rejected: list[str] = []
    push = Repository.push

    def push_after_someone_else(self: Repository, sha: str) -> None:
        if not rejected:
            rejected.append(sha)
            remote.push({"content/c.md": "# C\n"})  # the remote moves on during the push
        push(self, sha)

    monkeypatch.setattr(Repository, "push", push_after_someone_else)
    content.write_file(db, database, root, ctx, fresh(db, space.id), "d.md", b"# D\n")
    db.commit()
    git_sync.run_sync(database, ctx, space.id)
    assert rejected and remote.read("content/d.md") == "# D\n"
    assert remote.read("content/c.md") == "# C\n"

    rene = Identity("René", "rene@example.com")
    ctx.local_repository(fresh(db, space.id)).commit_changes(
        {"content/e.md": b"# E\n"}, rene, "pas encore envoyée"
    )
    other = Remote(tmp_path / "ailleurs")
    other.push({"content/autre.md": "# Autre\n"})
    spaces.update_space(db, root, fresh(db, space.id), repo_url=other.url)
    db.commit()
    git_sync.run_sync(database, ctx, space.id)  # new clone, local work carried over
    assert other.read("content/e.md") == "# E\n"

    ctx.local_repository(fresh(db, space.id)).commit_changes(
        {"content/e.md": b"# Local\n"}, rene, "local"
    )
    other.push({"content/e.md": "# Distant\n"})
    git_sync.run_sync(database, ctx, space.id)
    assert fresh(db, space.id).sync_status == "conflict"
    git_sync.run_resolution(database, ctx, space.id, {"content/e.md": "local"})
    assert other.read("content/e.md") == "# Local\n"

    kinds = {kind for kind, _ in network_probe}
    assert {"clone", "fetch", "push", "ls-remote"} <= kinds
    assert [call for call in network_probe if not call[1]] == []


def test_writes_during_the_push_are_kept_for_the_next_one(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The push happens outside the lock: a write can arrive meanwhile. The integrated commit
    goes out; the new write will go out with the next sync."""
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "avant.md", b"# Avant\n")
    db.commit()
    push = Repository.push

    def push_while_writing(self: Repository, sha: str) -> None:
        content.write_file(
            db, database, wiki.alice, ctx, fresh(db, wiki.infra.id), "pendant.md", b"# P\n"
        )
        db.commit()
        push(self, sha)

    monkeypatch.setattr(Repository, "push", push_while_writing)
    git_sync.run_sync(database, ctx, wiki.infra.id)
    monkeypatch.setattr(Repository, "push", push)

    assert wiki.remote.read("content/avant.md") == "# Avant\n"
    assert wiki.remote.read("content/pendant.md") is None
    synced = fresh(db, wiki.infra.id)
    remote_main = git(wiki.remote.bare, "rev-parse", "main")
    assert synced.sync_status == "ok" and synced.remote_head == remote_main
    assert ctx.local_repository(synced).remote_head() == remote_main
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert wiki.remote.read("content/pendant.md") == "# P\n"


def test_a_failed_push_keeps_what_was_integrated(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """A rejected push does not undo the integration already done: the integrated remote head is
    recorded (external commits will not be reported twice)."""
    external = wiki.remote.push({"content/externe.md": "# Externe\n"})
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "locale.md", b"# L\n")
    db.commit()
    hook = wiki.remote.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'deploy key is read-only' >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(GitError):
        git_sync.run_sync(database, ctx, wiki.infra.id)
    failed = fresh(db, wiki.infra.id)
    assert failed.sync_status == "error" and failed.remote_head == external
    assert "pending:Page created: externe.md" in pending_titles(db, ctx, wiki.infra.id)


# --- Same repository, another URL ----------------------------------------------------------------


def test_same_repository_under_a_new_url_keeps_pending_changes_and_decisions(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Switching from https to ssh (here: a trailing slash) empties neither the inbox nor the
    pages left by tidying."""
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "nouvelle.md", b"# N\n")
    db.commit()
    git_sync.run_sync(database, ctx, wiki.infra.id)
    space = fresh(db, wiki.infra.id)
    found = changes.pending(db, ctx, space)
    head = ctx.local_repository(space).head()
    assert head is not None
    left = changes.Change("serveurs/proxmox.md", "modified")
    changes.record(db, space, {left.path: ("left", "À relire par l'équipe", left)}, head)
    db.commit()
    before = sorted(f"{i.status}:{i.title}" for i, _ in found)
    assert "pending:Page created: nouvelle.md" in before
    watermark = space.change_watermark

    spaces.update_space(db, wiki.root, space, repo_url=wiki.remote.url + "/")
    db.commit()
    git_sync.run_sync(database, ctx, wiki.infra.id)

    after = fresh(db, wiki.infra.id)
    assert after.sync_status == "ok" and after.change_watermark == watermark
    titles = pending_titles(db, ctx, wiki.infra.id)
    assert "pending:Page created: nouvelle.md" in titles
    assert "left:Page changed: serveurs/proxmox.md" in titles
    assert db.scalar(select(ChangeMark.reason).where(ChangeMark.space_id == after.id)) == (
        "À relire par l'équipe"
    )


# --- Webhook for external pushes -----------------------------------------------------------------


def test_every_external_commit_is_reported(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beyond 200 external commits, the oldest ones used to be left out of the webhook."""
    work = wiki.remote.work
    git(work, "pull", "-q", "--ff-only", "origin", "main")
    for number in range(230):
        (work / "content" / f"lot-{number:03}.md").write_text(f"# {number}\n")
        git(work, "add", "-A")
        git(work, "commit", "-qm", f"Page {number}")
    git(work, "push", "-q", "origin", "HEAD:main")
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        webhooks, "emit", lambda _s, _sp, event, data: emitted.append((event, data))
    )
    git_sync.run_sync(database, ctx, wiki.infra.id)
    paths = [data["paths"] for event, data in emitted if event == "content.changed"]
    assert len(paths) == 1 and len(paths[0]) == 230 and "lot-000.md" in paths[0]


# --- Locks ---------------------------------------------------------------------------------------


def lock_states(database: Database) -> list[str]:
    with database.engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT a.state FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                    "WHERE l.locktype = 'advisory' AND l.granted AND a.pid <> pg_backend_pid() "
                    "AND a.datname = current_database()"  # other suites run alongside
                )
            ).scalars()
        )


def test_locks_do_not_leave_a_transaction_open(database: Database, wiki: Wiki) -> None:
    """An `idle_in_transaction_session_timeout` must not be able to release a lock silently:
    the connection holding it has no open transaction."""
    with space_lock(database.engine, wiki.infra.id):
        assert lock_states(database) == ["idle"]
    with sync_lock(database.engine, wiki.infra.id):
        assert lock_states(database) == ["idle"]


def test_the_sync_lock_leaves_the_space_lock_free(database: Database, wiki: Wiki) -> None:
    with sync_lock(database.engine, wiki.infra.id):
        with try_space_lock(database.engine, wiki.infra.id, timeout=0) as free:
            assert free  # writes possible during a sync's network phase
        with database.engine.connect() as connection:  # but only one sync
            key = int.from_bytes(wiki.infra.id.bytes[:4], "big", signed=True)
            taken = connection.execute(
                text("SELECT pg_try_advisory_lock(:cls, :key)"),
                {"cls": 0x53594E43, "key": key},
            ).scalar_one()
            assert taken is False


# --- Marker compared before writing --------------------------------------------------------------


def test_a_late_tidy_does_not_move_a_watermark_reset_meanwhile(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tidying run that ends after a sync has reset the marker (force-push) does not put back
    its own marker, computed on the old state."""
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "nouvelle.md", b"# N\n")
    db.commit()
    space = fresh(db, wiki.infra.id)
    old = space.change_watermark
    reset_by_sync = ctx.local_repository(space).rev("HEAD~2")
    assert old and reset_by_sync and reset_by_sync != old

    def pending_then_reset(session: Session, ws: Workspace, target: Space) -> list[Any]:
        with database.session() as other:  # the sync runs in the meantime
            other.execute(
                update(Space).where(Space.id == target.id).values(change_watermark=reset_by_sync)
            )
        return []

    monkeypatch.setattr(changes, "pending", pending_then_reset)
    changes.compact(db, ctx, space)
    db.commit()
    assert fresh(db, wiki.infra.id).change_watermark == reset_by_sync


def test_compact_still_moves_the_watermark(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    space = fresh(db, wiki.infra.id)
    content.write_file(db, database, wiki.alice, ctx, space, "nouvelle.md", b"# N\n")
    db.commit()
    space = fresh(db, wiki.infra.id)
    item = changes.pending(db, ctx, space)[0][1]
    head = ctx.local_repository(space).head()
    assert head is not None
    changes.record(db, space, {item.path: ("done", "vu", item)}, head)
    db.flush()
    changes.compact(db, ctx, space)
    db.commit()
    assert fresh(db, wiki.infra.id).change_watermark == head
    assert db.scalar(select(ChangeMark).where(ChangeMark.space_id == space.id)) is None


def test_a_git_command_line_is_hardened_everywhere(tmp_path: Path) -> None:
    """Short network settings on network commands only; hooks disabled everywhere."""
    network = Repository._command("fetch", network=True)
    local = Repository._command("cat-file", "blob", "x")
    assert "http.lowSpeedLimit=1000" in network and "http.lowSpeedTime=60" in network
    assert "http.lowSpeedLimit=1000" not in local
    assert any(arg.startswith("core.hooksPath=") for arg in local)
    assert subprocess.run(local[:-3] + ["--version"], capture_output=True).returncode == 0
