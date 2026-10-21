"""Verification and freshness of pages: who answers for them, when they were verified, when
to review them. Everything is in the page's YAML front matter, hence in Git (who verified and
when: the history tells):

    ---
    owner: ada            # username of an account, or free-form name
    verified: 2026-10-03  # date of the last verification
    review_every: 6m      # 90d, 12w, 6m, 1y; missing: the space's interval; never: never
    ---

State of a Markdown page: **verified** (verification date, and due date not reached yet),
**to review** (due date passed) or **never verified**. A page without an interval (neither
its own nor the space's) stays verified. Other files have no state.

Lenient reading: a missing or malformed front matter or an unreadable value never raises; the
field concerned is simply ignored. Minimal writing: only the lines of the three fields change
(the block is created if it does not exist), the rest of the text is kept byte for byte.

A commit that only changes these fields is not a change for the tidy run to review
(`without_verification`, used by `services.changes`), and AI agents can neither erase nor
change them by rewriting a page (`keep_verification`).
"""

import calendar
import json
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import Action, can
from synaptane.core.errors import SynaptaneError, invalid, not_found
from synaptane.git.repo import Repository
from synaptane.services import Actor
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Space, User

log = logging.getLogger("synaptane.freshness")

OWNER, VERIFIED, REVIEW = "owner", "verified", "review_every"
FIELDS = (OWNER, VERIFIED, REVIEW)
VERIFIED_STATE, STALE_STATE, NEVER_STATE = "verified", "stale", "never"
NEVER = "never"  # explicit interval: never to review, even if the space has one
MARKDOWN = (".md", ".markdown")
PREFIX = CONTENT_DIR + "/"
MAX_PAGE = 2 * 1024 * 1024  # beyond that, a page is not read (as for rendering)
MAX_OWNER = 120
COMMIT_SUBJECT = "Page verified"  # start of the message of a verification commit
SETTINGS_SUBJECT = "Page tracking updated"  # owner or interval only
# Starts of the subjects of verification commits, including those written in French before
# the code was translated (histories of existing spaces).
VERIFICATION_SUBJECTS = (
    COMMIT_SUBJECT,
    SETTINGS_SUBJECT,
    "Page vérifiée",
    "Suivi de la page mis à jour",
)

# Search ranking: a slight boost for verified, up-to-date pages, a slight penalty for pages
# to review. Multiplicative: relevance stays the first criterion.
SEARCH_BONUS = 1.10
SEARCH_MALUS = 0.93

_FRONT = re.compile(r"\A(﻿?)---[ \t]*(\r?\n)(.*?)^(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S | re.M)
_KEY = re.compile(r"^([A-Za-z_][\w-]*)[ \t]*:(?:[ \t]+|$)")


# --- Review interval ------------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    count: int
    unit: str  # d, w, m or y

    def __str__(self) -> str:
        return f"{self.count}{self.unit}"

    def after(self, start: date) -> date:
        if self.unit == "d":
            return start + timedelta(days=self.count)
        if self.unit == "w":
            return start + timedelta(weeks=self.count)
        months = self.count * (12 if self.unit == "y" else 1)
        year, month = divmod(start.month - 1 + months, 12)
        year += start.year
        day = min(start.day, calendar.monthrange(year, month + 1)[1])
        return date(year, month + 1, day)


_UNITS = {
    **dict.fromkeys(("d", "j", "day", "days", "jour", "jours"), "d"),
    **dict.fromkeys(("w", "s", "sem", "week", "weeks", "semaine", "semaines"), "w"),
    **dict.fromkeys(("m", "mo", "month", "months", "mois"), "m"),
    **dict.fromkeys(("y", "a", "an", "ans", "year", "years", "année", "années"), "y"),
}
_NEVER_WORDS = {"never", "jamais", "none", "non", "no", "false", "0"}
_PERIOD = re.compile(r"(\d{1,4})\s*([^\W\d_]*)")


def parse_period(value: object) -> Period | str | None:
    """`90d`, `12w`, `6m`, `1y` (and `90 jours`, `6 mois`, `1 an`…) → Period; `never`
    (or `jamais`, `0`) → NEVER; empty or unreadable → None (the space's interval)."""
    text = str(value or "").strip().strip("'\"").strip().lower()
    if not text:
        return None
    if text in _NEVER_WORDS:
        return NEVER
    match = _PERIOD.fullmatch(text)
    if match is None:
        return None
    count, unit = int(match[1]), _UNITS.get(match[2] or "d")
    if unit is None or count <= 0:
        return NEVER if count == 0 and unit else None
    limits = {"d": 3660, "w": 520, "m": 120, "y": 10}
    return Period(min(count, limits[unit]), unit)


def clean_period(value: str) -> str:
    """Interval as entered (form, REST, MCP) → stored form (`6m`, `never`, "").
    An unreadable value is refused rather than ignored."""
    if not value.strip():
        return ""
    parsed = parse_period(value)
    if parsed is None:
        raise invalid(
            "Invalid review period: {value}. Examples: 90d (days), 12w (weeks), 6m (months), "
            "1y (year), never.",
            value=value.strip()[:40],
        )
    return str(parsed)


# --- Reading the front matter ---------------------------------------------------------------


@dataclass(frozen=True)
class Meta:
    """Verification fields of a page, as written (raw values kept)."""

    owner: str = ""
    verified: date | None = None
    review_every: str = ""
    raw: dict[str, str] = field(default_factory=dict)  # field present → value as written

    @property
    def empty(self) -> bool:
        return not self.raw


def _value(raw: str) -> str:
    """Simple YAML value: quotes removed, trailing comment removed."""
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            decoded, _ = json.JSONDecoder().raw_decode(raw)
            return str(decoded).strip()
        except ValueError:
            return raw.strip('"').strip()
    if raw.startswith("'"):
        match = re.match(r"'((?:[^']|'')*)'", raw)
        return (match[1] if match else raw.strip("'")).replace("''", "'").strip()
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _date(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def _block(text: str) -> re.Match[str] | None:
    return _FRONT.match(text) if text.startswith(("---", "﻿---")) else None


def _entries(body: str) -> list[tuple[str, int, int, str]]:
    """Top-level keys of the block: (key, first line, line after the value, raw value). A
    multi-line value (list, indented text) covers its lines."""
    lines = body.splitlines(keepends=True)
    found: list[tuple[str, int, int, str]] = []
    index = 0
    while index < len(lines):
        match = _KEY.match(lines[index])
        if match is None:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and (
            lines[end][:1] in (" ", "\t") or (lines[end].startswith("-") and lines[end].strip())
        ):
            end += 1
        found.append((match[1], index, end, lines[index][match.end() :].rstrip("\r\n")))
        index = end
    return found


def read_meta(text: str) -> Meta:
    """Verification fields of a Markdown page (empty if nothing is readable)."""
    match = _block(text)
    if match is None:
        return Meta()
    raw: dict[str, str] = {}
    for key, start, end, value in _entries(match[3]):
        if key in FIELDS and key not in raw and end == start + 1:  # single-line value
            raw[key] = value.strip()
    if not raw:
        return Meta()
    owner = _value(raw.get(OWNER, ""))[:MAX_OWNER]
    verified = _date(_value(raw.get(VERIFIED, ""))) if VERIFIED in raw else None
    return Meta(owner, verified, _value(raw.get(REVIEW, "")), raw)


# --- State -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Freshness:
    state: str  # verified, stale or never
    owner: str = ""
    verified: date | None = None
    due: date | None = None  # date on which the page is due for review (None: never)
    period: str = ""  # effective interval (`6m`, `never`, "")
    inherited: bool = False  # interval inherited from the space

    @property
    def is_verified(self) -> bool:
        return self.state == VERIFIED_STATE

    @property
    def stale(self) -> bool:
        return self.state == STALE_STATE

    def search_factor(self) -> float:
        if self.state == VERIFIED_STATE:
            return SEARCH_BONUS
        return SEARCH_MALUS if self.state == STALE_STATE else 1.0

    def as_dict(self) -> dict[str, object]:
        """Fields of REST and MCP answers."""
        return {
            "state": self.state,
            "verified": self.is_verified,
            "stale": self.stale,
            "verified_at": self.verified.isoformat() if self.verified else None,
            "owner": self.owner or None,
            "review_due": self.due.isoformat() if self.due else None,
            "review_every": self.period or None,
        }


NEVER_VERIFIED = Freshness(NEVER_STATE)


def state_of(meta: Meta, default: str, today: date) -> Freshness:
    """State of a page from its fields, the space's interval (`default`) and today's
    date."""
    own = parse_period(meta.review_every)
    inherited = own is None
    period = parse_period(default) if inherited else own
    shown = str(period) if period is not None else ""
    if meta.verified is None:
        return Freshness(NEVER_STATE, meta.owner, None, None, shown, inherited)
    due = period.after(meta.verified) if isinstance(period, Period) else None
    state = STALE_STATE if due is not None and today >= due else VERIFIED_STATE
    return Freshness(state, meta.owner, meta.verified, due, shown, inherited)


def of_text(text: str | None, default: str, today: date) -> Freshness:
    return state_of(read_meta(text or ""), default, today)


def today_for(space: Space, now: datetime | None = None) -> date:
    """Today's date in the space's time zone (that of the tidy run)."""
    moment = now or datetime.now(UTC)
    try:
        return moment.astimezone(ZoneInfo(space.tidy_timezone or "UTC")).date()
    except (ZoneInfoNotFoundError, ValueError):
        return moment.astimezone(UTC).date()


def is_page(path: str) -> bool:
    return path.lower().endswith(MARKDOWN)


# --- Minimal writing -------------------------------------------------------------------------

_PLAIN = re.compile(r"[^\W\d][\w .@+'’-]*", re.UNICODE)
_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "~", "y", "n"}


def scalar(value: str) -> str:
    """YAML value: as is if unambiguous, otherwise quoted."""
    if (
        _PLAIN.fullmatch(value)
        and value == value.strip()
        and value.lower() not in _RESERVED
        and " #" not in value
    ):
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d+[a-z]", value):  # date, interval
        return value
    return json.dumps(value, ensure_ascii=False)


def update_front_matter(text: str, values: dict[str, str | None]) -> str:
    """`text` with the fields `values` set (None: removed), lines written as is (already in
    YAML form). Only these lines change; the block is created at the top if it does not exist,
    and removed if it holds nothing more after a removal."""
    match = _block(text)
    if match is None:
        lines = [f"{key}: {value}" for key, value in values.items() if value is not None]
        if not lines:
            return text
        bom = "﻿" if text.startswith("﻿") else ""
        newline = "\r\n" if "\r\n" in text[:4000] else "\n"
        block = "---" + newline + newline.join(lines) + newline + "---" + newline
        return bom + block + text[len(bom) :]
    newline = match[2]
    body = match[3]
    lines = body.splitlines(keepends=True)
    entries = {key: (start, end) for key, start, end, _ in reversed(_entries(body))}
    replaced: dict[int, list[str]] = {}
    dropped: set[int] = set()
    added: list[str] = []
    removed = False
    for key, value in values.items():
        if key in entries:
            start, end = entries[key]
            original = lines[start]
            prefix = original[: _KEY.match(original).end()]  # type: ignore[union-attr]
            if not prefix.endswith((" ", "\t")):
                prefix += " "
            dropped.update(range(start + 1, end))
            if value is None:
                dropped.add(start)
                removed = True
            else:
                ending = newline if original.endswith(("\n", "\r")) else ""
                replaced[start] = [f"{prefix}{value}{ending}"]
        elif value is not None:
            added.append(f"{key}: {value}{newline}")
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in replaced:
            out += replaced[index]
        elif index not in dropped:
            out.append(line)
    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += newline
    out += added
    new_body = "".join(out)
    start, end = match.span(3)
    if removed and not new_body.strip():
        return match[1] + text[match.end() :]
    return text[:start] + new_body + text[end:]


def without_verification(text: str) -> str:
    """Text without the verification fields (nor the block, if it held only them): two
    versions of a page that differ only by them give the same result."""
    meta = read_meta(text)
    if meta.empty:
        return text
    return update_front_matter(text, dict.fromkeys(meta.raw))


def same_but_verification(old: str, new: str) -> bool:
    """True if two versions differ only by the verification fields (and whitespace or blank
    lines, like `git diff -w --ignore-blank-lines`)."""

    def squeeze(text: str) -> str:
        return re.sub(r"\s+", "", without_verification(text))

    return squeeze(old) == squeeze(new)


def keep_verification(old: str | None, new: str) -> tuple[str, bool]:
    """Text written by an AI agent, whose verification fields are those of the previous
    version: put back if it erased them, restored if it changed them, removed if it added
    some. Only a human verifies a page ("Mark as verified", `verify_page`).
    Returns the text and True if a correction was needed."""
    before = read_meta(old or "")
    after = read_meta(new)
    if before.raw == after.raw:
        return new, False
    values: dict[str, str | None] = {}
    for key in [*before.raw, *(k for k in FIELDS if k not in before.raw)]:  # previous order
        if before.raw.get(key) != after.raw.get(key):
            values[key] = before.raw.get(key)
    return update_front_matter(new, values), True


# --- Index of a space (verified pages, owners) ----------------------------------------------

_LOCK = threading.Lock()
_METAS: OrderedDict[str, Meta] = OrderedDict()  # blob digest → fields
_METAS_MAX = 200_000
_MAPS: OrderedDict[tuple[str, str], dict[str, Meta]] = OrderedDict()  # (copy, commit)
_MAPS_MAX = 32


def page_metas(repo: Repository, head: str | None = None) -> dict[str, Meta]:
    """Verification fields of the Markdown pages of content/ (paths relative to content/), at
    commit `head` (HEAD by default). Only the pages that have some are listed.

    Cached per commit, and each page per digest: a new commit only rereads the changed
    pages."""
    commit = head or repo.head()
    if commit is None:
        return {}
    key = (str(repo.path), commit)
    with _LOCK:
        if (cached := _MAPS.get(key)) is not None:
            _MAPS.move_to_end(key)
            return cached
    blobs = [
        b
        for b in repo.files(CONTENT_DIR, commit)
        if b.path.startswith(PREFIX) and is_page(b.path) and b.size <= MAX_PAGE
    ]
    with _LOCK:
        missing = [b.sha for b in blobs if b.sha not in _METAS]
    read = repo.read_objects(missing) if missing else {}
    found: dict[str, Meta] = {}
    with _LOCK:
        for sha, data in read.items():
            _METAS[sha] = read_meta(data.decode("utf-8", "replace"))
        for blob in blobs:
            meta = _METAS.get(blob.sha)
            if meta is not None:
                _METAS.move_to_end(blob.sha)
                if not meta.empty:
                    found[blob.path[len(PREFIX) :]] = meta
        while len(_METAS) > _METAS_MAX:
            _METAS.popitem(last=False)
        _MAPS[key] = found
        while len(_MAPS) > _MAPS_MAX:
            _MAPS.popitem(last=False)
    return found


def states(repo: Repository, default: str, today: date) -> dict[str, Freshness]:
    """State of each page that carries verification fields (the others: never
    verified)."""
    return {path: state_of(meta, default, today) for path, meta in page_metas(repo).items()}


def space_states(ws: Workspace, space: Space, today: date | None = None) -> dict[str, Freshness]:
    """Like `states`, for a space; empty if it is not synchronized yet. No permission check:
    only call it for a space already readable by the caller."""
    try:
        repo = ws.local_repository(space)
        return states(repo, space.review_every or "", today or today_for(space))
    except SynaptaneError:
        return {}


def owned_by(owner: str, user_names: tuple[str, ...]) -> bool:
    """True if the written owner designates this account (username or email address,
    ignoring case and a leading "@")."""
    wanted = owner.strip().lstrip("@").casefold()
    return bool(wanted) and wanted in {n.strip().casefold() for n in user_names if n}


# --- Mark as verified ------------------------------------------------------------------------


@dataclass(frozen=True)
class Verification:
    commit: str | None  # None: nothing changed (already verified today, for example)
    freshness: Freshness
    path: str


def verify_page(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    path: str,
    *,
    owner: str | None = None,
    review_every: str | None = None,
    verify: bool = True,
    source: str = "web",
) -> Verification:
    """Marks a page as verified today (contributor): a commit that only changes its front
    matter (`verified`, and `owner` if it is empty: the person).

    `owner`, `review_every`: None, unchanged; "" removes them (no owner; the space's
    interval). `verify=False`: only the owner and the interval. This commit is not a change
    for the tidy run to review."""
    from synaptane.services import content  # late import: content imports a lot

    full = content.content_path(path)
    if full == CONTENT_DIR or not is_page(full):
        raise invalid("Only Markdown pages can be verified.")
    period = clean_period(review_every) if review_every is not None else None
    who = None if owner is None else " ".join(owner.split())[:MAX_OWNER]
    today = today_for(space)
    with content.writable(session, database, actor, ws, space) as repo:
        data = repo.blob(full)
        if data is None:
            raise not_found("Page not found.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise invalid("Unreadable page (encoding other than UTF-8).") from None
        if len(data) > MAX_PAGE:
            raise invalid("Page too large to be verified here (2 MB at most).")
        current = read_meta(text)
        values: dict[str, str | None] = {}
        if verify:
            values[VERIFIED] = today.isoformat()
            if who is None and not current.owner:
                who = actor.user.username
            elif who == "":
                who = current.owner or actor.user.username
        if who is not None and (who != current.owner or OWNER not in current.raw):
            values[OWNER] = scalar(who) if who else None  # unchanged: the line stays as is
        if period is not None:
            values[REVIEW] = period or None
        values = {k: v for k, v in values.items() if v != current.raw.get(k)}
        values = {k: v for k, v in values.items() if v is not None or k in current.raw}
        relative = content.relative_path(full)
        sha = None
        if values:
            updated = update_front_matter(text, values)
            subject = f"{COMMIT_SUBJECT if verify else SETTINGS_SUBJECT}: {relative}"
            sha = content.commit_files(repo, ws, actor, source, {full: updated.encode()}, subject)
            text = updated
    if sha:
        content.after_write(session, actor, space, "content.verified", full, source)
    return Verification(sha, of_text(text, space.review_every or "", today), relative)


# --- Reminder to owners ----------------------------------------------------------------------

REMINDER = "freshness.reminder"  # audit log action, one per email sent
REMINDER_QUIET = timedelta(days=6, hours=20)  # once a week at most
REMINDER_HOURS = range(8, 18)  # working days, from 8 am to 6 pm (the space's time zone)
REMINDER_PAGES = 50  # pages listed in an email


@dataclass
class _Reminder:
    user: User
    pages: list[tuple[Space, str, Freshness]] = field(default_factory=list)


def send_reminders(database: Database, ws: Workspace, now: datetime | None = None) -> int:
    """Grouped email, at most once a week, to each owner of pages to review: all their due
    pages, across all spaces.

    The owner is recognized by their username or email address (`owner`). Same conditions as
    routine emails: active account, verified address, "routine emails" preference checked
    (My account), and permission to read the space. Sent on a working day, during the day in
    the space's time zone. Returns the number of emails sent."""
    from synaptane.services import load_actor, mail

    moment = now or datetime.now(UTC)
    with database.session() as session:
        if not mail.configured(session):
            return 0
        users = session.scalars(
            select(User).where(
                User.is_active.is_(True),
                User.email.is_not(None),
                User.email_verified_at.is_not(None),
                User.notify_routines.is_(True),
            )
        )
        by_name: dict[str, User] = {}
        for user in users:
            for name in (user.username, user.email or ""):
                if name:
                    by_name.setdefault(name.casefold(), user)
        if not by_name:
            return 0
        reminders: dict[object, _Reminder] = {}
        readers: dict[object, Actor] = {}
        for space in session.scalars(select(Space).where(Space.repo_url.is_not(None))):
            local = moment.astimezone(_zone(space))
            if local.weekday() >= 5 or local.hour not in REMINDER_HOURS:
                continue
            for path, state in sorted(space_states(ws, space, local.date()).items()):
                owner = by_name.get(state.owner.strip().lstrip("@").casefold())
                if not state.stale or owner is None:
                    continue
                if owner.id not in readers:
                    readers[owner.id] = load_actor(session, owner)
                access = readers[owner.id].access
                if not can(access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id):
                    continue
                reminders.setdefault(owner.id, _Reminder(owner)).pages.append((space, path, state))
        sent = 0
        for reminder in reminders.values():
            if not _reminded(session, reminder.user, moment) and _send_reminder(
                session, ws, reminder, moment
            ):
                sent += 1
        return sent


def _zone(space: Space) -> ZoneInfo:
    try:
        return ZoneInfo(space.tidy_timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _reminded(session: Session, user: User, moment: datetime) -> bool:
    found = session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.action == REMINDER,
            AuditEvent.details["user"].astext == str(user.id),
            AuditEvent.at > moment - REMINDER_QUIET,
        )
    )
    return found is not None


def _send_reminder(session: Session, ws: Workspace, reminder: _Reminder, moment: datetime) -> bool:
    from synaptane.services import mail

    user, pages = reminder.user, reminder.pages
    label = "due since" if user.locale == "en" else "à revoir depuis le"
    lines = []
    for space, path, state in pages[:REMINDER_PAGES]:
        since = state.due.isoformat() if state.due else ""
        url = f"{ws.public_url}/o/{space.address}/p/{quote(path, safe='/')}"
        lines.append(f"- {space.name} · {path} ({label} {since})\n  {url}")
    if len(pages) > REMINDER_PAGES:
        lines.append(f"- … (+{len(pages) - REMINDER_PAGES})")
    subject, body = mail.compose(
        "review_reminder",
        user.locale,
        count=len(pages),
        pages="\n".join(lines),
        account=f"{ws.public_url}/account",
    )
    try:
        mail.send(session, ws.box, user.email or "", subject, body)
    except mail.MailError as error:
        log.warning("Review reminder not sent to %s: %s", user.email, error)
        return False
    # Dated at `moment`, which `_reminded` compares with (not the clock: `now` may be given).
    session.add(
        AuditEvent(
            action=REMINDER,
            at=moment.astimezone(UTC),
            details={"user": str(user.id), "pages": str(len(pages))},
        )
    )
    session.flush()
    return True
