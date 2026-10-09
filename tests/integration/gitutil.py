"""Test Git repositories: a bare remote and an external workstation."""

import subprocess
from pathlib import Path

from synaptane.config import Settings
from synaptane.core.security import SecretBox
from synaptane.git.repo import Identity
from synaptane.services.workspace import Workspace

COMMITTER = Identity("Synaptane", "synaptane@localhost")
ALICE = Identity("Alice Externe", "alice@example.com")


def git(cwd: Path, *args: str, author: Identity = ALICE) -> str:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": author.name,
        "GIT_AUTHOR_EMAIL": author.email,
        "GIT_COMMITTER_NAME": author.name,
        "GIT_COMMITTER_EMAIL": author.email,
    }
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class Remote:
    """A bare "remote" repository and an external workstation that pushes to it."""

    def __init__(self, root: Path) -> None:
        self.bare = root / "remote.git"
        self.work = root / "external"
        root.mkdir(parents=True, exist_ok=True)
        git(root, "init", "--quiet", "--bare", "-b", "main", str(self.bare))
        self.url = f"file://{self.bare}"

    def push(self, files: dict[str, str | None], message: str = "Modification externe") -> str:
        if not self.work.exists():
            git(self.bare.parent, "clone", "--quiet", self.url, str(self.work))
            git(self.work, "checkout", "--quiet", "-B", "main")
        else:
            git(self.work, "pull", "--quiet", "--ff-only", "origin", "main")
        for path, content in files.items():
            target = self.work / path
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        git(self.work, "add", "-A")
        git(self.work, "commit", "--quiet", "-m", message)
        git(self.work, "push", "--quiet", "origin", "HEAD:main")
        return git(self.work, "rev-parse", "HEAD")

    def read(self, path: str) -> str | None:
        result = subprocess.run(
            ["git", "show", f"main:{path}"], cwd=self.bare, capture_output=True, text=True
        )
        return result.stdout if result.returncode == 0 else None

    def files(self, prefix: str) -> list[str]:
        out = git(self.bare, "ls-tree", "-r", "--name-only", "main", "--", prefix)
        return out.split("\n") if out else []

    def last_commit(self) -> str:
        return git(self.bare, "log", "-1", "--format=%an <%ae> / %cn <%ce> / %s", "main")


def workspace_for(settings: Settings) -> Workspace:
    box = SecretBox(settings.key_file.read_bytes())
    return Workspace(
        repos_dir=settings.repos_dir,
        extract_dir=settings.data_dir / "extract",
        committer=COMMITTER,
        box=box,
        allow_local_repos=True,
    )
