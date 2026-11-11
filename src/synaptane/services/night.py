"""The night of a space: its routines and its tidy-up in a fixed order, then one report.

A routine joins the night with a night schedule in its file (`schedule: night`, `night monday`,
`night 1`; `routine_files`) and its place, `night: before` or `after` the tidy-up (after by
default). Every night that has something to do, at the tidy-up's time of the space:
1. the routines placed before the tidy-up, by file name;
2. the scheduled tidy-up (`tidy.run_held`), with its maintenance, if it is on;
3. the routines placed after it, by file name;
4. one report: by email (nobody, the organization's admins or the space's members) and/or to
   a Slack webhook (a secret of the space, never written to the repository), plus the
   `night.completed` event of the organization's webhooks.

Scheduling (`schedule`, each minute, before `tidy.schedule`): a night is planned when the
space has routines for that day, or when it reports and the tidy-up is due. Its steps are
written once, in a row of `night_runs` unique per space and local day: a night never runs
twice. A space without either keeps the plain scheduled tidy-up, as before.

Running (`run`, a `night` task in the worker's slow lane): the space's pass lock
(`tidy_commit.pass_lock`) is held for the whole night, so no tidy-up or routine of the space
slips in between; busy, the task comes back two minutes later. Each step is recorded before
and after it runs:
- a step that fails never stops the next ones; a transient failure is retried later on its
  own (a `tidy` or `routine` task), outside the night;
- a provider failure that breaks the AI connection (expired login, refused key…,
  `tidy_journal.CONNECTION`) skips the AI steps that remain: they would fail the same way;
- the month's budget used up skips the routines left (the tidy-up says it itself, when it had
  something to do);
- a step found "running" when the night resumes was cut off by a stopped worker: it is not run
  again (it may have cost already), it is marked interrupted;
- a night left waiting while the next one comes is skipped: never two nights in a row.

Outputs of a routine of the night stay its own (page, inbox, email, `routine.done`): the
routine's file chose them. The report only gives each routine's status, its summary in one
line and the link to its page: it never repeats the routine's text. Alerts do not change
either (a failure still alerts the admins, at most once a day per kind).

Quiet nights stay quiet: with nothing filed, fixed or run, and no failure, no report is sent,
unless the space asks for one every night (`night_report_always`).

Texts from the agents (summaries, titles of inbox items, errors) are data: the Slack text
escapes `&`, `<` and `>`, so that no `<!channel>`, `<@U…>` or link can come from them, and
defuses `@channel`, `@here` and `@everyone`; the email is plain text, never HTML.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx2
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from synaptane import __version__
from synaptane.ai import quota
from synaptane.ai.providers import open_conversation
from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError, invalid, too_many
from synaptane.core.security import SecretBox
from synaptane.services import (
    Actor,
    audit,
    budget,
    jobs,
    mail,
    routine_agent,
    routines,
    secrets,
    tidy,
    tidy_commit,
    tidy_journal,
    webhooks,
)
from synaptane.services import checks as team_checks
from synaptane.services.ai_settings import config_for
from synaptane.services.tidy_tools import Op
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import (
    AuditEvent,
    NightRun,
    Routine,
    RoutineRun,
    Secret,
    Space,
    TidyRun,
    User,
)

log = logging.getLogger("synaptane.night")

NIGHT = "night"  # kind of task: the night of a space (slow lane, with the tidy-up)
SLACK = "night_slack"  # kind of task: the report posted to Slack (webhook lane)
REPORT_WEBHOOK = "night_report_webhook"  # purpose of the secret: the Slack webhook's URL
EMAIL_TARGETS = ("", "admins", "members")
RETRY_DELAY = timedelta(minutes=2)  # a step failed on a transient error: retried on its own
STALE = timedelta(hours=12)  # a night still open after that was cut off for good
MAX_SUMMARY = 300  # characters of an agent's summary in the report
MAX_TITLES = 10  # inbox items named per routine
MAX_TITLE = 120
MAX_SLACK = 12_000  # characters of the Slack text (Slack accepts 40,000)
TEST_ACTION = "space.night_report_test"  # audit action of "Send a test report"
TESTS_PER_HOUR = 5  # per space: enough to try a webhook, not to flood its channel
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # but \t and \n
# Direction marks and overrides: they would make a text read in another order than written
# (a link or an address disguised as another one).
_BIDI = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")

# Why a step did not run, or how it failed (`steps[].reason`).
REASONS = {
    "disabled": "turned off",
    "earlier": "already done today",
    "no_ai": "no AI provider configured",
    "budget": "skipped: monthly AI budget reached",
    "connection": "skipped: the AI connection failed earlier in the night",
    "changed": "skipped: the routine was changed or removed since",
    "retry": "failed, tried again later",
    "interrupted": "interrupted (worker stopped)",
    "superseded": "skipped: a later night ran instead",
    "internal": "internal error",
}
# Reasons that are news even without a run: something should have happened and did not.
NOTABLE = frozenset({"budget", "connection", "retry", "interrupted", "superseded", "internal"})

ConversationFactory = tidy.ConversationFactory


# --- Settings of the report ------------------------------------------------------------------


@dataclass(frozen=True)
class ReportSettings:
    """Who receives the night's report. `webhook`: a Slack webhook is configured (its URL is
    never shown again)."""

    email: str
    always: bool
    webhook: bool

    @property
    def active(self) -> bool:
        return bool(self.email or self.webhook)


def has_webhook(session: Session, space_id: UUID) -> bool:
    found = session.scalar(
        select(Secret.id).where(Secret.space_id == space_id, Secret.purpose == REPORT_WEBHOOK)
    )
    return found is not None


def report_settings(session: Session, space: Space) -> ReportSettings:
    return ReportSettings(
        space.night_report_email, space.night_report_always, has_webhook(session, space.id)
    )


def check_webhook_url(url: str, allow_private: bool = False) -> str:
    """A Slack incoming webhook (or a compatible one: Mattermost, Rocket.Chat…): HTTPS, a host,
    no credentials in the address. Internal addresses are refused when it is called."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username:
        raise invalid("Invalid webhook URL.")
    if parts.scheme == "http" and not allow_private:
        raise invalid("The webhook must use HTTPS.")
    return url


def update_report(
    session: Session,
    actor: Actor,
    box: SecretBox,
    space: Space,
    *,
    email: str | None = None,
    always: bool | None = None,
    webhook_url: str | None = None,
    allow_private: bool = False,
) -> None:
    """`webhook_url`: None keeps it, "" removes it; it is stored encrypted, bound to the space,
    and only "configured" is ever shown again."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    changed: list[str] = []
    if email is not None:
        if email not in EMAIL_TARGETS:
            raise invalid("Unknown email recipients.")
        if email != space.night_report_email:
            space.night_report_email = email
            changed.append("report_email")
    if webhook_url is not None:
        url = webhook_url.strip()
        if url:
            secrets.put_space_secret(
                session,
                box,
                space.id,
                REPORT_WEBHOOK,
                check_webhook_url(url, allow_private),
                actor.user.id,
            )
            changed.append("report_webhook")
        elif secrets.delete_space_secret(session, space.id, REPORT_WEBHOOK):
            changed.append("report_webhook")
    if always is not None and always != space.night_report_always:
        space.night_report_always = always
        changed.append("report_always")
    if changed:
        session.flush()
        audit(session, actor, "space.tidy_settings", space=space, fields=" ".join(changed))


# --- Planning ---------------------------------------------------------------------------------


def due_day(space: Space, now: datetime) -> date | None:
    """Local day whose night is due: the tidy-up's time reached, the space synchronized once,
    and not created after that time today (as `tidy.due`, whether the tidy-up is on or not)."""
    if not space.repo_url or space.sync_status == "never":
        return None
    try:
        tz = tidy.zone(space.tidy_timezone)
        at = tidy.parse_time(space.tidy_time)
    except SynaptaneError:
        return None
    local = now.astimezone(tz)
    if local.time() < at:
        return None
    if space.created_at and space.created_at >= datetime.combine(local.date(), at, tzinfo=tz):
        return None
    return local.date()


def _step(routine: Routine) -> dict[str, object]:
    return {
        "kind": "routine",
        "when": routine.night,
        "name": routine.name,
        "title": routine.title or routine.name,
        "routine_id": str(routine.id),
        "status": "queued",
    }


def plan(routines_of_day: list[Routine], tidy_step: dict[str, object]) -> list[dict[str, object]]:
    """Steps in order: routines before the tidy-up, the tidy-up, routines after it; each group
    by file name (a team orders them by naming them `10-…`, `20-…`)."""
    ordered = sorted(routines_of_day, key=lambda r: r.name)
    return (
        [_step(r) for r in ordered if r.night == "before"]
        + [tidy_step]
        + [_step(r) for r in ordered if r.night != "before"]
    )


def schedule(database: Database, now: datetime | None = None) -> int:
    """Plans the nights due and queues them (one task per space). Returns their number.

    Called before `tidy.schedule`: the tidy-up of a night is taken out of the plain schedule
    (`tidy_last_date` set), so it runs once, in its place in the night."""
    now = now or datetime.now(UTC)
    count = 0
    with database.session() as session:
        candidates: dict[UUID, list[Routine]] = {}
        for routine in session.scalars(
            select(Routine).where(
                Routine.night != "", Routine.enabled.is_(True), Routine.error.is_(None)
            )
        ):
            candidates.setdefault(routine.space_id, []).append(routine)
        reporting = set(
            session.scalars(select(Space.id).where(Space.night_report_email != "")).all()
        ) | set(
            session.scalars(select(Secret.space_id).where(Secret.purpose == REPORT_WEBHOOK)).all()
        )
        wanted = set(candidates) | reporting
        if not wanted:
            return 0
        for space in session.scalars(select(Space).where(Space.id.in_(wanted))):
            day = due_day(space, now)
            if day is None:
                continue
            tidy_due = tidy.due(space, now) == day
            tonight = [
                r
                for r in candidates.get(space.id, [])
                if r.last_date != day and routines.matches(r, day)
            ]
            if not tonight and not (tidy_due and space.id in reporting):
                continue  # nothing of the night: the tidy-up alone stays with `tidy.schedule`
            if tidy_due:
                tidy_step: dict[str, object] = {"kind": "tidy", "when": "", "status": "queued"}
            else:
                reason = "disabled" if not space.tidy_enabled else "earlier"
                tidy_step = {"kind": "tidy", "when": "", "status": "skipped", "reason": reason}
            created = session.execute(
                insert(NightRun)
                .values(space_id=space.id, day=day, status="queued", steps=plan(tonight, tidy_step))
                .on_conflict_do_nothing(index_elements=["space_id", "day"])
                .returning(NightRun.id)
            ).scalar_one_or_none()
            for routine in tonight:
                routine.last_date = day
            if tidy_due:
                space.tidy_last_date = day
            if created is None:
                continue  # already planned for that day: never twice
            jobs.enqueue(session, NIGHT, space.id, {"night": str(created)})
            count += 1
    return count


# --- Running ----------------------------------------------------------------------------------


def run(
    database: Database,
    ws: Workspace,
    space_id: UUID,
    *,
    factory: ConversationFactory = open_conversation,
    public_url: str = "",
) -> NightRun | None:
    """Runs the waiting night of a space (worker). Returns it, or None if there was none or
    the space was busy (queued again a little later)."""
    with tidy_commit.pass_lock(database.engine, space_id) as acquired:
        if acquired:
            return _run_pending(database, ws, space_id, factory, public_url)
    with database.session() as session:
        jobs.enqueue(session, NIGHT, space_id, delay=tidy.BUSY_DELAY)
    log.info("Space %s: a tidy-up or a routine is running, night queued again.", space_id)
    return None


def _close(night: NightRun, reason: str, now: datetime) -> None:
    """Closes a night that will not run (any longer): its waiting steps are skipped."""
    steps = []
    for step in night.steps:
        if step.get("status") == "queued":
            step = {**step, "status": "skipped", "reason": reason}
        elif step.get("status") == "running":
            step = {**step, "status": "failed", "reason": "interrupted"}
        steps.append(step)
    night.steps = steps
    night.status, night.finished_at = "done", now
    night.started_at = night.started_at or now


def _run_pending(
    database: Database,
    ws: Workspace,
    space_id: UUID,
    factory: ConversationFactory,
    public_url: str,
) -> NightRun | None:
    with database.session() as session:
        waiting = list(
            session.scalars(
                select(NightRun)
                .where(NightRun.space_id == space_id, NightRun.status != "done")
                .order_by(NightRun.day)
            )
        )
        if not waiting:
            return None
        *older, night = waiting
        now = datetime.now(UTC)
        for old in older:
            _close(old, "superseded", now)
        # Resumed after a stopped worker: what was running is not run again.
        night.steps = [
            {**s, "status": "failed", "reason": "interrupted"}
            if s.get("status") == "running"
            else s
            for s in night.steps
        ]
        night.status, night.started_at = "running", night.started_at or now
        night_id, count = night.id, len(night.steps)
    broken = False
    for index in range(count):
        with database.session() as session:
            found = session.get(NightRun, night_id)
            if found is None:
                return None  # space deleted
            step = dict(found.steps[index])
            if step.get("status") != "queued":
                continue
            _save_step(found, index, {**step, "status": "running"})
            started = datetime.now(UTC)
        try:
            result = (
                _tidy(database, ws, space_id, broken, factory, public_url, started)
                if step["kind"] == "tidy"
                else _routine(database, ws, space_id, step, broken, factory, public_url, started)
            )
        except Exception:  # noqa: BLE001 — a step must never stop the night
            log.exception("Night %s: step %d failed", night_id, index)
            result = {"status": "failed", "reason": "internal"}
        broken = broken or bool(result.pop("broken", False))
        with database.session() as session:
            found = session.get(NightRun, night_id)
            if found is None:
                return None
            _save_step(found, index, {**step, **result})
    return finish(database, ws, night_id, public_url)


def _save_step(night: NightRun, index: int, step: dict[str, object]) -> None:
    steps = list(night.steps)
    steps[index] = step
    night.steps = steps  # a new list: the JSON column is written again


def _connection_broken(error: str | None) -> bool:
    """The provider refuses the connection itself (login, key, model, address): the other
    steps of the night would fail the same way. A rate limit or an overloaded provider passes."""
    code = tidy_journal.explain(error).code
    return code in tidy_journal.CONNECTION and code not in tidy_journal.PASSING


def _latest_run(session: Session, model: type[Any], since: datetime, **where: object) -> Any:
    query = select(model).where(model.started_at >= since - timedelta(seconds=5))
    for name, value in where.items():
        query = query.where(getattr(model, name) == value)
    return session.scalars(query.order_by(model.started_at.desc()).limit(1)).first()


def _tidy(
    database: Database,
    ws: Workspace,
    space_id: UUID,
    broken: bool,
    factory: ConversationFactory,
    public_url: str,
    started: datetime,
) -> dict[str, object]:
    with database.session() as session:
        space = session.get(Space, space_id)
        if space is None or not space.tidy_enabled:
            return {"status": "skipped", "reason": "disabled"}
        if config_for(session, ws.box, space) is None:
            return {"status": "skipped", "reason": "no_ai"}
    if broken:
        return {"status": "skipped", "reason": "connection"}
    try:
        record = tidy.run_held(database, ws, space_id, factory=factory, public_url=public_url)
    except tidy.RetryLater:
        with database.session() as session:
            jobs.enqueue(session, tidy.TIDY, space_id, {"trigger": "schedule"}, delay=RETRY_DELAY)
            failed = _latest_run(session, TidyRun, started, space_id=space_id)
            return {
                "status": "failed",
                "reason": "retry",
                "run_id": str(failed.id) if failed else None,
            }
    if record is None:
        return {"status": "nothing"}
    result: dict[str, object] = {"status": record.status, "run_id": str(record.id)}
    if record.status == "failed" and _connection_broken(record.error):
        result["broken"] = True
    return result


def _routine(
    database: Database,
    ws: Workspace,
    space_id: UUID,
    step: dict[str, object],
    broken: bool,
    factory: ConversationFactory,
    public_url: str,
    started: datetime,
) -> dict[str, object]:
    routine_id = UUID(str(step["routine_id"]))
    with database.session() as session:
        routine = session.get(Routine, routine_id)
        if routine is None or routine.error is not None or not routine.enabled or not routine.night:
            return {"status": "skipped", "reason": "changed"}
        space = session.get(Space, space_id)
        assert space is not None
        if config_for(session, ws.box, space) is None:
            return {"status": "skipped", "reason": "no_ai"}
        if budget.refusal(session, space.org_id) is not None:
            return {"status": "skipped", "reason": "budget"}
    if broken:
        return {"status": "skipped", "reason": "connection"}
    try:
        record = routine_agent.run_one(
            database, ws, routine_id, trigger="night", factory=factory, public_url=public_url
        )
    except tidy.RetryLater:
        # `run_one` put the request back on the routine: its own task runs it again later.
        with database.session() as session:
            jobs.enqueue(session, routines.ROUTINE, space_id, delay=RETRY_DELAY)
            failed = _latest_run(session, RoutineRun, started, routine_id=routine_id)
            return {
                "status": "failed",
                "reason": "retry",
                "run_id": str(failed.id) if failed else None,
            }
    if record is None:
        return {"status": "skipped", "reason": "changed"}
    result: dict[str, object] = {"status": record.status, "run_id": str(record.id)}
    if record.status == "failed" and _connection_broken(record.error):
        result["broken"] = True
    return result


def close_orphans(database: Database, now: datetime | None = None) -> int:
    """Closes the nights still open after STALE (task abandoned, worker gone for good): their
    waiting steps are skipped, nothing is sent."""
    now = now or datetime.now(UTC)
    with database.session() as session:
        stale = list(
            session.scalars(
                select(NightRun).where(NightRun.status != "done", NightRun.created_at < now - STALE)
            )
        )
        for night in stale:
            _close(night, "interrupted", now)
        return len(stale)


# --- The report -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """Why a step failed, as `tidy_journal.explain` puts it (templates to translate)."""

    message: str
    params: dict[str, str]


@dataclass(frozen=True)
class TidyLine:
    status: str  # done, failed, nothing, skipped, queued
    reason: str = ""
    filed: int = 0
    left: int = 0
    fixed: int = 0
    problems_left: int = 0
    summary: str = ""
    problem: Problem | None = None

    @property
    def notable(self) -> bool:
        if self.reason in NOTABLE or self.status == "failed":
            return True
        return self.status == "done" and bool(
            self.filed + self.left + self.fixed + self.problems_left
        )


@dataclass(frozen=True)
class RoutineLine:
    title: str
    when: str
    status: str
    reason: str = ""
    summary: str = ""
    page: str = ""
    inbox: tuple[str, ...] = ()
    problem: Problem | None = None
    routine_id: str = ""

    @property
    def notable(self) -> bool:
        return self.status in ("done", "failed") or self.reason in NOTABLE


@dataclass(frozen=True)
class Report:
    space: str
    org: str
    day: date
    base: str  # the space's address on the web: `{public_url}/o/{org}/{space}`
    tidy: TidyLine | None
    routines: tuple[RoutineLine, ...]
    checks: int = 0  # team checks broken at the last commit
    cost: Decimal | None = None
    quota: dict[str, Any] | None = None
    month: tuple[Decimal, Decimal] | None = None  # (spent, budget) of the month
    test: bool = False
    steps: list[dict[str, object]] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        """Nothing filed, fixed or run, no failure: no report unless asked for every night."""
        lines: list[TidyLine | RoutineLine] = [*self.routines]
        if self.tidy is not None:
            lines.append(self.tidy)
        return not any(line.notable for line in lines)


Scrub = Callable[[str], str]


def scrub_agent_text(text: str) -> str:
    """THE point every free text of an agent goes through before the report (summaries of the
    tidy-up and of the routines, titles of the items they dropped, details of an error): the
    hook where the team checks' masking of personal data (`Checks.scrub`) plugs in. Here: one
    line, without control characters nor direction overrides. Never a page's content: the
    report is built from the counters and operations already logged, and from these short
    texts only."""
    return " ".join(_BIDI.sub("", _CONTROL.sub(" ", text)).split())


def _problem(error: str | None, scrub: Scrub) -> Problem | None:
    if not error:
        return None
    why = tidy_journal.explain(error)
    return Problem(why.message, {k: scrub(v) for k, v in why.params.items()})


def _one_line(text: str, scrub: Scrub, limit: int = MAX_SUMMARY) -> str:
    text = scrub(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build(
    session: Session,
    space: Space,
    night: NightRun,
    public_url: str,
    scrub: Scrub = scrub_agent_text,
) -> Report:
    """What the night did, read from its steps and their logs. `scrub` cleans every free
    text of an agent (`scrub_agent_text`)."""
    tidy_line: TidyLine | None = None
    lines: list[RoutineLine] = []
    cost: Decimal | None = None
    last_quota: dict[str, Any] | None = None
    for step in night.steps:
        run_id = step.get("run_id")
        status, reason = str(step.get("status", "")), str(step.get("reason", ""))
        if step.get("kind") == "tidy":
            run = session.get(TidyRun, UUID(str(run_id))) if run_id else None
            if run is None:
                tidy_line = TidyLine(status, reason)
                continue
            ops = [str(op.get("op")) for op in run.operations or []]
            tidy_line = TidyLine(
                status,
                reason,
                run.items_done,
                run.items_left,
                ops.count(Op.FIXED),
                ops.count(Op.PROBLEM_LEFT),
                _one_line(run.summary, scrub),
                _problem(run.error, scrub) if run.status == "failed" else None,
            )
            if run.cost is not None:
                cost = (cost or Decimal(0)) + run.cost
            last_quota = run.quota or last_quota
            continue
        routine_run = session.get(RoutineRun, UUID(str(run_id))) if run_id else None
        title = _one_line(str(step.get("title") or step.get("name") or ""), scrub, MAX_TITLE)
        if routine_run is None:
            lines.append(
                RoutineLine(
                    title,
                    str(step.get("when", "")),
                    status,
                    reason,
                    routine_id=str(step.get("routine_id") or ""),
                )
            )
            continue
        outputs = routine_run.outputs or {}
        found = outputs.get("inbox")
        titles: list[object] = found if isinstance(found, list) else []
        lines.append(
            RoutineLine(
                title,
                str(step.get("when", "")),
                routine_run.status,
                reason,
                _one_line(routine_run.summary, scrub) if routine_run.status == "done" else "",
                str(outputs.get("page") or ""),
                tuple(_one_line(str(t), scrub, MAX_TITLE) for t in titles),
                _problem(routine_run.error, scrub) if routine_run.status == "failed" else None,
                str(step.get("routine_id") or ""),
            )
        )
        if routine_run.cost is not None:
            cost = (cost or Decimal(0)) + routine_run.cost
        last_quota = routine_run.quota or last_quota
    org = space.organization
    spending = budget.spending(session, space.org_id)
    return Report(
        space.name,
        org.name,
        night.day,
        f"{public_url}/o/{org.slug}/{space.slug}",
        tidy_line,
        tuple(lines),
        cost=cost,
        quota=last_quota,
        month=(spending.spent, spending.budget) if spending.budget else None,
        steps=list(night.steps),
    )


def _checks(ws: Workspace, space: Space) -> int:
    """Team checks broken at the space's last commit (as Health counts them); 0 if unknown."""
    try:
        return team_checks.at_commit(ws.local_repository(space)).total
    except Exception:  # noqa: BLE001 — an extra line of the report, never a failure
        log.warning("Night of %s: team checks not counted", space.id, exc_info=True)
        return 0


# --- Writing it: plain text (email) and Slack ---------------------------------------------------


_MENTION = re.compile(r"@(channel|here|everyone)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Style:
    """How a report is written: `text` makes an agent's text safe, `link` and `bold` format."""

    text: Callable[[str], str]
    link: Callable[[str, str, Callable[..., str]], str]  # url, label, translate
    bold: Callable[[str], str]


def plain_text(value: str) -> str:
    return _CONTROL.sub("", value)


def slack_text(value: str) -> str:
    """Slack mrkdwn: `&`, `<` and `>` escaped (no `<!channel>`, `<@U…>` or `<url|…>` can come
    from an agent's text), `@channel`, `@here` and `@everyone` defused (a zero-width space)."""
    value = _CONTROL.sub("", value)
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MENTION.sub("@​\\1", value)


PLAIN = Style(
    plain_text,
    lambda url, label, translate: translate("{label}: {url}", label=label, url=url),
    lambda text: text,
)
SLACK_STYLE = Style(
    slack_text,
    # Our own addresses, their paths quoted: no `|` or `>` can end the link early.
    lambda url, label, _translate: f"<{url}|{slack_text(label)}>",
    lambda text: f"*{text}*",
)


def _euros(amount: Decimal, locale: str) -> str:
    value = float(amount)
    digits = 2 if value >= 0.01 or value == 0 else 4
    if locale == "fr":
        return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",") + " €"
    return f"€{value:,.{digits}f}"


# Counts, written in the catalog with their two forms (`i18n.plural`).
COUNTS = {
    "filed": ("{n} item filed", "{n} items filed"),
    "fixed": ("{n} problem fixed", "{n} problems fixed"),
    "problems_left": ("{n} problem left", "{n} problems left"),
}
STATUSES = {
    "done": "done",
    "failed": "failed",
    "nothing": "nothing to do",
    "queued": "not run",
    "running": "not run",
    "skipped": "not run",
}
PLACES = {"before": "before the tidy-up", "after": "after the tidy-up"}


def _count(translate: Callable[..., str], key: str, n: int) -> str:
    from synaptane.web.i18n import plural

    one, many = COUNTS[key]
    return translate(plural("en", n, one, many), n=n)


def _status(translate: Callable[..., str], status: str, reason: str) -> str:
    if reason in REASONS:
        return translate(REASONS[reason])
    return translate(STATUSES[status]) if status in STATUSES else status


def _cause(
    translate: Callable[..., str], problem: Problem | None, safe: Callable[[str], str]
) -> str:
    if problem is None:
        return translate(STATUSES["failed"])
    return translate(problem.message, **{k: safe(v) for k, v in problem.params.items()})


def _tidy_lines(report: Report, translate: Callable[..., str], style: Style) -> list[str]:
    line = report.tidy
    if line is None:
        return []
    if line.status == "done":
        parts = [
            _count(translate, "filed", line.filed),
            translate("{n} left in the inbox", n=line.left),
        ]
        if line.fixed or line.problems_left:
            parts += [
                _count(translate, "fixed", line.fixed),
                _count(translate, "problems_left", line.problems_left),
            ]
        out = [translate("Tidy-up: {result}.", result=", ".join(parts))]
        if line.summary:
            out.append("  " + style.text(line.summary))
    elif line.status == "failed":
        out = [
            translate("Tidy-up failed: {cause}", cause=_cause(translate, line.problem, style.text))
        ]
        if line.reason == "retry":
            out.append("  " + translate("It is tried again later, outside the night."))
    else:
        out = [translate("Tidy-up: {result}.", result=_status(translate, line.status, line.reason))]
    return [*out, ""]


def _routine_lines(report: Report, translate: Callable[..., str], style: Style) -> list[str]:
    from synaptane.web.i18n import quoted_names

    if not report.routines:
        return []
    safe = style.text
    out = [style.bold(translate("Routines"))]
    for line in report.routines:
        if line.status == "failed" and line.problem is not None:
            status = translate("failed: {cause}", cause=_cause(translate, line.problem, safe))
        else:
            status = _status(translate, line.status, line.reason)
        title = safe(line.title)
        if line.when in PLACES:
            title = translate("{title} ({place})", title=title, place=translate(PLACES[line.when]))
        out.append("- " + translate("{title}: {status}", title=title, status=status))
        if line.summary:
            out.append("  " + safe(line.summary))
        if line.page:
            url = f"{report.base}/p/{quote(line.page, safe='/')}"
            out.append("  " + style.link(url, translate("See the report"), translate))
        if line.inbox:
            shown = quoted_names(translate, [safe(t) for t in line.inbox[:MAX_TITLES]])
            if len(line.inbox) > MAX_TITLES:
                shown += f" (+{len(line.inbox) - MAX_TITLES})"
            out.append(
                "  "
                + translate(
                    "Dropped into the inbox ({n}): {titles}", n=len(line.inbox), titles=shown
                )
            )
        if line.routine_id and (line.status == "failed" or line.reason in NOTABLE):
            out.append(
                "  "
                + style.link(
                    f"{report.base}/-/routines/{line.routine_id}",
                    translate("Routine log"),
                    translate,
                )
            )
    return [*out, ""]


def write(report: Report, locale: str, style: Style = PLAIN) -> str:
    """The report in a language and a style. Every text from an agent goes through
    `style.text`; links are built here, from the space's address."""
    from synaptane.web.i18n import translator

    translate = translator(locale)
    lines: list[str] = []
    if report.test:
        lines += [translate("Test report: this is what the night's report looks like."), ""]
    title = translate(
        "Night of {day} in “{space}” ({org})",
        day=report.day,
        space=style.text(report.space),
        org=style.text(report.org),
    )
    lines += [style.bold(title), ""]
    lines += _tidy_lines(report, translate, style)
    lines += _routine_lines(report, translate, style)
    if report.checks:
        health = style.link(f"{report.base}/health", translate("Health"), translate)
        lines += [
            translate("Team checks: {n} broken at the last commit.", n=report.checks)
            + " "
            + health,
            "",
        ]
    if report.quota:
        lines.append(
            translate("Subscription quota: {usage}", usage=quota.summary(report.quota, locale))
        )
    elif report.cost is not None:
        cost = translate("Cost of the night: ≈ {amount}", amount=_euros(report.cost, locale))
        if report.month is not None:
            cost += " " + translate(
                "(this month: {spent} of {budget})",
                spent=_euros(report.month[0], locale),
                budget=_euros(report.month[1], locale),
            )
        lines.append(cost)
    lines.append(style.link(f"{report.base}/tidy", translate("Log of the night"), translate))
    text = "\n".join(lines).strip() + "\n"
    if style is SLACK_STYLE and len(text) > MAX_SLACK:
        text = text[:MAX_SLACK].rsplit("\n", 1)[0] + "\n…\n"
    return text


def night_event(report: Report) -> dict[str, object]:
    """Data of the `night.completed` webhook event (JSON: the receiver escapes it)."""
    return {
        "day": report.day.isoformat(),
        "quiet": report.quiet,
        "steps": [
            {
                k: step.get(k)
                for k in ("kind", "when", "name", "title", "status", "reason", "run_id")
                if step.get(k) is not None
            }
            for step in report.steps
        ],
        "checks": report.checks,
        "cost": str(report.cost) if report.cost is not None else None,
    }


def _send_emails(
    session: Session,
    ws: Workspace,
    space: Space,
    report: Report,
    target: str,
    public_url: str,
    only: list[User] | None = None,
) -> int:
    if not target or not mail.configured(session):
        return 0
    people = only if only is not None else routine_agent.recipients(session, space, target)
    texts: dict[str, str] = {}
    sent = 0
    for user in people:
        if not user.email:
            continue
        text = texts.setdefault(user.locale, write(report, user.locale))
        subject, message = mail.compose(
            "night_report",
            user.locale,
            space=space.name,
            org=space.organization.name,
            day=report.day,
            text=text,
            link=f"{report.base}/tidy",
            account=f"{public_url}/account",
        )
        if report.test:
            subject = "[test] " + subject
        try:
            mail.send(session, ws.box, user.email, subject, message)
            sent += 1
        except mail.MailError as error:
            log.warning("Night report not sent to %s: %s", user.email, error)
    return sent


def _slack_locale(session: Session, space_id: UUID) -> str:
    """Language of the Slack text: that of the person who set the webhook."""
    secret = session.scalar(
        select(Secret).where(Secret.space_id == space_id, Secret.purpose == REPORT_WEBHOOK)
    )
    if secret is None or secret.updated_by is None:
        return "en"
    user = session.get(User, secret.updated_by)
    return user.locale if user is not None else "en"


def _queue_slack(session: Session, space: Space, report: Report, night_id: UUID | None) -> bool:
    if not has_webhook(session, space.id):
        return False
    text = write(report, _slack_locale(session, space.id), SLACK_STYLE)
    payload: dict[str, object] = {"space_id": str(space.id), "text": text}
    if night_id is not None:
        payload["night"] = str(night_id)
    # No space on the task: deliveries are not merged (a test next to the night's report).
    jobs.enqueue(session, SLACK, None, payload)
    return True


def finish(database: Database, ws: Workspace, night_id: UUID, public_url: str) -> NightRun | None:
    """End of the night: the report, sent once (the night is closed in the same transaction
    that queues it)."""
    link = public_url or ws.public_url
    with database.session() as session:
        night = session.get(NightRun, night_id, with_for_update=True)
        if night is None or night.status == "done":
            return night
        space = session.get(Space, night.space_id)
        assert space is not None
        report = build(session, space, night, link)
        report = replace(report, checks=_checks(ws, space))
        settings = report_settings(session, space)
        sent: dict[str, object] = {"quiet": report.quiet}
        if settings.active and (not report.quiet or settings.always):
            sent["emails"] = _send_emails(session, ws, space, report, settings.email, link)
            sent["slack"] = "queued" if _queue_slack(session, space, report, night.id) else ""
        webhooks.emit(session, space, "night.completed", night_event(report))
        night.report = sent
        night.status, night.finished_at = "done", datetime.now(UTC)
        session.flush()
        session.expunge(night)
        return night


def send_test(
    session: Session, actor: Actor, ws: Workspace, space: Space, public_url: str
) -> tuple[bool, bool]:
    """ "Send a test report": the last night's report (or an empty one), marked as a test, by
    email to the person asking only (never to every member), and to the Slack webhook;
    TESTS_PER_HOUR per space, so that the button cannot flood a channel. Returns (emailed,
    posted)."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    settings = report_settings(session, space)
    if not settings.active:
        raise invalid("Choose who receives the report first: by email, or a Slack webhook.")
    recent = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.space_id == space.id,
            AuditEvent.action == TEST_ACTION,
            AuditEvent.at >= datetime.now(UTC) - timedelta(hours=1),
        )
    )
    if (recent or 0) >= TESTS_PER_HOUR:
        raise too_many(
            "At most {limit} test reports per hour for a space: try again later.",
            limit=TESTS_PER_HOUR,
        )
    last = session.scalars(
        select(NightRun)
        .where(NightRun.space_id == space.id, NightRun.status == "done")
        .order_by(NightRun.day.desc())
        .limit(1)
    ).first()
    org = space.organization
    if last is not None:
        report = build(session, space, last, public_url)
    else:
        report = Report(
            space.name,
            org.name,
            datetime.now(UTC).astimezone(tidy.zone(space.tidy_timezone)).date(),
            f"{public_url}/o/{org.slug}/{space.slug}",
            TidyLine("nothing"),
            (),
        )
    report = replace(report, test=True)
    emailed = False
    if settings.email:
        user = actor.user
        if not user.email or user.email_verified_at is None:
            raise invalid("Your account has no verified email address: the test cannot be sent.")
        if not mail.configured(session):
            raise invalid("Email sending is not configured on this instance.")
        emailed = _send_emails(session, ws, space, report, settings.email, public_url, [user]) > 0
        if not emailed:
            raise invalid("The test email could not be sent: see the server log.")
    posted = _queue_slack(session, space, report, None)
    audit(session, actor, TEST_ACTION, space=space)
    return emailed, posted


# --- Slack delivery (worker, webhook lane) --------------------------------------------------------


def deliver_slack(
    database: Database, box: SecretBox, payload: dict[str, Any], *, allow_private: bool = False
) -> None:
    """Posts a report to the space's Slack webhook: `{"text": …}` in mrkdwn, without link
    previews. Same protections as the organization's webhooks (internal addresses refused,
    the checked address is the one connected to, no redirect); a transient failure is retried
    by the queue. The URL is read at delivery time: a webhook removed since gets nothing."""
    space_id = UUID(str(payload["space_id"]))
    with database.session() as session:
        url = secrets.get_space_secret(session, box, space_id, REPORT_WEBHOOK)
    if not url:
        return
    body = json.dumps(
        {
            "text": str(payload["text"]),
            "mrkdwn": True,
            "unfurl_links": False,
            "unfurl_media": False,
        },
        ensure_ascii=False,
    ).encode()
    headers = {"Content-Type": "application/json", "User-Agent": f"Synaptane/{__version__}"}
    extensions: dict[str, str] = {}
    error: webhooks.DeliveryError | None = None
    try:
        if not allow_private:
            url, host, extensions = webhooks.pinned_request(url)
            headers |= host
        with httpx2.Client(timeout=webhooks.TIMEOUT, follow_redirects=False) as client:
            request = client.build_request(
                "POST", url, content=body, headers=headers, extensions=extensions
            )
            response = client.send(request, stream=True)
            response.close()
        status = str(response.status_code)
        if response.status_code >= 500 or response.status_code == 429:
            error = webhooks.DeliveryError(f"Response {status}", transient=True)
        elif response.status_code >= 400:
            error = webhooks.DeliveryError(f"Response {status}", transient=False)
    except httpx2.HTTPError as failure:
        status = f"network error: {type(failure).__name__}"
        error = webhooks.DeliveryError("Receiver unreachable.", transient=True)
    except webhooks.DeliveryError as failure:
        # The host or the address it names stays in the task's error (administrators): the
        # night's log is seen by every reader of the space.
        status = "Receiver unreachable." if failure.transient else "Internal address refused."
        error = failure
    night_id = payload.get("night")
    if night_id:
        with database.session() as session:
            night = session.get(NightRun, UUID(str(night_id)), with_for_update=True)
            if night is not None:
                night.report = {**night.report, "slack": "sent" if error is None else status[:200]}
    if error is not None:
        raise error


# --- Shown to people ----------------------------------------------------------------------------


def view(session: Session, actor: Actor, space: Space) -> ReportSettings:
    """The report's settings (organization admins: they name recipients and a secret)."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    return report_settings(session, space)


def nights(session: Session, actor: Actor, space: Space, limit: int = 7) -> list[NightRun]:
    """The last nights of the space, from the most recent (its Tidy-up tab, REST)."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    return list(
        session.scalars(
            select(NightRun)
            .where(NightRun.space_id == space.id)
            .order_by(NightRun.day.desc())
            .limit(limit)
        )
    )


def routines_of(session: Session, actor: Actor, space: Space) -> list[Routine]:
    """The space's routines of the night, in their order: before the tidy-up, then after it,
    each group by file name (whatever their days)."""
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    found = session.scalars(
        select(Routine).where(Routine.space_id == space.id, Routine.night != "")
    ).all()
    return sorted(found, key=lambda r: (r.night != "before", r.name))


def step_label(step: dict[str, object]) -> str:
    """What a step did, as an interface text to translate."""
    return _status(lambda text, **_: text, str(step.get("status", "")), str(step.get("reason", "")))
