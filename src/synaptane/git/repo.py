"""Local clone of a space repository, driven by the `git` CLI (no shell).

One working copy per space under `data_dir/repos/<space_id>`: search runs ripgrep on it.
Never a force-push; commits carry a real author and Synaptane as committer.
"""

import os
import re
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from synaptane.git.credentials import GitCredentials, git_environment

LOCAL_TIMEOUT = 120
# Overall timeout of a network command. None runs under the space lock: it only bounds the wait
# for a sync, since a forge that stops answering is abandoned well before anyway by
# `NETWORK_OPTIONS` (HTTPS) or `SSH_TIMEOUTS` (SSH).
NETWORK_TIMEOUT = 300
# HTTPS: transfer abandoned after 60 s below 1,000 bytes per second (half-open connection,
# stuck TLS, load balancer in maintenance).
NETWORK_OPTIONS = ("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60")
TRANSIENT_MARKERS = (
    "could not resolve host",
    "connection timed out",
    "connection reset",
    "connection refused",
    "failed to connect",
    "could not connect",
    "network is unreachable",
    "temporary failure in name resolution",
    "operation timed out",
    "the remote end hung up",
    "early eof",
    "502",
    "503",
    "504",
    "rpc failed",
)


# Common hosting errors, explained: (markers in git's output, message). The message starts
# the stored error (`Space.sync_error`); `describe_error` translates it when shown.
EXPLANATIONS = (
    (
        (
            "could not read username",
            "terminal prompts disabled",
            "authentication failed",
            "http basic: access denied",
            "returned error: 401",
            "invalid username or password",
        ),
        "The repository requires authentication: save a valid access token (HTTPS) or a deploy "
        "key (SSH) in “Repository access”.",
    ),
    (
        ("you are not allowed to push", "protected branch", "pre-receive hook declined"),
        "Push refused on a protected branch. GitLab: a token with the Maintainer role, or allow "
        "Developers to push to this branch.",
    ),
    (
        (
            "you are not allowed to download code",
            "returned error: 403",
            "permission to",
            "write access to repository not granted",
            "permission denied (publickey)",
        ),
        "The token (or key) is recognized but lacks the required rights. GitLab: at least the "
        "Reporter role to read, Maintainer to push to `main`; GitHub: “Contents” read and write "
        "permission.",
    ),
    (
        ("repository not found", "returned error: 404", "does not appear to be a git repository"),
        "Repository not found: check the address, or that the token has access to this repository.",
    ),
    (
        ("could not resolve host", "name or service not known"),
        "Host not found: check the repository address.",
    ),
    (
        ("host key verification failed",),
        "Unknown SSH host key: save the deploy key again to read the host's keys again.",
    ),
)
LOCAL_REPOS_DISABLED = "Local repositories (file://) are disabled on this instance."
REMOTE_TOO_FAST = "The remote repository changes too fast: retrying later."
# The same messages as stored in French before the interface texts became English.
_FRENCH = {
    "Le dépôt demande une authentification : enregistrez un jeton d'accès valide (HTTPS) ou une "
    "clé de déploiement (SSH) dans « Accès au dépôt ».": EXPLANATIONS[0][1],
    "Envoi refusé sur une branche protégée. GitLab : jeton avec le rôle Maintainer, ou autoriser "
    "les Developers à pousser sur cette branche.": EXPLANATIONS[1][1],
    "Le jeton (ou la clé) est reconnu mais n'a pas les droits nécessaires. GitLab : rôle Reporter "
    "au moins pour lire, Maintainer pour pousser sur `main` ; GitHub : droit « Contents » en "
    "lecture et écriture.": EXPLANATIONS[2][1],
    "Dépôt introuvable : vérifiez l'adresse, ou que le jeton a accès à ce dépôt.": (
        EXPLANATIONS[3][1]
    ),
    "Hôte introuvable : vérifiez l'adresse du dépôt.": EXPLANATIONS[4][1],
    "Clé d'hôte SSH inconnue : enregistrez à nouveau la clé de déploiement pour relire les clés "
    "de l'hôte.": EXPLANATIONS[5][1],
    "Les dépôts locaux (file://) sont désactivés sur cette instance.": LOCAL_REPOS_DISABLED,
    "Le dépôt distant change trop vite : nouvel essai plus tard.": REMOTE_TOO_FAST,
}


def explain(command: str, output: str) -> str:
    """Readable Git error message: the explanation if known, then the detail."""
    lowered = output.lower()
    for markers, message in EXPLANATIONS:
        if any(marker in lowered for marker in markers):
            return f"{message} (git {command}: {output})"
    return f"git {command}: {output}"


def describe_error(text: str, translate: Callable[..., str]) -> str:
    """A stored Git error (`explain`, or a message of Synaptane) for display: its known message
    translated, Git's own output kept as it is. Also reads errors stored in French."""
    known = [message for _, message in EXPLANATIONS] + [LOCAL_REPOS_DISABLED, REMOTE_TOO_FAST]
    for message in known:
        if text.startswith(message):
            return translate(message) + text[len(message) :]
    for french, message in _FRENCH.items():
        if text.startswith(french):
            return translate(message) + text[len(french) :]
    return text


class GitError(Exception):
    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class PathRefused(GitError):
    """Write path refused (absolute, `..`, `.git`, symbolic link, under a file)."""


class PushRejected(GitError):
    """The remote moved between fetch and push: merge again, then push again."""


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


@dataclass(frozen=True)
class Commit:
    sha: str
    author: Identity
    committer_email: str
    subject: str
    paths: tuple[str, ...]
    date: str = ""  # ISO 8601, filled in by `history`


@dataclass(frozen=True)
class Blob:
    path: str
    sha: str
    size: int


# File lists by (clone, commit, prefix): see Repository.files.
_FILES: OrderedDict[tuple[str, str, str], list["Blob"]] = OrderedDict()
_FILES_LOCK = threading.Lock()
_FILES_MAX = 32


def safe_path(path: str) -> str:
    """Safe relative path in the repository (no absolute path, no `..` or `.git` at any level,
    no control character: a newline would break line-by-line outputs)."""
    pure = PurePosixPath(path)
    if (
        not path
        or pure.is_absolute()
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part in ("", ".", "..") for part in path.split("/"))
        or any(part.lower() == ".git" for part in pure.parts)
    ):
        raise PathRefused(f"path refused: {path!r}")
    return pure.as_posix()


# NUL-separated Git log: paths and messages cannot break the parsing
# (quotes, backslash, tab, newline or control character in a subject).
LOG_FORMAT = "--format=%x00%H%x00%an%x00%ae%x00%ce%x00%cI%x00%s"
# Header of a `cat-file --batch=%(objectname) %(objecttype) %(objectsize)` answer.
BATCH_HEADER = re.compile(rb"([0-9a-f]{40,64}) ([a-z]+) ([0-9]+)")


def parse_log(raw: bytes) -> list[tuple[Commit, list[tuple[str, str, str]]]]:
    """Parses `git log -z --name-status LOG_FORMAT`: commits and (status, path, old path)."""
    tokens = raw.decode("utf-8", "replace").split("\0")
    found: list[tuple[Commit, list[tuple[str, str, str]]]] = []
    i = 0
    while i < len(tokens):
        if tokens[i]:  # each commit starts with a NUL; resync otherwise
            i += 1
            continue
        header = tokens[i + 1 : i + 7]
        if len(header) < 6:
            break
        i += 7
        sha, name, email, committer, date, subject = header
        entries: list[tuple[str, str, str]] = []
        while i < len(tokens) and tokens[i]:
            status = tokens[i].lstrip("\n")[:1]
            if status in ("R", "C") and i + 2 < len(tokens):
                entries.append(("R" if status == "R" else "A", tokens[i + 2], tokens[i + 1]))
                i += 3
            elif i + 1 < len(tokens):
                entries.append((status, tokens[i + 1], ""))
                i += 2
            else:
                break
        commit = Commit(
            sha, Identity(name, email), committer, subject, tuple(e[1] for e in entries), date
        )
        found.append((commit, entries))
    return found


class Repository:
    def __init__(
        self,
        path: Path,
        remote: str,
        branch: str,
        committer: Identity,
        credentials: GitCredentials | None = None,
        network_timeout: int = NETWORK_TIMEOUT,
    ) -> None:
        self.path = path
        self.remote = remote
        self.branch = branch
        self.committer = committer
        self.credentials = credentials
        self.network_timeout = network_timeout

    @property
    def remote_ref(self) -> str:
        return f"refs/remotes/origin/{self.branch}"

    # --- Execution ----------------------------------------------------------------------------

    def _env(self, author: Identity | None = None) -> dict[str, str]:
        author = author or self.committer
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.path.parent),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            # A path is a name, never a pattern: `[ab].md` does not mean `a.md`.
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_AUTHOR_NAME": author.name,
            "GIT_AUTHOR_EMAIL": author.email,
            "GIT_COMMITTER_NAME": self.committer.name,
            "GIT_COMMITTER_EMAIL": self.committer.email,
        }

    @staticmethod
    def _command(*args: str, network: bool = False) -> list[str]:
        """Hardened command line: every `git` command on a clone goes through here."""
        return [
            "git",
            "-c",
            "core.quotePath=false",
            "-c",
            "gc.autoDetach=false",
            # A symbolic link coming from the remote becomes a plain text file: otherwise, a
            # write "into" that link would escape the clone (up to .git/config).
            "-c",
            "core.symlinks=false",
            # No automatic `gc` during a commit (under the space lock): it is run separately,
            # with the lock released (see `gc`).
            "-c",
            "gc.auto=0",
            # Never hooks nor file system monitor: a clone coming from an archive must not be
            # able to execute anything (see also `sanitize`).
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            *(NETWORK_OPTIONS if network else ()),
            *args,
        ]

    def _git(
        self,
        *args: str,
        network: bool = False,
        cwd: Path | None = None,
        author: Identity | None = None,
        check: bool = True,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        env = self._env(author)
        command = self._command(*args, network=network)
        directory = cwd or self.path
        try:
            if network:
                with git_environment(self.credentials, self.remote) as extra:
                    result = subprocess.run(
                        command,
                        cwd=directory,
                        env=env | extra,
                        capture_output=True,
                        timeout=self.network_timeout,
                        stdin=subprocess.DEVNULL,
                    )
            else:
                # Input provided (`cat-file --batch`), else closed: never a waiting prompt.
                result = subprocess.run(
                    command,
                    cwd=directory,
                    env=env,
                    capture_output=True,
                    timeout=LOCAL_TIMEOUT,
                    input=stdin if stdin is not None else b"",
                )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]}: timed out", transient=True) from None
        if check and result.returncode != 0:
            message = (result.stderr or result.stdout).decode("utf-8", "replace").strip()[-2000:]
            transient = network and any(m in message.lower() for m in TRANSIENT_MARKERS)
            raise GitError(explain(args[0], message), transient=transient)
        return result

    def _out(self, *args: str) -> str:
        return self._git(*args).stdout.decode("utf-8", "replace").strip()

    # --- Local clone --------------------------------------------------------------------------

    def exists(self) -> bool:
        return (self.path / ".git").is_dir()

    def remote_has_branch(self) -> bool:
        heads = self._git(
            "ls-remote",
            "--heads",
            "--",
            self.remote,
            f"refs/heads/{self.branch}",
            network=True,
            cwd=self.path.parent,
        )
        return bool(heads.stdout.strip())

    def clone(self) -> None:
        """Clones the branch; if it does not exist yet (empty repository), sets up a local one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            shutil.rmtree(self.path)
        if self.remote_has_branch():
            self._git(
                "clone",
                "--no-tags",
                "--single-branch",
                "--branch",
                self.branch,
                "--",
                self.remote,
                str(self.path),
                network=True,
                cwd=self.path.parent,
            )
        else:
            self._git("init", "--quiet", "-b", self.branch, str(self.path), cwd=self.path.parent)
            self._git("remote", "add", "origin", self.remote)

    def matches_configuration(self) -> bool:
        """False if the URL or the branch changed since the clone."""
        url = self._git("remote", "get-url", "origin", check=False).stdout.decode().strip()
        branch = self._git("symbolic-ref", "--short", "HEAD", check=False).stdout.decode().strip()
        return url == self.remote and branch == self.branch

    def remove_symlinks(self) -> list[str]:
        """Replaces the symbolic links of an old clone with regular files."""
        found = [
            p
            for p in self.path.rglob("*")
            if p.is_symlink() and ".git" not in p.relative_to(self.path).parts
        ]
        for link in found:
            link.unlink()
        paths = [p.relative_to(self.path).as_posix() for p in found]
        if paths:
            self._git("checkout", "--", *paths, check=False)
        return paths

    def heal(self, stale_after: float = 120) -> list[str]:
        """Repairs a clone left half-done by a killed process (to call under the space lock):
        orphan index lock, interrupted merge, uncommitted files.
        Returns what was repaired, for the log."""
        if not self.exists():
            return []
        repaired = []
        lock = self.path / ".git" / "index.lock"
        if lock.exists() and time.time() - lock.stat().st_mtime > stale_after:
            lock.unlink(missing_ok=True)
            repaired.append("orphan index lock removed")
        if (self.path / ".git" / "MERGE_HEAD").exists():
            self._git("merge", "--abort", check=False)
            repaired.append("interrupted merge aborted")
        if self.head() is not None and self._out("status", "--porcelain", "--untracked-files=all"):
            self._git("reset", "--quiet", "--hard", "HEAD", check=False)
            self._git("clean", "-fdq", check=False)
            repaired.append("uncommitted files removed")
        return repaired

    def discard(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def fetch(self) -> bool:
        """Updates `origin/<branch>`. False if the branch does not exist on the remote."""
        try:
            self._git(
                "fetch",
                "--no-tags",
                "origin",
                f"+refs/heads/{self.branch}:{self.remote_ref}",
                network=True,
            )
        except GitError as error:
            if "couldn't find remote ref" in str(error).lower():
                return False  # empty repository or branch not created yet
            raise
        return True

    def rev(self, ref: str) -> str | None:
        result = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return result.stdout.decode().strip() or None

    def head(self) -> str | None:
        return self.rev("HEAD")

    def remote_head(self) -> str | None:
        return self.rev(self.remote_ref)

    def ahead_behind(self) -> tuple[int, int]:
        """Local commits not pushed, remote commits not merged."""
        local, remote = self.head(), self.remote_head()
        if local is None:
            return 0, 1 if remote else 0
        if remote is None:
            return int(self._out("rev-list", "--count", "HEAD")), 0
        ahead, behind = self._out(
            "rev-list", "--left-right", "--count", f"HEAD...{self.remote_ref}"
        ).split()
        return int(ahead), int(behind)

    # --- Integration --------------------------------------------------------------------------

    def fast_forward(self) -> None:
        if self.head() is None:
            self._git("reset", "--hard", "--quiet", self.remote_ref)
        else:
            self._git("merge", "--ff-only", "--quiet", self.remote_ref)

    def _unmerged(self) -> list[str]:
        out = self._git("diff", "--name-only", "--diff-filter=U", "-z").stdout
        return sorted({p.decode("utf-8", "replace") for p in out.split(b"\0") if p})

    def merge_remote(self, message: str) -> list[str]:
        """Merges the remote. Returns the conflicting files; the merge is then aborted.

        Histories without a common ancestor (local skeleton, repository created separately)
        merge like the others: a file present on both sides becomes a conflict."""
        result = self._git(
            "merge",
            "--no-ff",
            "--no-edit",
            "--allow-unrelated-histories",
            "-m",
            message,
            self.remote_ref,
            check=False,
        )
        if result.returncode == 0:
            return []
        conflicts = self._unmerged()
        self._git("merge", "--abort", check=False)
        if not conflicts:
            raise GitError("git merge: " + result.stderr.decode("utf-8", "replace")[-2000:])
        return conflicts

    def merge_with_choices(
        self, choices: Mapping[str, str], message: str, author: Identity | None = None
    ) -> list[str]:
        """Merges the remote, settling each conflict: `local` or `remote`. `author`: the
        person who decided (Synaptane remains the committer).

        Returns the conflicting files left without a choice (the merge is then aborted).
        """
        self._git(
            "merge",
            "--no-ff",
            "--no-commit",
            "--allow-unrelated-histories",  # like merge_remote, which produced the conflict
            self.remote_ref,
            check=False,
        )
        if not (self.path / ".git" / "MERGE_HEAD").exists():
            return []  # nothing to merge (already resolved, or duplicate request)
        conflicts = self._unmerged()
        missing = [p for p in conflicts if choices.get(p) not in ("local", "remote")]
        if missing:
            self._git("merge", "--abort", check=False)
            return missing
        for path in conflicts:
            stage = "2" if choices[path] == "local" else "3"  # 2 = ours, 3 = theirs
            present = self._git("cat-file", "-e", f":{stage}:{path}", check=False).returncode == 0
            if present:
                side = "--ours" if stage == "2" else "--theirs"
                self._git("checkout", side, "--", path)
                self._git("add", "--", path)
            else:
                self._git("rm", "--quiet", "--", path)
        self._git("commit", "--quiet", "--no-verify", "-m", message, author=author)
        return []

    # --- Recovery of local commits -----------------------------------------------------------

    def merge_base(self, one: str, other: str) -> str | None:
        result = self._git("merge-base", one, other, check=False)
        return result.stdout.decode().strip() or None

    def local_changes(
        self, base: str | None, prefixes: tuple[str, ...]
    ) -> dict[str, tuple[bytes | None, bytes | None]]:
        """Files under `prefixes` changed from `base` (None: nothing) to HEAD:
        path → (base version, local version), None for a missing file."""
        if self.head() is None:
            return {}
        if base is None:
            out = self._git("ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *prefixes)
            paths = [p.decode("utf-8", "replace") for p in out.stdout.split(b"\0") if p]
        else:
            out = self._git(
                "diff", "--no-renames", "--name-only", "-z", base, "HEAD", "--", *prefixes
            )
            paths = [p.decode("utf-8", "replace") for p in out.stdout.split(b"\0") if p]
        before = self.read_many(paths, base) if base else {}
        after = self.read_many(paths, "HEAD")
        return {p: (before.get(p), after.get(p)) for p in paths}

    def bundle(self, destination: Path) -> Path | None:
        """Saves the local branch to a Git bundle (restorable with `git clone`)."""
        if self.head() is None:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._git("bundle", "create", str(destination), self.branch)
        return destination

    def reset_to_remote(self) -> None:
        """Drops the local branch for the remote state (after saving it with `bundle`)."""
        self._git("reset", "--hard", "--quiet", self.remote_ref)

    # --- Writing ------------------------------------------------------------------------------

    def commit_changes(
        self, changes: Mapping[str, bytes | None], author: Identity, message: str
    ) -> str | None:
        """Writes (or deletes, value None) files then commits. None if nothing changes.

        Deleting a missing path does nothing. A file covered by the repository `.gitignore`
        is versioned anyway: Synaptane wrote it, it must be in Git."""
        paths = [safe_path(p) for p in changes]
        for path, content in zip(paths, changes.values(), strict=True):
            if content is not None:
                self._check_destination(path)
        written = [p for p, c in zip(paths, changes.values(), strict=True) if c is not None]
        removed = [p for p, c in zip(paths, changes.values(), strict=True) if c is None]
        try:
            for path, content in zip(paths, changes.values(), strict=True):
                target = self.path / path
                if content is None:
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            if removed:
                self._git("rm", "-q", "--cached", "--ignore-unmatch", "--", *removed)
            if written:
                self._git("add", "-f", "--", *written)
            if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return None
            self._git("commit", "--quiet", "--no-verify", "-m", message, author=author)
        except BaseException:
            # No half-written file in the clone: it would block the sync.
            self._git("reset", "--quiet", "--hard", "HEAD", check=False)
            if written:
                self._git("clean", "-fdqx", "--", *written, check=False)
            raise
        return self.head()

    def _check_destination(self, path: str) -> None:
        """Refuses to write through a symbolic link or under a file."""
        current = self.path
        parts = PurePosixPath(path).parts
        for index, part in enumerate(parts):
            current = current / part
            if current.is_symlink():
                raise PathRefused(f"path refused (symbolic link): {path!r}")
            if index < len(parts) - 1 and current.exists() and not current.is_dir():
                raise PathRefused(f"path refused ({'/'.join(parts[: index + 1])} is a file)")
            if index == len(parts) - 1 and current.is_dir():
                raise PathRefused(f"path refused ({path} is a directory)")

    def push(self, sha: str) -> None:
        """Pushes the commit `sha` (not HEAD, which can move during the push) to the branch.

        Without the space lock: the push only reads objects, never the working copy nor the
        index. `pushed` then records the new remote head."""
        result = self._git(
            "push",
            "--porcelain",
            "origin",
            f"{sha}:refs/heads/{self.branch}",
            network=True,
            check=False,
        )
        if result.returncode != 0:
            text = (result.stdout + result.stderr).decode("utf-8", "replace")
            if "[rejected]" in text or "non-fast-forward" in text or "fetch first" in text:
                raise PushRejected("the remote moved during the push")
            transient = any(m in text.lower() for m in TRANSIENT_MARKERS)
            raise GitError(explain("push", text.strip()[-2000:]), transient=transient)

    def pushed(self, sha: str) -> None:
        """`origin/<branch>` after a successful push of `sha` (under the space lock)."""
        self._git("update-ref", self.remote_ref, sha)

    # --- Reading ------------------------------------------------------------------------------

    def commits_between(self, old: str | None, new: str) -> list[Commit]:
        """Commits from `old` (excluded) to `new`, oldest first, with their files
        (a deletion and a creation for a rename; nothing for a merge).

        A single `git log`, with no count limit: a push of 500 commits is reported in full
        (webhook `content.changed`)."""
        span = f"{old}..{new}" if old else new
        out = self._git(
            "log", "--reverse", "--name-status", "--no-renames", "-z", LOG_FORMAT, span
        ).stdout
        return [commit for commit, _ in parse_log(out)]

    def changes_since(
        self, since: str, prefix: str, until: str = "HEAD"
    ) -> list[tuple[Commit, list[tuple[str, str, str]]]]:
        """Commits from `since` (excluded) to `until` touching `prefix`, oldest first, each
        with (status A/M/D/R, path, old path if renamed). Without merges.

        No count limit: the marker only moves once everything is examined, a limit would
        lose the changes beyond it."""
        out = self._git(
            "log",
            "--no-merges",
            "--reverse",
            "-M",
            "--name-status",
            "-z",
            LOG_FORMAT,
            f"{since}..{until}",
            "--",
            prefix,
        ).stdout
        return [(commit, entries) for commit, entries in parse_log(out)]

    def real_changes(self, since: str, until: str, prefix: str) -> set[str]:
        """Files under `prefix` that change from `since` to `until` other than by whitespace
        or blank lines (a single call, whatever their number)."""
        out = self._git(
            "diff",
            "--numstat",
            "-z",
            "--no-renames",
            "-w",
            "--ignore-blank-lines",
            since,
            until,
            "--",
            prefix,
        ).stdout.decode("utf-8", "replace")
        return {entry.split("\t", 2)[2] for entry in out.split("\0") if entry.count("\t") >= 2}

    def reachable(self, since: str, until: str) -> set[str]:
        """Commits reachable from `until` but not from `since`."""
        return set(self._out("rev-list", f"{since}..{until}").split())

    def ancestry(self, since: str, until: str = "HEAD") -> list[tuple[str, tuple[str, ...]]]:
        """Commits from `since` (excluded) to `until`, each with its parents, in topological
        order (a commit before its parents): a single call for all the "is this commit an
        ancestor of that one?" questions over this range."""
        lines = self._out("rev-list", "--topo-order", "--parents", f"{since}..{until}")
        return [(shas[0], tuple(shas[1:])) for shas in map(str.split, lines.splitlines()) if shas]

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self._git("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def only_whitespace(self, old: str, new: str, path: str) -> bool:
        """True if `path` only changed between two commits by whitespace or blank lines."""
        result = self._git(
            "diff", "--quiet", "-w", "--ignore-blank-lines", old, new, "--", path, check=False
        )
        return result.returncode == 0

    def files(self, prefix: str, rev: str = "HEAD") -> list["Blob"]:
        """Tracked files under `prefix` at commit `rev`, with their hash and size.

        Cached per commit: a commit's list never changes, and a new commit changes the key
        (nothing to invalidate).
        """
        commit = self.rev(rev)
        if commit is None:
            return []
        key = (str(self.path), commit, prefix)
        with _FILES_LOCK:
            cached = _FILES.get(key)
            if cached is not None:
                _FILES.move_to_end(key)
                return list(cached)
        blobs = self._list_files(prefix, commit)
        with _FILES_LOCK:
            _FILES[key] = blobs
            while len(_FILES) > _FILES_MAX:
                _FILES.popitem(last=False)
        return list(blobs)

    def _list_files(self, prefix: str, rev: str) -> list["Blob"]:
        out = self._git("ls-tree", "-r", "-z", "--long", rev, "--", prefix).stdout
        blobs = []
        for record in filter(None, out.split(b"\0")):
            meta, _, name = record.partition(b"\t")
            _mode, kind, sha, size = meta.split()
            if kind == b"blob":
                blobs.append(Blob(name.decode("utf-8", "replace"), sha.decode(), int(size)))
        return blobs

    def blob(self, path: str, rev: str = "HEAD") -> bytes | None:
        result = self._git("cat-file", "blob", f"{rev}:{safe_path(path)}", check=False)
        return result.stdout if result.returncode == 0 else None

    def exists_at(self, path: str, rev: str = "HEAD") -> bool:
        """True if `path` (file or directory) exists at commit `rev`."""
        return self._git("cat-file", "-e", f"{rev}:{safe_path(path)}", check=False).returncode == 0

    def blob_entry(self, path: str, rev: str = "HEAD") -> tuple[str, int] | None:
        """(hash, size) of a file, without reading its content."""
        out = self._git("ls-tree", "-l", "-z", rev, "--", safe_path(path), check=False).stdout
        meta, _, _name = out.rstrip(b"\0").partition(b"\t")
        parts = meta.split()
        if len(parts) != 4 or parts[1] != b"blob":
            return None
        return parts[2].decode(), int(parts[3])

    def read_many(self, paths: list[str], rev: str = "HEAD") -> dict[str, bytes]:
        """Content of several files in a single call (`git cat-file --batch`).

        A missing file (or a directory) is omitted. The header of each answer is
        "hash type size": it cannot be confused with the "<request> missing" line of a
        missing object, which repeats the path as is (so a path "x blob 12" breaks
        nothing). A truncated output is a Git error."""
        wanted = [p for p in paths if "\n" not in p]
        if not wanted:
            return {}
        request = "".join(f"{rev}:{safe_path(p)}\n" for p in wanted).encode()
        out = self._git(
            "cat-file", "--batch=%(objectname) %(objecttype) %(objectsize)", stdin=request
        ).stdout
        found: dict[str, bytes] = {}
        position = 0
        for path in wanted:
            end = out.find(b"\n", position)
            if end < 0:
                raise GitError("git cat-file: truncated answer")
            header = BATCH_HEADER.fullmatch(out[position:end])
            position = end + 1
            if header is None:
                continue  # "<request> missing" or "ambiguous": nothing to read
            size = int(header[3])
            if position + size > len(out):
                raise GitError("git cat-file: truncated answer")
            if header[2] == b"blob":
                found[path] = out[position : position + size]
            position += size + 1  # content (even of a directory), then newline
        return found

    def read_objects(self, shas: list[str]) -> dict[str, bytes]:
        """Content of several files given by their hash, in a single call: much faster than
        `read_many` on thousands of files (no path to resolve)."""
        wanted = [s for s in dict.fromkeys(shas) if re.fullmatch(r"[0-9a-f]{40,64}", s)]
        if not wanted:
            return {}
        out = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=self.path,
            env=self._env(),
            input="".join(f"{sha}\n" for sha in wanted).encode(),
            capture_output=True,
            timeout=LOCAL_TIMEOUT,
        ).stdout
        found: dict[str, bytes] = {}
        position = 0
        for sha in wanted:
            end = out.index(b"\n", position)
            header = out[position:end].split()
            position = end + 1
            if len(header) == 3 and header[1] == b"blob":
                size = int(header[2])
                found[sha] = out[position : position + size]
                position += size + 1
        return found

    def compare(self, path: str, other: str) -> str:
        """Diff of a file between HEAD and another revision (e.g. the remote branch)."""
        return self._out("diff", "--no-color", "HEAD", other, "--", safe_path(path))

    def object_data(self, sha: str) -> bytes | None:
        """Content of a file given by its hash (see `blob_entry`)."""
        result = self._git("cat-file", "blob", sha, check=False)
        return result.stdout if result.returncode == 0 else None

    def object_stream(self, sha: str, chunk: int = 65536) -> Generator[bytes]:
        """Content of a file, chunk by chunk (large files served without loading everything).

        Same hardened options as the other commands. An incomplete read (missing object,
        interrupted `git`) raises `GitError` instead of passing for a short file."""
        process = subprocess.Popen(
            self._command("cat-file", "blob", sha),
            cwd=self.path,
            env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        finished = False
        try:
            while data := process.stdout.read(chunk):
                yield data
            finished = True
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()
        if finished and process.returncode != 0:
            raise GitError(f"git cat-file: read of {sha[:12]} interrupted")

    def blob_sha(self, path: str, rev: str = "HEAD") -> str | None:
        """Hash of the file `path`, or None (missing, or it is a directory)."""
        spec = f"{rev}:{safe_path(path)}"
        kind = self._git("cat-file", "-t", spec, check=False).stdout.decode().strip()
        if kind != "blob":
            return None
        result = self._git("rev-parse", "--verify", "--quiet", spec, check=False)
        return result.stdout.decode().strip() or None

    def history(self, path: str, limit: int = 50) -> list[Commit]:
        """Commits that touched `path`, most recent first (renames followed)."""
        out = self._out(
            "log",
            f"--max-count={limit}",
            "--follow",
            "--format=%H%x1f%an%x1f%ae%x1f%ce%x1f%cI%x1f%s",
            "--",
            safe_path(path),
        )
        commits = []
        for line in filter(None, out.split("\n")):
            sha, name, email, committer, date, subject = line.split("\x1f", 5)
            commits.append(Commit(sha, Identity(name, email), committer, subject, (path,), date))
        return commits

    def last_changes(
        self, prefix: str, rev: str = "HEAD", since: str | None = None
    ) -> dict[str, str]:
        """Date (ISO 8601) of the last commit that touched each file under `prefix`, in a
        single walk of the history. `since`: only the commits after it, to update an already
        computed result. A renamed file dates from its rename."""
        span = f"{since}..{rev}" if since else rev
        out = self._git(
            "log", "-z", "--name-only", "--no-renames", "--format=%x01%cI", span, "--", prefix
        ).stdout
        dates: dict[str, str] = {}
        date = ""
        for token in out.decode("utf-8", "replace").split("\0"):
            if token.startswith("\x01"):
                date = token[1:]
            elif path := token.removeprefix("\n"):
                dates.setdefault(path, date)  # most recent first
        return dates

    def recent(
        self,
        prefix: str | tuple[str, ...],
        limit: int = 20,
        skip: int = 0,
        *,
        since: str | None = None,
    ) -> list[Commit]:
        """Latest commits touching `prefix` (one or several directories), with the affected
        files under these directories; `skip` for the next page, `since` (ISO 8601 date,
        already validated) to stop at a date."""
        prefixes = (prefix,) if isinstance(prefix, str) else prefix
        out = self._git(
            "log",
            f"--max-count={limit}",
            f"--skip={max(skip, 0)}",
            *([f"--since={since}"] if since else []),
            "--name-status",
            "--no-renames",
            "-z",
            LOG_FORMAT,
            "--",
            *prefixes,
        ).stdout
        return [
            replace(commit, paths=tuple(p for p in commit.paths if p.startswith(prefixes)))
            for commit, _ in parse_log(out)
        ]

    def compare_range(self, old: str, new: str, path: str) -> str:
        """Diff of a file between two commits (deletion and creation included)."""
        return self._out("diff", "--no-color", old, new, "--", safe_path(path))

    def diff(self, sha: str, path: str) -> str:
        """Change introduced by the commit `sha` on `path` (unified format)."""
        if not all(c in "0123456789abcdef" for c in sha) or not 7 <= len(sha) <= 64:
            raise GitError("invalid commit id")
        out = self._git(
            "show",
            "--format=",
            "--patch",
            "--no-color",
            "--find-renames",
            sha,
            "--",
            safe_path(path),
        ).stdout
        return out.decode("utf-8", "replace")

    def changed_paths(self, old: str, new: str) -> set[str]:
        """Files changed between two commits."""
        out = self._git("diff", "--name-only", "-z", old, new).stdout
        return {p.decode("utf-8", "replace") for p in out.split(b"\0") if p}

    def revert(self, sha: str, author: Identity, message: str) -> str | None:
        """Reverts a commit with a new commit. None if the revert conflicts."""
        result = self._git("revert", "--no-commit", sha, check=False)
        if result.returncode != 0:
            self._git("revert", "--abort", check=False)
            self._git("reset", "--hard", "--quiet", "HEAD", check=False)
            return None
        self._git("commit", "--quiet", "--no-verify", "--allow-empty", "-m", message, author=author)
        return self.head()

    def gc(self) -> None:
        self._git("-c", "gc.auto=6700", "gc", "--auto", "--quiet", check=False)


# Keys kept in the `.git/config` of a restored clone: everything else (filters, external diff,
# `include.path`, `core.sshCommand`, `url.*.insteadOf`…) could execute a command or divert a
# push.
SAFE_CONFIG = re.compile(
    r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase)"
    r"|extensions\.objectformat"
    r"|remote\.origin\.(url|fetch)"
    r"|branch\.[^.]+\.(remote|merge))$"
)


def sanitize(path: Path) -> list[str]:
    """Reduces the configuration of a clone to the safe keys and deletes its hooks (clone
    coming from a backup archive). Returns the removed keys."""

    git_dir = path / ".git"
    config = git_dir / "config"
    if not config.is_file() or config.is_symlink():
        return []
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1"}
    listed = subprocess.run(
        ["git", "config", "--file", str(config), "--no-includes", "--list", "-z"],
        env=env | {"GIT_CONFIG_GLOBAL": os.devnull},
        capture_output=True,
        timeout=LOCAL_TIMEOUT,
        check=True,
    ).stdout.decode(errors="replace")
    kept: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    for entry in filter(None, listed.split("\0")):
        key, _, value = entry.partition("\n")
        (kept if SAFE_CONFIG.match(key.lower()) else dropped).append((key, value))
    fresh = git_dir / "config.new"
    fresh.unlink(missing_ok=True)
    fresh.touch()
    for key, value in kept:
        subprocess.run(
            ["git", "config", "--file", str(fresh), "--add", key, value],
            env=env | {"GIT_CONFIG_GLOBAL": os.devnull},
            capture_output=True,
            timeout=LOCAL_TIMEOUT,
            check=True,
        )
    fresh.replace(config)
    hooks = git_dir / "hooks"
    if hooks.is_symlink() or hooks.is_file():
        hooks.unlink()
    elif hooks.is_dir():
        shutil.rmtree(hooks)
    return [key for key, _ in dropped]
