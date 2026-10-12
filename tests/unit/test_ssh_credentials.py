"""SSH credentials of a space: serialization, target host, reading of the keys.

The real-world check only covered an HTTPS token: what is specific to SSH (`git@host:path` or
`ssh://…` URLs, host keys read before saving) was never executed.
No real SSH connection here: `ssh-keyscan` is replaced by a fake one on the PATH.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from synaptane.core.errors import SynaptaneError
from synaptane.git.credentials import GitCredentials, scan_host_keys, ssh_host

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
HOSTS = "forge.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExemple"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@h:p", ("h", 22)),
        ("git@forge.example.com:acme/infra.git", ("forge.example.com", 22)),
        ("ssh://u@h:2222/p", ("h", 2222)),
        ("ssh://git@forge.example.com/acme/infra.git", ("forge.example.com", 22)),
        ("ssh://forge.example.com:2222/acme/infra.git", ("forge.example.com", 2222)),
        ("ssh:///chemin/sans/hote", None),
        ("git@:p", None),  # user without host
        ("https://github.com/acme/infra.git", None),
        ("https://user@forge.example.com/acme/infra.git", None),  # "@" but not SSH
        ("http://forge.example.com/acme/infra.git", None),
        ("file:///srv/depots/infra.git", None),
        ("/srv/depots/infra.git", None),
        ("", None),
    ],
)
def test_ssh_host_of_each_url_form(remote: str, expected: tuple[str, int] | None) -> None:
    assert ssh_host(remote) == expected


@pytest.mark.parametrize("remote", ["ssh://git@h:abc/p", "ssh://h:99999/p"])
def test_ssh_host_with_an_invalid_port_is_not_ssh(remote: str) -> None:
    assert ssh_host(remote) is None


def test_ssh_credentials_roundtrip_and_normalisation() -> None:
    """The encrypted JSON in the database gives back exactly the same credentials: key ending
    with a newline (otherwise `ssh` refuses it), Windows line endings removed."""
    pasted = GitCredentials.ssh(KEY.replace("\n", "\r\n") + "\r\n\r\n", f"  {HOSTS}\r\n")
    assert pasted.private_key == KEY + "\n" and pasted.known_hosts == HOSTS + "\n"
    assert pasted.username == "" and pasted.token == ""
    again = GitCredentials.from_json(pasted.to_json())
    assert again == pasted and again.kind == "ssh"
    assert "token" not in pasted.to_json()  # nothing HTTPS in an SSH credential


@pytest.mark.parametrize(
    ("remote", "ssh", "https"),
    [
        ("git@forge.example.com:acme/infra.git", True, False),
        ("ssh://git@forge.example.com:2222/acme/infra.git", True, False),
        ("https://forge.example.com/acme/infra.git", False, True),
        ("http://forge.example.com/acme/infra.git", False, False),
        ("file:///srv/depots/infra.git", False, False),
        ("/srv/depots/infra.git", False, False),
    ],
)
def test_credentials_match_only_their_kind_of_url(remote: str, ssh: bool, https: bool) -> None:
    """An SSH key is never presented to an HTTPS repository, nor a token to an SSH one."""
    assert GitCredentials.ssh(KEY, HOSTS).matches(remote) is ssh
    assert GitCredentials.https("glpat-secret").matches(remote) is https


@pytest.mark.parametrize(
    ("remote", "message"),
    [
        ("https://github.com/acme/infra.git", "does not use SSH"),
        ("file:///srv/depots/infra.git", "does not use SSH"),
        # The host goes as an argument to ssh-keyscan: a disguised option is refused beforehand.
        ("git@-oProxyCommand=evil:p", "Invalid SSH host name"),
        ("git@h;reboot:p", "Invalid SSH host name"),
        ("ssh://u@h%20x/p", "Invalid SSH host name"),
    ],
)
def test_scan_refuses_what_is_not_a_plain_ssh_host(remote: str, message: str) -> None:
    with pytest.raises(SynaptaneError) as error:
        scan_host_keys(remote)
    assert message in error.value.message


@pytest.fixture
def fake_keyscan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """A fake `ssh-keyscan` first in the PATH, which returns a real host public key (so that
    the real `ssh-keygen` computes its fingerprint) and records its arguments."""
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen absent")
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "hote")], check=True
    )
    public = (tmp_path / "hote.pub").read_text().split()
    line = f"forge.example.com {public[0]} {public[1]}"
    bin_dir, args = tmp_path / "bin", tmp_path / "args"
    bin_dir.mkdir()
    script = bin_dir / "ssh-keyscan"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args}"\n'
        f'echo "# forge.example.com:2222 SSH-2.0-OpenSSH_9.6"\necho "{line}"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return line, args


def test_scan_reads_host_keys_on_the_url_port(fake_keyscan: tuple[str, Path]) -> None:
    """`ssh://…:2222`: the URL's port is the one queried, the host comes after `--`, comments
    are dropped and each key gets its SHA256 fingerprint for the user to check."""
    line, args = fake_keyscan
    keys, fingerprints = scan_host_keys("ssh://git@forge.example.com:2222/acme/infra.git", 3)
    assert keys == line + "\n"
    assert args.read_text().split("\n")[:-1] == ["-T", "3", "-p", "2222", "--", "forge.example.com"]
    [fingerprint] = fingerprints
    assert "SHA256:" in fingerprint and "ED25519" in fingerprint
    # The keys read can be pasted as is into the space's SSH form.
    assert GitCredentials.ssh(KEY, keys).known_hosts == keys


def test_scan_of_a_scp_like_url_uses_port_22(fake_keyscan: tuple[str, Path]) -> None:
    _, args = fake_keyscan
    scan_host_keys("git@forge.example.com:acme/infra.git")
    assert args.read_text().split("\n")[2:4] == ["-p", "22"]
