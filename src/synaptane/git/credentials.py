"""Git credentials of a space and their ephemeral provision for a command.

No secret goes through the command line or the Git configuration: an HTTPS token is read by a
credential helper from a 0600 file, an SSH key is written to a 0600 file. No file is executed
from the temporary directory (mounted `noexec` in Docker). Everything is deleted at the end of
the operation.
"""

import json
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from synaptane.core.errors import invalid

HELPER = Path(__file__).with_name("credential_helper.py")


@dataclass(frozen=True)
class GitCredentials:
    kind: Literal["https", "ssh"]
    username: str = ""
    token: str = ""
    private_key: str = ""
    known_hosts: str = ""

    @classmethod
    def https(cls, token: str, username: str = "") -> "GitCredentials":
        token, username = token.strip(), username.strip() or "synaptane"
        if not token or any(c in token + username for c in "\r\n\0"):
            raise invalid("Invalid access token.")
        return cls(kind="https", username=username, token=token)

    @classmethod
    def ssh(cls, private_key: str, known_hosts: str) -> "GitCredentials":
        key = private_key.strip().replace("\r\n", "\n") + "\n"
        hosts = known_hosts.strip().replace("\r\n", "\n")
        if "PRIVATE KEY-----" not in key:
            raise invalid("Invalid SSH private key (OpenSSH or PEM format expected).")
        if "ENCRYPTED" in key:
            raise invalid("The SSH key must not be protected by a passphrase.")
        if not hosts:
            raise invalid(
                "Host keys (known_hosts) are required: they protect against Git server "
                "impersonation."
            )
        return cls(kind="ssh", private_key=key, known_hosts=hosts + "\n")

    def to_json(self) -> str:
        if self.kind == "https":
            return json.dumps({"kind": "https", "username": self.username, "token": self.token})
        data = {"kind": "ssh", "private_key": self.private_key, "known_hosts": self.known_hosts}
        return json.dumps(data)

    @classmethod
    def from_json(cls, value: str) -> "GitCredentials":
        data = json.loads(value)
        if data["kind"] == "https":
            return cls(kind="https", username=data["username"], token=data["token"])
        return cls(kind="ssh", private_key=data["private_key"], known_hosts=data["known_hosts"])

    def matches(self, remote: str) -> bool:
        return (
            remote.startswith("https://")
            if self.kind == "https"
            else not remote.startswith(("https://", "http://", "file://", "/"))
        )


def _private_file(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    return path


# A silent SSH server does not block a command: connection abandoned after 20 s, session cut
# after one minute without answer (4 probes 15 s apart).
SSH_TIMEOUTS = (
    "-o",
    "ConnectTimeout=20",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
)


def https_host(remote: str) -> str:
    """Host (and port if written) of an HTTPS URL, as Git describes it to the credential
    helper (`host=…`). Empty for another URL."""
    parts = urlsplit(remote)
    if parts.scheme != "https":
        return ""
    return parts.netloc.rsplit("@", 1)[-1].lower()


@contextmanager
def git_environment(
    credentials: GitCredentials | None, remote: str = ""
) -> Iterator[dict[str, str]]:
    """Environment variables of a network Git command, valid for the duration of the block.

    `remote`: repository URL. The HTTPS token is only given to its host."""
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        # Without a key: refuse any prompt and any unknown host rather than block.
        "GIT_SSH_COMMAND": shlex.join(
            ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
            + list(SSH_TIMEOUTS)
        ),
    }
    if credentials is None:
        yield env
        return
    directory = Path(tempfile.mkdtemp(prefix="synaptane-git-"))
    try:
        if credentials.kind == "https":
            secret = json.dumps({"username": credentials.username, "token": credentials.token})
            secret_file = _private_file(directory, "credentials.json", secret)
            helper = "!" + shlex.join(
                [sys.executable, "-I", str(HELPER), str(secret_file), https_host(remote) or "-"]
            )
            # An emptied helper list, then ours (configuration through the environment).
            env |= {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "GIT_CONFIG_KEY_1": "credential.helper",
                "GIT_CONFIG_VALUE_1": helper,
            }
        else:
            key = _private_file(directory, "identity", credentials.private_key)
            hosts = _private_file(directory, "known_hosts", credentials.known_hosts)
            ssh = shutil.which("ssh") or "ssh"
            env["GIT_SSH_COMMAND"] = shlex.join(
                [
                    ssh,
                    "-F",
                    "/dev/null",
                    "-i",
                    str(key),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "IdentityAgent=none",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={hosts}",
                    "-o",
                    "GlobalKnownHostsFile=/dev/null",
                    *SSH_TIMEOUTS,
                ]
            )
        yield env
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def ssh_host(remote: str) -> tuple[str, int] | None:
    """SSH host and port of a `git@host:path` or `ssh://[user@]host[:port]/path` URL."""
    if remote.startswith("ssh://"):
        parts = urlsplit(remote)
        try:
            port = parts.port or 22
        except ValueError:  # non-numeric or out-of-range port: not a usable SSH host
            return None
        return (parts.hostname, port) if parts.hostname else None
    if "@" in remote and ":" in remote and "://" not in remote:
        host = remote.split("@", 1)[1].split(":", 1)[0]
        return (host, 22) if host else None
    return None


def scan_host_keys(remote: str, timeout: int = 10) -> tuple[str, list[str]]:
    """Reads the public keys of the SSH server (`ssh-keyscan`) and their SHA256 fingerprints.

    To be confirmed by a human: compare the fingerprints with those published by the host.
    """

    import subprocess

    from synaptane.core.errors import invalid

    target = ssh_host(remote)
    if target is None:
        raise invalid("This repository URL does not use SSH.")
    host, port = target
    if not host.replace(".", "").replace("-", "").isalnum():
        raise invalid("Invalid SSH host name.")
    try:
        scan = subprocess.run(
            ["ssh-keyscan", "-T", str(timeout), "-p", str(port), "--", host],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise invalid("Cannot reach the SSH server.") from None
    keys = "\n".join(line for line in scan.stdout.splitlines() if line and not line.startswith("#"))
    if not keys:
        raise invalid("The SSH server returned no keys.")
    prints = subprocess.run(
        ["ssh-keygen", "-l", "-f", "-"], input=keys + "\n", capture_output=True, text=True
    )
    return keys + "\n", [line for line in prints.stdout.splitlines() if line]
