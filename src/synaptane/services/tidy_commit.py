"""End of a tidy-up run: run lock, merge, patient commit.

The model is the expensive part of a run: once its work is done, it is never run again for a
writing problem.

- A single run at a time per space, even with several workers: a "tidy" advisory lock, held
  for the whole run, separate from the local copy's lock.
- A page modified by a human during the run: three-way merge, file by file (`git merge-file`
  between the base, the head and the agent's version). If it fails, the human version is kept
  for that file only; the rest is committed.
- Local copy busy (sync under way): the lock is awaited longer, the work in memory
  (`Overlay`) is kept; never a new run for that.
"""

import os
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import Engine, text

from synaptane.git.repo import Repository

# Two-integer keys: a key space separate from that of `space_lock` (a bigint key), PostgreSQL
# never mixes them up. 0x74696479: "tidy".
PASS_LOCK_CLASS = 0x74696479

# Wait for the local copy's lock at commit time: longer than the network timeout of a sync,
# which holds it while it talks to the forge (30 min at most).
COMMIT_WAIT = timedelta(minutes=35)

MERGE_TIMEOUT = 60


def _pass_key(space_id: UUID) -> int:
    return int.from_bytes(space_id.bytes[:4], "big", signed=True)


@contextmanager
def pass_lock(engine: Engine, space_id: UUID) -> Iterator[bool]:
    """The space's "tidy" lock, without waiting: False if a run is already under way."""
    params = {"cls": PASS_LOCK_CLASS, "key": _pass_key(space_id)}
    with engine.connect() as connection:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(CAST(:cls AS integer), CAST(:key AS integer))"),
            params,
        ).scalar_one()
        connection.commit()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(CAST(:cls AS integer), CAST(:key AS integer))"),
                    params,
                )
                connection.commit()


@dataclass
class Merged:
    """The agent's writes carried over onto the current head."""

    files: dict[str, bytes | None] = field(default_factory=dict)  # to commit
    merged: set[str] = field(default_factory=set)  # merged with a human change
    conflicts: set[str] = field(default_factory=set)  # human version kept


def merge(repo: Repository, base: str, head: str, changes: Mapping[str, bytes | None]) -> Merged:
    """Carries the agent's writes (made on `base`) over onto `head`, file by file."""
    paths = list(changes)
    before, now = repo.read_many(paths, base), repo.read_many(paths, head)
    result = Merged()
    for path, ours in changes.items():
        old, theirs = before.get(path), now.get(path)
        if theirs == old:  # nobody else touched it
            result.files[path] = ours
        elif ours in (old, theirs):  # the agent changes nothing, or reaches the same result
            continue
        elif (merged := merge_text(old, theirs, ours)) is not None:
            result.files[path] = merged
            result.merged.add(path)
        else:
            result.conflicts.add(path)
    return result


def changed(repo: Repository, base: str, head: str, paths: list[str]) -> set[str]:
    """Files among `paths` whose content differs between two commits (moves included: the
    contents are compared, not a list of names)."""
    before, now = repo.read_many(paths, base), repo.read_many(paths, head)
    return {path for path in paths if before.get(path) != now.get(path)}


def merge_text(base: bytes | None, theirs: bytes | None, ours: bytes | None) -> bytes | None:
    """Three-way merge of a text file; None in case of conflict.

    A deletion against a change, or a binary file, is a conflict: no guessing. Two creations
    at the same path are merged from an empty file."""
    if theirs is None or ours is None:
        return None
    if b"\0" in theirs or b"\0" in ours or (base is not None and b"\0" in base):
        return None
    with tempfile.TemporaryDirectory(prefix="synaptane-merge-") as directory:
        names = []
        for name, data in (("head", theirs), ("base", base or b""), ("agent", ours)):
            target = Path(directory) / name
            target.write_bytes(data)
            names.append(str(target))
        try:
            result = subprocess.run(
                ["git", "merge-file", "-p", *names],
                cwd=directory,
                capture_output=True,
                timeout=MERGE_TIMEOUT,
                stdin=subprocess.DEVNULL,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
    # 0: clean merge; > 0: number of conflicts; < 0: error.
    return result.stdout if result.returncode == 0 else None
