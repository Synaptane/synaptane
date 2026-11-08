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

Steps that can fail without consequence (token, owner, server keys) come first. Once the
repository exists, a later failure is reported with the repository's address: the space is
still created on it, and the admin enters its credentials by hand. A remote repository is
never deleted by Synaptane.

A self-hosted GitLab is only reached over HTTPS, at a public address (same check as the
webhooks: resolved once, then connected to the checked address), unless the account is the
super admin, who can reach a GitLab of the local network.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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
TIMEOUT = httpx2.Timeout(15.0, connect=5.0)
MAX_ANSWER = 1_000_000  # bytes read from an answer, at most
# Tests: simulated provider APIs (`httpx2.MockTransport`). Never a real call in the tests.
TRANSPORT: httpx2.BaseTransport | None = None

_HOST_KEY = re.compile(r"(?:ssh|ecdsa|sk)-[\w@.-]+ [A-Za-z0-9+/]+=*")
_REPO_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")
_GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_GITLAB_PART = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}"
_GITLAB_OWNER = re.compile(rf"{_GITLAB_PART}(?:/{_GITLAB_PART})*")
# Role of GitLab's project access token: Maintainer, allowed to push to the protected branch.
GITLAB_MAINTAINER = 40


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
    token_expires: date | None = None

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


class _Api:
    """Calls to a provider's API: no redirect followed (the token stays with this host), a
    timeout on each call, and for a self-hosted GitLab, the public address checked."""

    def __init__(
        self, provider: str, base: str, headers: Mapping[str, str], *, pin: bool, rights: Label
    ) -> None:
        self.name = PROVIDERS[provider]
        self.base, self.pin, self.rights = base, pin, rights
        self.headers = {"User-Agent": f"Synaptane/{__version__}", **headers}

    def call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        known: Mapping[int, Callable[[httpx2.Response], SynaptaneError]] | None = None,
        anonymous: bool = False,
        optional: bool = False,
    ) -> Any:
        """The decoded JSON answer. `known`: errors specific to this call, by status.
        `optional`: None instead of an error when the answer is not a success."""
        url, headers = self.base + path, dict(self.headers)
        extensions: dict[str, str] = {}
        if anonymous:
            headers = {"User-Agent": self.headers["User-Agent"], "Accept": "application/json"}
        if self.pin:
            try:
                url, host, extensions = pinned_request(url)
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
            with httpx2.Client(
                timeout=TIMEOUT, follow_redirects=False, transport=TRANSPORT
            ) as client:
                request = client.build_request(
                    method, url, json=body, headers=headers, extensions=extensions
                )
                response = _bounded(client.send(request, stream=True))
        except httpx2.HTTPError:
            raise unavailable(
                "{provider} cannot be reached: try again in a moment.", provider=self.name
            ) from None
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


def _bounded(response: httpx2.Response) -> httpx2.Response:
    """The answer read up to MAX_ANSWER bytes: a server that sends without end does not fill
    the memory. Beyond, an unreadable (empty) body."""
    content = b""
    try:
        for chunk in response.iter_bytes():
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


def _text(response: httpx2.Response) -> str:
    try:
        return json.dumps(response.json()).lower()
    except ValueError:
        return response.text.lower()


def _field(data: Any, key: str) -> Any:
    if not isinstance(data, dict) or key not in data:
        raise invalid("Unexpected answer from the Git provider.")
    return data[key]


def _taken(provider: str, owner: str, name: str) -> Callable[[httpx2.Response], SynaptaneError]:
    """The name is already taken (GitHub: 422, GitLab: 400), or refused."""

    def error(response: httpx2.Response) -> SynaptaneError:
        text = _text(response)
        if "already exists" in text or "has already been taken" in text:
            return conflict(
                "A repository named “{name}” already exists in {owner} on {provider}.",
                name=name,
                owner=owner,
                provider=PROVIDERS[provider],
            )
        return invalid(
            "{provider} refused the repository name “{name}”.",
            provider=PROVIDERS[provider],
            name=name,
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


def _github(new: NewRepository, key_title: str, description: str) -> CreatedRepository:
    api = _Api(
        "github",
        GITHUB_API,
        {
            "Authorization": f"Bearer {new.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        pin=False,
        rights=GITHUB_RIGHTS,
    )
    login = str(_field(api.call("GET", "/user"), "login"))
    # SSH keys of github.com, from its API over HTTPS: authenticated by TLS.
    keys = _field(api.call("GET", "/meta", anonymous=True), "ssh_keys")
    known_hosts = "\n".join(
        f"github.com {k}" for k in keys if isinstance(k, str) and _HOST_KEY.fullmatch(k)
    )
    if not known_hosts:
        raise invalid("Unexpected answer from the Git provider.")
    private, public = _deploy_key()
    owner = new.owner or login
    path = "/user/repos" if owner.lower() == login.lower() else f"/orgs/{quote(owner)}/repos"
    body = {"name": new.name, "private": True, "auto_init": False, "description": description}
    known = {404: _owner_not_found("github", owner), 422: _taken("github", owner, new.name)}
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


def _gitlab(
    new: NewRepository, key_title: str, description: str, *, allow_private: bool
) -> CreatedRepository:
    base = new.gitlab_url
    host = urlsplit(base).netloc
    api = _Api(
        "gitlab",
        base + "/api/v4",
        {"PRIVATE-TOKEN": new.token, "Accept": "application/json"},
        pin=base != GITLAB_COM and not allow_private,
        rights=GITLAB_RIGHTS,
    )
    username = str(_field(api.call("GET", "/user"), "username"))
    # Scopes of the token, when GitLab tells them: no project created with a token that
    # could not finish the job.
    scopes = api.call("GET", "/personal_access_tokens/self", optional=True)
    if isinstance(scopes, dict) and "api" not in (scopes.get("scopes") or []):
        raise invalid(GITLAB_RIGHTS)
    owner = new.owner or username
    body: dict[str, Any] = {
        "name": new.name,
        "path": new.name,
        "visibility": "private",
        "initialize_with_readme": False,
        "description": description,
    }
    if owner.lower() != username.lower():
        namespace = api.call(
            "GET",
            f"/namespaces/{quote(owner, safe='')}",
            known={404: _owner_not_found("gitlab", owner)},
        )
        body["namespace_id"] = _field(namespace, "id")
    project = api.call("POST", "/projects", body, known={400: _taken("gitlab", owner, new.name)})
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

    def refused(_: httpx2.Response) -> SynaptaneError:
        return invalid(
            "GitLab refused to create a project access token. On GitLab.com, they require a "
            "Premium or Ultimate subscription."
        )

    try:
        answer = api.call(
            "POST",
            f"/projects/{_field(project, 'id')}/access_tokens",
            token_body,
            known={400: refused, 403: refused, 404: refused},
        )
        token = GitCredentials.https(str(_field(answer, "token")))
    except SynaptaneError as error:
        return replace(created, access_error=error)
    expires = answer.get("expires_at") if isinstance(answer, dict) else None
    try:
        expiry = date.fromisoformat(expires) if isinstance(expires, str) else None
    except ValueError:
        expiry = None
    return replace(created, credentials=token, token_expires=expiry)


def create_repository(
    new: NewRepository, *, key_title: str, description: str = "", allow_private: bool = False
) -> CreatedRepository:
    """Creates the private repository and the credentials Synaptane keeps (see the module).
    `new` must have gone through `_check`. Raises a business error if nothing was created."""
    description = " ".join(description.split())[:250]
    if new.provider == "github":
        return _github(new, key_title, description)
    return _gitlab(new, key_title, description, allow_private=allow_private)


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
) -> tuple[Space, CreatedRepository]:
    """A new space and its repository, created by Synaptane on GitHub or GitLab (organization
    admin). Everything is checked before the first call; if the repository is created but its
    credentials cannot be set up, the space is still created on it (`access_error`).

    `allow_private`: a self-hosted GitLab may be on the local network (development, or the
    super admin)."""
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    slug = spaces.check_new_space(session, actor, org, slug=slug, name=name, branch=branch)
    new = _check(new, slug)
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
    except SynaptaneError as error:
        raise conflict(
            "The repository {url} was created on {provider}, but not the space: {reason} "
            "The repository was not deleted.",
            url=created.web_url,
            provider=created.provider_name,
            reason=Label(error.template, **error.params),
        ) from None
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
    return space, created
