"""Outgoing webhooks of an organization (see docs/admin/webhooks.md).

Each event is delivered through the job queue (retried on a transient failure), as signed
JSON: `X-Synaptane-Signature: sha256=<HMAC-SHA256(secret, body)>`.

Events:
- `inbox.item_added`: a piece of information is deposited in a space's inbox;
- `content.changed`: a space's content changed (web, REST, MCP, tidy-up, Git push);
- `tidy.completed`: a tidy-up run finished (or failed);
- `sync.conflict`: a space is in conflict with its remote repository;
- `routine.done`: a routine run finished (or failed), with its output text, if the routine
  has the webhook among its outputs;
- `ping`: test delivery.

Deliveries have their own thread in the worker: a slow or silent receiver delays neither
synchronizations nor indexing. Circuit breaker: after BREAKER_FAILURES consecutive failures
spread over at least BREAKER_AFTER, the webhook is disabled and the organization's admins are
warned; it stays disabled until an admin re-enables it (webhooks page). A short outage of the
receiver therefore cuts nothing off.
"""

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx2
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane import __version__
from synaptane.core.access import Action, require
from synaptane.core.errors import invalid, not_found
from synaptane.core.security import SecretBox
from synaptane.services import Actor, audit, jobs
from synaptane.storage.db import Database
from synaptane.storage.models import Organization, Space, Webhook

EVENTS = (
    "inbox.item_added",
    "content.changed",
    "tidy.completed",
    "tidy.budget",
    "space.alert",
    "sync.conflict",
    "routine.done",
)
WEBHOOK = "webhook"
TIMEOUT = httpx2.Timeout(10.0, connect=5.0)
BREAKER_FAILURES = 10  # consecutive failures (each attempt counts)…
BREAKER_AFTER = timedelta(hours=1)  # … over at least one hour


def _context(webhook_id: UUID) -> str:
    return f"webhook:{webhook_id}:secret"


class DeliveryError(Exception):
    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


# --- Administration (organization admin) ------------------------------------------------------


def _check_url(url: str, allow_private: bool) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username:
        raise invalid("Invalid webhook URL.")
    if parts.scheme == "http" and not allow_private:
        raise invalid("The webhook must use HTTPS.")
    return url


def list_webhooks(session: Session, actor: Actor, org: Organization) -> list[Webhook]:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    return list(
        session.scalars(
            select(Webhook).where(Webhook.org_id == org.id).order_by(Webhook.created_at)
        )
    )


def create(
    session: Session,
    actor: Actor,
    box: SecretBox,
    org: Organization,
    url: str,
    events: list[str],
    *,
    allow_private: bool = False,
) -> tuple[Webhook, str]:
    """Returns the webhook and its secret (shown only once)."""
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    if not events or any(e not in EVENTS for e in events):
        raise invalid("Choose at least one known event.")
    webhook = Webhook(
        id=uuid4(), org_id=org.id, url=_check_url(url, allow_private), events=sorted(set(events))
    )
    secret = "whsec_" + secrets.token_urlsafe(32)
    webhook.secret = box.encrypt(secret, _context(webhook.id))
    session.add(webhook)
    audit(session, actor, "org.webhook_created", org_id=org.id, url=webhook.url)
    session.flush()
    return webhook, secret


def get(session: Session, actor: Actor, org: Organization, webhook_id: UUID) -> Webhook:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    webhook = session.get(Webhook, webhook_id)
    if webhook is None or webhook.org_id != org.id:
        raise not_found("Webhook not found.")
    return webhook


def delete(session: Session, actor: Actor, org: Organization, webhook_id: UUID) -> None:
    webhook = get(session, actor, org, webhook_id)
    session.delete(webhook)
    audit(session, actor, "org.webhook_deleted", org_id=org.id, url=webhook.url)


def reactivate(session: Session, actor: Actor, org: Organization, webhook_id: UUID) -> None:
    """Puts back into service a webhook cut off by the circuit breaker, counter reset. Events
    that occurred while it was cut off are not replayed."""
    webhook = get(session, actor, org, webhook_id)
    webhook.active, webhook.failures, webhook.failing_since = True, 0, None
    audit(session, actor, "org.webhook_reactivated", org_id=org.id, url=webhook.url)


def send_test(session: Session, actor: Actor, org: Organization, webhook_id: UUID) -> None:
    webhook = get(session, actor, org, webhook_id)
    _enqueue(session, webhook, "ping", org.slug, None, {"message": "Synaptane test"})


# --- Emission ---------------------------------------------------------------------------------


def _enqueue(
    session: Session,
    webhook: Webhook,
    event: str,
    org: str,
    space: str | None,
    data: dict[str, Any],
) -> None:
    body = {
        "id": str(uuid4()),
        "event": event,
        "occurred_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "organization": org,
        "space": space,
        "data": data,
    }
    jobs.enqueue(session, WEBHOOK, None, {"webhook_id": str(webhook.id), "body": body})


def emit(session: Session, space: Space, event: str, data: dict[str, Any]) -> None:
    """Queues one delivery per webhook of the organization subscribed to `event`."""
    targets = session.scalars(
        select(Webhook).where(Webhook.org_id == space.org_id, Webhook.active.is_(True))
    )
    address = space.address
    for webhook in targets:
        if event in webhook.events:
            _enqueue(session, webhook, event, space.organization.slug, address, data)


# --- Delivery (worker) ------------------------------------------------------------------------


def _public_host(host: str) -> str:
    """Refuses internal addresses (SSRF): loopback, private networks, link-local…

    Returns the checked address, which is then connected to: a second DNS resolution could
    answer something else ("DNS rebinding").
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise DeliveryError(f"Host name not found: {host}", transient=True) from None
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for address in addresses:
        if not address.is_global:
            raise DeliveryError(f"Internal address refused: {address}", transient=False)
    if not addresses:
        raise DeliveryError(f"Host name not found: {host}", transient=True)
    return str(addresses[0])


def pinned_request(url: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """(URL to the checked address, Host header, TLS extensions) for `url`."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    address = _public_host(host)
    netloc = f"[{address}]" if ":" in address else address
    if parts.port:
        netloc += f":{parts.port}"
    original = host if not parts.port else f"{host}:{parts.port}"
    extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
    return parts._replace(netloc=netloc).geturl(), {"Host": original}, extensions


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _record_failure(webhook: Webhook, now: datetime) -> bool:
    """Counts a failure; true if the circuit breaker has just cut the webhook off."""
    webhook.failures += 1
    webhook.failing_since = webhook.failing_since or now
    if (
        webhook.active
        and webhook.failures >= BREAKER_FAILURES
        and now - webhook.failing_since >= BREAKER_AFTER
    ):
        webhook.active = False
        return True
    return False


def deliver(
    database: Database,
    box: SecretBox,
    payload: dict[str, Any],
    *,
    allow_private: bool = False,
    public_url: str = "",
) -> None:
    with database.session() as session:
        webhook = session.get(Webhook, UUID(str(payload["webhook_id"])))
        if webhook is None or not webhook.active:
            return
        url, secret = webhook.url, box.decrypt(webhook.secret, _context(webhook.id))
    body = json.dumps(payload["body"], ensure_ascii=False).encode()
    status: str
    error: DeliveryError | None = None
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"Synaptane-Webhook/{__version__}",
        "X-Synaptane-Event": str(payload["body"]["event"]),
        "X-Synaptane-Delivery": str(payload["body"]["id"]),
        "X-Synaptane-Signature": sign(secret, body),
    }
    extensions: dict[str, str] = {}
    try:
        if not allow_private:
            url, host, extensions = pinned_request(url)
            headers |= host
        with httpx2.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            request = client.build_request(
                "POST", url, content=body, headers=headers, extensions=extensions
            )
            # Response never read: only the status matters (no huge body in memory).
            response = client.send(request, stream=True)
            response.close()
        status = f"{response.status_code}"
        if response.status_code >= 500 or response.status_code == 429:
            error = DeliveryError(f"Response {response.status_code}", transient=True)
        elif response.status_code >= 400:
            error = DeliveryError(f"Response {response.status_code}", transient=False)
    except httpx2.HTTPError as failure:
        status, error = (
            f"network error: {type(failure).__name__}",
            DeliveryError("Receiver unreachable.", transient=True),
        )
    except DeliveryError as failure:
        status, error = str(failure), failure
    with database.session() as session:
        # Row lock: two workers failing at the same time count two failures, and only one of
        # them cuts the webhook off (a single alert).
        found = session.get(Webhook, UUID(str(payload["webhook_id"])), with_for_update=True)
        if found is not None:
            now = datetime.now(UTC)
            found.last_delivery_at, found.last_status = now, status[:200]
            if error is None:
                found.failures, found.failing_since = 0, None
            elif _record_failure(found, now):
                from synaptane.services import alerts

                alerts.notify_org(
                    session,
                    box,
                    found.org_id,
                    "webhook_disabled",
                    public_url,
                    url=found.url,
                    failures=found.failures,
                    status=status[:200],
                )
                error = DeliveryError(
                    f"Webhook disabled after {found.failures} consecutive failures ({status}).",
                    transient=False,
                )
    if error is not None:
        raise error
