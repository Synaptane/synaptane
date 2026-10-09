"""No surprise bill: unknown price never counted as zero, suggested budget, manual runs and
deposits limited, embeddings counted."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from synaptane.ai import embeddings, pricing
from synaptane.ai.embeddings import EmbeddingConfig
from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.services import ai_settings, budget, deposits, semantic, tidy
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Job, Space, TidyRun
from tests.integration.conftest import token_for
from tests.integration.test_semantic import ConceptEmbedder, enable
from tests.integration.test_tidy import CloseAll, deposit_note, queued_tidy
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki


def with_budget(db: Session, ctx: Workspace, wiki: Wiki, amount: str = "10", **prices: Any) -> None:
    ai_settings.configure(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        provider="anthropic",
        model="claude-sonnet-5-5",
        api_key="sk-ant-test",
        monthly_budget=Decimal(amount),
        **prices,
    )
    db.commit()


# --- 1. Model without a known price --------------------------------------------------------------


def test_a_space_model_without_price_is_refused_when_a_budget_is_set(
    db: Session, ctx: Workspace, wiki: Wiki, client: TestClient, database: Database
) -> None:
    tidy.update_settings(db, wiki.root, wiki.infra, model="modele-maison")  # no budget
    db.commit()
    with pytest.raises(SynaptaneError, match="chosen by these spaces: Infra \\(modele-maison\\)"):
        with_budget(db, ctx, wiki)
    db.rollback()
    tidy.update_settings(db, wiki.root, wiki.infra, model="")
    with_budget(db, ctx, wiki)
    with pytest.raises(SynaptaneError, match="Unknown price for the model “modele-maison”"):
        tidy.update_settings(db, wiki.root, wiki.infra, model="modele-maison", delay=5)
    db.rollback()
    found = db.get(Space, wiki.infra.id)
    assert found is not None and found.ai_model is None and found.tidy_delay is None
    tidy.update_settings(db, wiki.root, wiki.infra, model="claude-opus-5-5")  # public price
    db.commit()

    # REST and web: same refusal.
    headers = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    refused = client.patch(
        "/api/v1/organizations/acme/spaces/infra/tidy-settings",
        json={"model": "modele-maison"},
        headers=headers,
    )
    assert refused.status_code == 400 and "Unknown price" in refused.json()["message"]
    login(client, "root")
    page = "/o/acme/infra/-/tidy-settings"
    response = client.post(
        page,
        data={
            "csrf": csrf(client, page),
            "tidy_time": "02:00",
            "tidy_timezone": "Europe/Paris",
            "ai_model": "modele-maison",
        },
    )
    assert "Tarif inconnu pour le modèle" in response.text
    db.expire_all()
    found = db.get(Space, wiki.infra.id)
    assert found is not None and found.ai_model == "claude-opus-5-5"

    # A price entered by the organization applies to all its models.
    with_budget(db, ctx, wiki, price_input=Decimal(1), price_output=Decimal(2))
    tidy.update_settings(db, wiki.root, wiki.infra, model="modele-maison")


def test_an_unknown_price_is_counted_at_the_highest_known_price(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    with_budget(db, ctx, wiki)
    # Model removed from the table after an update: the setting would have refused it.
    found = db.get(Space, wiki.infra.id)
    assert found is not None
    found.ai_model = "claude-retire-1"
    db.commit()
    deposit_note(db, database, ctx, wiki)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory)
    assert record is not None and record.status == "done"
    # 1,000 × 10 + 200 × 50 per million: the highest price in the table.
    assert record.cost == Decimal("0.0200")
    [note] = [op for op in record.operations if op["op"] == "attention"]
    assert note["path"] == "claude-retire-1" and "highest known price" in str(note["detail"])
    db.expire_all()
    assert budget.spent(db, wiki.infra.org_id) == Decimal("0.0200")

    # Without a budget: no fallback, unknown cost (and nothing made up in the log).
    ai_settings.configure(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        provider="anthropic",
        model="claude-sonnet-5-5",
    )
    db.commit()
    deposit_note(db, database, ctx, wiki)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory)
    assert record is not None and record.cost is None
    assert not [op for op in record.operations if op["op"] == "attention"]


def test_prudent_price_is_the_most_expensive_public_price() -> None:
    price = pricing.prudent_price()
    assert (price.input, price.output) == max(pricing.PUBLIC_PRICES.values(), key=lambda p: p[1])
    assert price.cache_read == pricing.CACHE_READ


# --- 2. Suggested budget and warning -------------------------------------------------------------


def test_the_ai_page_suggests_a_budget_and_warns_without_one(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    login(client, "root")
    page = client.get("/o/acme/-/ai").text
    assert 'name="monthly_budget" inputmode="decimal" value="20"' in page
    assert "Aucun budget mensuel" not in page  # nothing configured: no spending yet

    # Subscription: the suggested budget does not apply, it is ignored.
    token = csrf(client, "/o/acme/-/ai")
    client.post(
        "/o/acme/-/ai",
        data={
            "csrf": token,
            "provider": "claude_code",
            "monthly_budget": "20",
            "budget_suggested": "20",
        },
    )
    view = ai_settings.view(db, wiki.root, ctx.box, wiki.infra.organization)
    assert view is not None and view.provider == "claude_code" and view.monthly_budget is None
    assert "Aucun budget mensuel" not in client.get("/o/acme/-/ai").text

    # Billed API key: the suggested budget is kept.
    token = csrf(client, "/o/acme/-/ai")
    client.post(
        "/o/acme/-/ai",
        data={
            "csrf": token,
            "provider": "anthropic",
            "api_key": "sk-ant-test",
            "monthly_budget": "20",
            "budget_suggested": "20",
        },
    )
    db.expire_all()
    view = ai_settings.view(db, wiki.root, ctx.box, wiki.infra.organization)
    assert view is not None and view.monthly_budget == Decimal(20) and view.metered

    # Budget removed: the warning is shown, and the REST API says so too.
    token = csrf(client, "/o/acme/-/ai")
    client.post("/o/acme/-/ai", data={"csrf": token, "provider": "anthropic"})
    page = client.get("/o/acme/-/ai").text
    assert "Aucun budget mensuel" in page
    headers = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    ai = client.get("/api/v1/organizations/acme/ai", headers=headers).json()
    assert ai["metered"] is True and ai["monthly_budget"] is None


def test_metered_providers() -> None:
    assert budget.metered("anthropic", False) and budget.metered("mistral", True)
    assert budget.metered("openai_compatible", True)
    assert not budget.metered("openai_compatible", False)
    assert not budget.metered("ollama", True) and not budget.metered("claude_code", False)


# --- 3. Manual runs ------------------------------------------------------------------------------


def test_manual_runs_are_limited_per_person_and_day(
    db: Session, database: Database, wiki: Wiki, client: TestClient
) -> None:
    alice = wiki.alice.user.id
    for hours in (1, 2, 3, 4, 30):  # the fifth is outside the 24-hour window
        db.add(
            TidyRun(
                space_id=wiki.infra.id,
                trigger="manual",
                requested_by=alice,
                status="done",
                started_at=datetime.now(UTC) - timedelta(hours=hours),
            )
        )
    db.add(TidyRun(space_id=wiki.infra.id, trigger="chain", requested_by=alice, status="done"))
    db.commit()
    tidy.request_run(db, wiki.alice, wiki.infra)  # 4 within 24 hours: still allowed
    db.add(TidyRun(space_id=wiki.infra.id, trigger="manual", requested_by=alice, status="done"))
    db.commit()
    with pytest.raises(SynaptaneError) as refused:
        tidy.request_run(db, wiki.alice, wiki.infra)
    assert refused.value.code is ErrorCode.TOO_MANY
    assert "5 manual tidying runs" in str(refused.value)
    tidy.request_run(db, wiki.root, wiki.infra)  # organization admin: higher cap
    db.commit()

    headers = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    response = client.post("/api/v1/organizations/acme/spaces/infra/tidy", headers=headers)
    assert response.status_code == 429


# --- 4. Deposit bursts ---------------------------------------------------------------------------


def test_deposits_are_rate_limited_per_account(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, client: TestClient
) -> None:
    def deposited(at: datetime, actor_id: Any = None) -> AuditEvent:
        return AuditEvent(
            action="inbox.deposited",
            actor_id=actor_id or wiki.alice.user.id,
            org_id=wiki.infra.org_id,
            space_id=wiki.infra.id,
            at=at,
        )

    now = datetime.now(UTC)
    db.add_all(deposited(now - timedelta(seconds=n)) for n in range(1, 120))
    db.add_all(deposited(now - timedelta(hours=2)) for _ in range(50))  # outside the window
    db.add_all(deposited(now, wiki.root.user.id) for _ in range(200))  # another account
    db.commit()
    deposit_note(db, database, ctx, wiki)  # 120th deposit of the hour: allowed
    with pytest.raises(SynaptaneError) as refused:
        deposit_note(db, database, ctx, wiki)
    assert refused.value.code is ErrorCode.TOO_MANY
    assert "Too many deposits: at most 120 notes per hour" in str(refused.value)
    db.rollback()
    [item] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    with pytest.raises(SynaptaneError, match="Too many deposits"):  # moving is limited too
        deposits.move_item(db, database, wiki.root, ctx, wiki.infra, item.name, wiki.other)
    db.rollback()

    headers = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    response = client.post(
        "/api/v1/organizations/acme/spaces/infra/inbox",
        json={"title": "Encore", "content": "x"},
        headers=headers,
    )
    assert response.status_code == 429 and "Too many deposits" in response.json()["message"]

    db.execute(delete(AuditEvent).where(AuditEvent.actor_id == wiki.alice.user.id))
    db.commit()
    deposit_note(db, database, ctx, wiki)  # the window slides: allowed again


def test_chained_passes_stop_when_the_budget_is_spent(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tidy, "MAX_ITEMS", 1)
    # 1,000 × 5 + 200 × 25 per million = €0.01: the first run exhausts the budget.
    with_budget(db, ctx, wiki, "0.01", price_input=Decimal(5), price_output=Decimal(25))
    for _ in range(2):
        deposit_note(db, database, ctx, wiki)
    db.execute(delete(Job).where(Job.kind == tidy.TIDY))
    db.commit()
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory)
    assert record is not None and record.items_done == 1 and record.cost == Decimal("0.0100")
    assert queued_tidy(db) == []  # no chained run: it would be refused
    assert not tidy.schedule_soon(db, db.get(Space, wiki.infra.id) or wiki.infra)


# --- 5. Embeddings --------------------------------------------------------------------------------


class Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code, self.payload, self.text = 200, payload, ""

    def json(self) -> dict[str, Any]:
        return self.payload


def test_embedding_usage_is_read_from_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[dict[str, Any]] = [
        {"data": [{"index": 0, "embedding": [1.0]}], "usage": {"prompt_tokens": 7}},
        {"data": [{"index": 0, "embedding": [1.0]}]},  # no usage: overestimated
    ]
    monkeypatch.setattr(
        "synaptane.ai.embeddings.httpx2.post", lambda *_, **__: Response(answers.pop(0))
    )
    tally = embeddings.Tally()
    config = EmbeddingConfig("openai", "text-embedding-3-small", "sk")
    embeddings.embed(config, ["bonjour"], tally)
    assert (tally.tokens, tally.estimated) == (7, False)
    embeddings.embed(config, ["x" * 30], tally)
    assert (tally.tokens, tally.estimated) == (18, True)

    assert pricing.embedding_cost("openai", "text-embedding-3-small", 1_000_000) == Decimal("0.02")
    assert pricing.embedding_cost("voyage", "modele-inconnu", 1_000_000) == Decimal("0.15")
    assert pricing.embedding_cost("ollama", "bge-m3", 1_000_000) is None
    assert pricing.embedding_cost("openai", "text-embedding-3-small", 0) is None


def test_indexing_counts_in_the_monthly_budget(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable(db, ctx, wiki)
    concepts = ConceptEmbedder()

    def billed(
        config: EmbeddingConfig, texts: list[str], tally: embeddings.Tally | None = None
    ) -> list[list[float]]:
        if tally is not None:
            tally.tokens += 500_000
        return concepts(config, texts)

    monkeypatch.setattr(embeddings, "embed", billed)
    with database.session() as session:
        config = semantic.org_config(session, ctx.box, wiki.infra.org_id)
    assert config is not None
    repo = ctx.local_repository(wiki.infra)
    vectorised, _ = semantic.index_space(database, ctx, wiki.infra.id, repo, config)
    assert vectorised > 0
    db.expire_all()
    [event] = db.scalars(select(AuditEvent).where(AuditEvent.action == budget.EMBEDDING_COST))
    calls = len(concepts.calls)
    assert event.details["tokens"] == str(500_000 * calls)
    assert budget.spent(db, wiki.infra.org_id) == Decimal("0.01") * calls

    # A fake provider passed by hand is not counted; neither is a local provider.
    semantic.clear(db, wiki.infra.id)
    db.commit()
    semantic.index_space(database, ctx, wiki.infra.id, repo, config, partial(billed, tally=None))
    local = EmbeddingConfig("ollama", "bge-m3")
    assert budget.record_embeddings(db, wiki.infra, local.provider, local.model, 10, False) is None
    db.expire_all()
    events = select(AuditEvent).where(AuditEvent.action == budget.EMBEDDING_COST)
    assert len(db.scalars(events).all()) == 1
