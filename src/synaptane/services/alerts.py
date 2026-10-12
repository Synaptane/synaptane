"""Alerts to the administrators of an organization: what needs their attention.

An alert goes out by email (if sending is configured), by webhook (`space.alert`) and to the
audit log. The same alert (kind and space) is not sent again before `QUIET`.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole
from synaptane.core.errors import SynaptaneError
from synaptane.core.security import SecretBox
from synaptane.services import audit, jobs, mail, webhooks
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Organization, OrgMember, Space, User

log = logging.getLogger("synaptane.alerts")
QUIET = timedelta(hours=20)
KINDS = (
    "deletion_cap",
    "tidy_failed",
    "sync_error",
    "inbox_backlog",
    "quota_high",
    "big_changes",
    "tidy_interrupted",
    "job_abandoned",
    "routine_failed",
)


def notify(
    session: Session,
    box: SecretBox,
    space: Space,
    kind: str,
    public_url: str = "",
    **values: object,
) -> bool:
    """Notifies the admins of the organization of `space`. False if notified recently."""
    assert kind in KINDS
    action = f"alert.{kind}"
    recent = session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.action == action,
            AuditEvent.space_id == space.id,
            AuditEvent.at > datetime.now(UTC) - QUIET,
        )
    )
    if recent is not None:
        return False
    details = {k: str(mail.localized(v, "en")) for k, v in values.items()}  # log: English
    audit(session, None, action, space=space, **details)
    webhooks.emit(session, space, "space.alert", {"kind": kind, **details})
    if not mail.configured(session):
        return True
    org = space.organization
    admins = session.scalars(
        select(User)
        .join(OrgMember, OrgMember.user_id == User.id)
        .where(
            OrgMember.org_id == org.id,
            OrgMember.role == OrgRole.ADMIN.value,
            User.is_active.is_(True),
            User.email.is_not(None),
        )
    )
    link = f"{public_url}/o/{org.slug}/{space.slug}"
    for admin in admins:
        subject, body = mail.compose(
            f"alert_{kind}", admin.locale, space=space.name, org=org.name, link=link, **values
        )
        try:
            mail.send(session, box, admin.email or "", subject, body)
        except mail.MailError as error:
            log.warning("Alert %s not sent to %s: %s", kind, admin.email, error)
    return True


# Organization alerts, without a space: each one matches a transition (a webhook cut off by
# the circuit breaker), which does not repeat without an admin's action.
ORG_KINDS = ("webhook_disabled",)


def notify_org(
    session: Session,
    box: SecretBox,
    org_id: UUID,
    kind: str,
    public_url: str = "",
    **values: object,
) -> None:
    """Notifies the admins of an organization: audit log and email (no webhook: the alert may
    be about webhooks themselves)."""
    assert kind in ORG_KINDS
    org = session.get(Organization, org_id)
    if org is None:
        return
    details = {k: str(mail.localized(v, "en")) for k, v in values.items()}  # log: English
    audit(session, None, f"alert.{kind}", org_id=org.id, space=None, **details)
    if not mail.configured(session):
        return
    admins = session.scalars(
        select(User)
        .join(OrgMember, OrgMember.user_id == User.id)
        .where(
            OrgMember.org_id == org.id,
            OrgMember.role == OrgRole.ADMIN.value,
            User.is_active.is_(True),
            User.email.is_not(None),
        )
    )
    link = f"{public_url}/o/{org.slug}"
    for admin in admins:
        subject, body = mail.compose(
            f"alert_{kind}", admin.locale, org=org.name, link=link, **values
        )
        try:
            mail.send(session, box, admin.email or "", subject, body)
        except mail.MailError as error:
            log.warning("Alert %s not sent to %s: %s", kind, admin.email, error)


BACKLOG_DAYS = 3


def check_backlogs(database: Database, ws: Workspace) -> int:
    """Notifies when an inbox keeps items never reviewed for more than BACKLOG_DAYS days
    (tidy run broken or overwhelmed). Returns the number of alerts.

    Nothing for a space without an active tidy run (disabled, or organization without AI): the
    inbox waits for a human there, and a daily alert that cannot be silenced would help
    no one."""
    from synaptane.services import changes, tidy
    from synaptane.services.deposits import list_items

    limit = datetime.now(UTC) - timedelta(days=BACKLOG_DAYS)
    sent = 0
    with database.session() as session:
        candidates = list(
            session.scalars(
                select(Space).where(Space.repo_url.is_not(None), Space.tidy_enabled.is_(True))
            )
        )
        for space in candidates:
            if not tidy.active(session, ws.box, space):
                continue
            try:
                notes = list_items(tidy.system_reader(space), ws, space)
            except SynaptaneError:
                continue  # not synchronized yet
            items = [i for i in notes if i.status != "left"] + [
                item for item, _ in changes.pending(session, ws, space) if item.status != "left"
            ]
            dates = [_parse(i.deposited_at) for i in items]
            oldest = min((d for d in dates if d is not None), default=None)
            if oldest is None or oldest > limit:
                continue
            days = (datetime.now(UTC) - oldest).days
            if notify(
                session, ws.box, space, "inbox_backlog", ws.public_url, count=len(items), days=days
            ):
                sent += 1
    return sent


ABANDON_WINDOW = timedelta(hours=1)  # abandoned jobs looked at on each maintenance run
# Jobs whose abandonment says nothing: synchronization is restarted by the next one
# (`schedule_polls`) and has its own alert when the transient error persists (`git_sync`).
ABANDON_IGNORED = frozenset({"sync"})


def check_abandoned_jobs(database: Database, ws: Workspace) -> int:
    """Notifies when a job of a space has used up its attempts (`jobs.MAX_ATTEMPTS`): a
    persistent transient error must not end in silence. Called on each worker maintenance
    run; `notify` avoids duplicates. Returns the number of alerts."""

    sent = 0
    with database.session() as session:
        for job in jobs.abandoned(session, datetime.now(UTC) - ABANDON_WINDOW):
            if job.kind in ABANDON_IGNORED:
                continue
            space = session.get(Space, job.space_id)
            if space is None:
                continue
            if notify(
                session,
                ws.box,
                space,
                "job_abandoned",
                ws.public_url,
                task=job.kind,
                attempts=job.attempts,
                error=(job.error or "?")[:500],
            ):
                sent += 1
    return sent


def _parse(value: str) -> datetime | None:
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)
