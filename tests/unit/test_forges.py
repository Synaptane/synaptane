"""Creating a repository on GitHub or GitLab: what can be checked without a database."""

import shutil
import socket
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

import httpcore2
import httpx2
import pytest

from synaptane.core.errors import SynaptaneError
from synaptane.git.credentials import GitCredentials
from synaptane.services import forges
from synaptane.web.i18n import format_day


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
    bounded = forges._bounded(big, forges.Deadline())
    assert bounded.content == b"" and bounded.headers["x-ratelimit-remaining"] == "0"
    small = forges._bounded(httpx2.Response(201, json={"a": 1}, request=request), forges.Deadline())
    assert small.json() == {"a": 1}


class Dribble:
    """Local HTTP server that answers drop by drop: one byte every `pause` seconds, from the
    status line (`headers=True`) or only for the body."""

    def __init__(self, headers: bool, pause: float = 0.1) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.headers, self.pause = headers, pause
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        connection, _ = self.server.accept()
        with connection:
            connection.recv(65536)
            head = b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n"
            if not self.headers:
                connection.sendall(head)
                head = b""
            for byte in head + b"x" * 100000:
                if self.stop.is_set():
                    return
                try:
                    connection.sendall(bytes([byte]))
                except OSError:
                    return
                time.sleep(self.pause)

    def close(self) -> None:
        self.stop.set()
        self.server.close()


def api(port: int, deadline: forges.Deadline) -> forges._Api:
    return forges._Api(
        "gitlab",
        f"http://127.0.0.1:{port}",
        {},
        pin=False,
        rights=forges.GITLAB_RIGHTS,
        deadline=deadline,
    )


@pytest.mark.parametrize("headers", [True, False])
def test_a_server_answering_drop_by_drop_is_cut_off(
    monkeypatch: pytest.MonkeyPatch, headers: bool
) -> None:
    """The read timeout (15 s) restarts with each byte: only the call's deadline stops a server
    that sends one byte at a time, in its headers as in its body."""
    monkeypatch.setattr(forges, "CALL_SECONDS", 1.0)
    server = Dribble(headers)
    started = time.monotonic()
    try:
        with pytest.raises(SynaptaneError, match="did not answer in time"):
            api(server.port, forges.Deadline()).call("GET", "/api/v4/user")
    finally:
        server.close()
    assert time.monotonic() - started < 5


def test_the_operation_deadline_bounds_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    server = Dribble(headers=False)
    deadline = forges.Deadline(seconds=0.5)  # shorter than one call (20 s)
    started = time.monotonic()
    try:
        with pytest.raises(SynaptaneError, match="did not answer in time"):
            api(server.port, deadline).call("GET", "/api/v4/user")
    finally:
        server.close()
    assert time.monotonic() - started < 4
    with pytest.raises(SynaptaneError, match="did not answer in time"):
        api(server.port, deadline).call("GET", "/api/v4/user")  # nothing left: no connection


def test_the_deadline_is_installed_on_the_real_transport() -> None:
    """`client` reaches into httpx2's transport: a new version that moves it must fail here."""
    deadline = forges.Deadline()
    with forges.client(deadline) as http:
        transport = http._transport
        assert isinstance(transport, httpx2.HTTPTransport)
        assert isinstance(transport._pool._network_backend, forges._DeadlineBackend)


def test_the_expiry_of_a_created_token_is_kept_with_it() -> None:
    kept = GitCredentials.https("glpat-x", expiry=date(2027, 11, 1))
    back = GitCredentials.from_json(kept.to_json())
    assert back.expiry == date(2027, 11, 1) and back.token == "glpat-x"
    assert "glpat-x" not in repr(back)
    assert GitCredentials.from_json(GitCredentials.https("t").to_json()).expiry is None
    assert GitCredentials(kind="https", token="t", expires="garbage").expiry is None
    assert format_day(date(2027, 11, 1), "fr") == "1er novembre 2027"
    assert format_day(date(2027, 11, 8), "en") == "November 8, 2027"


def test_the_deadline_is_installed_behind_a_proxy_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxies of the environment are kept, and their transport is bounded as well."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    with forges.client(forges.Deadline()) as http:
        proxied = [t for t in http._mounts.values() if isinstance(t, httpx2.HTTPTransport)]
        assert proxied
        for transport in proxied:
            assert isinstance(transport._pool, httpcore2.HTTPProxy)
            assert isinstance(transport._pool._network_backend, forges._DeadlineBackend)


def test_slow_name_resolution_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """`socket.create_connection` would resolve without any timeout: the backend resolves
    within the deadline."""
    monkeypatch.setattr(forges, "CALL_SECONDS", 0.5)
    real = socket.getaddrinfo

    def slow(host: str, *args: object, **kwargs: object) -> object:
        if host == "slow.invalid":
            time.sleep(6)  # far beyond the deadline: a cut is still clear under load
        return real(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    gitlab = forges._Api(
        "gitlab",
        "http://slow.invalid:8080",
        {},
        pin=False,
        rights=forges.GITLAB_RIGHTS,
        deadline=forges.Deadline(),
    )
    started = time.monotonic()
    with pytest.raises(SynaptaneError, match="did not answer in time"):
        gitlab.call("GET", "/api/v4/user")
    assert time.monotonic() - started < 4


def test_every_address_of_a_name_is_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first address refuses the connection, the second one answers."""
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def answer() -> None:
        connection, _ = server.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

    threading.Thread(target=answer, daemon=True).start()

    def two(host: str, *args: object, **kwargs: object) -> object:
        # Nothing listens on 127.0.0.2 (the server is bound to 127.0.0.1 only): refused.
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", two)
    gitlab = forges._Api(
        "gitlab",
        f"http://two.invalid:{port}",
        {},
        pin=False,
        rights=forges.GITLAB_RIGHTS,
        deadline=forges.Deadline(),
    )
    try:
        assert gitlab.call("GET", "/api/v4/user") == {}
    finally:
        server.close()
