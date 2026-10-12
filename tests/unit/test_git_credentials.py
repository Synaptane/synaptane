import hashlib
import hmac
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from synaptane.core.errors import SynaptaneError
from synaptane.git.credentials import GitCredentials, git_environment
from synaptane.git.repo import GitError, safe_path
from synaptane.services.git_sync import verify_webhook

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"


def test_https_token_reaches_git_without_any_executable_file() -> None:
    """The token is given to Git by a credential helper. Nothing is executed from the
    temporary folder: in Docker, `/tmp` is mounted `noexec` (bug found during deployment)."""
    credentials = GitCredentials.https("glpat-secret", "bot")
    with git_environment(credentials, "https://gitlab.com/acme/brain.git") as env:
        assert "glpat-secret" not in " ".join(env.values())  # neither as an argument nor in config
        helper = shlex.split(env["GIT_CONFIG_VALUE_1"].removeprefix("!"))
        assert helper[-1] == "gitlab.com"  # the repository host, the token's only recipient
        secret_dir = Path(helper[-2]).parent
        assert all(os.stat(p).st_mode & 0o177 == 0 for p in secret_dir.iterdir())  # 0600
        filled = fill(env, "protocol=https\nhost=gitlab.com\n\n")
        assert "username=bot" in filled.stdout and "password=glpat-secret" in filled.stdout
    assert not secret_dir.exists()


def fill(env: dict[str, str], request: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "credential", "fill"],
        input=request,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, **env},
    )


@pytest.mark.parametrize(
    ("remote", "asked"),
    [
        # Forge redirect to another host: Git asks again for the new host.
        ("https://gitlab.com/acme/brain.git", "protocol=https\nhost=evil.example\n\n"),
        # Same host, other port: another service.
        ("https://gitlab.com/acme/brain.git", "protocol=https\nhost=gitlab.com:8443\n\n"),
        # Never in clear text.
        ("https://gitlab.com/acme/brain.git", "protocol=http\nhost=gitlab.com\n\n"),
        # Unknown repository: nobody.
        ("", "protocol=https\nhost=gitlab.com\n\n"),
    ],
)
def test_https_token_is_only_given_to_the_repository_host(remote: str, asked: str) -> None:
    """The token only goes to the host of the repository URL, and over HTTPS."""
    with git_environment(GitCredentials.https("glpat-secret", "bot"), remote) as env:
        filled = fill(env, asked)
    assert "glpat-secret" not in filled.stdout
    assert filled.returncode != 0  # with no answer and no possible prompt, Git gives up


def test_https_token_follows_an_explicit_port() -> None:
    with git_environment(GitCredentials.https("tok"), "https://Forge.local:8443/a.git") as env:
        assert "password=tok" in fill(env, "protocol=https\nhost=forge.local:8443\n\n").stdout


def test_ssh_command_is_strict_and_files_are_removed() -> None:
    with git_environment(GitCredentials.ssh(KEY, "github.com ssh-ed25519 AAAA")) as env:
        args = shlex.split(env["GIT_SSH_COMMAND"])
        assert "StrictHostKeyChecking=yes" in args and "BatchMode=yes" in args
        # Silent forge: connection dropped after 20 s, session after one minute without answer.
        assert "ConnectTimeout=20" in args and "ServerAliveInterval=15" in args
        key_file = Path(args[args.index("-i") + 1])
        assert key_file.read_text().startswith("-----BEGIN OPENSSH")
    assert not key_file.exists()


def test_ssh_without_key_does_not_wait_forever() -> None:
    with git_environment(None, "git@github.com:a/b.git") as env:
        args = shlex.split(env["GIT_SSH_COMMAND"])
    assert "BatchMode=yes" in args and "ConnectTimeout=20" in args


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: GitCredentials.https(""), "token"),
        (lambda: GitCredentials.ssh("pas une clé", "host key"), "private key"),
        (lambda: GitCredentials.ssh(KEY, ""), "known_hosts"),
        (lambda: GitCredentials.ssh(KEY.replace("abc", "Proc-Type: 4,ENCRYPTED"), "h k"), "phrase"),
    ],
)
def test_invalid_credentials(factory: object, message: str) -> None:
    with pytest.raises(SynaptaneError) as error:
        factory()  # type: ignore[operator]
    assert message in error.value.message


def test_credentials_roundtrip_and_url_matching() -> None:
    https = GitCredentials.https("tok")
    assert GitCredentials.from_json(https.to_json()) == https
    assert https.matches("https://github.com/a/b.git")
    assert not https.matches("git@github.com:a/b.git")
    assert GitCredentials.ssh(KEY, "h k").matches("git@github.com:a/b.git")


def test_webhook_signatures() -> None:
    body = b'{"ref":"refs/heads/main"}'
    signature = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_webhook("s3cret", {"x-hub-signature-256": "sha256=" + signature}, body)
    assert verify_webhook("s3cret", {"x-gitea-signature": signature}, body)
    assert verify_webhook("s3cret", {"x-gitlab-token": "s3cret"}, body)
    assert not verify_webhook("s3cret", {"x-hub-signature-256": "sha256=00"}, body)
    assert not verify_webhook("s3cret", {"x-gitlab-token": "nope"}, body)
    assert not verify_webhook("s3cret", {}, body)


@pytest.mark.parametrize("path", ["", "/etc/passwd", "../x", "a/../b", ".git/config", "a\\b"])
def test_unsafe_paths_are_refused(path: str) -> None:
    with pytest.raises(GitError):
        safe_path(path)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("fatal: could not read Username for 'https://x': terminal prompts disabled", "token"),
        ("remote: You are not allowed to download code. … returned error: 403", "Reporter"),
        ("remote: GitLab: You are not allowed to push code to protected branches", "protected"),
        ("remote: Repository not found.", "Repository not found:"),
        ("fatal: unable to access: Could not resolve host: forge.example", "Host not found"),
    ],
)
def test_common_git_errors_are_explained(output: str, expected: str) -> None:
    """A readable message before git's raw output."""
    from synaptane.git.repo import explain

    message = explain("ls-remote", output)
    assert expected in message and message.endswith(f"(git ls-remote: {output})")


def test_unknown_git_errors_stay_raw() -> None:
    from synaptane.git.repo import explain

    assert explain("fetch", "fatal: bizarre") == "git fetch: fatal: bizarre"


def test_network_timeout_is_five_minutes_and_configurable(tmp_path: Path) -> None:
    """A 30-minute default network timeout meant 30 minutes of waiting per attempt."""
    from synaptane.config import Settings
    from synaptane.core.security import SecretBox
    from synaptane.git.repo import Identity, Repository
    from synaptane.services.workspace import Workspace

    assert Repository(tmp_path, "", "main", Identity("a", "a@b")).network_timeout == 300
    box = SecretBox(SecretBox.generate_key())
    settings = Settings(database_url="postgresql://x", data_dir=tmp_path)
    assert Workspace.from_settings(settings, box).network_timeout == 300
    longer = Settings(database_url="postgresql://x", data_dir=tmp_path, git_timeout=1200)
    assert Workspace.from_settings(longer, box).network_timeout == 1200
