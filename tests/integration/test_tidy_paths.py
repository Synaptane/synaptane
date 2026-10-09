"""Tidying: costly or silent paths otherwise never exercised by the tests.

One test per path: model stops (budget, caps, refusal), subscription CLI errors, closing page
changes, busy space, repaired copy, and reverting a pass. The simulated models are those of
`test_tidy` (`Scripted`, `CloseAll`).
"""

import json
import logging
import subprocess
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from synaptane.ai import cli_agents
from synaptane.ai.base import AIConfig, AIError, ToolSpec, Turn, Usage
from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.services import ai_settings, changes, content, deposits, tidy
from synaptane.services.inbox import InboxItem
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database, try_space_lock
from synaptane.storage.models import AuditEvent, ChangeMark, Job, Space, TidyRun
from tests.integration.test_tidy import CloseAll, Scripted, configure_ai
from tests.integration.test_tidy_robustness import note
from tests.integration.wikidata import Wiki

CLIHandler = Any  # `cli_agents.ToolHandler`: tool name and arguments → text


def item_named(ctx: Workspace, wiki: Wiki, name: str) -> InboxItem | None:
    return next(
        (i for i in deposits.list_items(wiki.alice, ctx, wiki.infra) if i.name == name), None
    )


def change_names(db: Session, ctx: Workspace, wiki: Wiki) -> dict[str, str]:
    """Pending page changes: title → item name."""
    db.expire_all()
    return {
        i.title: i.name
        for i in deposits.inbox(db, wiki.alice, ctx, wiki.infra)
        if i.type == "change"
    }


def subscription(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    ai_settings.configure(db, wiki.root, ctx.box, wiki.infra.organization, provider="claude_code")
    db.commit()


@contextmanager
def space_held(database: Database, space_id: UUID) -> Iterator[None]:
    """Another process (sync, write) holds the space lock."""
    with try_space_lock(database.engine, space_id, timeout=1) as acquired:
        assert acquired
        yield


def impatient_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same PostgreSQL lock, but a 0.2 s wait instead of 30 or 120 s."""

    def shortened(engine: Engine, space_id: UUID, timeout: float) -> AbstractContextManager[bool]:
        return try_space_lock(engine, space_id, 0.2)

    monkeypatch.setattr(tidy, "try_space_lock", shortened)


def untreated(record: TidyRun, words: str) -> bool:
    """Interrupted item (budget, cap, refusal): not "left" but "not processed", picked up
    on the next pass with a counter."""
    return any(
        op["op"] == "non traité" and words in str(op.get("detail", "")) for op in record.operations
    )


# --- Model stops (API-key providers) ------------------------------------------------------------


def test_monthly_budget_reached_during_a_pass_stops_the_model(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """The model stops as soon as the pass would exhaust the monthly budget, and what is
    already done is committed (otherwise, paid for nothing)."""
    ai_settings.configure(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        provider="anthropic",
        model="modele-maison",
        api_key="sk-ant-test",
        price_input=Decimal(5),
        price_output=Decimal(10),
        monthly_budget=Decimal("0.01"),
    )
    db.commit()
    first = note(db, database, ctx, wiki, "Première")
    second = note(db, database, ctx, wiki, "Seconde")
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": first}),
                ("write_page", {"path": "premiere.md", "content": "# Première\n"}),
                ("mark_done", {"item": first, "summary": "page créée"}),
            ],
            [("read_inbox_item", {"item": second})],  # €0.014: the budget is exceeded here
            [("mark_done", {"item": second, "summary": "jamais demandé"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit
    assert script.index == 2  # the third turn was never requested from the model
    assert "Monthly AI budget reached during the run" in record.summary
    assert (record.items_done, record.items_left) == (1, 0)
    assert record.input_tokens == 2000 and record.cost == Decimal("0.0140")
    assert ctx.local_repository(wiki.infra).blob("content/premiere.md") == "# Première\n".encode()
    pending = item_named(ctx, wiki, second)
    assert pending is not None and pending.status == "pending"
    assert untreated(record, "Monthly AI budget")


@pytest.mark.parametrize(("cap", "value"), [("MAX_STEPS", 2), ("MAX_TOKENS", 2000)])
def test_step_or_token_cap_interrupts_the_pass(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
    cap: str,
    value: int,
) -> None:
    """A model going round in circles (endless reads) is stopped: two turns of 1,200
    tokens reach either cap, the item is left with the reason."""
    monkeypatch.setattr(tidy, cap, value)
    configure_ai(db, ctx, wiki)
    name = note(db, database, ctx, wiki, "Une note")
    script = Scripted([[("read_page", {"path": "README.md"})]] * 10)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done"
    assert script.index == 2 and record.input_tokens == 2000
    assert "Step or token cap reached" in record.summary
    pending = item_named(ctx, wiki, name)
    assert pending is not None and pending.status == "pending"
    assert untreated(record, "cap reached")


class RefusesAt(Scripted):
    """Simulated model that refuses to continue at turn `refuse_at`."""

    def __init__(self, turns: list[list[tuple[str, dict[str, Any]]]], refuse_at: int) -> None:
        super().__init__(turns)
        self.refuse_at = refuse_at

    def step(self) -> Turn:
        if self.index == self.refuse_at:
            self.index += 1
            return Turn("Je ne peux pas continuer.", [], "refusal", 50, 0)
        return super().step()


def test_model_refusal_stops_the_pass_and_keeps_the_work(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """A refusal is not retried (no "items remain" nudge): the pass stops, the work done
    is committed, the rest is left with the reason."""
    configure_ai(db, ctx, wiki)
    first = note(db, database, ctx, wiki, "Première")
    second = note(db, database, ctx, wiki, "Seconde")
    script = RefusesAt(
        [
            [
                ("read_inbox_item", {"item": first}),
                ("write_page", {"path": "premiere.md", "content": "# Première\n"}),
                ("mark_done", {"item": first, "summary": "page créée"}),
            ],
            [("mark_done", {"item": second, "summary": "jamais joué"})],
        ],
        refuse_at=1,
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit
    assert script.nudges == [] and "The model refused to continue." in record.summary
    assert (record.items_done, record.items_left) == (1, 0)
    assert record.input_tokens == 1050  # the refused turn is counted too
    pending = item_named(ctx, wiki, second)
    assert pending is not None and pending.status == "pending"
    assert untreated(record, "refused")


# --- Subscriptions: the CLI drives the loop -----------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        AIError("Claude Code : limite d'usage atteinte", transient=True),
        OSError("Argument list too long"),
    ],
    ids=["provider", "launch"],
)
def test_cli_error_during_a_pass_keeps_the_work_done(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """With a subscription, the work done by the tools before the CLI error is committed:
    nothing is lost, and the error stays in the journal."""
    subscription(db, ctx, wiki)
    first = note(db, database, ctx, wiki, "Première")
    second = note(db, database, ctx, wiki, "Seconde")

    def fake_run(
        config: AIConfig, system: str, prompt: str, specs: list[ToolSpec], handler: CLIHandler
    ) -> cli_agents.CLIRun:
        handler("read_inbox_item", {"item": first})
        handler("write_page", {"path": "premiere.md", "content": "# Première\n"})
        handler("mark_done", {"item": first, "summary": "page créée"})
        raise failure

    monkeypatch.setattr(cli_agents, "run_agent", fake_run)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual")
    assert record is not None and record.status == "done" and record.commit
    assert str(failure) in (record.error or "")
    assert (record.items_done, record.cost) == (1, None)
    assert ctx.local_repository(wiki.infra).blob("content/premiere.md") is not None
    assert item_named(ctx, wiki, first) is None
    assert item_named(ctx, wiki, second) is not None  # not processed, but still there


@pytest.mark.parametrize("provider", ["api", "cli"])
def test_provider_failing_before_any_work_is_never_silent(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """Every night, a subscription whose sign-in has expired must not vanish from the journal:
    with no trace nor alert, the inbox grows without anyone knowing why."""
    error = AIError("Claude Code n'est pas connecté sur le serveur.")

    class Fails(Scripted):
        def step(self) -> Turn:
            raise error

    def fake_run(
        config: AIConfig, system: str, prompt: str, specs: list[ToolSpec], handler: CLIHandler
    ) -> cli_agents.CLIRun:
        raise error

    if provider == "cli":
        subscription(db, ctx, wiki)
        monkeypatch.setattr(cli_agents, "run_agent", fake_run)
    else:
        configure_ai(db, ctx, wiki)
    note(db, database, ctx, wiki, "Une note")
    record = tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=Fails([]).factory)
    assert record is not None and record.status == "failed"
    assert "pas connecté" in (record.error or "")


def test_cli_tool_call_cap_refuses_further_calls(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI drives the loop: past `MAX_TOOL_CALLS`, every call is refused (the subscription
    is not drained in a loop), but what was done before stays committed."""
    monkeypatch.setattr(tidy, "MAX_TOOL_CALLS", 3)
    subscription(db, ctx, wiki)
    first = note(db, database, ctx, wiki, "Première")
    second = note(db, database, ctx, wiki, "Seconde")
    refusals: list[str] = []

    def fake_run(
        config: AIConfig, system: str, prompt: str, specs: list[ToolSpec], handler: CLIHandler
    ) -> cli_agents.CLIRun:
        handler("read_inbox_item", {"item": first})
        handler("write_page", {"path": "premiere.md", "content": "# Première\n"})
        handler("mark_done", {"item": first, "summary": "page créée"})
        for _ in range(3):  # the agent insists: every call is refused
            with pytest.raises(cli_agents.ToolRejected) as refused:
                handler("read_inbox_item", {"item": second})
            refusals.append(str(refused.value))
        return cli_agents.CLIRun(Usage(input_tokens=900, output_tokens=90, turns=6), "Arrêt")

    monkeypatch.setattr(cli_agents, "run_agent", fake_run)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual")
    assert record is not None and record.status == "done" and record.commit
    assert len(refusals) == 3 and all("Call cap reached" in r for r in refusals)
    assert (record.items_done, record.items_left) == (1, 0)
    assert "before closing every item" in record.summary
    pending = item_named(ctx, wiki, second)
    assert pending is not None and pending.status == "pending"


def claude_code_output(*, is_error: bool) -> str:
    """Claude Code streaming output: two quota readings, then the final result."""

    def reading(five: float) -> dict[str, object]:
        windows = {"five_hour": {"utilization": five, "resetsAt": 1791303600}}
        info = {"status": "allowed", "rateLimitType": "five_hour", "unifiedWindows": windows}
        return {"type": "rate_limit_event", "rate_limit_info": info}

    result = {
        "type": "result",
        "subtype": "error_during_execution" if is_error else "success",
        "is_error": is_error,
        "result": "Claude AI usage limit reached" if is_error else "Fini",
        "num_turns": 6,
        "usage": {"input_tokens": 4000, "output_tokens": 300},
    }
    return "\n".join(json.dumps(e) for e in (reading(0.80), reading(0.97), result))


def test_claude_code_error_result_still_reports_usage_and_quota(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quota exhausted mid-pass: Claude Code answers `is_error`, but has used tokens and
    read the quota. The journal must keep them, and notify the admins."""
    subscription(db, ctx, wiki)
    note(db, database, ctx, wiki, "Une note")
    real_run = subprocess.run

    def fake_subprocess(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command[0] != "/fake/claude":
            return real_run(command, *args, **kwargs)  # Git, on the other hand, runs for real
        return subprocess.CompletedProcess(command, 1, claude_code_output(is_error=True), "")

    monkeypatch.setattr(cli_agents, "_binary", lambda name: f"/fake/{name}")
    monkeypatch.setattr(subprocess, "run", fake_subprocess)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual")
    assert record is not None and "usage limit" in (record.error or "")
    assert (record.input_tokens, record.output_tokens) == (4000, 300)
    assert record.quota is not None
    assert record.quota["windows"]["five_hour"]["after"] == 97.0  # type: ignore[index]
    db.expire_all()
    assert "alert.quota_high" in db.scalars(select(AuditEvent.action)).all()


# --- Page changes -------------------------------------------------------------------------------


def test_closing_a_page_change_records_the_decision(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """`mark_done` on a change computed from Git records it in the database (it no longer
    comes back), `leave_in_inbox` keeps it with its reason; the watermark moves forward."""
    configure_ai(db, ctx, wiki)
    name = note(db, database, ctx, wiki, "Une note")
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# A\n")
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "b.md", b"# B\n")
    db.commit()
    names = change_names(db, ctx, wiki)
    seen, left = names["Page created: a.md"], names["Page created: b.md"]
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": name}),
                ("read_inbox_item", {"item": seen}),
                ("mark_done", {"item": name, "summary": "doublon"}),
                ("mark_done", {"item": seen, "summary": "page bien placée"}),
                ("leave_in_inbox", {"item": left, "reason": "Titre à confirmer"}),
            ]
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit
    assert (record.items_done, record.items_left) == (2, 1)
    ops = [(op["op"], op["path"], op["detail"]) for op in record.operations]
    assert ("rangé", "Page created: a.md", "page bien placée") in ops
    assert ("laissé", "Page created: b.md", "Titre à confirmer") in ops

    db.expire_all()
    assert [(i.title, i.status, i.reason) for i, _ in changes.pending(db, ctx, wiki.infra)] == [
        ("Page created: b.md", "left", "Titre à confirmer")
    ]
    marks = db.scalars(select(ChangeMark.path)).all()
    assert marks == ["b.md"]  # closed decision cleared once the watermark has moved
    space = db.get(Space, wiki.infra.id)
    assert space is not None and space.change_watermark == record.commit
    assert tidy.fresh_count(db, ctx, space) == 0  # nothing to chain


def test_a_pass_that_only_closes_page_changes_writes_nothing(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No file to write: no lock, no commit, no sync; the decisions are still recorded,
    and the pass is "done", not "nothing to do"."""
    configure_ai(db, ctx, wiki)
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "a.md", b"# A\n")
    db.commit()
    [change] = change_names(db, ctx, wiki).values()
    head = ctx.local_repository(wiki.infra).head()

    def no_lock(engine: Engine, space_id: UUID, timeout: float) -> AbstractContextManager[bool]:
        raise AssertionError("verrou pris sans rien à écrire")

    monkeypatch.setattr(tidy, "try_space_lock", no_lock)
    db.query(Job).delete()
    db.commit()
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": change}),
                ("mark_done", {"item": change, "summary": "rien à corriger"}),
            ]
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit is None
    assert (record.items_done, record.items_left) == (1, 0)
    assert ctx.local_repository(wiki.infra).head() == head
    db.expire_all()
    assert db.scalars(select(Job.kind).where(Job.kind.in_(("sync", "index")))).all() == []
    assert changes.pending(db, ctx, wiki.infra) == []
    space = db.get(Space, wiki.infra.id)
    assert space is not None and space.change_watermark == head


# --- Commit under the space lock ----------------------------------------------------------------


def test_busy_space_fails_the_pass_without_running_it_again(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock held beyond the commit wait (stuck sync): the pass fails, with no new attempt
    that would pay for the model again; nothing is committed nor lost in the inbox, and the
    cost is counted."""
    impatient_lock(monkeypatch)
    configure_ai(db, ctx, wiki)
    name = note(db, database, ctx, wiki, "Une note")
    head = ctx.local_repository(wiki.infra).head()
    with space_held(database, wiki.infra.id):
        tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory)
    record = db.scalars(select(TidyRun)).one()
    assert record.status == "failed" and "busy" in (record.error or "")
    assert record.input_tokens == 1000 and record.cost
    assert ctx.local_repository(wiki.infra).head() == head
    found = item_named(ctx, wiki, name)
    assert found is not None and found.status == "pending"


def test_a_dirty_copy_is_repaired_before_the_commit(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Copy left half-done by a killed process (uncommitted file): repaired under the lock,
    with a trace in the log, then the pass commits normally."""
    configure_ai(db, ctx, wiki)
    deposits_name = note(db, database, ctx, wiki, "Une note")
    stray = ctx.clone_path(wiki.infra.id) / "content" / "orphelin.md"
    stray.write_text("# Écrit par un processus tué\n")
    with caplog.at_level(logging.WARNING, logger="synaptane.tidy"):
        record = tidy.run(
            database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory
        )
    assert record is not None and record.status == "done" and record.commit
    assert any("local copy repaired" in r.getMessage() for r in caplog.records)
    assert "uncommitted files removed" in caplog.text
    assert not stray.exists() and item_named(ctx, wiki, deposits_name) is None


# --- Reverting a pass ---------------------------------------------------------------------------


def tidied(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> TidyRun:
    """A committed pass that creates `stockage/nas.md`."""
    configure_ai(db, ctx, wiki)
    name = note(db, database, ctx, wiki, "Nouveau NAS")
    script = Scripted(
        [
            [
                ("read_inbox_item", {"item": name}),
                ("write_page", {"path": "stockage/nas.md", "content": "# NAS\n\n8 To.\n"}),
                ("mark_done", {"item": name, "summary": "ok", "pages": ["stockage/nas.md"]}),
            ]
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.commit
    return record


def test_reverting_an_unknown_pass(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Not found: unknown identifier, pass from another space, or pass without a commit."""
    record = tidied(db, database, ctx, wiki)
    empty = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=Scripted([]).factory)
    assert empty is not None and empty.status == "nothing" and empty.commit is None
    for space, run_id in (
        (wiki.infra, uuid4()),
        (wiki.other, record.id),
        (wiki.infra, empty.id),
    ):
        with pytest.raises(SynaptaneError) as missing:
            tidy.revert(db, database, wiki.root, ctx, space, run_id)
        assert missing.value.code == ErrorCode.NOT_FOUND
    db.expire_all()
    assert db.get(TidyRun, record.id).reverted_at is None  # type: ignore[union-attr]


def test_reverting_while_the_space_is_busy(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Busy space: conflict to retry, nothing reverted; once free, the revert goes through."""
    record = tidied(db, database, ctx, wiki)
    impatient_lock(monkeypatch)
    with space_held(database, wiki.infra.id), pytest.raises(SynaptaneError) as busy:
        tidy.revert(db, database, wiki.root, ctx, wiki.infra, record.id)
    assert busy.value.code == ErrorCode.CONFLICT and "busy" in busy.value.message
    repo = ctx.local_repository(wiki.infra)
    assert repo.head() == record.commit and repo.blob("content/stockage/nas.md") is not None
    tidy.revert(db, database, wiki.root, ctx, wiki.infra, record.id)
    db.commit()
    assert repo.blob("content/stockage/nas.md") is None


def test_reverting_a_pass_whose_pages_changed_since(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Tidied page edited since: the revert is refused (conflict), the copy stays clean
    and the human edit intact; the pass is not marked reverted."""
    record = tidied(db, database, ctx, wiki)
    edited = b"# NAS\n\n16 To, finalement.\n"
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "stockage/nas.md", edited)
    db.commit()
    with pytest.raises(SynaptaneError) as refused:
        tidy.revert(db, database, wiki.root, ctx, wiki.infra, record.id)
    assert refused.value.code == ErrorCode.CONFLICT and "changed since" in refused.value.message
    repo = ctx.local_repository(wiki.infra)
    assert repo.blob("content/stockage/nas.md") == edited
    assert repo._out("status", "--porcelain") == ""
    db.expire_all()
    assert db.get(TidyRun, record.id).reverted_at is None  # type: ignore[union-attr]
