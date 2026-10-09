"""Notice to the depositor: what the tidy-up did with their notes.

Without a notice, the loop stays open: the person who deposits knows neither where their note
was filed, nor why it was left. One e-mail per run and per person groups their notes: filed
(with the pages, as links) or left (with the reason, to complete).

Choice: on by default, can be turned off in "My account". A setting to tick that nobody
discovers would leave the loop open; and how to turn it off is recalled in every message.
Only verified addresses receive it, for active accounts that can still read the space. Page
changes ("change" items) are not deposits: no notice. A note already left and left again is
not sent again at each run.
"""

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.core.access import Action, can
from synaptane.core.security import SecretBox
from synaptane.services import load_actor, mail
from synaptane.services.inbox import InboxItem
from synaptane.storage.models import Space, User

log = logging.getLogger("synaptane.tidy")

_ADDRESS = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")


@dataclass(frozen=True)
class Notice:
    """Fate of a deposited note, for its depositor(s)."""

    title: str
    depositors: tuple[str, ...]  # e-mail addresses, in lowercase
    status: str  # done (filed) or left
    detail: str  # agent's summary, or reason
    pages: tuple[str, ...] = ()  # paths relative to content/


def addresses(deposited_by: str) -> tuple[str, ...]:
    """Addresses of the `deposited_by` field ("Name <e-mail>, Other <e-mail>")."""
    return tuple(dict.fromkeys(m.lower() for m in _ADDRESS.findall(deposited_by)))


def of(item: InboxItem, status: str, detail: str, pages: list[str] | None = None) -> Notice | None:
    """Notice for a closed item, or None (page change, note already left, no known
    depositor)."""
    if item.type != "note" or not (depositors := addresses(item.deposited_by)):
        return None
    if status == "left" and item.status == "left":
        return None  # already notified by the run that left it
    return Notice(item.title, depositors, status, detail.strip(), tuple(pages or ()))


def send(
    session: Session, box: SecretBox, space: Space, notices: list[Notice], public_url: str
) -> int:
    """One e-mail per depositor, with their notes grouped. Returns the number of e-mails
    sent.

    A failed sending is logged, never raised: the tidy-up is already committed."""
    if not notices or not mail.configured(session):
        return 0
    by_address: dict[str, list[Notice]] = {}
    for notice in notices:
        for address in notice.depositors:
            by_address.setdefault(address, []).append(notice)
    users = session.scalars(
        select(User).where(
            func.lower(User.email).in_(list(by_address)),
            User.email_verified_at.is_not(None),
            User.is_active.is_(True),
            User.notify_tidy.is_(True),
        )
    )
    org = space.organization
    base = f"{public_url}/o/{org.slug}/{space.slug}"
    sent = 0
    for user in users:
        access = load_actor(session, user).access
        if not can(access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id):
            continue  # no longer has access to the space: neither paths nor summaries
        mine = by_address[(user.email or "").lower()]
        left = any(n.status == "left" for n in mine)
        subject, body = mail.compose(
            "tidy_notice",
            user.locale,
            space=space.name,
            org=org.name,
            count=len(mine),
            items="\n\n".join(_line(n, base, user.locale) for n in mine),
            left=mail.compose("tidy_notice_left", user.locale, link=base)[1] if left else "",
            account=f"{public_url}/account",
        )
        try:
            mail.send(session, box, user.email or "", subject, body)
            sent += 1
        except mail.MailError as error:
            log.warning("Tidy-up notice not sent to %s: %s", user.email, error)
    return sent


def _line(notice: Notice, base: str, locale: str) -> str:
    """One note in the e-mail, in the recipient's language (French or English)."""
    english = locale != "fr"
    title = f"“{notice.title}”" if english else f"« {notice.title} »"
    colon = ":" if english else " :"
    detail = notice.detail[:500]
    if notice.status == "done":
        if notice.pages:
            where = "\n".join(f"  {base}/p/{quote(p, safe='/')}" for p in notice.pages)
            head = "filed in:" if english else "rangée dans :"
            return f"- {title} {head}\n{where}\n  {detail}"
        head = "closed, no page written" if english else "close sans page écrite"
        return f"- {title}{colon} {head} ({detail})"
    head = "left in the inbox" if english else "laissée dans l'inbox"
    return f"- {title}{colon} {head}.\n  {detail}"
