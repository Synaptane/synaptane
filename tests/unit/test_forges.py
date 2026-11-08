"""Creating a repository on GitHub or GitLab: what can be checked without a database."""

import shutil
import subprocess
from pathlib import Path

import httpx2
import pytest

from synaptane.core.errors import SynaptaneError
from synaptane.git.credentials import GitCredentials
from synaptane.services import forges


def test_the_deploy_key_is_a_usable_openssh_key(tmp_path: Path) -> None:
    private, public = forges._deploy_key()
    assert public.startswith("ssh-ed25519 ") and public.endswith(" synaptane")
    assert GitCredentials.ssh(private, "github.com ssh-ed25519 AAAA").private_key
    assert forges._deploy_key()[0] != private  # a new key each time
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen missing")
    key = tmp_path / "key"
    key.write_text(private)
    key.chmod(0o600)
    derived = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(key)], capture_output=True, text=True, check=True
    )
    assert derived.stdout.split()[:2] == public.split()[:2]


def test_the_token_never_shows_in_a_representation() -> None:
    new = forges.NewRepository("github", "ghp_secret")
    assert "ghp_secret" not in repr(new)
    created = forges.CreatedRepository(
        "gitlab", "gitlab.com", "a/b", "u", "w", credentials=GitCredentials.https("glpat-x")
    )
    assert "glpat-x" not in repr(created)


def test_names_and_owners() -> None:
    check = forges._check
    assert check(forges.NewRepository("github", " t ", " acme ", ""), "infra").name == "infra"
    assert check(forges.NewRepository("github", "t", "", "My_Repo.v2"), "x").name == "My_Repo.v2"
    gitlab = check(forges.NewRepository("gitlab", "t", "/acme/team/", "brain"), "x")
    assert gitlab.owner == "acme/team" and gitlab.gitlab_url == "https://gitlab.com"
    assert gitlab.token == "t"
    for owner in ("-acme", "acme_corp", "a" * 40):
        with pytest.raises(SynaptaneError, match="Invalid owner"):
            check(forges.NewRepository("github", "t", owner), "x")
    for name in (".hidden", "a b", "x.atom"):
        with pytest.raises(SynaptaneError, match="Invalid repository name"):
            check(forges.NewRepository("github", "t", "", name), "x")


def test_an_endless_answer_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forges, "MAX_ANSWER", 100)
    request = httpx2.Request("GET", "https://api.github.com/user")
    big = httpx2.Response(
        200, content=b"x" * 1000, headers={"x-ratelimit-remaining": "0"}, request=request
    )
    bounded = forges._bounded(big)
    assert bounded.content == b"" and bounded.headers["x-ratelimit-remaining"] == "0"
    small = forges._bounded(httpx2.Response(201, json={"a": 1}, request=request))
    assert small.json() == {"a": 1}
