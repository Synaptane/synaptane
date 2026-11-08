"""Creating the Git repository of a new space on GitHub or GitLab (docs/admin/git.md).

The admin gives an access token that can create a repository. It is used during the request
only, then forgotten: never stored, logged, audited nor returned. Synaptane keeps narrower
credentials, limited to the new repository:

- **GitHub**: an SSH deploy key with write access, generated here and registered on the
  repository. A personal token cannot be narrowed to a repository that does not exist yet,
  and the one that creates repositories can administer all the owner's repositories. The
  server's SSH keys come from GitHub's API (`/meta`), over HTTPS: nothing to confirm by hand.
- **GitLab**: a project access token (`read_repository` and `write_repository`, Maintainer
  role, to push to the protected default branch), created on the new project. A deploy key
  could not push to the default branch, which GitLab protects on the first push.

Steps that can fail without consequence (token, owner, plan, server keys) come first. Once
the repository exists, a later failure is reported with the repository's address: the space is
still created on it when possible, and the admin enters its credentials by hand. A remote
repository is never deleted by Synaptane.

Every call has a deadline that covers the whole exchange (connection, headers and body: a
server answering one byte per second is cut off), and the whole creation has one too. The
database connection is given back during the calls: a slow provider holds no connection.

A self-hosted GitLab is only reached over HTTPS, at a public address (same check as the
webhooks: resolved once, then connected to the checked address), unless the account is the
super admin, who can reach a GitLab of the local network.

An HTTPS token an admin saves by hand has its expiry date read from its provider
(`save_https_token`), with the same protections: one call, `EXPIRY_SECONDS` at most, to the
repository's own host (GitHub: api.github.com), never redirected. Unknown, it is simply not
kept: saving the token never depends on it.
"""

import json
import logging
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpcore2
import httpx2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from synaptane import __version__
from synaptane.core.access import Action, require
from synaptane.core.errors import (
    Label,
    SynaptaneError,
    conflict,
    invalid,
    too_many,
    unavailable,
)
from synaptane.core.security import SecretBox
from synaptane.git.credentials import GitCredentials
from synaptane.services import Actor, audit, git_sync, spaces
from synaptane.services.webhooks import DeliveryError, pinned_request
from synaptane.storage.models import Organization, Space

PROVIDERS = {"github": "GitHub", "gitlab": "GitLab"}
GITHUB_API = "https://api.github.com"
GITLAB_COM = "https://gitlab.com"
TIMEOUT = httpx2.Timeout(15.0, connect=5.0)  # each network operation
CALL_SECONDS = 20.0  # one call, from the connection to the last byte of the answer
OPERATION_SECONDS = 60.0  # the whole creation, every call included
MAX_ANSWER = 1_000_000  # bytes read from an answer, at most
SEARCH_PAGES = 5  # pages of 100 namespaces read, at most, when searching one
EXPIRY_SECONDS = 10.0  # reading the expiry of a token saved by hand: the admin waits
# Tests: simulated provider APIs (`httpx2.MockTransport`). Never a real call in the tests.
TRANSPORT: httpx2.BaseTransport | None = None

_HOST_KEY = re.compile(r"(?:ssh|ecdsa|sk)-[\w@.-]+ [A-Za-z0-9+/]+=*")
_REPO_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")
_GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_GITLAB_PART = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}"
_GITLAB_OWNER = re.compile(rf"{_GITLAB_PART}(?:/{_GITLAB_PART})*")
# Role of GitLab's project access token: Maintainer, allowed to push to the protected branch.
GITLAB_MAINTAINER = 40

log = logging.getLogger(__name__)
# Accounts creating a repository right now: one creation at a time per account (per server
# process), so a double submission or a script does not start several slow creations.
_CREATING: set[object] = set()
_CREATING_LOCK = threading.Lock()


@dataclass(frozen=True)
class NewRepository:
    """What the admin asks for. `owner`: account or organization (GitHub), user or group path
    (GitLab), "" for the token's own account. `name`: "" for the space's identifier.
    `gitlab_url`: address of a self-hosted GitLab, "" for gitlab.com."""

    provider: str
    token: str = field(repr=False)
    owner: str = ""
    name: str = ""
    gitlab_url: str = ""


@dataclass(frozen=True)
class CreatedRepository:
    """The repository created. `credentials`: what Synaptane keeps for the sync (None if
    setting them up failed: `access_error` says why)."""

    provider: str
    host: str
    path: str
    repo_url: str
    web_url: str
    credentials: GitCredentials | None = field(default=None, repr=False)
    access_error: SynaptaneError | None = None

    @property
    def token_expires(self) -> date | None:
        """Expiry of GitLab's project access token (None: none, or not GitLab)."""
        return self.credentials.expiry if self.credentials else None

    @property
    def provider_name(self) -> str:
        return PROVIDERS[self.provider]


# --- Validation (before any call) -------------------------------------------------------------


def gitlab_base(value: str) -> str:
    """Address of the GitLab server: https only, without credentials, query or fragment."""
    value = value.strip().rstrip("/") or GITLAB_COM
    parts = urlsplit(value)
    try:
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        port_ok = False
    if (
        parts.scheme != "https"
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or not port_ok
        or any(c.isspace() for c in value)
    ):
        raise invalid(
            "Invalid GitLab address: an https:// address is expected, such as "
            "https://gitlab.example.com."
        )
    return parts._replace(netloc=parts.netloc.lower()).geturl()


def _check(new: NewRepository, default_name: str) -> NewRepository:
    if new.provider not in PROVIDERS:
        raise invalid("Choose GitHub or GitLab to create the repository.")
    token = new.token.strip()
    if not token or any(c.isspace() or ord(c) < 32 for c in token):
        raise invalid("Enter the access token that can create the repository.")
    name = new.name.strip() or default_name
    if not _REPO_NAME.fullmatch(name) or name.lower().endswith((".git", ".atom")):
        raise invalid(
            "Invalid repository name: letters, digits, “.”, “-” and “_”, 100 characters at most."
        )
    owner = new.owner.strip().strip("/")
    pattern = _GITHUB_OWNER if new.provider == "github" else _GITLAB_OWNER
    if owner and not pattern.fullmatch(owner):
        raise invalid("Invalid owner: the account, organization or group as written in its URL.")
    base = gitlab_base(new.gitlab_url) if new.provider == "gitlab" else ""
    return NewRepository(new.provider, token, owner, name, base)


# --- HTTP calls -------------------------------------------------------------------------------


class Deadline:
    """Moment (monotonic clock) after which the current call is abandoned: the call's own
    deadline, never later than the whole operation's."""

    def __init__(self, seconds: float = OPERATION_SECONDS) -> None:
        self.operation = time.monotonic() + seconds
        self.at = self.operation

    def start_call(self) -> None:
        self.at = min(time.monotonic() + CALL_SECONDS, self.operation)

    def left(self, timeout: float | None, error: type[Exception]) -> float:
        remaining = self.at - time.monotonic()
        if remaining <= 0:
            raise error("deadline reached")
        return remaining if timeout is None else min(timeout, remaining)


class _DeadlineStream(httpcore2.NetworkStream):
    """A connection whose every read and write waits no later than the deadline."""

    def __init__(self, stream: httpcore2.NetworkStream, deadline: Deadline) -> None:
        self.stream, self.deadline = stream, deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self.stream.read(max_bytes, self.deadline.left(timeout, httpcore2.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.stream.write(buffer, self.deadline.left(timeout, httpcore2.WriteTimeout))

    def close(self) -> None:
        self.stream.close()

    def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.NetworkStream:
        wait = self.deadline.left(timeout, httpcore2.ConnectTimeout)
        return _DeadlineStream(
            self.stream.start_tls(ssl_context, server_hostname, wait), self.deadline
        )

    def get_extra_info(self, info: str) -> Any:
        return self.stream.get_extra_info(info)


# Name resolution has no timeout of its own: it runs here, waited for no later than the
# deadline. A slow DNS server is left behind; its thread ends with the resolver's own timeout,
# and at most 4 of them run at once (beyond, the wait itself reaches the deadline).
_RESOLVER = ThreadPoolExecutor(max_workers=4, thread_name_prefix="synaptane-dns")


def _within[T](deadline: Deadline, call: Callable[..., T], *args: Any) -> T:
    """`call(*args)` (a name resolution), given up at the deadline: TimeoutError."""
    future = _RESOLVER.submit(call, *args)
    try:
        return future.result(timeout=deadline.left(None, TimeoutError))
    except TimeoutError:
        future.cancel()
        raise


class _DeadlineBackend(httpcore2.NetworkBackend):
    def __init__(self, deadline: Deadline) -> None:
        self.backend, self.deadline = httpcore2.SyncBackend(), deadline

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore2.NetworkStream:
        """Resolves `host` within the deadline, then connects to its addresses in turn (the
        TLS certificate is still checked against the name, not the address)."""
        try:
            infos = _within(self.deadline, socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        except TimeoutError:
            raise httpcore2.ConnectTimeout("name resolution too slow") from None
        except OSError as error:
            raise httpcore2.ConnectError(str(error)) from None
        failure: Exception = httpcore2.ConnectError(f"no address for {host}")
        for *_, address in infos:
            wait = self.deadline.left(timeout, httpcore2.ConnectTimeout)
            try:
                stream = self.backend.connect_tcp(
                    str(address[0]), port, wait, local_address, socket_options
                )
            except httpcore2.ConnectError as error:
                failure = error
                continue
            return _DeadlineStream(stream, self.deadline)
        raise failure

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore2.NetworkStream:
        raise httpcore2.ConnectError("no Unix socket")

    def sleep(self, seconds: float) -> None:
        self.backend.sleep(seconds)


def client(deadline: Deadline) -> httpx2.Client:
    """HTTP client bound to `deadline`: proxies from the environment kept, no redirect
    followed (the token stays with this host)."""
    found = httpx2.Client(timeout=TIMEOUT, follow_redirects=False, transport=TRANSPORT)
    if TRANSPORT is None:
        backend = _DeadlineBackend(deadline)
        transports = [found._transport, *found._mounts.values()]
        for transport in transports:
            if isinstance(transport, httpx2.HTTPTransport):
                transport._pool._network_backend = backend
    return found


class _Api:
    """Calls to a provider's API: a deadline on each call, and for a self-hosted GitLab, the
    public address checked."""

    def __init__(
        self,
        provider: str,
        base: str,
        headers: Mapping[str, str],
        *,
        pin: bool,
        rights: Label,
        deadline: Deadline,
    ) -> None:
        self.name = PROVIDERS[provider]
        self.base, self.pin, self.rights, self.deadline = base, pin, rights, deadline
        self.headers = {"User-Agent": f"Synaptane/{__version__}", **headers}
        self.last_headers = httpx2.Headers()  # of the last answer (pagination)

    def call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        known: Mapping[int, Callable[[httpx2.Response], SynaptaneError]] | None = None,
        optional: bool = False,
    ) -> Any:
        """The decoded JSON answer. `known`: errors specific to this call, by status.
        `optional`: None instead of an error when the answer is not a success."""
        url, headers = self.base + path, dict(self.headers)
        extensions: dict[str, str] = {}
        self.deadline.start_call()
        if self.pin:
            try:
                url, host, extensions = _within(self.deadline, pinned_request, url)
            except TimeoutError:
                raise self.too_slow() from None
            except DeliveryError as error:
                host_name = urlsplit(self.base).hostname or ""
                if error.transient:
                    raise invalid("GitLab server not found: {host}.", host=host_name) from None
                raise invalid(
                    "This GitLab address is on a private or local network: only the super "
                    "admin can use it."
                ) from None
            headers |= host
        try:
            with client(self.deadline) as http:
                request = http.build_request(
                    method, url, json=body, headers=headers, extensions=extensions
                )
                response = _bounded(http.send(request, stream=True), self.deadline)
        except (httpx2.TimeoutException, TimeoutError):
            raise self.too_slow() from None
        except httpx2.HTTPError as error:
            if _certificate_refused(error):
                raise invalid(
                    "{provider} presented a TLS certificate that was refused (expired, "
                    "self-signed or for another name): check the address, or the certificate "
                    "authorities trusted by the server.",
                    provider=self.name,
                ) from None
            raise unavailable(
                "{provider} cannot be reached: try again in a moment.", provider=self.name
            ) from None
        self.last_headers = response.headers
        if response.status_code in (200, 201):
            try:
                return response.json()
            except ValueError:
                raise self.unexpected(response) from None
        if optional:
            return None
        if known and response.status_code in known:
            raise known[response.status_code](response)
        raise self.failure(response)

    def too_slow(self) -> SynaptaneError:
        return unavailable(
            "{provider} did not answer in time: try again in a moment.", provider=self.name
        )

    def failure(self, response: httpx2.Response) -> SynaptaneError:
        status = response.status_code
        limited = response.headers.get("x-ratelimit-remaining") == "0" or (
            "rate limit" in response.text.lower()
        )
        if status == 429 or (status == 403 and limited):
            return too_many(
                "{provider} limits the number of requests: try again in a few minutes.",
                provider=self.name,
            )
        if status == 401:
            return invalid(
                "{provider} refused the access token: it is invalid, expired or revoked.",
                provider=self.name,
            )
        if status == 403:
            return invalid(self.rights)
        if status >= 500:
            return unavailable(
                "{provider} cannot be reached: try again in a moment.", provider=self.name
            )
        return self.unexpected(response)

    def unexpected(self, response: httpx2.Response) -> SynaptaneError:
        return invalid(
            "Unexpected answer from {provider} (HTTP {status}).",
            provider=self.name,
            status=response.status_code,
        )


def _bounded(response: httpx2.Response, deadline: Deadline) -> httpx2.Response:
    """The answer read up to MAX_ANSWER bytes and before the deadline: a server that sends
    without end, or drop by drop, neither fills the memory nor holds the request. Beyond the
    size, an unreadable (empty) body."""
    content = b""
    try:
        for chunk in response.iter_bytes():
            deadline.left(None, TimeoutError)
            content += chunk
            if len(content) > MAX_ANSWER:
                content = b""
                break
    finally:
        response.close()
    kept = {
        k: v
        for k, v in response.headers.items()
        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
    }
    return httpx2.Response(
        response.status_code, headers=kept, content=content, request=response.request
    )


def _certificate_refused(error: BaseException) -> bool:
    """A TLS certificate refused somewhere in the chain of causes."""
    seen: BaseException | None = error
    for _ in range(10):
        if seen is None:
            return False
        if isinstance(seen, ssl.SSLCertVerificationError) or (
            "CERTIFICATE_VERIFY_FAILED" in str(seen)
        ):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _text(response: httpx2.Response) -> str:
    try:
        return json.dumps(response.json()).lower()
    except ValueError:
        return response.text.lower()


def _detail(response: httpx2.Response) -> str:
    """The provider's own explanation (`message`), on one line, 200 characters at most."""
    try:
        message = response.json().get("message", "")
    except (ValueError, AttributeError):
        message = ""
    if isinstance(message, dict):
        message = "; ".join(
            f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
            for k, v in message.items()
        )
    text = " ".join(str(message).split())
    return "".join(c for c in text if c.isprintable())[:200] or f"HTTP {response.status_code}"


def _field(data: Any, key: str) -> Any:
    if not isinstance(data, dict) or key not in data:
        raise invalid("Unexpected answer from the Git provider.")
    return data[key]


def _taken(owner: str, name: str) -> Callable[[httpx2.Response], SynaptaneError]:
    """GitHub's 422 on creation: the name is already taken, or refused."""

    def error(response: httpx2.Response) -> SynaptaneError:
        if "already exists" in _text(response):
            return conflict(
                "A repository named “{name}” already exists in {owner} on {provider}.",
                name=name,
                owner=owner,
                provider="GitHub",
            )
        return invalid(
            "{provider} refused the repository name “{name}”.", provider="GitHub", name=name
        )

    return error


def _gitlab_refused(owner: str, name: str) -> Callable[[httpx2.Response], SynaptaneError]:
    """GitLab's 400 on creation. `{"message": {"name": ["has already been taken"]}}`: name
    taken; `{"message": {"limit_reached": [...]}}`: personal project limit;
    `{"message": {"namespace": ["is not valid"]}}`: no right to create projects there."""

    def error(response: httpx2.Response) -> SynaptaneError:
        text = _text(response)
        if "has already been taken" in text:
            return conflict(
                "A repository named “{name}” already exists in {owner} on {provider}.",
                name=name,
                owner=owner,
                provider="GitLab",
            )
        if "limit" in text:
            return invalid(
                "GitLab refused the project: the account of the token reached its limit of "
                "projects. Free one, or ask the GitLab administrator to raise the limit."
            )
        if "namespace" in text:
            return invalid(
                "GitLab does not let the token create projects in “{owner}”: its account needs "
                "a role allowed to create projects there.",
                owner=owner,
            )
        return invalid(
            "{provider} refused the repository name “{name}”.", provider="GitLab", name=name
        )

    return error


def _owner_not_found(provider: str, owner: str) -> Callable[[httpx2.Response], SynaptaneError]:
    def error(_: httpx2.Response) -> SynaptaneError:
        if provider == "github":
            return invalid(
                "Owner not found on GitHub: “{owner}” must be your account or an organization "
                "the token can access.",
                owner=owner,
            )
        return invalid(
            "Namespace not found on GitLab: “{owner}” does not exist, or the token cannot see "
            "it (user or group path, such as acme/team).",
            owner=owner,
        )

    return error


def _deploy_key() -> tuple[str, str]:
    """(private key in OpenSSH format, public key) of a new Ed25519 key."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    return private, public.decode() + " synaptane"


GITHUB_RIGHTS = Label(
    "The GitHub token does not have the rights needed: a fine-grained token needs the "
    "Administration permission (read and write) on all the owner's repositories."
)
GITLAB_RIGHTS = Label(
    "The GitLab token does not have the rights needed: it needs the api scope, and a role "
    "allowed to create projects in the namespace."
)


def _github_api(token: str, deadline: Deadline) -> _Api:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _Api("github", GITHUB_API, headers, pin=False, rights=GITHUB_RIGHTS, deadline=deadline)


def github_known_hosts(token: str = "", deadline: Deadline | None = None) -> str:
    """`known_hosts` lines of github.com, from its API over HTTPS (authenticated by TLS: never
    trusted on first SSH connection). Raises a business error if GitHub does not give them."""
    api = _github_api(token, deadline or Deadline(CALL_SECONDS))
    keys = _field(api.call("GET", "/meta"), "ssh_keys")
    lines = (
        [f"github.com {k}" for k in keys if isinstance(k, str) and _HOST_KEY.fullmatch(k)]
        if isinstance(keys, list)
        else []
    )
    if not lines:
        raise invalid("Unexpected answer from the Git provider.")
    return "\n".join(lines) + "\n"


def _github(
    new: NewRepository, key_title: str, description: str, deadline: Deadline
) -> CreatedRepository:
    api = _github_api(new.token, deadline)
    login = str(_field(api.call("GET", "/user"), "login"))
    known_hosts = github_known_hosts(new.token, deadline)
    private, public = _deploy_key()
    owner = new.owner or login
    path = "/user/repos" if owner.lower() == login.lower() else f"/orgs/{quote(owner)}/repos"
    body = {"name": new.name, "private": True, "auto_init": False, "description": description}
    known = {404: _owner_not_found("github", owner), 422: _taken(owner, new.name)}
    repo = api.call("POST", path, body, known=known)
    full_name = str(_field(repo, "full_name"))
    created = CreatedRepository(
        provider="github",
        host="github.com",
        path=full_name,
        repo_url=f"git@github.com:{full_name}.git",
        web_url=str(_field(repo, "html_url")),
    )
    try:
        key = {"title": key_title, "key": public, "read_only": False}
        api.call("POST", f"/repos/{full_name}/keys", key)
    except SynaptaneError as error:
        return replace(created, access_error=error)
    return replace(created, credentials=GitCredentials.ssh(private, known_hosts))


def _namespace(api: _Api, owner: str) -> dict[str, Any]:
    """The GitLab namespace (user or group) at path `owner`. A proxy that decodes `%2F` makes
    `/namespaces/acme%2Fteam` unreachable: then the search, filtered on the exact path."""
    found = api.call("GET", f"/namespaces/{quote(owner, safe='')}", optional=True)
    if isinstance(found, dict) and "id" in found:
        return found
    last = quote(owner.rsplit("/", 1)[-1], safe="")
    not_found = _owner_not_found("gitlab", owner)
    page = "1"
    for _ in range(SEARCH_PAGES):
        results = api.call(
            "GET",
            f"/namespaces?search={last}&per_page=100&page={page}",
            known={404: not_found},
        )
        for candidate in results if isinstance(results, list) else []:
            if (
                isinstance(candidate, dict)
                and str(candidate.get("full_path", "")).lower() == owner.lower()
                and "id" in candidate
            ):
                return candidate
        page = api.last_headers.get("x-next-page", "")
        if not page.isdigit():
            break
    raise not_found(httpx2.Response(404))


def _token_refused(gitlab_com: bool) -> Callable[[httpx2.Response], SynaptaneError]:
    """Refusal to create the project access token, explained by the answer and the host."""

    def error(response: httpx2.Response) -> SynaptaneError:
        if "access level" in _text(response):
            return invalid(
                "GitLab refused to give the project access token the Maintainer role: the "
                "token's account needs at least this role on the new project."
            )
        if gitlab_com:
            return invalid(
                "GitLab refused to create a project access token. On GitLab.com, they require "
                "a Premium or Ultimate subscription."
            )
        return invalid(
            "GitLab refused to create the project access token: {detail}",
            detail=_detail(response),
        )

    return error


def _gitlab(
    new: NewRepository, key_title: str, description: str, *, allow_private: bool, deadline: Deadline
) -> CreatedRepository:
    base = new.gitlab_url
    gitlab_com = base == GITLAB_COM
    host = urlsplit(base).netloc
    api = _Api(
        "gitlab",
        base + "/api/v4",
        {"PRIVATE-TOKEN": new.token, "Accept": "application/json"},
        pin=not gitlab_com and not allow_private,
        rights=GITLAB_RIGHTS,
        deadline=deadline,
    )
    username = str(_field(api.call("GET", "/user"), "username"))
    # Scopes of the token, when GitLab tells them: no project created with a token that
    # could not finish the job.
    scopes = api.call("GET", "/personal_access_tokens/self", optional=True)
    if isinstance(scopes, dict) and "api" not in (scopes.get("scopes") or []):
        raise invalid(GITLAB_RIGHTS)
    owner = new.owner or username
    namespace = _namespace(api, owner)
    # GitLab.com gives the plan of the namespace: on the free plan, no project access token,
    # so no access for Synaptane. Refused before creating anything.
    if gitlab_com and namespace.get("plan") == "free":
        raise invalid(
            "On GitLab.com, Synaptane can only set up its access to a project of a Premium or "
            "Ultimate namespace (project access tokens), and “{owner}” is on the free plan. "
            "Create the repository yourself on GitLab, then choose “Use an existing "
            "repository”.",
            owner=owner,
        )
    body: dict[str, Any] = {
        "name": new.name,
        "path": new.name,
        "namespace_id": namespace["id"],
        "visibility": "private",
        "initialize_with_readme": False,
        "description": description,
    }
    project = api.call("POST", "/projects", body, known={400: _gitlab_refused(owner, new.name)})
    full_path = str(_field(project, "path_with_namespace"))
    created = CreatedRepository(
        provider="gitlab",
        host=host,
        path=full_path,
        repo_url=f"{base}/{full_path}.git",
        web_url=str(_field(project, "web_url")),
    )
    token_body = {
        "name": "Synaptane",
        "description": key_title,
        "scopes": ["read_repository", "write_repository"],
        "access_level": GITLAB_MAINTAINER,
    }
    refused = _token_refused(gitlab_com)
    try:
        answer = api.call(
            "POST",
            f"/projects/{_field(project, 'id')}/access_tokens",
            token_body,
            known={400: refused, 403: refused, 404: refused},
        )
        expires = answer.get("expires_at") if isinstance(answer, dict) else None
        try:
            expiry = date.fromisoformat(expires) if isinstance(expires, str) else None
        except ValueError:
            expiry = None
        token = GitCredentials.https(str(_field(answer, "token")), expiry=expiry)
    except SynaptaneError as error:
        return replace(created, access_error=error)
    return replace(created, credentials=token)


def create_repository(
    new: NewRepository,
    *,
    key_title: str,
    description: str = "",
    allow_private: bool = False,
    deadline: Deadline | None = None,
) -> CreatedRepository:
    """Creates the private repository and the credentials Synaptane keeps (see the module).
    `new` must have gone through `_check`. Raises a business error if nothing was created."""
    description = " ".join(description.split())[:250]
    deadline = deadline or Deadline()
    if new.provider == "github":
        return _github(new, key_title, description, deadline)
    return _gitlab(new, key_title, description, allow_private=allow_private, deadline=deadline)


# --- Expiry of a token saved by hand ------------------------------------------------------------


def _github_expiry(value: str) -> date | None:
    """`GitHub-Authentication-Token-Expiration: 2027-11-08 07:00:00 UTC` (or `+0100`): the
    day, in UTC. None for any other form."""
    value = value.strip().replace(" UTC", " +0000")
    try:
        moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None
    return moment.astimezone(UTC).date()


def _gitlab_expiry(answer: Any) -> date | None:
    expires = answer.get("expires_at") if isinstance(answer, dict) else None
    try:
        return date.fromisoformat(expires[:10]) if isinstance(expires, str) else None
    except ValueError:
        return None


def token_expiry(repo_url: str, token: str, *, allow_private: bool = False) -> date | None:
    """Expiry date of an HTTPS access token, read from the provider of `repo_url`:
    - github.com: the `GitHub-Authentication-Token-Expiration` header GitHub adds to any
      answer for an expiring token (`GET /rate_limit`, which counts against no limit);
    - any other host, taken for a GitLab: `GET /api/v4/personal_access_tokens/self` and its
      `expires_at` (personal, group and project access tokens). Another Git host answers 404.

    The token only goes to the repository's own host, which receives it for the sync anyway
    (GitHub: its API). None when the date is unknown: no expiry, refused (the API scope may be
    missing), another provider, unreachable, too slow, a private address (unless
    `allow_private`). Never raises, never logs the token."""
    parts = urlsplit(repo_url)
    try:
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return None
    deadline = Deadline(EXPIRY_SECONDS)
    try:
        if host == "github.com":
            api = _github_api(token, deadline)
            if api.call("GET", "/rate_limit", optional=True) is None:
                return None
            header = api.last_headers.get("github-authentication-token-expiration", "")
            return _github_expiry(header)
        base = f"https://{host}" + (f":{port}" if port else "")
        api = _Api(
            "gitlab",
            base + "/api/v4",
            {"PRIVATE-TOKEN": token, "Accept": "application/json"},
            pin=base != GITLAB_COM and not allow_private,
            rights=GITLAB_RIGHTS,
            deadline=deadline,
        )
        return _gitlab_expiry(api.call("GET", "/personal_access_tokens/self", optional=True))
    except SynaptaneError as error:
        log.info("Expiry of the token for %s not read: %s", host, error.message)
        return None


def save_https_token(
    session: Session,
    actor: Actor,
    box: SecretBox,
    space: Space,
    token: str,
    username: str = "",
    *,
    allow_private: bool = False,
) -> GitCredentials:
    """An HTTPS token saved by an organization admin (web, REST), with its expiry date when the
    provider gives it (`token_expiry`): the Health page and the alerts then know it.

    Rights and the token's form are checked first, before any call; the transaction is
    committed before the call (no database connection held while the provider answers).
    `allow_private`: a host of the local network may be asked (development; always for the
    super admin)."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    credentials = GitCredentials.https(token, username)
    repo_url = space.repo_url or ""
    if repo_url and credentials.matches(repo_url):
        session.commit()
        expiry = token_expiry(
            repo_url, credentials.token, allow_private=allow_private or actor.access.is_superadmin
        )
        if expiry is not None:
            credentials = replace(credentials, expires=expiry.isoformat())
    git_sync.set_credentials(session, actor, box, space, credentials)
    return credentials


# --- Use case ---------------------------------------------------------------------------------


def create_space_with_repository(
    session: Session,
    actor: Actor,
    box: SecretBox,
    org: Organization,
    new: NewRepository,
    *,
    slug: str,
    name: str,
    description: str = "",
    branch: str = "main",
    restricted: bool = False,
    allow_private: bool = False,
    public_host: str = "",
    then: Callable[[Space], None] | None = None,
) -> tuple[Space, CreatedRepository]:
    """A new space and its repository, created by Synaptane on GitHub or GitLab (organization
    admin). Everything is checked before the first call; if the repository is created but its
    credentials cannot be set up, the space is still created on it (`access_error`).

    The transaction is committed before the calls (this request has written nothing yet: the
    connection goes back to the pool while the provider answers), then again once the space is
    created: `then` (the purpose, say) runs before that commit. Any failure after the remote
    creation is reported with the repository's address.

    `allow_private`: a self-hosted GitLab may be on the local network (development, or the
    super admin)."""
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    slug = spaces.check_new_space(session, actor, org, slug=slug, name=name, branch=branch)
    new = _check(new, slug)
    with _CREATING_LOCK:
        if actor.user.id in _CREATING:
            raise conflict(
                "A repository is already being created from your account: wait until it is "
                "done, then try again."
            )
        _CREATING.add(actor.user.id)
    try:
        return _create_space_with_repository(
            session,
            actor,
            box,
            org,
            new,
            slug=slug,
            name=name,
            description=description,
            branch=branch,
            restricted=restricted,
            allow_private=allow_private,
            public_host=public_host,
            then=then,
        )
    finally:
        with _CREATING_LOCK:
            _CREATING.discard(actor.user.id)


def _create_space_with_repository(
    session: Session,
    actor: Actor,
    box: SecretBox,
    org: Organization,
    new: NewRepository,
    *,
    slug: str,
    name: str,
    description: str,
    branch: str,
    restricted: bool,
    allow_private: bool,
    public_host: str,
    then: Callable[[Space], None] | None,
) -> tuple[Space, CreatedRepository]:
    session.commit()
    title = f"Synaptane ({public_host})" if public_host else "Synaptane"
    created = create_repository(
        new,
        key_title=title,
        description=description,
        allow_private=allow_private or actor.access.is_superadmin,
    )
    try:
        space = spaces.create_space(
            session,
            actor,
            org,
            slug=slug,
            name=name,
            description=description,
            repo_url=created.repo_url,
            branch=branch,
            restricted=restricted,
        )
        audit(
            session,
            actor,
            "space.repository_created",
            space=space,
            provider=created.provider,
            host=created.host,
            repository=created.path,
            access=created.credentials.kind if created.credentials else "none",
        )
        if created.credentials is not None:
            git_sync.set_credentials(session, actor, box, space, created.credentials)
        if then is not None:
            then(space)
        session.commit()
    except Exception as error:  # noqa: BLE001 — every failure here leaves a remote repository
        session.rollback()
        if isinstance(error, SynaptaneError):
            reason = Label(error.template, **error.params)
        elif isinstance(error, IntegrityError):  # the same identifier, created meanwhile
            reason = Label("The space “{slug}” already exists in this organization.", slug=slug)
        else:
            reason = Label("Unexpected error: see the server log.")
        log.warning(
            "Repository %s created on %s, but not the space %s: %s",
            created.web_url,
            created.provider_name,
            slug,
            type(error).__name__,
            exc_info=not isinstance(error, SynaptaneError | IntegrityError),
        )
        raise conflict(
            "The repository {address} was created on {provider}, but not the space: {reason} "
            "The repository was not deleted.",
            address=created.web_url,
            provider=created.provider_name,
            reason=reason,
        ) from None
    return space, created
