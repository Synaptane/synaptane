from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from synaptane.ai import pricing
from synaptane.ai.base import AIConfig, ToolCall, ToolResult, ToolSpec, Turn, Usage
from synaptane.core.errors import SynaptaneError
from synaptane.services import ai_settings, budget, content, deposits, tidy
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Job, Space
from tests.integration.gitutil import git
from tests.integration.wikidata import Wiki

CONFIG = AIConfig(provider="anthropic", model="fake-model", api_key="k")


class Scripted:
    """Simulated model: plays turns of tool calls, keeps the results it receives."""

    def __init__(
        self,
        turns: list[list[tuple[str, dict[str, Any]]]],
        on_step: Callable[[int], None] | None = None,
    ) -> None:
        self.turns = turns
        self.on_step = on_step
        self.results: list[ToolResult] = []
        self.system = ""
        self.first = ""
        self.nudges: list[str] = []
        self.index = 0

    def factory(
        self, config: AIConfig, system: str, first: str, tools: list[ToolSpec]
    ) -> "Scripted":
        self.system, self.first = system, first
        assert {t.name for t in tools} >= {"list_tree", "write_page", "mark_done", "finish"}
        return self

    def step(self) -> Turn:
        if self.on_step:
            self.on_step(self.index)
        if self.index >= len(self.turns):
            return Turn("Fini.", [], "end", 10, 5)
        calls = [
            ToolCall(f"c{self.index}-{n}", name, args)
            for n, (name, args) in enumerate(self.turns[self.index])
        ]
        self.index += 1
        return Turn("", calls, "tool_use", 1000, 200)

    def reply(self, results: list[ToolResult]) -> None:
        self.results.extend(results)

    def nudge(self, text: str) -> None:
        self.nudges.append(text)


def deposit_note(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> str:
    path = deposits.deposit(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        title="Nouveau NAS",
        body="Le NAS passe à 8 To.",
        source="mcp",
        hint="infra/stockage",
        attachments=[("devis.pdf", b"%PDF devis")],
    )
    db.commit()
    return path.rsplit("/", 1)[-1]


def configure_ai(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    ai_settings.configure(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        provider="anthropic",
        model="claude-opus-5-5",
        api_key="sk-ant-test",
    )
    db.commit()


def test_full_tidy_pass_commits_once_and_can_be_reverted(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    note = deposit_note(db, database, ctx, wiki)
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, "notes.md", b"# Notes\n")
    db.commit()
    change = next(
        i.name for i in deposits.inbox(db, wiki.alice, ctx, wiki.infra) if i.type == "change"
    )
    script = Scripted(
        [
            [("list_tree", {}), ("read_inbox_item", {"item": note})],
            [
                (
                    "write_page",
                    {"path": "stockage/nas.md", "content": "# NAS\n\n8 To. [Devis](devis.pdf)\n"},
                ),
                (
                    "file_attachment",
                    {"item": note, "attachment": "devis.pdf", "path": "stockage/devis.pdf"},
                ),
                ("write_page", {"path": "../.synaptane/evil.md", "content": "x"}),
                (  # the created page is linked: the final check has nothing to object
                    "edit_page",
                    {"path": "README.md", "old": "Voir [", "new": "Voir [NAS](stockage/nas.md), ["},
                ),
                (
                    "mark_done",
                    {"item": note, "summary": "Page NAS créée", "pages": ["stockage/nas.md"]},
                ),
            ],
            [("leave_in_inbox", {"item": change, "reason": "Page de notes à confirmer"})],
            [("finish", {"summary": "1 rangé, 1 laissé"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done", record and record.error
    assert (record.items_done, record.items_left) == (1, 1)
    assert "Nouveau NAS" in script.first and "infra/stockage" in script.first
    assert "Infra" in script.system and "Instructions specific" not in script.system
    assert any(r.is_error and "Invalid path" in r.content for r in script.results)
    assert record.input_tokens == 3000 and record.commit  # stops as soon as everything is closed
    assert record.cost == Decimal("0.0240")  # Opus 5.5 public price: 4 and 20 per million

    clone = ctx.clone_path(wiki.infra.id)
    log = git(clone, "log", "-1", "--format=%an|%s", "--name-status")
    assert log.startswith("Synaptane tidy-up|Tidy-up: 1 item(s) filed, 1 left")
    assert (clone / "content/stockage/nas.md").read_text().startswith("# NAS")
    assert (clone / "content/stockage/devis.pdf").read_bytes() == b"%PDF devis"
    db.expire_all()
    remaining = deposits.inbox(db, wiki.alice, ctx, wiki.infra)
    assert [(i.type, i.status, i.reason) for i in remaining] == [
        ("change", "left", "Page de notes à confirmer")  # decision kept in the database
    ]
    assert {"sync", "index"} <= {
        j.kind for j in db.scalars(select(Job).where(Job.status == "queued"))
    }
    ops = {(op["op"], op["path"]) for op in record.operations}
    assert ("créé", "stockage/nas.md") in ops and ("rangé", "Nouveau NAS") in ops
    done = next(op for op in record.operations if op["op"] == "rangé")
    assert done["detail"] == "Page NAS créée → stockage/nas.md"

    tidy.revert(db, database, wiki.root, ctx, wiki.infra, record.id)
    db.commit()
    assert not (clone / "content/stockage/nas.md").exists()
    assert {i.title for i in deposits.list_items(wiki.alice, ctx, wiki.infra)} >= {"Nouveau NAS"}
    with pytest.raises(SynaptaneError):
        tidy.revert(db, database, wiki.root, ctx, wiki.infra, record.id)


def test_custom_space_instructions_are_used(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    deposit_note(db, database, ctx, wiki)
    repo = ctx.local_repository(wiki.infra)
    from synaptane.git.repo import Identity

    repo.commit_changes(
        {tidy.PROMPT_FILE: "Toujours écrire en anglais.".encode()}, Identity("A", "a@x"), "prompt"
    )
    script = Scripted([])
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert "Toujours écrire en anglais." in script.system


def test_conflicting_human_edit_keeps_the_human_page_and_commits_the_rest(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    # Regression: the pass used to be abandoned then retried, paying the model up to 5 times.
    configure_ai(db, ctx, wiki)
    note = deposit_note(db, database, ctx, wiki)

    def human_edit(step: int) -> None:
        if step == 1:
            with database.session() as session:
                content.write_file(
                    session, database, wiki.alice, ctx, wiki.infra, "README.md", b"# Humain\n"
                )

    script = Scripted(
        [
            [("read_page", {"path": "README.md"}), ("read_inbox_item", {"item": note})],
            [
                ("edit_page", {"path": "README.md", "old": "# Infra", "new": "# Infrastructure"}),
                ("mark_done", {"item": note, "summary": "ok"}),
            ],
        ],
        on_step=human_edit,
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit
    assert script.index == 2  # a single model pass
    assert {"op": "conflit", "path": "README.md"}.items() <= next(
        op for op in record.operations if op["op"] == "conflit"
    ).items()
    assert not any(op["op"] == "modifié" and op["path"] == "README.md" for op in record.operations)
    assert (ctx.clone_path(wiki.infra.id) / "content/README.md").read_text() == "# Humain\n"
    assert record.items_done == 1 and deposits.list_items(wiki.alice, ctx, wiki.infra) == []


def test_model_that_never_closes_items_is_stopped(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    deposit_note(db, database, ctx, wiki)
    script = Scripted([])
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and len(script.nudges) == 2
    # Presented but not closed: an interruption, not a decision. The item stays new,
    # with a counter, and the admins are notified.
    assert record.status == "done" and (record.items_done, record.items_left) == (0, 0)
    assert "1 item(s) not processed" in (record.summary or "")
    assert record.input_tokens == 30  # three turns of 10 tokens: counted in the journal
    [item] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    assert item.status == "pending"
    db.expire_all()
    assert db.scalars(select(AuditEvent.action).where(AuditEvent.action.like("alert.%"))).all() == [
        "alert.tidy_interrupted"
    ]
    # Picked up the next night; left on the third interrupted pass, with the reason.
    for _ in range(tidy.MAX_ITEM_ATTEMPTS - 1):
        nightly = tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=script.factory)
        assert nightly is not None
    assert nightly is not None and nightly.items_left == 1
    [item] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    assert item.status == "left" and "Not processed after 3 interrupted runs" in item.reason
    nightly = tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=script.factory)
    assert nightly is None  # nothing new any more: no pass the next night


def test_nothing_to_do_and_missing_ai(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    failed = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=Scripted([]).factory)
    assert failed is not None and failed.status == "failed"
    assert "No AI provider" in (failed.error or "")
    nightly = tidy.run(
        database, ctx, wiki.infra.id, trigger="schedule", factory=Scripted([]).factory
    )
    assert nightly is None  # without AI, no "failure" in the journal every night
    configure_ai(db, ctx, wiki)
    manual = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=Scripted([]).factory)
    assert manual is not None and manual.status == "nothing"
    assert (
        tidy.run(database, ctx, wiki.infra.id, trigger="schedule", factory=Scripted([]).factory)
        is None
    )


def test_schedule_respects_time_zone_and_runs_once_a_day(
    db: Session, database: Database, wiki: Wiki
) -> None:
    tidy.update_settings(db, wiki.root, wiki.infra, at="02:00", timezone="Europe/Paris")
    db.commit()
    before = datetime(2026, 10, 3, 23, 59, tzinfo=UTC)  # 01:59 in Paris
    after = datetime(2026, 10, 4, 0, 1, tzinfo=UTC)  # 02:01 in Paris
    assert tidy.schedule(database, before) == 0
    assert tidy.schedule(database, after) == 2  # the fixture's two spaces
    assert tidy.schedule(database, after) == 0
    with database.session() as session:
        found = session.get(Space, wiki.infra.id)
        assert found is not None and found.tidy_last_date == date(2026, 10, 4)
        payloads = [j.payload for j in session.scalars(select(Job).where(Job.kind == "tidy"))]
        assert payloads == [{"trigger": "schedule"}] * 2
    with pytest.raises(SynaptaneError):
        tidy.update_settings(db, wiki.root, wiki.infra, timezone="Mars/Olympus")
    with pytest.raises(SynaptaneError):
        tidy.update_settings(db, wiki.alice, wiki.infra, at="03:00")


def test_ai_settings_keep_the_key_secret(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    org = wiki.infra.organization
    view = ai_settings.configure(db, wiki.root, ctx.box, org, provider="mistral", api_key="mk-1")
    assert (view.provider, view.model, view.has_key) == ("mistral", "mistral-large-latest", True)
    ai_settings.configure(db, wiki.root, ctx.box, org, provider="mistral", model="mistral-small")
    config = ai_settings.config_for(db, ctx.box, wiki.infra)
    assert config is not None and (config.api_key, config.model) == ("mk-1", "mistral-small")
    tidy.update_settings(db, wiki.root, wiki.infra, model="mistral-medium")
    assert ai_settings.config_for(db, ctx.box, wiki.infra).model == "mistral-medium"  # type: ignore[union-attr]
    bad: dict[str, Any]
    for bad in (
        {"provider": "skynet"},
        {"provider": "openai_compatible"},
        {"provider": "openai", "model": ""},
        {"provider": "mistral", "price_input": Decimal(1)},  # output price missing
    ):
        with pytest.raises(SynaptaneError):
            ai_settings.configure(db, wiki.root, ctx.box, org, **bad)
    with pytest.raises(SynaptaneError):
        ai_settings.configure(db, wiki.alice, ctx.box, org, provider="mistral")


def test_subscription_pass_runs_through_the_cli_tool_server(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synaptane.ai import cli_agents
    from synaptane.ai.base import Usage

    ai_settings.configure(db, wiki.root, ctx.box, wiki.infra.organization, provider="claude_code")
    db.commit()
    note = deposit_note(db, database, ctx, wiki)
    seen: dict[str, Any] = {}

    def fake_run(
        config: AIConfig, system: str, prompt: str, specs: list[ToolSpec], handler: Any
    ) -> Any:
        seen.update(provider=config.provider, prompt=prompt)
        handler("write_page", {"path": "nas.md", "content": "# NAS\n"})
        with pytest.raises(cli_agents.ToolRejected):
            handler("delete", {"path": "absent.md"})
        handler("read_inbox_item", {"item": note})
        handler("read_inbox_item", {"item": note})
        handler("mark_done", {"item": note, "summary": "ok"})
        spent = Usage(input_tokens=500, output_tokens=50, turns=3)
        spent.quota = {
            "status": "allowed",
            "windows": {"five_hour": {"before": 80.0, "after": 92.0, "resets_at": 1791303600}},
        }
        return cli_agents.CLIRun(spent, "Fini")

    monkeypatch.setattr(cli_agents, "run_agent", fake_run)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual")
    assert record is not None and record.status == "done" and record.items_done == 1
    assert seen["provider"] == "claude_code" and note in seen["prompt"]
    assert (record.input_tokens, record.output_tokens) == (500, 50)
    assert (ctx.clone_path(wiki.infra.id) / "content/nas.md").exists()
    # Subscription: the pass's quota is kept, and admins are notified above 90%.
    assert record.quota is not None and record.cost is None
    assert record.quota["windows"]["five_hour"]["after"] == 92.0  # type: ignore[index]
    from sqlalchemy import select

    from synaptane.storage.models import AuditEvent

    db.expire_all()
    actions = db.scalars(select(AuditEvent.action)).all()
    assert "alert.quota_high" in actions


def test_only_superadmin_can_choose_a_subscription(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    from synaptane.core.access import OrgRole
    from synaptane.services import organizations
    from tests.integration.conftest import actor_of

    organizations.set_member(db, wiki.root, wiki.infra.organization, wiki.alice.user, OrgRole.ADMIN)
    org_admin = actor_of(db, wiki.alice.user)
    with pytest.raises(SynaptaneError) as denied:
        ai_settings.configure(db, org_admin, ctx.box, wiki.infra.organization, provider="codex")
    assert denied.value.code == "forbidden"
    view = ai_settings.configure(db, wiki.root, ctx.box, wiki.infra.organization, provider="codex")
    assert view.provider == "codex" and ai_settings.config_for(db, ctx.box, wiki.infra) is not None


class CloseAll:
    """Simulated model that closes every item offered, then finishes."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []
        self.calls: list[ToolCall] = []
        self.done = False

    def factory(
        self, config: AIConfig, system: str, first: str, tools: list[ToolSpec]
    ) -> "CloseAll":
        names = [line[2:].split(" [", 1)[0] for line in first.splitlines() if line.startswith("- ")]
        self.seen.append(names)
        self.calls = [
            ToolCall(f"r{n}", "read_inbox_item", {"item": name}) for n, name in enumerate(names)
        ]
        self.calls += [
            ToolCall(f"c{n}", "mark_done", {"item": name, "summary": "vu"})
            for n, name in enumerate(names)
        ]
        self.calls.append(ToolCall("end", "finish", {"summary": "fini"}))
        self.done = False
        return self

    def step(self) -> Turn:
        if self.done:
            return Turn("Fini.", [], "end", 10, 5)
        self.done = True
        return Turn("", self.calls, "tool_use", 1000, 200)

    def reply(self, results: list[ToolResult]) -> None:
        assert not any(r.is_error for r in results), results

    def nudge(self, text: str) -> None:
        raise AssertionError(text)


def queued_tidy(db: Session) -> list[dict[str, object]]:
    db.expire_all()
    return [
        j.payload
        for j in db.scalars(select(Job).where(Job.kind == tidy.TIDY, Job.status == "queued"))
    ]


def test_passes_are_chained_until_the_inbox_is_empty(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tidy, "MAX_ITEMS", 2)
    configure_ai(db, ctx, wiki)
    for n in range(3):
        deposits.deposit(
            db, database, wiki.alice, ctx, wiki.infra, title=f"Note {n}", body="x", source="web"
        )
    db.commit()
    model = CloseAll()
    first = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=model.factory)
    assert first is not None and first.items_done == 2
    assert queued_tidy(db) == [{"trigger": "chain", "chain": 1}]

    db.execute(delete(Job).where(Job.kind == tidy.TIDY))
    db.commit()
    second = tidy.run(database, ctx, wiki.infra.id, trigger="chain", chain=1, factory=model.factory)
    assert second is not None and second.trigger == "chain" and second.items_done == 1
    assert queued_tidy(db) == []  # empty inbox: the chain stops
    assert [len(names) for names in model.seen] == [2, 1]


def test_chain_stops_at_its_limit(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tidy, "MAX_ITEMS", 1)
    configure_ai(db, ctx, wiki)
    for n in range(2):
        deposits.deposit(
            db, database, wiki.alice, ctx, wiki.infra, title=f"Note {n}", body="x", source="web"
        )
    db.commit()
    record = tidy.run(
        database,
        ctx,
        wiki.infra.id,
        trigger="chain",
        chain=tidy.MAX_CHAIN,
        factory=CloseAll().factory,
    )
    assert record is not None and record.items_done == 1
    assert queued_tidy(db) == []


def test_monthly_budget_alerts_then_stops_tidying(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
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
    deposit_note(db, database, ctx, wiki)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory)
    assert record is not None and record.status == "done"
    assert record.cost == Decimal("0.0070")  # 1,000 × 5 + 200 × 10, per million
    db.expire_all()
    assert budget.spent(db, wiki.infra.org_id) == Decimal("0.0070")

    deposit_note(db, database, ctx, wiki)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory)
    assert record is not None and record.status == "done"  # €0.014: crosses 100%
    alerts = db.scalars(
        select(AuditEvent.details).where(AuditEvent.action == "org.ai_budget_alert")
    ).all()
    assert [a["level"] for a in alerts] == ["100"]  # one notice, at the highest threshold crossed

    deposit_note(db, database, ctx, wiki)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory)
    assert record is not None and record.status == "failed"
    assert "Monthly AI budget reached" in (record.error or "")


def test_cost_estimates() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cached_tokens=2_000_000)
    price = pricing.price_for("anthropic", "claude-haiku-4-5-20251001", None, None)
    assert (
        price is not None
        and price.public
        and (price.input, price.output) == (Decimal(1), Decimal(5))
    )
    assert pricing.cost(usage, price) == Decimal("1.7000")  # 1 + 0.2 (cache) + 0.5
    assert pricing.price_for("claude_code", "claude-opus-5-5", None, None) is None
    assert pricing.price_for("openai", "inconnu", None, None) is None
    own = pricing.price_for("openai", "inconnu", Decimal(2), Decimal(8))
    assert own is not None and not own.public
