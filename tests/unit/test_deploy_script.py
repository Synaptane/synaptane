"""deploy/host/deploy.sh, run against a fake `docker`: update, rollback, dry run.

The real Docker is not needed: the fake one keeps a server's state (running image, database
revision, tags) in a folder, and records each call. The Git repositories, however, are real:
`git fetch` and `git checkout` are the script's own.
"""

import io
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
DEPLOY = ROOT / "deploy" / "host" / "deploy.sh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "git", "flock", "tar")),
    reason="bash, git, flock and tar required",
)

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker: state in $FAKE_DOCKER (running, revision, tags/), calls in calls.log.
set -u
f="$FAKE_DOCKER"
echo "docker $*${GIT_SHA:+ [GIT_SHA=$GIT_SHA]}" >> "$f/calls.log"
tag_of() {
  if [ -f "$f/tags/$1" ]; then cat "$f/tags/$1"; else echo "${1#sha256:img-}"; fi
}
if [ "$1" = compose ]; then
  shift
  case "$*" in
    "ps -q app") echo cid-app ;;
    "ps -q worker") echo cid-worker ;;
    "config --images app") echo "${FAKE_IMAGE:-synaptane:latest}" ;;
    "port app 8000") echo 127.0.0.1:8000 ;;
    "exec -T app synaptane --version") r=$(cat "$f/running"); echo "synaptane 1.0 (${r:0:7})" ;;
    "exec -T app synaptane doctor") echo "[ok] Configuration" ;;
    "exec -T app synaptane backup --output -")
      cp "$f/revision" "$f/revision.saved"; cat "$f/archive.tar.gz" ;;
    "exec -T postgres psql"*) cat "$f/revision" ;;
    "build --pull") echo "$GIT_SHA" > "$f/tags/synaptane:latest" ;;
    "up -d --no-build")
      r=$(cat "$f/tags/synaptane:latest"); echo "$r" > "$f/running"
      if [ "$r" = "${FAKE_MIGRATES:-}" ]; then echo 0099 > "$f/revision"; fi ;;
    "stop app worker") ;;
    "run --rm --no-deps -T app synaptane restore --force -")
      cat > /dev/null; cp "$f/revision.saved" "$f/revision" ;;
    *) echo "unexpected compose: $*" >&2; exit 1 ;;
  esac
  exit 0
fi
case "$1" in
  inspect)
    case "$3" in
      *Config.Image*) echo synaptane:latest ;;
      *Health*) echo healthy ;;
      *) echo "sha256:img-$(cat "$f/running")" ;;
    esac ;;
  tag) tag_of "$2" > "$f/tags/$3" ;;
  image) ;;
  *) echo "unexpected docker: $*" >&2; exit 1 ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
# Fake curl: /readyz at 503 for the FAKE_BROKEN commit, GitHub API answer forced.
case "$*" in
  *api.github.com*) echo "${FAKE_CI_ANSWER:-}"; exit 0 ;;
esac
if [ "$(cat "$FAKE_DOCKER/running")" = "${FAKE_BROKEN:-}" ]; then echo 503; else echo 200; fi
"""


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


@dataclass
class Server:
    tmp: Path
    repo: Path
    fake: Path
    old: str  # running commit
    new: str  # commit pushed to origin/main

    def deploy(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        bin_dir = self.tmp / "bin"
        full_env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "SYNAPTANE_DIR": str(self.repo),
            "STATE_DIR": str(self.tmp / "state"),
            "BACKUP_DIR": str(self.tmp / "backups"),
            "FAKE_DOCKER": str(self.fake),
            "DEPLOY_TIMEOUT": "1",
            "CHECK_INTERVAL": "0.1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            **env,
        }
        return subprocess.run(
            ["bash", str(DEPLOY), *args],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> str:
        log = self.fake / "calls.log"
        return log.read_text() if log.exists() else ""

    def state(self, name: str) -> str:
        return (self.fake / name).read_text().strip()

    def journal(self) -> list[str]:
        log = self.tmp / "state" / "deploy.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def server(tmp_path: Path) -> Server:
    origin, work, repo = tmp_path / "origin.git", tmp_path / "work", tmp_path / "synaptane"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "a.txt").write_text("1\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "un")
    git(work, "push", "-q", str(origin), "main")
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    (repo / ".env").write_text("PUBLIC_URL=https://brain.example.com\n")
    (work / "a.txt").write_text("2\n")
    git(work, "commit", "-q", "-am", "deux")
    git(work, "tag", "v1.0.0-rc.1")  # without a reference, deploy.sh targets the latest release
    git(work, "push", "-q", "--tags", str(origin), "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)
    fake = tmp_path / "fake"
    (fake / "tags").mkdir(parents=True)
    old, new = git(repo, "rev-parse", "HEAD"), git(work, "rev-parse", "HEAD")
    (fake / "running").write_text(old)
    (fake / "revision").write_text("0015")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for name in ("manifest.json", "database.dump"):
            entry = tarfile.TarInfo(name)
            entry.size = 2
            tar.addfile(entry, io.BytesIO(b"{}"))
    (fake / "archive.tar.gz").write_bytes(archive.getvalue())
    return Server(tmp_path, repo, fake, old, new)


def test_deploy_builds_the_commit_and_logs_it(server: Server) -> None:
    done = server.deploy()
    assert done.returncode == 0, done.stdout + done.stderr
    short = server.new[:7]
    assert git(server.repo, "rev-parse", "HEAD") == server.new
    assert f"docker compose build --pull [GIT_SHA={server.new}]" in server.calls()
    assert server.state("running") == server.new
    assert server.state(f"tags/synaptane:{short}") == server.new
    assert server.state("tags/synaptane:precedent") == server.old  # kept for a rollback
    assert server.journal()[-1].endswith(f" {short} ok")
    backups = list((server.tmp / "backups").glob(f"synaptane-*-avant-{short}.tar.gz"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    assert "restore" not in server.calls()


def test_failed_deploy_with_a_migration_restores_the_backup(server: Server) -> None:
    done = server.deploy(FAKE_BROKEN=server.new, FAKE_MIGRATES=server.new)
    assert done.returncode == 1
    assert "/readyz answers 503" in done.stderr
    assert git(server.repo, "rev-parse", "HEAD") == server.old
    assert server.state("running") == server.old
    assert "synaptane restore --force -" in server.calls()
    assert server.state("revision") == "0015"  # database from before the migration
    assert server.journal()[-1].endswith(f"rolled back to {server.old[:7]}: ok")


def test_failed_deploy_without_migration_keeps_the_base(server: Server) -> None:
    done = server.deploy(FAKE_BROKEN=server.new)
    assert done.returncode == 1
    assert server.state("running") == server.old
    assert "restore" not in server.calls()  # nothing to restore: the database did not move
    assert "Database unchanged" in done.stdout


def test_dry_run_changes_nothing(server: Server) -> None:
    done = server.deploy(DRY_RUN="1")
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"[dry run] GIT_SHA={server.new} docker compose build --pull" in done.stdout
    assert git(server.repo, "rev-parse", "HEAD") == server.old
    for mutation in ("build", " up ", " tag ", "backup"):
        assert mutation not in server.calls()
    assert server.journal() == []


def test_deploy_refuses_to_build_under_the_published_image_name(server: Server) -> None:
    """compose.yaml pulls the published image by default: without SYNAPTANE_IMAGE in .env, the
    local build would take its name, and the rollback would no longer find the image."""
    refused = server.deploy(FAKE_IMAGE="ghcr.io/synaptane/synaptane:latest")
    assert refused.returncode == 2 and "SYNAPTANE_IMAGE=synaptane:latest" in refused.stderr
    assert git(server.repo, "rev-parse", "HEAD") == server.old  # back to the running commit
    assert "build" not in server.calls() and server.state("running") == server.old
    assert "refused: image" in server.journal()[-1]


def test_deploy_targets_the_latest_release_by_default(server: Server) -> None:
    """A final release comes before its pre-releases; origin/main is still possible."""
    origin = server.tmp / "origin.git"
    git(server.tmp, "--git-dir", str(origin), "tag", "v1.0.0", server.old)
    git(server.tmp, "--git-dir", str(origin), "tag", "v9.9-archive", server.new)  # ignored
    latest = server.deploy(DRY_RUN="1")
    assert f"target: {server.old[:7]} (v1.0.0)" in latest.stdout, latest.stdout + latest.stderr
    main = server.deploy("origin/main", DRY_RUN="1")
    assert f"target: {server.new[:7]} (origin/main)" in main.stdout


def test_check_ci_refuses_a_commit_without_green_ci(server: Server) -> None:
    refused = server.deploy("--check-ci", GITHUB_TOKEN="x", FAKE_CI_ANSWER='{"total_count": 0}')
    assert refused.returncode == 2 and "No green CI" in refused.stderr
    assert "build" not in server.calls()
    public = server.deploy("--check-ci", DRY_RUN="1", FAKE_CI_ANSWER='{"total_count": 1}')
    assert public.returncode == 0 and "Green CI" in public.stdout  # public repository: no token
    green = server.deploy(
        "--check-ci", DRY_RUN="1", GITHUB_TOKEN="x", FAKE_CI_ANSWER='{\n  "total_count": 1,'
    )
    assert green.returncode == 0 and "Green CI" in green.stdout


def test_nginx_site_is_the_production_one() -> None:
    """install-nginx.sh cuts the template at the marker for the pre-certificate HTTP site."""
    site = (ROOT / "deploy" / "nginx" / "host-site.conf").read_text(encoding="utf-8")
    http, marker, https = site.partition("# --- end of HTTP block")
    assert marker and "listen 80;" in http and "ssl" not in http.split("server {", 1)[1]
    assert "return 301 https://brain.example.com$request_uri;" in http
    assert "listen 443 ssl;" in https and "Strict-Transport-Security" in https
    assert site.count("server_tokens off;") == 2
