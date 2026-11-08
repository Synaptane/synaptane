"""Creating a new space's repository on GitHub or GitLab (docs/admin/git.md).

The providers' APIs are simulated (`httpx2.MockTransport`): no test reaches the network. What
is checked: the calls made (private repository, deploy key with write access, project access
token limited to the repository), the credentials kept, every refusal with its message, the
repository created but not its access, the rights, the CSRF check, the address of a
self-hosted GitLab, and that the admin's token is never kept, logged, audited nor returned.
"""

import json
import logging
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole
from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.core.security import SecretBox
from synaptane.git.credentials import GitCredentials
from synaptane.services import accounts, forges, organizations, spaces
from synaptane.services.git_sync import GIT_CREDENTIALS
from synaptane.services.secrets import get_space_secret
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Job, Space
from tests.integration.conftest import actor_of, make_user, token_for
from tests.integration.test_space_settings_web import flash
from tests.integration.test_web import csrf, login

TOKEN = "ghp_CreationTokenNeverKept0123456789"
PROJECT_TOKEN = "glpat-ProjectTokenKeptEncrypted"
GITHUB_KEYS = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
    "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxk",
]
Answer = httpx2.Response | Callable[[httpx2.Request], httpx2.Response]


@dataclass
class Forge:
    """Simulated provider API: answers by (method, path), records every request."""

    routes: dict[tuple[str, str], Answer] = field(default_factory=dict)
    seen: list[httpx2.Request] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        answer = self.routes.get((request.method, request.url.raw_path.decode()))
        if answer is None:
            return httpx2.Response(404, json={"message": "404 Not Found"})
        return answer(request) if callable(answer) else answer

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.raw_path.decode()}" for r in self.seen]

    def body(self, method: str, path: str) -> Any:
        found = [r for r in self.seen if (r.method, r.url.raw_path.decode()) == (method, path)]
        assert found, self.paths()
        return json.loads(found[-1].content)


def github(forge: Forge, owner: str = "carla-gh", user_path: str = "/user/repos") -> Forge:
    forge.routes |= {
        ("GET", "/user"): httpx2.Response(200, json={"login": "carla-gh"}),
        ("GET", "/meta"): httpx2.Response(200, json={"ssh_keys": GITHUB_KEYS}),
        ("POST", user_path): httpx2.Response(
            201,
            json={
                "full_name": f"{owner}/infra",
                "html_url": f"https://github.com/{owner}/infra",
                "ssh_url": f"git@github.com:{owner}/infra.git",
            },
        ),
        ("POST", f"/repos/{owner}/infra/keys"): httpx2.Response(201, json={"id": 7}),
    }
    return forge


def gitlab(forge: Forge, *, namespace: bool = True) -> Forge:
    path = "acme/team/infra" if namespace else "carla/infra"
    forge.routes |= {
        ("GET", "/api/v4/user"): httpx2.Response(200, json={"username": "carla", "id": 3}),
        ("GET", "/api/v4/personal_access_tokens/self"): httpx2.Response(
            200, json={"scopes": ["api"]}
        ),
        ("GET", "/api/v4/namespaces/acme%2Fteam"): httpx2.Response(
            200, json={"id": 12, "kind": "group", "full_path": "acme/team", "plan": "premium"}
        ),
        ("GET", "/api/v4/namespaces/carla"): httpx2.Response(
            200, json={"id": 3, "kind": "user", "full_path": "carla"}
        ),
        ("POST", "/api/v4/projects"): httpx2.Response(
            201,
            json={
                "id": 42,
                "path_with_namespace": path,
                "web_url": f"https://gitlab.com/{path}",
            },
        ),
        ("POST", "/api/v4/projects/42/access_tokens"): httpx2.Response(
            201, json={"token": PROJECT_TOKEN, "expires_at": "2027-11-08"}
        ),
    }
    return forge


REAL_GETADDRINFO = socket.getaddrinfo


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> Iterator[Forge]:
    """Simulated APIs. github.com and gitlab.com are fixed hosts: never resolved first (any
    other name, the database's for one, resolves as usual)."""
    simulated = Forge()
    monkeypatch.setattr(forges, "TRANSPORT", httpx2.MockTransport(simulated.handle))

    def no_forge_dns(host: str, *args: Any, **kwargs: Any) -> Any:
        assert "git" not in str(host), f"unexpected name resolution: {host}"
        return REAL_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", no_forge_dns)
    yield simulated


@pytest.fixture
def acme(db: Session) -> None:
    """Acme: carla administers it (without being super admin), bob is a member."""
    root = actor_of(db, make_user(db, "root", superadmin=True))
    org = organizations.create_organization(db, root, slug="acme", name="Acme")
    organizations.set_member(db, root, org, make_user(db, "carla"), OrgRole.ADMIN)
    organizations.set_member(db, root, org, make_user(db, "bob"), OrgRole.MEMBER)
    db.commit()


def box_of(client: TestClient) -> SecretBox:
    return client.app.state.workspace.box  # type: ignore[attr-defined,no-any-return]


def create(
    db: Session,
    box: SecretBox,
    username: str = "carla",
    *,
    slug: str = "infra",
    allow_private: bool = False,
    **new: str,
) -> tuple[Space, forges.CreatedRepository]:
    actor = actor_of(db, accounts.get_user(db, username))
    org = organizations.get_organization(db, actor, "acme")
    request = forges.NewRepository(new.pop("provider", "github"), new.pop("token", TOKEN), **new)
    return forges.create_space_with_repository(
        db,
        actor,
        box,
        org,
        request,
        slug=slug,
        name="Infra",
        description="Servers\nand network",
        allow_private=allow_private,
        public_host="brain.example.com",
    )


def credentials(db: Session, box: SecretBox, space: Space) -> GitCredentials | None:
    raw = get_space_secret(db, box, space.id, GIT_CREDENTIALS)
    return GitCredentials.from_json(raw) if raw else None


def events(db: Session) -> dict[str, dict[str, Any]]:
    return {e.action: e.details for e in db.scalars(select(AuditEvent).order_by(AuditEvent.at))}


def assert_no_token_kept(db: Session, box: SecretBox, *texts: str) -> None:
    for space in db.scalars(select(Space)):
        raw = get_space_secret(db, box, space.id, GIT_CREDENTIALS) or ""
        assert TOKEN not in raw
    assert all(TOKEN not in json.dumps(details) for details in events(db).values())
    assert all(TOKEN not in text for text in texts)


def refusal(call: Callable[[], object]) -> SynaptaneError:
    with pytest.raises(SynaptaneError) as raised:
        call()
    return raised.value


# --- GitHub ------------------------------------------------------------------------------------


def test_github_private_repository_with_a_deploy_key(
    db: Session, client: TestClient, acme: None, forge: Forge, caplog: pytest.LogCaptureFixture
) -> None:
    github(forge)
    box = box_of(client)
    caplog.set_level(logging.DEBUG)
    space, created = create(db, box)
    db.commit()

    assert forge.paths() == [
        "GET /user",
        "GET /meta",
        "POST /user/repos",
        "POST /repos/carla-gh/infra/keys",
    ]
    assert all(r.url.host == "api.github.com" for r in forge.seen)
    assert forge.seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    # The server keys: with the token (60 calls per hour without one).
    assert forge.seen[1].headers["authorization"] == f"Bearer {TOKEN}"
    repo = forge.body("POST", "/user/repos")
    assert repo == {
        "name": "infra",
        "private": True,
        "auto_init": False,  # empty: the first sync writes Synaptane's skeleton
        "description": "Servers and network",
    }
    key = forge.body("POST", "/repos/carla-gh/infra/keys")
    assert key["read_only"] is False and key["title"] == "Synaptane (brain.example.com)"
    assert key["key"].startswith("ssh-ed25519 ")

    assert space.repo_url == "git@github.com:carla-gh/infra.git" and space.branch == "main"
    assert created.web_url == "https://github.com/carla-gh/infra" and created.access_error is None
    kept = credentials(db, box, space)
    assert kept is not None and kept.kind == "ssh"
    assert "OPENSSH PRIVATE KEY" in kept.private_key
    assert kept.known_hosts == "".join(f"github.com {k}\n" for k in GITHUB_KEYS)
    assert db.scalar(select(Job.kind).where(Job.space_id == space.id)) == "sync"
    found = events(db)
    assert found["space.repository_created"] == {
        "provider": "github",
        "host": "github.com",
        "repository": "carla-gh/infra",
        "access": "ssh",
    }
    assert found["space.git_credentials_set"] == {"kind": "ssh"}
    assert_no_token_kept(db, box, caplog.text, repr(created))


def test_github_repository_of_an_organization(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    github(forge, owner="acme-corp", user_path="/orgs/acme-corp/repos")
    space, _ = create(db, box_of(client), owner="acme-corp", name="")
    assert "POST /orgs/acme-corp/repos" in forge.paths()
    assert space.repo_url == "git@github.com:acme-corp/infra.git"


def test_github_owner_written_like_the_token_account_is_the_account(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    github(forge)
    create(db, box_of(client), owner="Carla-GH")
    assert "POST /user/repos" in forge.paths()


@pytest.mark.parametrize(
    ("answer", "code", "message"),
    [
        (httpx2.Response(401, json={"message": "Bad credentials"}), ErrorCode.INVALID, "invalid"),
        (
            httpx2.Response(403, json={"message": "Resource not accessible by personal token"}),
            ErrorCode.INVALID,
            "Administration",
        ),
        (
            httpx2.Response(403, headers={"x-ratelimit-remaining": "0"}, json={}),
            ErrorCode.TOO_MANY,
            "limits the number of requests",
        ),
        (httpx2.Response(429, json={}), ErrorCode.TOO_MANY, "limits the number of requests"),
        (
            httpx2.Response(404, json={"message": "Not Found"}),
            ErrorCode.INVALID,
            "Owner not found on GitHub",
        ),
        (
            httpx2.Response(
                422, json={"errors": [{"message": "name already exists on this account"}]}
            ),
            ErrorCode.CONFLICT,
            "already exists in acme-corp",
        ),
        (
            httpx2.Response(422, json={"errors": [{"message": "name is invalid"}]}),
            ErrorCode.INVALID,
            "refused the repository name",
        ),
        (httpx2.Response(502, text="Bad gateway"), ErrorCode.UNAVAILABLE, "cannot be reached"),
        (httpx2.Response(418, json={}), ErrorCode.INVALID, "HTTP 418"),
        (httpx2.Response(201, text="not json"), ErrorCode.INVALID, "Unexpected answer"),
        (httpx2.Response(201, json={"id": 1}), ErrorCode.INVALID, "Unexpected answer"),
    ],
)
def test_github_refusals_create_nothing(
    db: Session,
    client: TestClient,
    acme: None,
    forge: Forge,
    answer: httpx2.Response,
    code: ErrorCode,
    message: str,
) -> None:
    github(forge, owner="acme-corp", user_path="/orgs/acme-corp/repos")
    forge.routes[("POST", "/orgs/acme-corp/repos")] = answer
    error = refusal(lambda: create(db, box_of(client), owner="acme-corp"))
    assert error.code is code and message in error.message
    assert TOKEN not in error.message
    db.rollback()
    assert db.scalar(select(Space)) is None
    assert "space.repository_created" not in events(db)


def test_github_token_refused_before_anything_is_created(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    github(forge)
    forge.routes[("GET", "/user")] = httpx2.Response(401, json={"message": "Bad credentials"})
    error = refusal(lambda: create(db, box_of(client)))
    assert error.message == "GitHub refused the access token: it is invalid, expired or revoked."
    assert forge.paths() == ["GET /user"]


def test_github_unreachable(db: Session, client: TestClient, acme: None, forge: Forge) -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    forge.routes[("GET", "/user")] = slow
    error = refusal(lambda: create(db, box_of(client)))
    assert error.code is ErrorCode.UNAVAILABLE
    assert error.message == "GitHub did not answer in time: try again in a moment."
    forge.routes[("GET", "/user")] = down
    error = refusal(lambda: create(db, box_of(client)))
    assert error.message == "GitHub cannot be reached: try again in a moment."


def test_github_without_server_keys_creates_nothing(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    github(forge)
    forge.routes[("GET", "/meta")] = httpx2.Response(200, json={"ssh_keys": ["no key"]})
    refusal(lambda: create(db, box_of(client)))
    assert "POST /user/repos" not in forge.paths()


def test_repository_created_but_deploy_key_refused(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    """Not a silent orphan: the space is created on the repository, the admin is told to
    enter its credentials, and the repository is never deleted."""
    github(forge)
    forge.routes[("POST", "/repos/carla-gh/infra/keys")] = httpx2.Response(
        403, json={"message": "Resource not accessible"}
    )
    box = box_of(client)
    space, created = create(db, box)
    assert space.repo_url == "git@github.com:carla-gh/infra.git"
    assert created.credentials is None and created.access_error is not None
    assert "Administration" in created.access_error.message
    assert credentials(db, box, space) is None
    assert events(db)["space.repository_created"]["access"] == "none"
    assert not [p for p in forge.paths() if p.startswith("DELETE")]


def test_repository_created_but_not_the_space(
    db: Session, client: TestClient, acme: None, forge: Forge, monkeypatch: pytest.MonkeyPatch
) -> None:
    github(forge)

    def taken(*_: object, **__: object) -> Space:
        raise SynaptaneError(ErrorCode.CONFLICT, "The space “{slug}” already exists.", slug="x")

    monkeypatch.setattr(spaces, "create_space", taken)
    error = refusal(lambda: create(db, box_of(client)))
    assert error.code is ErrorCode.CONFLICT
    assert "https://github.com/carla-gh/infra" in error.message
    assert "was not deleted" in error.message


# --- GitLab ------------------------------------------------------------------------------------


def test_gitlab_project_in_a_group_with_a_project_access_token(
    db: Session, client: TestClient, acme: None, forge: Forge, caplog: pytest.LogCaptureFixture
) -> None:
    gitlab(forge)
    box = box_of(client)
    caplog.set_level(logging.DEBUG)
    space, created = create(db, box, provider="gitlab", owner="acme/team", name="infra")
    db.commit()
    assert forge.paths() == [
        "GET /api/v4/user",
        "GET /api/v4/personal_access_tokens/self",
        "GET /api/v4/namespaces/acme%2Fteam",
        "POST /api/v4/projects",
        "POST /api/v4/projects/42/access_tokens",
    ]
    assert all(r.url.host == "gitlab.com" for r in forge.seen)
    assert forge.seen[0].headers["private-token"] == TOKEN
    assert forge.body("POST", "/api/v4/projects") == {
        "name": "infra",
        "path": "infra",
        "namespace_id": 12,
        "visibility": "private",
        "initialize_with_readme": False,
        "description": "Servers and network",
    }
    token = forge.body("POST", "/api/v4/projects/42/access_tokens")
    assert token["scopes"] == ["read_repository", "write_repository"]
    assert token["access_level"] == 40 and "expires_at" not in token  # the longest allowed

    assert space.repo_url == "https://gitlab.com/acme/team/infra.git"
    kept = credentials(db, box, space)
    assert kept is not None and kept.kind == "https" and kept.token == PROJECT_TOKEN
    assert str(created.token_expires) == "2027-11-08"
    assert events(db)["space.repository_created"] == {
        "provider": "gitlab",
        "host": "gitlab.com",
        "repository": "acme/team/infra",
        "access": "https",
    }
    assert_no_token_kept(db, box, caplog.text)


def test_gitlab_personal_project_without_scope_information(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    """An older GitLab without `personal_access_tokens/self`: the creation goes on."""
    gitlab(forge, namespace=False)
    del forge.routes[("GET", "/api/v4/personal_access_tokens/self")]
    forge.routes[("POST", "/api/v4/projects/42/access_tokens")] = httpx2.Response(
        201, json={"token": PROJECT_TOKEN, "expires_at": None}
    )
    space, created = create(db, box_of(client), provider="gitlab")
    assert forge.body("POST", "/api/v4/projects")["namespace_id"] == 3
    assert space.repo_url == "https://gitlab.com/carla/infra.git"
    assert created.token_expires is None


def test_gitlab_token_without_the_api_scope_creates_nothing(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    gitlab(forge)
    forge.routes[("GET", "/api/v4/personal_access_tokens/self")] = httpx2.Response(
        200, json={"scopes": ["read_api"]}
    )
    error = refusal(lambda: create(db, box_of(client), provider="gitlab", owner="acme/team"))
    assert "api scope" in error.message
    assert "POST /api/v4/projects" not in forge.paths()


@pytest.mark.parametrize(
    ("route", "answer", "code", "message"),
    [
        (
            ("GET", "/api/v4/namespaces/acme%2Fteam"),
            httpx2.Response(404, json={"message": "404 Namespace Not Found"}),
            ErrorCode.INVALID,
            "Namespace not found on GitLab: “acme/team”",
        ),
        (
            ("POST", "/api/v4/projects"),
            httpx2.Response(400, json={"message": {"path": ["has already been taken"]}}),
            ErrorCode.CONFLICT,
            "“infra” already exists in acme/team on GitLab",
        ),
        (
            ("POST", "/api/v4/projects"),
            httpx2.Response(403, json={"message": "403 Forbidden"}),
            ErrorCode.INVALID,
            "role allowed to create projects",
        ),
        (
            ("POST", "/api/v4/projects"),
            httpx2.Response(429, json={}),
            ErrorCode.TOO_MANY,
            "GitLab limits",
        ),
        (
            ("GET", "/api/v4/user"),
            httpx2.Response(401, json={"message": "401 Unauthorized"}),
            ErrorCode.INVALID,
            "GitLab refused the access token",
        ),
        (
            ("GET", "/api/v4/user"),
            httpx2.Response(503, json={}),
            ErrorCode.UNAVAILABLE,
            "GitLab cannot be reached",
        ),
        (
            ("GET", "/api/v4/user"),
            httpx2.Response(302, headers={"location": "https://elsewhere.example/"}),
            ErrorCode.INVALID,
            "HTTP 302",
        ),
    ],
)
def test_gitlab_refusals_create_nothing(
    db: Session,
    client: TestClient,
    acme: None,
    forge: Forge,
    route: tuple[str, str],
    answer: httpx2.Response,
    code: ErrorCode,
    message: str,
) -> None:
    gitlab(forge)
    forge.routes[route] = answer
    error = refusal(lambda: create(db, box_of(client), provider="gitlab", owner="acme/team"))
    assert error.code is code and message in error.message, error.message
    assert TOKEN not in error.message
    assert not [r for r in forge.seen if "elsewhere" in str(r.url)]  # no redirect followed
    db.rollback()
    assert db.scalar(select(Space)) is None


def test_gitlab_project_created_but_no_project_access_token(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    gitlab(forge)
    forge.routes[("POST", "/api/v4/projects/42/access_tokens")] = httpx2.Response(
        404, json={"message": "404 Not Found"}
    )
    box = box_of(client)
    space, created = create(db, box, provider="gitlab", owner="acme/team")
    assert created.access_error is not None and "Premium" in created.access_error.message
    assert space.repo_url == "https://gitlab.com/acme/team/infra.git"
    assert credentials(db, box, space) is None


# --- Self-hosted GitLab: HTTPS and public addresses only ---------------------------------------


def resolving(monkeypatch: pytest.MonkeyPatch, address: str) -> list[str]:
    """The self-hosted GitLab's name resolves to `address`."""
    asked: list[str] = []

    def answer(host: str, *args: Any, **kwargs: Any) -> Any:
        if "git" not in str(host):
            return REAL_GETADDRINFO(host, *args, **kwargs)
        asked.append(host)
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", answer)
    return asked


def test_self_hosted_gitlab_at_a_public_address_is_pinned(
    db: Session, client: TestClient, acme: None, forge: Forge, monkeypatch: pytest.MonkeyPatch
) -> None:
    gitlab(forge)
    asked = resolving(monkeypatch, "140.82.112.3")
    space, created = create(
        db,
        box_of(client),
        provider="gitlab",
        owner="acme/team",
        gitlab_url="https://GitLab.Example.com:8443/",
    )
    assert set(asked) == {"gitlab.example.com"}
    # Connected to the address checked, under the server's name.
    assert {r.url.host for r in forge.seen} == {"140.82.112.3"}
    assert {r.headers["host"] for r in forge.seen} == {"gitlab.example.com:8443"}
    assert space.repo_url == "https://gitlab.example.com:8443/acme/team/infra.git"
    assert created.host == "gitlab.example.com:8443"


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"])
def test_self_hosted_gitlab_on_an_internal_address_is_refused(
    db: Session,
    client: TestClient,
    acme: None,
    forge: Forge,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    gitlab(forge)
    resolving(monkeypatch, address)
    error = refusal(
        lambda: create(db, box_of(client), provider="gitlab", gitlab_url="https://gitlab.internal")
    )
    assert "private or local network" in error.message
    assert forge.seen == []


def test_self_hosted_gitlab_unknown_host(
    db: Session, client: TestClient, acme: None, forge: Forge, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown(host: str, *args: Any, **kwargs: Any) -> Any:
        if "git" not in str(host) and "nope" not in str(host):
            return REAL_GETADDRINFO(host, *args, **kwargs)
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    error = refusal(
        lambda: create(db, box_of(client), provider="gitlab", gitlab_url="https://nope.example")
    )
    assert error.message == "GitLab server not found: nope.example."
    assert forge.seen == []


def test_super_admin_and_development_reach_a_local_gitlab(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    """The super admin already runs the server; `allow_private_webhooks` is for development.
    Neither resolves the name first (the `forge` fixture refuses any resolution)."""
    gitlab(forge, namespace=False)
    space, _ = create(db, box_of(client), "root", provider="gitlab", gitlab_url="https://git.lan")
    assert space.repo_url == "https://git.lan/carla/infra.git"
    _, created = create(
        db,
        box_of(client),
        slug="infra-2",
        provider="gitlab",
        gitlab_url="https://git.lan",
        allow_private=True,
    )
    assert created.host == "git.lan"


@pytest.mark.parametrize(
    "address",
    [
        "http://gitlab.example.com",
        "https://user:secret@gitlab.example.com",
        "https://gitlab.example.com?x=1",
        "https://gitlab.example.com/#top",
        "https://gitlab.example.com:99999",
        "ftp://gitlab.example.com",
        "https://",
        "https://gitlab example.com",
    ],
)
def test_invalid_gitlab_address(
    db: Session, client: TestClient, acme: None, forge: Forge, address: str
) -> None:
    error = refusal(lambda: create(db, box_of(client), provider="gitlab", gitlab_url=address))
    assert error.message.startswith("Invalid GitLab address")
    assert forge.seen == []


def test_gitlab_address_normalized() -> None:
    assert forges.gitlab_base("") == "https://gitlab.com"
    assert forges.gitlab_base(" https://Git.Example.com/gitlab/ ") == (
        "https://git.example.com/gitlab"
    )


# --- Input, rights ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "message"),
    [
        ({"provider": "bitbucket"}, "Choose GitHub or GitLab"),
        ({"token": "  "}, "Enter the access token"),
        ({"token": "two words"}, "Enter the access token"),
        ({"name": "../evil"}, "Invalid repository name"),
        ({"name": "brain.git"}, "Invalid repository name"),
        ({"name": "x" * 101}, "Invalid repository name"),
        ({"owner": "acme/team"}, "Invalid owner"),  # no subgroup on GitHub
        ({"owner": "acme?x=1"}, "Invalid owner"),
        ({"provider": "gitlab", "owner": "acme/../x"}, "Invalid owner"),
    ],
)
def test_invalid_request_sends_nothing(
    db: Session, client: TestClient, acme: None, forge: Forge, new: dict[str, str], message: str
) -> None:
    error = refusal(lambda: create(db, box_of(client), **cast(Any, new)))
    assert error.code is ErrorCode.INVALID and message in error.message
    assert forge.seen == []


def test_space_identifier_taken_sends_nothing(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    root = actor_of(db, accounts.get_user(db, "root"))
    spaces.create_space(
        db, root, organizations.get_organization(db, root, "acme"), slug="infra", name="Infra"
    )
    error = refusal(lambda: create(db, box_of(client)))
    assert error.code is ErrorCode.CONFLICT
    assert forge.seen == []


def test_only_an_organization_admin_creates_a_repository(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    github(forge)
    error = refusal(lambda: create(db, box_of(client), "bob"))
    assert error.code in (ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND)
    assert forge.seen == []


# --- Web ----------------------------------------------------------------------------------------


def form(token: str, **fields: str) -> dict[str, str]:
    return {
        "csrf": token,
        "slug": "infra",
        "name": "Infra",
        "branch": "main",
        "repo_mode": "create",
        "provider": "github",
        "repo_owner": "",
        "repo_name": "",
        "repo_token": TOKEN,
        **fields,
    }


def test_new_space_form_offers_to_create_the_repository(client: TestClient, acme: None) -> None:
    login(client, "carla")
    page = client.get("/o/acme/-/new-space").text
    assert "Créer le dépôt pour moi sur GitHub ou GitLab" in page
    assert 'name="repo_token" type="password" autocomplete="new-password"' in page
    assert "Dépôt créé par Synaptane" in page and "Dépôt existant" in page  # without JS
    assert 'data-repo-provider="gitlab"' in page


def test_web_creation(client: TestClient, database: Database, acme: None, forge: Forge) -> None:
    github(forge)
    login(client, "carla")
    response = client.post(
        "/o/acme/-/spaces", data=form(csrf(client, "/o/acme/-/new-space")), follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/o/acme/infra/-/git"
    assert TOKEN not in json.dumps(dict(response.headers))
    page = client.get("/o/acme/infra/-/git").text
    assert flash(page) == (
        "ok",
        "Espace créé, et son dépôt privé https://github.com/carla-gh/infra créé sur GitHub : "
        "Synaptane le prépare.",
    )
    assert TOKEN not in page
    with database.session() as session:
        assert_no_token_kept(session, box_of(client))
        space = session.scalar(select(Space))
        assert space is not None and space.repo_url == "git@github.com:carla-gh/infra.git"
    journal = client.get("/o/acme/-/journal").text
    assert "Dépôt Git créé par Synaptane" in journal
    assert "github.com/carla-gh/infra" in journal and "Clé de déploiement SSH" in journal


def test_web_creation_gitlab_mentions_the_token_expiry(
    client: TestClient, acme: None, forge: Forge
) -> None:
    gitlab(forge)
    login(client, "carla")
    data = form(
        csrf(client, "/o/acme/-/new-space"),
        provider="gitlab",
        repo_owner="acme/team",
        gitlab_url="",
    )
    client.post("/o/acme/-/spaces", data=data, follow_redirects=False)
    message = flash(client.get("/o/acme/infra/-/git").text)
    assert message is not None and message[0] == "ok"
    assert "expire le 8 novembre 2027" in message[1]
    page = client.get("/o/acme/infra/-/git").text
    assert "expire le 8 novembre 2027" in page and 'data-test="git-token-expiry"' in page


def test_web_creation_without_access(
    client: TestClient, database: Database, acme: None, forge: Forge
) -> None:
    github(forge)
    forge.routes[("POST", "/repos/carla-gh/infra/keys")] = httpx2.Response(500, json={})
    login(client, "carla")
    client.post(
        "/o/acme/-/spaces", data=form(csrf(client, "/o/acme/-/new-space")), follow_redirects=False
    )
    message = flash(client.get("/o/acme/infra/-/git").text)
    assert message is not None and message[0] == "warning"
    assert "https://github.com/carla-gh/infra" in message[1]
    assert "GitHub est injoignable" in message[1]
    assert "Le dépôt n'a pas été supprimé" in message[1]


def test_web_refusal_comes_back_to_the_form_translated(
    client: TestClient, database: Database, acme: None, forge: Forge
) -> None:
    github(forge)
    forge.routes[("GET", "/user")] = httpx2.Response(401, json={"message": "Bad credentials"})
    login(client, "carla")
    response = client.post(
        "/o/acme/-/spaces", data=form(csrf(client, "/o/acme/-/new-space")), follow_redirects=False
    )
    assert response.headers["location"] == "/o/acme/-/new-space"
    page = client.get("/o/acme/-/new-space").text
    assert flash(page) == (
        "error",
        "GitHub a refusé le jeton d'accès : il est invalide, expiré ou révoqué.",
    )
    assert TOKEN not in page
    with database.session() as session:
        assert session.scalar(select(Space)) is None


def test_web_refuses_without_csrf_or_as_a_member(
    client: TestClient, database: Database, acme: None, forge: Forge
) -> None:
    github(forge)
    login(client, "carla")
    response = client.post("/o/acme/-/spaces", data=form("forged"), follow_redirects=False)
    assert response.status_code == 403
    client.cookies.clear()
    login(client, "bob")
    response = client.post("/o/acme/-/spaces", data=form(csrf(client)), follow_redirects=False)
    assert response.status_code in (303, 403, 404)
    assert forge.seen == []
    with database.session() as session:
        assert session.scalar(select(Space)) is None


# --- REST API ----------------------------------------------------------------------------------


def body(**repository: str) -> dict[str, Any]:
    return {
        "slug": "infra",
        "name": "Infra",
        "create_repository": {"provider": "github", "token": TOKEN, **repository},
    }


def test_api_creation(client: TestClient, database: Database, acme: None, forge: Forge) -> None:
    github(forge)
    headers = {"Authorization": f"Bearer {token_for(database, 'carla')}"}
    response = client.post("/api/v1/organizations/acme/spaces", json=body(), headers=headers)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["repo_url"] == "git@github.com:carla-gh/infra.git"
    assert data["repository"] == {
        "provider": "github",
        "host": "github.com",
        "path": "carla-gh/infra",
        "url": "https://github.com/carla-gh/infra",
        "access": "ssh",
        "access_error": None,
        "token_expires": None,
    }
    assert TOKEN not in response.text
    plain = client.post(
        "/api/v1/organizations/acme/spaces",
        json={"slug": "notes", "name": "Notes"},
        headers=headers,
    )
    assert plain.status_code == 201 and plain.json()["repository"] is None


def test_api_creation_without_access_says_why_in_the_callers_language(
    client: TestClient, database: Database, acme: None, forge: Forge
) -> None:
    gitlab(forge)
    forge.routes[("POST", "/api/v4/projects/42/access_tokens")] = httpx2.Response(403, json={})
    headers = {
        "Authorization": f"Bearer {token_for(database, 'carla')}",
        "Accept-Language": "fr",
    }
    response = client.post(
        "/api/v1/organizations/acme/spaces",
        json=body(provider="gitlab", owner="acme/team"),
        headers=headers,
    )
    assert response.status_code == 201
    repository = response.json()["repository"]
    assert repository["access"] is None
    assert repository["access_error"].startswith("GitLab a refusé de créer un jeton")


def test_api_refusals(client: TestClient, database: Database, acme: None, forge: Forge) -> None:
    github(forge)
    forge.routes[("POST", "/user/repos")] = httpx2.Response(
        422, json={"errors": [{"message": "name already exists on this account"}]}
    )
    carla = {"Authorization": f"Bearer {token_for(database, 'carla')}"}
    url = "/api/v1/organizations/acme/spaces"
    taken = client.post(url, json=body(), headers=carla)
    assert taken.status_code == 409 and taken.json()["error"] == "conflict"
    assert TOKEN not in taken.text
    both = client.post(url, json={**body(), "repo_url": "https://x.example/r.git"}, headers=carla)
    assert both.status_code == 400 and "not both" in both.json()["message"]
    malformed = client.post(
        url, json={**body(), "create_repository": {"token": TOKEN}}, headers=carla
    )
    assert malformed.status_code == 400 and TOKEN not in malformed.text  # validated by the service
    forge.seen.clear()
    bob = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    assert client.post(url, json=body(), headers=bob).status_code == 403
    assert forge.seen == []
    with database.session() as session:
        assert session.scalar(select(Space)) is None


# --- Second review ------------------------------------------------------------------------------


def test_no_database_transaction_held_during_the_calls(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    """A slow provider must not keep a connection of the pool "idle in transaction"."""
    github(forge)
    during: list[bool] = []
    handle = forge.handle

    def watching(request: httpx2.Request) -> httpx2.Response:
        during.append(db.in_transaction())
        return handle(request)

    forge.handle = watching  # type: ignore[method-assign]
    monkeypatch_transport(forge)
    create(db, box_of(client))
    assert during == [False, False, False, False]
    assert db.scalar(select(Space.slug)) == "infra"  # created, and committed


def monkeypatch_transport(forge: Forge) -> None:
    forges.TRANSPORT = httpx2.MockTransport(forge.handle)  # the fixture restores it


def test_gitlab_namespace_found_by_search_behind_a_decoding_proxy(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    gitlab(forge)
    del forge.routes[("GET", "/api/v4/namespaces/acme%2Fteam")]
    forge.routes[("GET", "/api/v4/namespaces?search=team")] = httpx2.Response(
        200,
        json=[
            {"id": 99, "full_path": "other/team"},
            {"id": 12, "full_path": "Acme/Team", "plan": "premium"},
        ],
    )
    create(db, box_of(client), provider="gitlab", owner="acme/team")
    assert forge.body("POST", "/api/v4/projects")["namespace_id"] == 12


def test_gitlab_search_without_the_exact_path_is_not_found(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    gitlab(forge)
    del forge.routes[("GET", "/api/v4/namespaces/acme%2Fteam")]
    forge.routes[("GET", "/api/v4/namespaces?search=team")] = httpx2.Response(
        200, json=[{"id": 99, "full_path": "other/team"}]
    )
    error = refusal(lambda: create(db, box_of(client), provider="gitlab", owner="acme/team"))
    assert error.message.startswith("Namespace not found on GitLab")


def test_gitlab_com_free_plan_is_refused_before_anything_is_created(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    gitlab(forge, namespace=False)
    forge.routes[("GET", "/api/v4/namespaces/carla")] = httpx2.Response(
        200, json={"id": 3, "kind": "user", "full_path": "carla", "plan": "free"}
    )
    error = refusal(lambda: create(db, box_of(client), provider="gitlab"))
    assert "free plan" in error.message and "Use an existing repository" in error.message
    assert "POST /api/v4/projects" not in forge.paths()


def test_self_hosted_gitlab_plan_is_not_checked(
    db: Session, client: TestClient, acme: None, forge: Forge
) -> None:
    gitlab(forge, namespace=False)
    forge.routes[("GET", "/api/v4/namespaces/carla")] = httpx2.Response(
        200, json={"id": 3, "full_path": "carla", "plan": "free"}
    )
    _, created = create(
        db, box_of(client), provider="gitlab", gitlab_url="https://git.lan", allow_private=True
    )
    assert created.credentials is not None


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        # Projects::CreateService#validate_create_permissions
        ({"message": {"namespace": ["is not valid"]}}, "role allowed to create projects there"),
        (
            {"message": {"limit_reached": ["Personal project creation is not allowed."]}},
            "reached its limit of projects",
        ),
        (
            {"message": {"project_namespace.name": ["has already been taken"]}},
            "already exists in acme/team on GitLab",
        ),
        ({"message": {"path": ["can contain only letters"]}}, "refused the repository name"),
    ],
)
def test_gitlab_project_refusals_say_why(
    db: Session,
    client: TestClient,
    acme: None,
    forge: Forge,
    answer: dict[str, Any],
    message: str,
) -> None:
    gitlab(forge)
    forge.routes[("POST", "/api/v4/projects")] = httpx2.Response(400, json=answer)
    error = refusal(lambda: create(db, box_of(client), provider="gitlab", owner="acme/team"))
    assert message in error.message, error.message


@pytest.mark.parametrize(
    ("gitlab_url", "answer", "message"),
    [
        (
            "",
            httpx2.Response(
                400,
                json={
                    "message": "Access level of the token can't be greater the access level of "
                    "the user who created the token"
                },
            ),
            "Maintainer role",
        ),
        ("", httpx2.Response(403, json={"message": "403 Forbidden"}), "Premium"),
        (
            "https://git.lan",
            httpx2.Response(400, json={"message": {"expires_at": ["is too far"]}}),
            "GitLab refused to create the project access token: expires_at: is too far",
        ),
    ],
)
def test_gitlab_token_refusal_depends_on_the_answer_and_the_host(
    db: Session,
    client: TestClient,
    acme: None,
    forge: Forge,
    gitlab_url: str,
    answer: httpx2.Response,
    message: str,
) -> None:
    gitlab(forge)
    forge.routes[("POST", "/api/v4/projects/42/access_tokens")] = answer
    _, created = create(
        db,
        box_of(client),
        provider="gitlab",
        owner="acme/team",
        gitlab_url=gitlab_url,
        allow_private=True,
    )
    assert created.access_error is not None
    assert message in created.access_error.message, created.access_error.message
    if gitlab_url:
        assert "Premium" not in created.access_error.message


def test_slug_taken_meanwhile_reports_the_repository(
    db: Session, client: TestClient, acme: None, forge: Forge, monkeypatch: pytest.MonkeyPatch
) -> None:
    github(forge)

    def race(*_: object, **__: object) -> Space:
        raise IntegrityError("INSERT INTO spaces", {}, Exception("duplicate key"))

    monkeypatch.setattr(spaces, "create_space", race)
    error = refusal(lambda: create(db, box_of(client)))
    assert error.code is ErrorCode.CONFLICT
    assert "https://github.com/carla-gh/infra" in error.message
    assert "“infra” already exists" in error.message


def test_unexpected_failure_after_the_creation_reports_the_repository(
    db: Session,
    client: TestClient,
    acme: None,
    forge: Forge,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The purpose written to disk fails (`then`): nothing of the space is kept, the message and
    the server log give the repository's address, never the token."""
    github(forge)
    actor = actor_of(db, accounts.get_user(db, "carla"))
    org = organizations.get_organization(db, actor, "acme")

    def broken(_: Space) -> None:
        raise OSError("disk full")

    error = refusal(
        lambda: forges.create_space_with_repository(
            db,
            actor,
            box_of(client),
            org,
            forges.NewRepository("github", TOKEN),
            slug="infra",
            name="Infra",
            then=broken,
        )
    )
    assert error.code is ErrorCode.CONFLICT
    assert "https://github.com/carla-gh/infra" in error.message
    assert "Unexpected error" in error.message
    assert "https://github.com/carla-gh/infra" in caplog.text and TOKEN not in caplog.text
    assert db.scalar(select(Space)) is None
    assert "space.repository_created" not in events(db)


def test_web_existing_mode_requires_the_address(
    client: TestClient, database: Database, acme: None, forge: Forge
) -> None:
    """Without JavaScript, the address field is not marked required: the server checks it."""
    login(client, "carla")
    data = form(csrf(client, "/o/acme/-/new-space"), repo_mode="existing", repo_url=" ")
    response = client.post("/o/acme/-/spaces", data=data, follow_redirects=False)
    assert response.headers["location"] == "/o/acme/-/new-space"
    assert flash(client.get("/o/acme/-/new-space").text) == (
        "error",
        "Saisissez l'adresse du dépôt Git, ou choisissez de le faire créer.",
    )
    assert forge.seen == []
    with database.session() as session:
        assert session.scalar(select(Space)) is None


def test_web_purpose_is_kept_with_a_created_repository(
    client: TestClient, database: Database, acme: None, forge: Forge
) -> None:
    github(forge)
    login(client, "carla")
    data = form(csrf(client, "/o/acme/-/new-space"), vocation_template="job-search")
    client.post("/o/acme/-/spaces", data=data, follow_redirects=False)
    with database.session() as session:
        actions = set(session.scalars(select(AuditEvent.action)))
    assert {"space.repository_created", "space.vocation_updated"} <= actions


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        # `slug` missing: FastAPI's 422 would quote the whole body, token included.
        (
            "/api/v1/organizations/acme/spaces",
            {"name": "Infra", "create_repository": {"provider": "github", "token": TOKEN}},
        ),
        (
            "/api/v1/organizations/acme/spaces",
            {"slug": "x", "name": "X", "create_repository": TOKEN},
        ),
        # Another route with a secret: Git credentials without `kind`.
        ("/api/v1/organizations/acme/spaces/infra/git/credentials", {"token": TOKEN}),
    ],
)
def test_api_validation_errors_never_quote_the_values(
    client: TestClient, database: Database, acme: None, path: str, payload: dict[str, Any]
) -> None:
    headers = {"Authorization": f"Bearer {token_for(database, 'carla')}"}
    method = client.put if path.endswith("credentials") else client.post
    response = method(path, json=payload, headers=headers)
    assert response.status_code == 422
    assert TOKEN not in response.text
    detail = response.json()["detail"]
    assert detail and all(set(item) <= {"type", "loc", "msg"} for item in detail)
    assert all(item["loc"] and item["msg"] and item["type"] for item in detail)
