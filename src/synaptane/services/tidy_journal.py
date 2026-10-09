"""Summarised tidy-up log: what happened, what is wrong, and what to do.

The tidy-up records its errors as free text (see `tidy.run`), often a technical message from
the provider ("[Errno 111] Connection refused"). This module recognises them afterwards and
maps them to a code (provider unreachable, key refused…), hence a translated message and an
action to suggest. The original text is still shown to administrators as a technical detail:
a message that is not recognised is shown as it is, nothing is lost.

It also holds what summarises the log instead of piling it up: the retries of a same night
grouped into one entry, the current series of failures (banner at the top), the request
waiting for the worker ("Running" card as soon as the button is clicked) and the time of the
next scheduled run.

The patterns recognise both the French texts of older runs and the English ones written now.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.ai.base import AIConfig
from synaptane.core.access import OrgRole
from synaptane.core.errors import SynaptaneError
from synaptane.services.tidy import NO_PAGE, TIDY, due, parse_time
from synaptane.services.tidy_tools import Op
from synaptane.storage.models import (
    AuditEvent,
    Job,
    OrgAISettings,
    OrgMember,
    Space,
    TidyRun,
    User,
)

SOON = timedelta(seconds=90)  # a request due by then is "queued": the page waits for it


STALLED = timedelta(minutes=5)  # request due for longer: is the worker running?


ORPHAN = timedelta(hours=2)  # older "running" run: worker killed (see close_orphans)


FAILURES_BEFORE_WARNING = 3  # failures in a row before advising against "Tidy up now"


# --- Errors: from the recorded text to a message that says what to do --------------------------


@dataclass(frozen=True)
class Explanation:
    """Recognised error. `label` and `message` are templates to translate (with `params`);
    `action`: "test" (test the connection), "settings" (AI settings), "wait" (the run will be
    retried, nothing to do) or ""; `detail`: the recorded text, as it is."""

    code: str
    label: str
    message: str
    detail: str
    action: str = ""
    params: dict[str, str] = field(default_factory=dict)
    technical: bool = True  # the detail comes from the provider or the system: worth showing

    @property
    def show_detail(self) -> bool:
        """Technical detail to show: not when it repeats the message (a text of Synaptane,
        sometimes outdated: "organisation page"), nor when it *is* the message."""
        return bool(self.detail) and self.technical and self.code != "other"


@dataclass(frozen=True)
class _Rule:
    code: str
    pattern: re.Pattern[str]
    label: str
    message: str
    action: str
    with_address: str = ""  # variant of the message when the provider's address is known
    technical: bool = True


def _rule(
    code: str,
    pattern: str,
    label: str,
    message: str,
    action: str,
    with_address: str = "",
    *,
    technical: bool = True,
) -> _Rule:
    return _Rule(
        code, re.compile(pattern, re.IGNORECASE), label, message, action, with_address, technical
    )


# Order matters: the first rule that recognises the text wins (a timeout is also an
# "unreachable" provider for httpx, but the advice is not the same).
RULES = (
    _rule(
        "no_provider",
        r"Aucun fournisseur d'IA configuré|No AI provider configured",
        "no AI configured",
        "No AI provider is configured for the organization.",
        "settings",
        technical=False,
    ),
    _rule(
        "budget",
        r"Budget mensuel|Monthly AI budget",
        "budget reached",
        "The organization's monthly AI budget is reached: tidying resumes next month, or as "
        "soon as the budget is raised.",
        "settings",
        technical=False,
    ),
    _rule(
        "cli_missing",
        r"n'est pas installé sur le serveur|not installed on the server",
        "program missing",
        "The subscription's program is not installed on the server.",
        "test",
    ),
    _rule(
        "cli_login",
        r"n'est pas connecté sur le serveur|connexion de Claude Code a expiré|not logged in on the "
        r"server|login has expired",
        "subscription logged out",
        "The subscription is no longer logged in on the server: log it in again (steps on the "
        "organization's AI page).",
        "test",
    ),
    _rule(
        "auth",
        r"Clé (?:\w+ )?refusée|key refused|\b40[13]\b|authenticat|invalid[ _]api[ _]key"
        r"|unauthorized",
        "key refused",
        "The provider refuses the API key: check or replace it.",
        "test",
    ),
    _rule(
        "model",
        r"Modèle introuvable|model[ _]not[ _]found|\b404\b",
        "model not found",
        "The provider cannot find the requested model: check its name.",
        "test",
    ),
    _rule(
        "rate_limit",
        r"Limite de débit|rate.?limit|\b429\b",
        "rate limit",
        "The provider is rate-limiting requests: the run will be retried later.",
        "wait",
    ),
    _rule(
        "timeout",
        r"ne répond pas à temps|délai dépassé|timed? ?out|does not answer in time",
        "no answer in time",
        "The provider does not answer in time: it may be overloaded.",
        "test",
        "The provider at {address} does not answer in time: it may be overloaded.",
    ),
    _rule(
        "unreachable",
        r"injoignable|Impossible de joindre|unreachable|Connection refused|Errno 111"
        r"|Name or service not known|getaddrinfo|No route to host|connect(?:ion)? error",
        "provider unreachable",
        "The provider is unreachable: check the API address and that the service is running.",
        "test",
        "The provider is unreachable at {address}: check the address and that the service is "
        "running.",
    ),
    _rule(
        "unavailable",
        r"Fournisseur indisponible|Provider unavailable|(?:Erreur Anthropic|Anthropic error) 5\d\d"
        r"|overloaded"
        r"|\b50[0-4]\b",
        "provider unavailable",
        "The provider is temporarily unavailable: the run will be retried.",
        "wait",
    ),
    _rule(
        "bad_response",
        r"Réponse inattendue|Unexpected response",
        "unexpected answer",
        "The provider answers, but not in the expected format: check the API address and the "
        "model.",
        "test",
    ),
    _rule(
        "no_tools",
        r"n'appelle pas les outils|does not call the tools",
        "model without tools",
        "The model answers but does not call tools: pick one that supports them.",
        "settings",
        technical=False,
    ),
    _rule(
        "refused",
        r"Requête refusée|Request refused",
        "request refused",
        "The provider refuses the request: see the technical detail.",
        "test",
    ),
    _rule(
        "run_timeout",
        r"dépassé le temps maximal|exceeded the maximum time",
        "run too long",
        "The run exceeded the time limit: it will resume at the next tidying.",
        "wait",
        technical=False,
    ),
    _rule(
        "busy",
        r"L'espace est occupé|space (?:is|stayed) busy",
        "space busy",
        "The space was busy (sync in progress): the run was postponed.",
        "wait",
        technical=False,
    ),
    _rule(
        "conflict",
        r"modifiés pendant le rangement|modified during the tidy-up",
        "pages changed meanwhile",
        "Pages were changed during the run: it was dropped and will be retried.",
        "wait",
        technical=False,
    ),
    _rule(
        "interrupted",
        r"worker arrêté|arrêt du worker|worker stopped",
        "run interrupted",
        "The run was interrupted (worker stopped): it will resume at the next tidying.",
        "wait",
        technical=False,
    ),
    _rule(
        "internal",
        r"^(?:Erreur interne|Internal error)",
        "internal error",
        "Synaptane internal error: details are in the server logs.",
        "",
    ),
)
UNKNOWN = "Unknown cause: details are in the worker logs."
# Templates produced here: the French catalog must know them all (test).
MESSAGES = tuple(
    text for rule in RULES for text in (rule.label, rule.message, rule.with_address) if text
) + ("error", "{detail}", UNKNOWN)

_ADDRESS = re.compile(r"https?://[^\s()«»\"']+")


def explain(error: str | None, config: AIConfig | None = None) -> Explanation:
    """Recorded error → code, message and action. The provider's address comes from the
    text if it quotes it, otherwise from the organisation's current configuration."""
    if not error or not error.strip():
        return Explanation("unknown", "error", UNKNOWN, "")
    detail = error.strip()
    for rule in RULES:
        if not rule.pattern.search(detail):
            continue
        if rule.with_address and (address := _address(detail, config)):
            return Explanation(
                rule.code,
                rule.label,
                rule.with_address,
                detail,
                rule.action,
                {"address": address},
                rule.technical,
            )
        return Explanation(
            rule.code, rule.label, rule.message, detail, rule.action, technical=rule.technical
        )
    return Explanation("other", "error", "{detail}", detail, "", {"detail": detail})


def _address(text: str, config: AIConfig | None) -> str:
    if found := _ADDRESS.search(text):
        return found.group(0).rstrip(".,:;")
    return config.endpoint if config is not None else ""


# --- Operations: label, link to the page, pages where an item was integrated ------------------


LINKED = (  # wiki paths
    Op.CREATED,
    Op.MODIFIED,
    Op.MOVED,
    Op.ATTACHMENT,
    Op.REDUCED,
    Op.MERGED,
    Op.CONFLICT,
)


@dataclass(frozen=True)
class Operation:
    """Line of the log. `path` is a wiki path (link) if `linked`, otherwise the title of an
    inbox item or a deleted page. `pages`: pages where the item was integrated."""

    op: str
    path: str
    linked: bool
    detail: str = ""
    pages: tuple[str, ...] = ()
    no_page: bool = False  # filed without a page given (duplicate, already there… or lost)


# End of the detail of an item filed without a page: now, and in runs logged in French.
NO_PAGES = (NO_PAGE, " — aucune page indiquée")


def operation(entry: dict[str, object]) -> Operation:
    """Reads a recorded operation. The detail of a filed item ends with " → a.md, b.md"
    (pages given by the agent) or with `NO_PAGES`: the pages become links. An explicit
    `pages` list, if the tidy-up records one, prevails."""
    op, path = str(entry.get("op", "")), str(entry.get("path", ""))
    detail = str(entry.get("detail") or "")
    pages: tuple[str, ...] = ()
    no_page = False
    if isinstance(listed := entry.get("pages"), list):
        pages = tuple(str(p) for p in listed)
    elif op == Op.FILED and " → " in detail:
        detail, _, written = detail.rpartition(" → ")
        pages = tuple(p.strip() for p in written.split(", ") if p.strip())
    if op == Op.FILED and (end := next((e for e in NO_PAGES if detail.endswith(e)), "")):
        detail, no_page = detail[: -len(end)], True
    return Operation(op, path, op in LINKED, detail.strip(), pages, no_page)


# --- Log entries: retries of a same night grouped ---------------------------------------------


@dataclass
class Entry:
    """A card of the log: one run, or several failed attempts grouped (`attempts`, the most
    recent first; `run` is the most recent)."""

    run: TidyRun
    attempts: list[TidyRun]
    explanation: Explanation | None
    operations: list[Operation]

    @property
    def grouped(self) -> bool:
        return len(self.attempts) > 1

    @property
    def tokens(self) -> int:
        return sum(r.input_tokens + r.output_tokens + r.cached_tokens for r in self.attempts)

    @property
    def cost(self) -> Decimal | None:
        costs = [r.cost for r in self.attempts if r.cost is not None]
        return sum(costs, Decimal(0)) if costs else None


def _local_day(value: datetime, tz: ZoneInfo) -> date:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(tz).date()


def entries(runs: list[TidyRun], tz: ZoneInfo, config: AIConfig | None = None) -> list[Entry]:
    """Cards of the log (runs from the most recent to the oldest). Consecutive failures of a
    same local day, with the same trigger and the same cause, are the retries of a same
    request (the queue tries 5 times): a single card, that can be expanded."""
    result: list[Entry] = []
    for run in runs:
        explanation = explain(run.error, config) if run.error or run.status == "failed" else None
        previous = result[-1] if result else None
        if (
            previous is not None
            and run.status == "failed"
            and previous.run.status == "failed"
            and run.trigger == previous.run.trigger
            and _local_day(run.started_at, tz) == _local_day(previous.run.started_at, tz)
            and explanation is not None
            and previous.explanation is not None
            and explanation.code == previous.explanation.code
        ):
            previous.attempts.append(run)
            continue
        ops = [operation(op) for op in run.operations or []]
        result.append(Entry(run, [run], explanation, ops))
    return result


def zone_of(space: Space) -> ZoneInfo:
    try:
        return ZoneInfo(space.tidy_timezone)
    except (ValueError, KeyError):
        return ZoneInfo("UTC")


# --- Current series of failures ---------------------------------------------------------------


@dataclass(frozen=True)
class Streak:
    """Failures in a row since the last successful run (the most recent: `latest`)."""

    count: int
    since: datetime
    latest: TidyRun
    explanation: Explanation
    notified: datetime | None  # alert sent to the administrators during the series


def streak(session: Session, space_id: UUID, config: AIConfig | None = None) -> Streak | None:
    last_ok = session.scalar(
        select(func.max(TidyRun.started_at)).where(
            TidyRun.space_id == space_id, TidyRun.status.in_(("done", "nothing"))
        )
    )
    failed = [TidyRun.space_id == space_id, TidyRun.status == "failed"]
    if last_ok is not None:
        failed.append(TidyRun.started_at > last_ok)
    count, since = session.execute(
        select(func.count(), func.min(TidyRun.started_at)).where(*failed)
    ).one()
    if not count:
        return None
    latest = session.scalars(
        select(TidyRun).where(*failed).order_by(TidyRun.started_at.desc()).limit(1)
    ).one()
    explanation = explain(latest.error, config)
    notified = session.scalar(
        select(func.max(AuditEvent.at)).where(
            AuditEvent.action == "alert.tidy_failed",
            AuditEvent.space_id == space_id,
            AuditEvent.at >= since,
        )
    )
    return Streak(count, since, latest, explanation, notified)


def failing(session: Session, space_id: UUID) -> bool:
    """Did the last finished run fail? (badge of the "Tidy-up" tab)"""
    status = session.scalar(
        select(TidyRun.status)
        .where(TidyRun.space_id == space_id, TidyRun.status != "running")
        .order_by(TidyRun.started_at.desc())
        .limit(1)
    )
    return status == "failed"


def org_admins(session: Session, org_id: UUID) -> list[str]:
    """Names of the organisation's administrators: who to warn when the tidy-up breaks."""
    return list(
        session.scalars(
            select(User.display_name)
            .join(OrgMember, OrgMember.user_id == User.id)
            .where(
                OrgMember.org_id == org_id,
                OrgMember.role == OrgRole.ADMIN.value,
                User.is_active.is_(True),
            )
            .order_by(User.display_name)
        )
    )


def last_test_failed(session: Session, org_id: UUID) -> bool:
    """Did the organisation's last "Test the connection" fail?"""
    row = session.get(OrgAISettings, org_id)
    return row is not None and row.test_ok is False


def average_cost(session: Session, space_id: UUID, last: int = 10) -> Decimal | None:
    """Average cost of the latest billed runs: what the confirmation announces."""
    costs = [
        cost
        for cost in session.scalars(
            select(TidyRun.cost)
            .where(TidyRun.space_id == space_id, TidyRun.cost > 0)
            .order_by(TidyRun.started_at.desc())
            .limit(last)
        )
        if cost is not None
    ]
    return sum(costs, Decimal(0)) / len(costs) if costs else None


# --- Current request: "Running" card as soon as the button is clicked ------------------------


@dataclass(frozen=True)
class Activity:
    """Tidy-up requested or under way. `state`: "queued" (waiting for the worker), "running"
    (taken by the worker, run not logged yet), "retry" (new attempt planned at `at`); `run`:
    "running" run already logged, if there is one."""

    state: str
    at: datetime | None = None
    stalled: bool = False
    run: UUID | None = None

    @property
    def poll(self) -> bool:
        """The page polls the server as long as the result can arrive at any moment (not for
        a new attempt planned in an hour)."""
        return self.state in ("queued", "running")


def activity(session: Session, space_id: UUID, now: datetime | None = None) -> Activity | None:
    now = now or datetime.now(UTC)
    running = session.scalar(
        select(TidyRun.id)
        .where(
            TidyRun.space_id == space_id,
            TidyRun.status == "running",
            TidyRun.started_at > now - ORPHAN,
        )
        .order_by(TidyRun.started_at.desc())
        .limit(1)
    )
    if running is not None:
        return Activity("running", run=running)
    jobs = session.execute(
        select(Job.status, Job.run_after).where(
            Job.kind == TIDY,
            Job.space_id == space_id,
            Job.status.in_(("queued", "running")),
        )
    ).all()
    if any(status == "running" for status, _ in jobs):
        return Activity("running")
    if not jobs:
        return None
    run_after = min(at for _, at in jobs)
    if run_after > now + SOON:
        return Activity("retry", at=run_after)
    return Activity("queued", at=run_after, stalled=now - run_after > STALLED)


def version(session: Session, space_id: UUID) -> str:
    """Fingerprint of the log's state: the page reloads when it changes."""
    latest = session.execute(
        select(TidyRun.id, TidyRun.status)
        .where(TidyRun.space_id == space_id)
        .order_by(TidyRun.started_at.desc())
        .limit(1)
    ).first()
    current = activity(session, space_id)
    return ":".join(
        [str(latest[0]) if latest else "-", latest[1] if latest else "-"]
        + [current.state if current else "idle"]
    )


# --- Next scheduled run -----------------------------------------------------------------------


@dataclass(frozen=True)
class NextRun:
    """Next scheduled run, in the space's local time; `imminent`: due at the scheduler's next
    round (time passed, not done yet today)."""

    at: datetime
    imminent: bool = False

    def day(self, now: datetime) -> str:
        """ "tonight", "today", "tomorrow" or "later" (label chosen by the page)."""
        today = now.astimezone(self.at.tzinfo).date()
        night = self.at.hour < 6
        if self.at.date() == today:
            return "tonight" if night else "today"
        if self.at.date() == today + timedelta(days=1):
            return "tonight" if night else "tomorrow"
        return "later"


def next_run(space: Space, now: datetime | None = None) -> NextRun | None:
    now = now or datetime.now(UTC)
    if not space.tidy_enabled or not space.repo_url:
        return None
    try:
        at = parse_time(space.tidy_time)
    except SynaptaneError:
        return None
    tz = zone_of(space)
    local = now.astimezone(tz)
    if due(space, now) is not None:
        return NextRun(local, imminent=True)
    later_today = local.time() < at and space.tidy_last_date != local.date()
    day = local.date() if later_today else local.date() + timedelta(days=1)
    return NextRun(datetime.combine(day, at, tzinfo=tz))
