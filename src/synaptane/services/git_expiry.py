"""Expiry of the spaces' Git access tokens (docs/admin/git.md#token-expiry).

A token's expiry date is known when Synaptane created it (GitLab project access token) or read
it from the provider when an admin saved it (`forges.token_expiry`). It is kept with the token,
in the space's encrypted secret: nothing else stores it, and nothing here ever reads the token
itself.

What follows from it:
- **Administration > Health** lists the spaces whose token expires within `WARN_DAYS` days
  (warning) or has expired (problem);
- the organization's admins get an alert at each step of `STEPS` (30, 7 and 1 day before), then
  on the day it expires: once per step and per expiry date, checked by the worker every hour. A
  new token (another date) starts over;
- a sync refused for authentication says that the token has expired (`git_sync`).

GitLab's tokens stop working on their expiry date (at 00:00 UTC): that day counts as expired.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from synaptane.core.security import SecretBox
from synaptane.git.credentials import GitCredentials
from synaptane.services import alerts
from synaptane.services.git_sync import GIT_CREDENTIALS
from synaptane.services.secrets import get_space_secret
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Organization, Secret, Space

log = logging.getLogger(__name__)
WARN_DAYS = 30
STEPS = (30, 7, 1)  # days left at which the admins are alerted, before the expiry itself


def today() -> date:
    """Today's date in UTC, the time zone of the providers' expiry dates."""
    return datetime.now(UTC).date()


def days_left(expires: date, on: date | None = None) -> int:
    """Days before the expiry: 0 or less, expired (the token stops working on its date)."""
    return (expires - (on or today())).days


def step(days: int) -> int | None:
    """Alert step reached with `days` left: 30, 7, 1, 0 (expired), or None (nothing yet)."""
    if days <= 0:
        return 0
    return next((limit for limit in sorted(STEPS) if days <= limit), None)


@dataclass(frozen=True)
class Expiry:
    """A space whose Git token has a known expiry date."""

    space: Space
    expires: date
    days: int  # days left; 0 or less: expired

    @property
    def expired(self) -> bool:
        return self.days <= 0

    @property
    def git_url(self) -> str:
        return f"/o/{self.space.address}/-/git"


def credentials_of(session: Session, box: SecretBox, space: Space) -> GitCredentials | None:
    raw = get_space_secret(session, box, space.id, GIT_CREDENTIALS)
    return GitCredentials.from_json(raw) if raw else None


def expiries(
    session: Session, box: SecretBox, *, on: date | None = None, within: int | None = WARN_DAYS
) -> list[Expiry]:
    """Spaces with a repository whose token expires within `within` days (None: whatever the
    date) or has expired, the closest first. A secret that does not decrypt (master key
    replaced: the Health page says so) is skipped."""
    on = on or today()
    spaces = session.scalars(
        select(Space)
        .join(Secret, Secret.space_id == Space.id)
        .join(Organization, Organization.id == Space.org_id)
        .options(joinedload(Space.organization))
        .where(Secret.purpose == GIT_CREDENTIALS, Space.repo_url.is_not(None))
        .order_by(Organization.slug, Space.slug)
    ).all()
    found = []
    for space in spaces:
        try:
            credentials = credentials_of(session, box, space)
        except Exception:  # noqa: BLE001 — unreadable secret: reported by the key check
            continue
        expires = credentials.expiry if credentials else None
        if expires is None:
            continue
        days = days_left(expires, on)
        if within is None or days <= within:
            found.append(Expiry(space, expires, days))
    return sorted(found, key=lambda e: e.days)  # stable: names stay in order


def _date(value: date) -> Callable[[str], str]:
    """The date written out in each recipient's language (`mail.localized`)."""

    def text(locale: str) -> str:
        from synaptane.web.i18n import format_day

        return format_day(value, locale)

    return text


def _when(days: int) -> Callable[[str], str]:
    """ "in 7 days", "tomorrow", in each recipient's language."""

    def text(locale: str) -> str:
        from synaptane.web.i18n import plural, translator

        translate = translator(locale)
        if days == 1:
            return translate("tomorrow")
        return translate(plural(locale, days, "in {n} day", "in {n} days"), n=days)

    return text


def check(database: Database, ws: Workspace, on: date | None = None) -> int:
    """Alerts the organization admins of each space whose token reached a new step (see the
    module). Called every hour by the worker; returns the number of alerts sent."""
    sent = 0
    with database.session() as session:
        for found in expiries(session, ws.box, on=on):
            reached = step(found.days)
            if reached is None:
                continue
            # Once per step and per expiry date: a renewed token (another date) starts over.
            once = f"{found.expires.isoformat()}/{reached}"
            if reached == 0:
                notified = alerts.notify(
                    session,
                    ws.box,
                    found.space,
                    "git_token_expired",
                    ws.public_url,
                    once=once,
                    date=_date(found.expires),
                )
            else:
                notified = alerts.notify(
                    session,
                    ws.box,
                    found.space,
                    "git_token_expiring",
                    ws.public_url,
                    once=once,
                    date=_date(found.expires),
                    when=_when(found.days),
                )
            if notified:
                log.info(
                    "Git token of space %s: expiry alert (%s day(s) left)",
                    found.space.id,
                    found.days,
                )
                sent += 1
    return sent
