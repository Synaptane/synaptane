"""Tidy-up of the inbox by an AI agent (docs/concepts.md).

Course of a run:
1. The inbox is read at the latest commit; nothing to do if it has no item.
2. The agent explores `content/` (tree, reading, search, backlinks), then writes, moves or
   deletes pages. Its changes pile up in memory (`Overlay`): nothing is written until the run
   is over.
3. Each item is closed by `mark_done` (removed from the inbox) or `leave_in_inbox` (left with
   a reason: a decision of the agent). An item presented but not closed (provider error, cap,
   budget) is "not processed": it stays new, with a counter, and is only left after
   MAX_ITEM_ATTEMPTS interrupted runs; the admins are notified.
4. A single commit gathers everything, in the name of the tidy-up agent (`AGENT_NAME`), then
   it is pushed. A page modified by a human in the meantime is merged file by file; in case
   of conflict, its human version is kept and the items filed in it are not processed.
   The model is never run again for a write (see `tidy_commit`).
5. The run is logged (operations, cost) and can be undone (Git revert).

A single run at a time per space, even with several workers (`tidy_commit.pass_lock`).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.ai import pricing, quota
from synaptane.ai.base import (
    SUBSCRIPTION_PROVIDERS,
    AIConfig,
    AIError,
    Conversation,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from synaptane.ai.providers import open_conversation
from synaptane.core.access import Action, require
from synaptane.core.errors import Label, SynaptaneError, conflict, invalid, not_found, too_many
from synaptane.core.security import SecretBox
from synaptane.git.repo import GitError, Identity, Repository, safe_path
from synaptane.services import (
    Actor,
    alerts,
    audit,
    budget,
    jobs,
    tidy_commit,
    tidy_metrics,
    tidy_notices,
    vocation,
    webhooks,
)
from synaptane.services import changes as changes_service
from synaptane.services import search as search_service
from synaptane.services.ai_settings import check_space_model, config_for
from synaptane.services.deposits import list_items
from synaptane.services.inbox import (
    AGENT_NAME,
    CONTENT_DIR,
    attachments_dir,
    front_matter,
)
from synaptane.services.tidy_tools import (
    CUSTOM,
    INSTRUCTIONS,
    MAX_DELETIONS,
    TOOLS,
    VOCATION,
    Op,
    Overlay,
    RunState,
    ToolFailure,
    Tools,
    first_message,
    nearby_for,
)
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database, try_space_lock
from synaptane.storage.models import Space, TidyRun, User

log = logging.getLogger("synaptane.tidy")
TIDY = "tidy"


MAX_ITEMS = 25  # per run; the rest goes into a chained run


MAX_CHAIN = 8  # chained runs at most after the first one (plus the monthly budget)


MAX_STEPS = 120  # calls to the model


MAX_TOKENS = 2_000_000  # cumulated tokens (input + output) per run


MAX_TOOL_CALLS = 400  # tool calls per run (subscriptions: the CLI drives the loop)


PROMPT_FILE = vocation.RULES_FILE  # the space's own rules (`vocation.read_rules`)


ConversationFactory = Callable[[AIConfig, str, str, list[ToolSpec]], Conversation]


COMMIT_PREFIX = "Tidy-up: "  # subject of the run's commit, recognized by the web history


STEP_CAP = "Step or token cap reached: run interrupted."


REFUSED = "The model refused to continue."


NO_PAGE = " — no page given"  # end of the detail of an item filed without a page


@dataclass
class Outcome:
    status: str
    items_done: int = 0
    items_left: int = 0
    commit: str | None = None
    summary: str = ""
    operations: list[dict[str, object]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    deletion_cap_reached: bool = False
    big_changes: list[str] = field(default_factory=list)  # changes to report to the admins
    untreated: int = 0  # items not processed (interruption, conflict): taken up later
    interruption: str | None = None  # why the run stopped before the end
    given_up: list[str] = field(default_factory=list)  # left after MAX_ITEM_ATTEMPTS tries
    notices: list[tidy_notices.Notice] = field(default_factory=list)  # notices to depositors
    metrics: dict[str, Any] | None = None  # quality of the run (tidy_metrics), without the cost


REDUCED_SHARE = 0.3  # existing page reduced by at least 30 %…


REDUCED_MIN = 300  # … and by at least 300 characters


MANY_PAGES = 20  # existing pages modified, moved or deleted in one run


MANY_MOVES = 10


def big_changes(overlay: Overlay, operations: list[dict[str, object]]) -> list[str]:
    """Significant changes of a run, to report (never blocked: Git keeps everything).

    Adds to the log an `Op.REDUCED` operation for each existing page that loses a good part
    of its content; returns the lines of the alert to the administrators."""
    prefix = CONTENT_DIR + "/"
    lines: list[str] = []
    touched = 0
    for path, data in sorted(overlay.changes.items()):
        if not path.startswith(prefix) or not overlay.existed(path):
            continue
        touched += 1
        if data is None:
            continue
        before = overlay.repo.blob(path, overlay.base) or b""
        removed = len(before) - len(data)
        if removed >= REDUCED_MIN and removed >= REDUCED_SHARE * len(before):
            share = round(100 * removed / len(before))
            detail = f"−{share} % ({len(before)} → {len(data)} bytes)"
            operations.append({"op": Op.REDUCED, "path": path[len(prefix) :], "detail": detail})
            lines.append(f"- {path[len(prefix) :]} : {detail}")
    moves = sum(1 for op in operations if op.get("op") == Op.MOVED)
    if moves >= MANY_MOVES:
        lines.append(f"- {moves} pages moved or renamed")
    if touched >= MANY_PAGES:
        lines.append(f"- {touched} existing pages modified, moved or deleted")
    return lines


def _left_item(data: bytes, reason: str) -> bytes:
    """Rewrites the header of a left item: status and reason."""
    from synaptane.services.inbox import parse_front_matter

    fields, body = parse_front_matter(data.decode("utf-8", "replace"))
    fields["status"] = "left"
    fields["reason"] = reason
    fields["left_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    fields.pop(ATTEMPTS, None)
    return (front_matter(fields) + "\n" + body).encode()


ATTEMPTS = "tidy_attempts"  # note header: runs interrupted before processing it


def _attempts(data: bytes) -> int:
    from synaptane.services.inbox import parse_front_matter

    value = parse_front_matter(data.decode("utf-8", "replace"))[0].get(ATTEMPTS, 0)
    return value if isinstance(value, int) else 0


def _untreated_item(data: bytes, attempts: int) -> bytes:
    """Note presented to an interrupted run: still new, with the counter."""
    from synaptane.services.inbox import parse_front_matter

    fields, body = parse_front_matter(data.decode("utf-8", "replace"))
    fields[ATTEMPTS] = attempts
    return (front_matter(fields) + "\n" + body).encode()


def _agent_identity(ws: Workspace) -> Identity:
    return Identity(AGENT_NAME, ws.committer.email)


def _commit_message(outcome: Outcome) -> str:
    title = f"{COMMIT_PREFIX}{outcome.items_done} item(s) filed"
    if outcome.items_left:
        title += f", {outcome.items_left} left"
    if outcome.untreated:
        title += f", {outcome.untreated} not processed"
    lines = [title, ""]
    if outcome.summary:
        lines += [outcome.summary, ""]
    lines += [
        f"- {Op.ENGLISH.get(str(op['op']), op['op'])}: {op['path']}"
        for op in outcome.operations[:200]
    ]
    return "\n".join(lines)


Limit = Callable[[Usage], str | None]  # reason to stop (budget), or None


def _drive_api(
    conversation: Conversation, tools: "Tools", state: RunState, usage: Usage, limit: Limit
) -> None:
    """Tool loop driven by Synaptane (API key providers). `usage` is kept up to date at each
    turn: the cost stays known even if the run fails afterwards."""
    nudges = 0
    # Once every item is closed, the end-of-run check may still ask for a round of fixes.
    while state.finished is None and (state.open_items() or tools.correcting):
        if usage.turns >= MAX_STEPS or usage.weighted >= MAX_TOKENS:
            usage.notes.append(STEP_CAP)
            break
        if reason := limit(usage):
            usage.notes.append(reason)
            break
        try:
            turn = conversation.step()
        except AIError as error:
            state.interrupted, state.failure = str(error), error
            usage.notes.append(f"Run interrupted by the provider: {error}")
            break
        usage.add(turn)
        if turn.stop == "refusal":
            usage.notes.append(REFUSED)
            break
        if turn.stop == "max_tokens" and turn.tool_calls:
            # A cut call may have an incomplete input (truncated page): nothing is run, the
            # model starts again with shorter calls.
            conversation.reply(
                [
                    ToolResult(
                        call.id,
                        "Answer cut (length limit): nothing was run. Split into shorter calls "
                        "(edit_page, smaller pages).",
                        is_error=True,
                    )
                    for call in turn.tool_calls
                ]
            )
            continue
        if not turn.tool_calls:
            if not state.open_items():
                break  # end-of-run fixes not done: the log keeps them
            nudges += 1
            if nudges > 2:
                usage.notes.append("The model stopped without closing every item.")
                break
            conversation.nudge(
                "Some items are still open: "
                + ", ".join(state.open_items())
                + ". Use the tools (mark_done or leave_in_inbox), then finish."
            )
            continue
        results = []
        for call in turn.tool_calls:
            try:
                results.append(ToolResult(call.id, tools.run(call)))
            except ToolFailure as error:
                results.append(ToolResult(call.id, str(error), is_error=True))
        conversation.reply(results)


def _drive_cli(
    config: AIConfig, system: str, first: str, tools: "Tools", state: RunState, usage: Usage
) -> None:
    """Loop driven by the subscription's official CLI (Claude Code, Codex)."""
    from synaptane.ai import cli_agents

    def handler(name: str, arguments: dict[str, Any]) -> str:
        if tools.calls >= MAX_TOOL_CALLS:
            raise cli_agents.ToolRejected("Call cap reached: call finish now.")
        try:
            return tools.run(ToolCall(f"cli-{tools.calls}", name, arguments))
        except ToolFailure as error:
            raise cli_agents.ToolRejected(str(error)) from None

    prompt = first + "\n\nProcess each item with the tools, then call finish."
    try:
        run = cli_agents.run_agent(config, system, prompt, TOOLS, handler)
    except AIError as error:
        state.interrupted, state.failure = str(error), error
        if error.usage is not None:  # the CLI worked before failing: tokens and quota
            usage.merge(error.usage)
        usage.notes.append(f"Run interrupted: {error}")
        return
    except OSError as error:  # CLI not found, arguments refused by the system…
        state.interrupted = f"Cannot start the CLI: {error}"
        usage.notes.append(state.interrupted)
        return
    usage.merge(run.usage)
    if state.open_items() and state.finished is None:
        usage.notes.append("The run ended before closing every item.")


def tidy_space(
    database: Database,
    ws: Workspace,
    space: Space,
    config: AIConfig,
    *,
    factory: ConversationFactory = open_conversation,
    scheduled: bool = False,
    spent: Usage | None = None,
    limit: Limit = lambda _: None,
) -> Outcome:
    """Runs a complete pass over a space. Does not record the log (see `run`).

    `spent` receives the usage turn by turn (it stays known if the run fails); `limit` can
    stop the model (budget). If the provider fails along the way, the work already done is
    committed; the items presented but not closed stay new, with a counter of interrupted
    runs, and are only left beyond MAX_ITEM_ATTEMPTS.

    Items never processed come before those the agent already left: otherwise, MAX_ITEMS
    left items would be enough to block the inbox. A scheduled run without any new item does
    not run (it does not retry the same left items every night).
    """
    repo = ws.local_repository(space)
    base = repo.head()
    assert base is not None
    with database.session() as session:
        found_space = session.get(Space, space.id)
        assert found_space is not None
        pending_changes = changes_service.pending(session, ws, found_space)
        refused = budget.refusal(session, space.org_id)
    every = list_items(system_reader(space), ws, space) + [item for item, _ in pending_changes]
    fresh = [i for i in every if i.status != "left"]
    if not fresh and (scheduled or not every):
        return Outcome("nothing", summary="Nothing new to tidy up.")
    if refused:
        raise TidyFailure(refused)
    items = (fresh + [i for i in every if i.status == "left"])[:MAX_ITEMS]
    overlay = Overlay(repo, base)
    state = RunState(
        items={i.name: i for i in items},
        changes={item.name: change for item, change in pending_changes},
    )

    reader = system_reader(space)

    def search_space(query: str) -> list[search_service.Hit]:
        """Hybrid search of the space: by words, and by meaning if embeddings are enabled
        there (duplicates by paraphrase: "NAS" and "storage array")."""
        try:
            with database.session() as session:
                return search_service.search(
                    session, reader, ws, query, addresses=[space.address], limit=15
                )
        except SynaptaneError:
            return []

    tools = Tools(
        overlay, state, ws, space, search_space, nearby_for(database, ws, space, repo, base)
    )
    custom = vocation.read_rules(repo, base)
    purpose = vocation.read(repo, base)  # at the starting commit, like the rules
    system = INSTRUCTIONS.format(
        name=space.name,
        address=space.address,
        description=space.description or "",
        language=vocation.language_note(vocation.space_language(repo, base, purpose)),
        vocation=VOCATION.format(text=vocation.for_prompt(purpose)) if purpose else "",
        custom=CUSTOM.format(text=custom.decode("utf-8", "replace")) if custom else "",
        max_deletions=MAX_DELETIONS,
    )
    usage = spent if spent is not None else Usage()
    first = first_message(items, tools.tag, datetime.now(UTC).date().isoformat())
    if config.provider in SUBSCRIPTION_PROVIDERS:
        _drive_cli(config, system, first, tools, state, usage)
    else:
        conversation = factory(config, system, first, TOOLS)
        _drive_api(conversation, tools, state, usage, limit)
    tools.record_problems()  # end-of-run check: what remains goes to the log, without blocking
    failure = state.failure
    if failure is not None and not (usage.turns or usage.input_tokens or overlay.changes):
        # The provider failed before any work (expired login, refused key): a failure, not
        # a "nothing" run that a scheduled run would erase from the log.
        raise TidyFailure(failure.label, transient=failure.transient)
    # Presented but not closed (provider error, cap, budget, model stopped): an
    # interruption, not a decision. They stay new, with a counter.
    untreated: dict[str, str] = {}
    interruption: str | None = None
    if (usage.turns or usage.input_tokens) and state.open_items():
        interruption = (
            state.interrupted or " ".join(usage.notes) or "items not closed by the model"
        )[:300]
        untreated = dict.fromkeys(state.open_items(), interruption)
    with database.session() as session:
        found_space = session.get(Space, space.id)
        assert found_space is not None
        tried = {
            name: changes_service.attempts(session, found_space, change.path)
            for name, change in state.changes.items()
        }
    closing = _close(repo, base, state, untreated, tried)
    if not overlay.changes and not closing.notes:
        _record_changes(database, ws, space, closing.decisions, base)
        outcome = _outcome(state, usage, closing, interruption, [], overlay)
        outcome.status = "nothing" if not closing.decisions else outcome.status
        return outcome
    with try_space_lock(
        database.engine, space.id, timeout=tidy_commit.COMMIT_WAIT.total_seconds()
    ) as acquired:
        if not acquired:  # the work is lost, but no run is started again for nothing
            raise TidyFailure(
                "The space stayed busy (sync?): the run's work was not saved. The items stay "
                "in the inbox."
            )
        if repaired := repo.heal():
            log.warning("Space %s: local copy repaired (%s)", space.id, ", ".join(repaired))
        head = repo.head()
        assert head is not None
        merged = tidy_commit.Merged(files=dict(overlay.changes))
        if head != base:
            merged = tidy_commit.merge(repo, base, head, overlay.changes)
            overlay.changes = dict(merged.files)
            touched_notes = tidy_commit.changed(repo, base, head, closing.note_paths(state))
            demoted = _demote(state, untreated, merged.conflicts)
            if demoted or touched_notes:
                closing = _close(repo, base, state, untreated, tried, touched_notes)
        outcome = _outcome(state, usage, closing, interruption, _conflict_ops(merged), overlay)
        files = {**overlay.changes, **closing.notes}
        for path in files:
            safe_path(path)
        if files:
            outcome.commit = repo.commit_changes(
                files, _agent_identity(ws), _commit_message(outcome)
            )
    _record_changes(database, ws, space, closing.decisions, base)
    return outcome


MAX_ITEM_ATTEMPTS = 3  # interrupted runs before an item is left, with an alert


CONFLICT_REASON = "page modified during the tidy-up"


@dataclass
class Closing:
    """Closing of a run's items: inbox files, decisions in the database, log."""

    notes: dict[str, bytes | None] = field(default_factory=dict)
    decisions: dict[str, tuple[str, str, changes_service.Change]] = field(default_factory=dict)
    operations: list[dict[str, object]] = field(default_factory=list)
    done: int = 0
    left: int = 0
    untreated: int = 0
    given_up: list[str] = field(default_factory=list)
    notices: list[tidy_notices.Notice] = field(default_factory=list)  # notices to depositors

    @staticmethod
    def note_paths(state: RunState) -> list[str]:
        paths = []
        for item in state.items.values():
            if item.path:
                paths.append(item.path)
                paths += [f"{attachments_dir(item.path)}/{a}" for a in item.attachments]
        return paths


def _close(
    repo: Repository,
    base: str,
    state: RunState,
    untreated: dict[str, str],
    tried: dict[str, int],
    touched: set[str] | frozenset[str] = frozenset(),
) -> Closing:
    """What becomes of the items: filed, left (agent's decision), or not processed
    (interruption; also left beyond MAX_ITEM_ATTEMPTS runs, with an alert).

    A note modified by a human during the run (`touched`) keeps its human version: it will be
    taken up as it is by the next run."""
    closing = Closing()
    for name, item in state.items.items():
        note = [item.path, *(f"{attachments_dir(item.path)}/{a}" for a in item.attachments)]
        if item.path and touched & set(note):
            closing.untreated += 1
            closing.operations.append(
                {
                    "op": Op.UNTREATED,
                    "path": item.title,
                    "detail": "item modified or removed during the tidy-up: human version kept",
                }
            )
            continue
        change = state.changes.get(name)
        if name in state.done:
            summary = state.done[name]
            closing.done += 1
            if change is not None:  # page change: decision recorded in the database
                closing.decisions[change.path] = ("done", summary, change)
                closing.operations.append({"op": Op.FILED, "path": item.title, "detail": summary})
                continue
            for path in note:
                closing.notes[path] = None
            pages = state.integrated.get(name) or []
            if notice := tidy_notices.of(item, "done", summary, pages):
                closing.notices.append(notice)
            # No silent closing: the log says where the item was integrated, or that no page
            # was given (duplicate, already there… or information lost).
            where = f" → {', '.join(pages)}" if pages else NO_PAGE
            closing.operations.append(
                {"op": Op.FILED, "path": item.title, "detail": summary + where}
            )
        elif name in state.left:
            reason = state.left[name]
            closing.left += 1
            closing.operations.append({"op": Op.LEFT, "path": item.title, "detail": reason})
            if change is not None:
                closing.decisions[change.path] = ("left", reason, change)
            else:
                closing.notes[item.path] = _left_item(repo.blob(item.path, base) or b"", reason)
                if notice := tidy_notices.of(item, "left", reason):
                    closing.notices.append(notice)
        elif name in untreated:
            why = untreated[name]
            data = b"" if change is not None else repo.blob(item.path, base) or b""
            count = (tried.get(name, 0) if change is not None else _attempts(data)) + 1
            if count >= MAX_ITEM_ATTEMPTS:
                reason = f"Not processed after {count} interrupted runs (last one: {why})"
                closing.left += 1
                closing.given_up.append(item.title)
                closing.operations.append({"op": Op.LEFT, "path": item.title, "detail": reason})
                if change is not None:
                    closing.decisions[change.path] = ("left", reason, change)
                else:
                    closing.notes[item.path] = _left_item(data, reason)
                continue
            closing.untreated += 1
            closing.operations.append(
                {"op": Op.UNTREATED, "path": item.title, "detail": f"{why} (attempt {count})"}
            )
            if change is not None:
                closing.decisions[change.path] = ("pending", why, change)
            else:
                closing.notes[item.path] = _untreated_item(data, count)
    return closing


def _demote(state: RunState, untreated: dict[str, str], conflicts: set[str]) -> bool:
    """Items filed in a page left in conflict: not processed (taken up later)."""
    pages = {p.removeprefix(CONTENT_DIR + "/") for p in conflicts}
    demoted = False
    for name in list(state.done):
        hit = sorted(pages & set(state.integrated.get(name) or []))
        if hit:
            del state.done[name]
            untreated[name] = f"{CONFLICT_REASON} ({', '.join(hit[:3])})"
            demoted = True
    return demoted


def _conflict_ops(merged: tidy_commit.Merged) -> list[dict[str, object]]:
    """Log of the writes carried over onto a page modified during the run."""
    prefix = CONTENT_DIR + "/"
    ops: list[dict[str, object]] = [
        {
            "op": Op.MERGED,
            "path": p.removeprefix(prefix),
            "detail": "with a human change",
        }
        for p in sorted(merged.merged)
    ]
    ops += [
        {
            "op": Op.CONFLICT,
            "path": p.removeprefix(prefix),
            "detail": CONFLICT_REASON + ": human version kept",
        }
        for p in sorted(merged.conflicts)
    ]
    return ops


def _outcome(
    state: RunState,
    usage: Usage,
    closing: Closing,
    interruption: str | None,
    conflict_ops: list[dict[str, object]],
    overlay: Overlay,
) -> Outcome:
    # Writes left in conflict are not in the commit: they leave the log.
    dropped = {str(op["path"]) for op in conflict_ops if op["op"] == Op.CONFLICT}
    operations = [op for op in state.operations if str(op.get("path")) not in dropped]
    big = big_changes(overlay, operations)
    try:
        metrics: dict[str, Any] | None = tidy_metrics.measure(overlay, state)
    except Exception:  # noqa: BLE001 — a failed measurement must never cost the run
        log.warning("Run metrics could not be computed", exc_info=True)
        metrics = None
    summary = state.finished or " ".join(usage.notes)
    if closing.untreated:
        summary = (
            f"{summary} {closing.untreated} item(s) not processed, taken up by the next run."
        ).strip()
    return Outcome(
        status="done",
        items_done=closing.done,
        items_left=closing.left,
        summary=summary,
        operations=operations + conflict_ops + closing.operations,
        usage=usage,
        error=state.interrupted,
        deletion_cap_reached=state.deletion_cap_reached,
        big_changes=big,
        untreated=closing.untreated,
        interruption=interruption,
        given_up=closing.given_up,
        notices=closing.notices,
        metrics=metrics,
    )


def _record_changes(
    database: Database,
    ws: Workspace,
    space: Space,
    decisions: dict[str, tuple[str, str, "changes_service.Change"]],
    base: str,
) -> None:
    """Decisions on the changed pages, then the watermark moves on if everything was reviewed."""
    with database.session() as session:
        found = session.get(Space, space.id)
        if found is None:
            return
        changes_service.record(session, found, decisions, base)
        session.flush()
        changes_service.compact(session, ws, found)


class TidyFailure(Exception):
    """Failure of a run; `transient`: it will be retried. `label`: the message to translate
    (alert email)."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient
        self.label = message if isinstance(message, Label) else Label(message)


def system_reader(space: Space) -> Actor:
    return Actor(user=User(username="synaptane"), access=_system_access(space))


def fresh_count(session: Session, ws: Workspace, space: Space) -> int:
    """Items never reviewed (notes and changes): enough to chain a run."""
    notes = [i for i in list_items(system_reader(space), ws, space) if i.status != "left"]
    return len(notes) + sum(
        1 for item, _ in changes_service.pending(session, ws, space) if item.status != "left"
    )


def _system_access(space: Space) -> Any:
    from synaptane.core.access import Access, SpaceRole

    return Access(user_id=UUID(int=0), space_roles={space.id: SpaceRole.READER})


def run(
    database: Database,
    ws: Workspace,
    space_id: UUID,
    *,
    trigger: str,
    requested_by: UUID | None = None,
    factory: ConversationFactory = open_conversation,
    chain: int = 0,
    public_url: str = "",
) -> TidyRun | None:
    """Logged run (worker). Returns the log, or None if there was nothing to do in a
    scheduled or chained run (no useless entry in the log).

    If new items are still waiting after a run that made progress (more than MAX_ITEMS
    items), a chained run is queued, MAX_CHAIN times at most; the organization's monthly
    budget stops it too.

    A run already under way on the space (another worker): the request is queued again a
    little later, without a log entry or a used attempt."""
    with tidy_commit.pass_lock(database.engine, space_id) as acquired:
        if acquired:
            return _run(
                database,
                ws,
                space_id,
                trigger=trigger,
                requested_by=requested_by,
                factory=factory,
                chain=chain,
                public_url=public_url,
            )
    payload: dict[str, object] = {"trigger": trigger}
    if requested_by:
        payload["by"] = str(requested_by)
    if chain:
        payload["chain"] = chain
    with database.session() as session:
        jobs.enqueue(session, TIDY, space_id, payload, delay=BUSY_DELAY)
    log.info("Space %s: a tidy-up is already running, request queued again.", space_id)
    return None


BUSY_DELAY = timedelta(minutes=2)  # new attempt when a run is already under way


def _run(
    database: Database,
    ws: Workspace,
    space_id: UUID,
    *,
    trigger: str,
    requested_by: UUID | None,
    factory: ConversationFactory,
    chain: int,
    public_url: str,
) -> TidyRun | None:
    background = trigger in ("schedule", "chain", "deposit")
    with database.session() as session:
        space = session.get(Space, space_id)
        if space is None:
            return None
        _ = space.organization
        config = config_for(session, ws.box, space)
        if config is None and background:
            return None  # without AI, no "failure" every night: the AI page already says so
        price, prudent = budget.run_price(session, space.org_id, config.model if config else "")
        remaining = budget.remaining(session, space.org_id)
        record = TidyRun(space_id=space_id, trigger=trigger, requested_by=requested_by)
        if config is not None:
            record.model = config.model
        session.add(record)
        session.flush()
        run_id = record.id
        session.expunge(space)
    error: str | None = None
    outcome: Outcome | None = None
    retry = False
    explained: Label | None = None  # the error, to translate (alert email)
    spent = Usage()  # kept up to date turn by turn: counted even if the run fails

    def limit(usage: Usage) -> str | None:
        """Stops the model when the run's cost would use up the month's budget."""
        cost = pricing.cost(usage, price)
        if remaining is not None and cost is not None and cost >= remaining:
            return "Monthly AI budget reached during the run: stopped, work done kept."
        return None

    try:
        if config is None:
            raise TidyFailure(
                "No AI provider configured for the organization (the organization's "
                '"Artificial intelligence" tab).'
            )
        outcome = tidy_space(
            database,
            ws,
            space,
            config,
            factory=factory,
            scheduled=background,
            spent=spent,
            limit=limit,
        )
    except TidyFailure as failure:
        error, retry, explained = str(failure), failure.transient, failure.label
    except AIError as failure:
        error, retry, explained = str(failure), failure.transient, failure.label
    except (SynaptaneError, GitError) as failure:
        error = str(failure)
    except Exception as failure:  # noqa: BLE001 — the log must never stay "running"
        log.exception("Tidy-up run %s: unexpected error", run_id)
        error = f"Internal error: {type(failure).__name__}: {failure}"[:2000]
    with database.session() as session:
        found = session.get(TidyRun, run_id)
        assert found is not None
        record = found
        record.finished_at = datetime.now(UTC)
        # Tokens and cost in every case, failure included: nothing escapes the budget.
        record.input_tokens = spent.input_tokens
        record.output_tokens = spent.output_tokens
        record.cached_tokens = spent.cached_tokens
        record.quota = spent.quota
        if config is not None:
            record.cost = pricing.cost(spent, price)
        if outcome is not None:
            record.status = outcome.status
            record.items_done, record.items_left = outcome.items_done, outcome.items_left
            record.operations, record.summary = outcome.operations, outcome.summary
            record.commit, record.error = outcome.commit, outcome.error
            if outcome.metrics is not None:
                record.metrics = tidy_metrics.with_usage(outcome.metrics, spent, record.cost)
            if outcome.commit:
                jobs.enqueue(session, "sync", space_id)
                jobs.enqueue(session, "index", space_id)
            if outcome.status == "nothing" and background and not spent.turns:
                session.delete(record)
                return None
        else:
            record.status, record.error = "failed", error
        if prudent and config is not None and record.cost:
            # Unknown price despite the budget: counted at the highest, and said in the log.
            note: dict[str, object] = {"op": Op.ATTENTION, "path": config.model, "detail": prudent}
            record.operations = [*record.operations, note]
        session.flush()
        found_space = session.get(Space, space_id)
        if found_space is not None and record.cost:
            budget.check_alerts(session, ws.box, found_space, public_url)
        if found_space is not None and outcome is None and not retry and config is not None:
            alerts.notify(
                session,
                ws.box,
                found_space,
                "tidy_failed",
                public_url or ws.public_url,
                error=explained or (error or "?")[:500],  # in each recipient's language
            )
        if found_space is not None and quota.highest(spent.quota) >= quota.HIGH:
            alerts.notify(
                session,
                ws.box,
                found_space,
                "quota_high",
                public_url or ws.public_url,
                usage=partial(quota.summary, spent.quota),  # recipient's language
            )
        if (
            found_space is not None
            and outcome is not None
            and outcome.commit
            and outcome.big_changes
        ):
            alerts.notify(
                session,
                ws.box,
                found_space,
                "big_changes",
                public_url or ws.public_url,
                details="\n".join(outcome.big_changes[:30]),
            )
        if found_space is not None and outcome is not None and outcome.commit:
            # Only after the commit: the notice says what really happened to the note.
            tidy_notices.send(
                session, ws.box, found_space, outcome.notices, public_url or ws.public_url
            )
        if found_space is not None and outcome is not None and outcome.deletion_cap_reached:
            alerts.notify(
                session, ws.box, found_space, "deletion_cap", public_url, limit=MAX_DELETIONS
            )
        if found_space is not None and outcome is not None:
            _notify_interruption(session, ws, found_space, outcome, public_url or ws.public_url)
        if (
            found_space is not None
            and outcome is not None
            and outcome.status == "done"
            and outcome.interruption is None  # no immediate (and paid again) restart after a stop
            and outcome.items_done + outcome.items_left > 0
            and chain < MAX_CHAIN
            and budget.refusal(session, found_space.org_id) is None
            and fresh_count(session, ws, found_space) > 0
        ):
            payload: dict[str, object] = {"trigger": "chain", "chain": chain + 1}
            if requested_by:
                payload["by"] = str(requested_by)
            jobs.enqueue(session, TIDY, space_id, payload)
        if found_space is not None:
            webhooks.emit(
                session,
                found_space,
                "tidy.completed",
                {
                    "status": record.status,
                    "items_done": record.items_done,
                    "items_left": record.items_left,
                    "summary": record.summary,
                    "commit": record.commit,
                    "error": record.error,
                },
            )
            if record.commit and outcome is not None:
                paths = sorted(
                    {
                        str(op["path"])
                        for op in outcome.operations
                        if op["op"]
                        not in (Op.FILED, Op.LEFT, Op.UNTREATED, Op.CONFLICT, Op.ATTENTION)
                    }
                )
                if paths:
                    webhooks.emit(
                        session,
                        found_space,
                        "content.changed",
                        {"paths": paths, "action": "tidied", "by": "synaptane", "source": "tidy"},
                    )
        session.expunge(record)
    if error and retry:
        raise RetryLater(error)
    return record


def _notify_interruption(
    session: Session, ws: Workspace, space: Space, outcome: Outcome, public_url: str
) -> None:
    """Notifies the admins of a run stopped before the end, or of items left because they
    could not be processed: otherwise, nothing would say so outside the log."""
    if outcome.interruption is None and not outcome.given_up:
        return
    alerts.notify(
        session,
        ws.box,
        space,
        "tidy_interrupted",
        public_url,
        reason=(outcome.interruption or CONFLICT_REASON)[:500],
        count=outcome.untreated,
        attempts=MAX_ITEM_ATTEMPTS,
        given_up=", ".join(outcome.given_up[:20]) or "—",
    )


def close_orphans(database: Database, older_than: timedelta = timedelta(hours=2)) -> int:
    """Closes the runs left "running" (worker killed during a run)."""
    limit = datetime.now(UTC) - older_than
    with database.session() as session:
        orphans = session.scalars(
            select(TidyRun).where(TidyRun.status == "running", TidyRun.started_at < limit)
        ).all()
        for record in orphans:
            record.status, record.finished_at = "failed", datetime.now(UTC)
            record.error = "Run interrupted (worker stopped during the tidy-up)."
        return len(orphans)


class RetryLater(Exception):
    """Transient failure: the task will be retried by the queue."""

    transient = True


MANUAL_PER_DAY = 5  # "Tidy up now" per contributor, space and 24 hours
MANUAL_PER_DAY_ADMIN = 20  # for an admin of the organization (the one who pays)


def request_run(session: Session, actor: Actor, space: Space) -> None:
    """ "Tidy up now". Each run costs: MANUAL_PER_DAY manual runs at most per person and per
    space over 24 hours (MANUAL_PER_DAY_ADMIN for an admin of the organization). Requests made
    while a run is waiting join it (one task per space): only the runs carried out count."""
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    limit = MANUAL_PER_DAY_ADMIN if actor.access.is_org_admin(space.org_id) else MANUAL_PER_DAY
    done = session.scalar(
        select(func.count())
        .select_from(TidyRun)
        .where(
            TidyRun.space_id == space.id,
            TidyRun.trigger == "manual",
            TidyRun.requested_by == actor.user.id,
            TidyRun.started_at >= datetime.now(UTC) - timedelta(days=1),
        )
    )
    if (done or 0) >= limit:
        raise too_many(
            "Limit reached: {limit} manual tidy-up runs per day in this space. Scheduled "
            "tidy-ups and tidy-ups after drops continue; try again later.",
            limit=limit,
        )
    # Replaces the payload of a waiting scheduled or chained run: the run does happen in
    # manual mode, left items included.
    jobs.replace_payload(session, TIDY, space.id, {"trigger": "manual", "by": str(actor.user.id)})
    audit(session, actor, "tidy.requested", space=space)


MAX_DELAY = 24 * 60  # minutes


def schedule_soon(session: Session, space: Space) -> bool:
    """Tidy-up as you go: a run `tidy_delay` minutes after a deposit, on top of the scheduled
    time. The following deposits join the run already waiting (a single task per space):
    close deposits are filed together. Nothing if automatic tidy-up is off, or if the month's
    budget is used up (the night's run will report it). Returns true if a run is waiting."""
    if not space.tidy_enabled or not space.tidy_delay or not space.repo_url:
        return False
    if budget.refusal(session, space.org_id):
        return False
    jobs.enqueue(
        session,
        TIDY,
        space.id,
        {"trigger": "deposit"},
        delay=timedelta(minutes=min(space.tidy_delay, MAX_DELAY)),
    )
    return True


def runs(session: Session, actor: Actor, space: Space, limit: int = 30) -> list[TidyRun]:
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    return list(
        session.scalars(
            select(TidyRun)
            .where(TidyRun.space_id == space.id)
            .order_by(TidyRun.started_at.desc())
            .limit(limit)
        )
    )


def revert(
    session: Session, database: Database, actor: Actor, ws: Workspace, space: Space, run_id: UUID
) -> str:
    """Undoes a run with a reverse commit. The filed items come back to the inbox."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    record = session.get(TidyRun, run_id)
    if record is None or record.space_id != space.id or not record.commit:
        raise not_found("Run not found.")
    if record.reverted_at:
        raise invalid("This run has already been undone.")
    repo = ws.local_repository(space)
    with try_space_lock(database.engine, space.id, timeout=30) as acquired:
        if not acquired:
            raise conflict("The space is busy (synchronization in progress). Try again.")
        author = Identity(actor.user.display_name, actor.user.email or ws.committer.email)
        sha = repo.revert(
            record.commit, author, f"Undo the tidy-up of {record.started_at:%Y-%m-%d %H:%M}"
        )
    if sha is None:
        raise conflict(
            "Cannot undo: filed pages were changed since. Fix them by hand (each page's history)."
        )
    record.reverted_at, record.reverted_by = datetime.now(UTC), actor.user.id
    jobs.enqueue(session, "sync", space.id)
    jobs.enqueue(session, "index", space.id)
    audit(session, actor, "tidy.reverted", space=space, run=str(run_id))
    return sha


def parse_time(value: str) -> time:
    try:
        hours, minutes = value.strip().split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        raise invalid("Invalid time (HH:MM).") from None


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise invalid("Unknown time zone (e.g. Europe/Paris).") from None


def active(session: Session, box: SecretBox, space: Space) -> bool:
    """True if the tidy-up runs in this space: enabled, and the organization has an AI.
    Otherwise nobody reviews the inbox in its place (watermark, delay alert)."""
    return space.tidy_enabled and config_for(session, box, space) is not None


def due(space: Space, now: datetime) -> date | None:
    """Local day of the run due, or None (disabled, already done, or time not reached).

    Never before the first synchronization (no local copy to tidy yet), and not on the day a
    space is created after its time: its first run is the next one, not right away."""
    if not space.tidy_enabled or not space.repo_url or space.sync_status == "never":
        return None
    try:
        tz = zone(space.tidy_timezone)
        local = now.astimezone(tz)
        at = parse_time(space.tidy_time)
    except SynaptaneError:
        return None
    if local.time() < at or space.tidy_last_date == local.date():
        return None
    if space.created_at and space.created_at >= datetime.combine(local.date(), at, tzinfo=tz):
        return None
    return local.date()


def schedule(database: Database, now: datetime | None = None) -> int:
    """Queues the runs due. Returns their number."""
    now = now or datetime.now(UTC)
    count = 0
    with database.session() as session:
        for space in session.scalars(select(Space).where(Space.tidy_enabled.is_(True))):
            day = due(space, now)
            if day is not None:
                space.tidy_last_date = day
                jobs.enqueue(session, TIDY, space.id, {"trigger": "schedule"})
                count += 1
    return count


def update_settings(
    session: Session,
    actor: Actor,
    space: Space,
    *,
    enabled: bool | None = None,
    at: str | None = None,
    timezone: str | None = None,
    model: str | None = None,
    delay: int | None = None,
) -> None:
    """`delay`: minutes between a deposit and the as-you-go run; 0 turns it off."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    if model is not None:  # before any change: a refusal leaves nothing half done
        check_space_model(session, space, model.strip())
    if delay is not None:
        if not 0 <= delay <= MAX_DELAY:
            raise invalid("Invalid delay (1 to 1,440 minutes, 0 to turn off).")
        space.tidy_delay = delay or None
    if at is not None:
        space.tidy_time = parse_time(at).strftime("%H:%M")
    if timezone is not None:
        zone(timezone)
        space.tidy_timezone = timezone
    if enabled is not None:
        space.tidy_enabled = enabled
    if model is not None:
        space.ai_model = model.strip() or None
    audit(session, actor, "space.tidy_settings", space=space)
