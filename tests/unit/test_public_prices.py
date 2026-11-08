"""Public prices per provider: lookup of the model's name, cache as each provider bills it,
long prompts, dated changes, prudent price, usage read from OpenAI-compatible APIs."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from synaptane.ai import pricing
from synaptane.ai.base import AIConfig, Turn, Usage
from synaptane.ai.openai_compat import OpenAICompatConversation

DAY = date(2026, 11, 8)  # the day the prices were read


def rate(provider: str, model: str, on: date = DAY) -> tuple[Decimal, Decimal, Decimal | None]:
    found = pricing.public_price(provider, model, on)
    assert found is not None, model
    return found.input, found.output, found.cached


def d(*values: str) -> tuple[Decimal, ...]:
    return tuple(Decimal(v) for v in values)


# --- Each provider -------------------------------------------------------------------------------


def test_anthropic_prices() -> None:
    assert rate("anthropic", "claude-fable-5-1") == d("10", "50", "0.25")
    assert rate("anthropic", "claude-opus-5-5") == d("4", "20", "0.20")
    assert rate("anthropic", "claude-sonnet-5-5") == d("2", "10", "0.10")
    assert rate("anthropic", "claude-sonnet-4-6") == d("3", "15", "0.30")
    assert rate("anthropic", "claude-haiku-5-5") == d("0.10", "0.50", "0.01")
    assert rate("anthropic", "claude-haiku-4-5") == d("1", "5", "0.10")
    # Retired models are not in the table.
    assert pricing.public_price("anthropic", "claude-opus-4-1", DAY) is None


def test_openai_prices() -> None:
    assert rate("openai", "gpt-6-astra") == d("10", "50", "1.00")
    assert rate("openai", "gpt-6.1-sol") == d("2", "10", "0.10")
    assert rate("openai", "gpt-5.4-mini") == d("0.75", "4.50", "0.075")
    assert rate("openai", "gpt-5") == d("1.25", "10", "0.125")
    assert rate("openai", "gpt-4o") == d("2.50", "10", "1.25")
    # Responses API only: Synaptane cannot use them, they have no price here.
    for model in ("gpt-5-pro", "gpt-5.5-pro", "o3-pro", "gpt-5.3-codex"):
        assert pricing.public_price("openai", model, DAY) is None


def test_gemini_prices_through_the_compatible_api() -> None:
    assert rate("openai_compatible", "gemini-3.8-flash") == d("0.75", "3.75", "0.075")
    assert rate("openai_compatible", "gemini-3.1-pro-preview") == d("2", "12", "0.20")
    assert rate("openai_compatible", "gemini-2.5-flash-lite") == d("0.10", "0.40", "0.01")


def test_mistral_prices() -> None:
    assert rate("mistral", "mistral-large-latest") == d("0.7", "2.1", "0.07")
    assert rate("mistral", "mistral-large-4") == d("0.7", "2.1", "0.07")
    assert rate("mistral", "mistral-small-2603") == d("0.15", "0.6", "0.015")
    assert rate("mistral", "codestral-latest") == d("0.3", "0.9", "0.03")
    # An older dated version is not the current one: no price borrowed.
    assert pricing.public_price("mistral", "mistral-small-2506", DAY) is None


def test_deepseek_prices_at_peak_hours() -> None:
    assert rate("openai_compatible", "deepseek-flash") == d("0.3", "1.2", "0.006")
    assert rate("openai_compatible", "deepseek-v4-flash") == d("0.3", "1.2", "0.006")  # legacy
    assert rate("openai_compatible", "deepseek-v4-pro") == d("1.32", "3.96", "0.044")
    assert pricing.public_price("openai_compatible", "deepseek-chat", DAY) is None


def test_the_tables_are_consistent() -> None:
    names = [name for table in pricing.PUBLIC_PRICES.values() for name in table]
    assert len(names) == len(set(names))  # one family per name: no ambiguous lookup
    for name in names:
        assert name == name.strip().lower() and "/" not in name
    for table in pricing.PUBLIC_PRICES.values():
        for tariffs in table.values():
            assert [t.since for t in tariffs] == sorted(
                (t.since for t in tariffs), key=lambda s: s or date.min
            )
            for tariff in tariffs:
                for r in (tariff.rate, tariff.long):
                    if r is None:
                        continue
                    assert 0 < r.input <= r.output * 5 and r.write in (1, Decimal("1.25"))
                    assert r.cached is None or r.cached < r.input
                if tariff.long is not None:
                    assert tariff.rate is not None and tariff.long_prompt >= 100_000
                    assert tariff.long.input > tariff.rate.input


# --- Which tables for which provider --------------------------------------------------------------


def test_a_model_is_found_whatever_the_provider_that_calls_it() -> None:
    # The OpenAI provider pointed at Anthropic's OpenAI-compatible endpoint, or at a gateway:
    # the Claude model keeps its price (it had it before the per-provider tables).
    sonnet = pricing.price_for("openai", "claude-sonnet-5-5", None, None, DAY)
    assert sonnet is not None and (sonnet.input, sonnet.output) == (Decimal(2), Decimal(10))
    assert sonnet.cache_read == Decimal("0.05") and sonnet.cache_write == Decimal("1.25")
    assert pricing.tariff("anthropic", "gpt-5", DAY) == pricing.tariff("openai", "gpt-5", DAY)
    assert pricing.price_for("mistral", "gemini-3.8-flash", None, None, DAY) is not None
    assert pricing.price_for("mistral", "openai/gpt-5.4-mini", None, None, DAY) is not None
    for model in ("claude-sonnet-5-5", "gpt-5", "mistral-large-latest", "deepseek-flash"):
        assert pricing.price_for("openai_compatible", model, None, None, DAY) is not None
    # Exact names only, in every table: no prefix borrowed through the fallback either.
    assert pricing.price_for("openai", "claude-opus-5-7", None, None, DAY) is None
    assert pricing.price_for("mistral", "gpt-5-pro", None, None, DAY) is None
    # The provider's own table comes first (the names never overlap anyway).
    assert pricing.PROVIDER_TABLES["mistral"][0] == "mistral"
    assert set(pricing.PROVIDER_TABLES["openai"]) == set(pricing.PUBLIC_PRICES)


def test_local_models_and_subscriptions_have_no_public_price() -> None:
    # Ollama runs locally: an open model with the name of a paid one costs nothing.
    assert pricing.price_for("ollama", "mistral-small-latest", None, None, DAY) is None
    assert pricing.price_for("ollama", "claude-sonnet-5-5", None, None, DAY) is None
    assert pricing.price_for("codex", "gpt-5", None, None, DAY) is None  # subscription
    assert pricing.price_for("claude_code", "claude-sonnet-5-5", None, None, DAY) is None
    assert pricing.price_for("unknown_provider", "gpt-5", None, None, DAY) is None


# --- Names ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "model", "known"),
    [
        ("openai", "gpt-5-2025-08-07", "gpt-5"),
        ("openai", "gpt-4.1-mini-2025-04-14", "gpt-4.1-mini"),
        ("openai", " GPT-5.4-Mini ", "gpt-5.4-mini"),
        ("openai_compatible", "openai/gpt-6-astra", "gpt-6-astra"),
        ("openai_compatible", "models/gemini-2.5-pro", "gemini-2.5-pro"),
        ("openai_compatible", "google/gemini-3.8-flash", "gemini-3.8-flash"),
        ("openai_compatible", "deepseek/deepseek-v4-pro", "deepseek-v4-pro"),
        ("openai_compatible", "mistralai/mistral-large-latest", "mistral-large-latest"),
        ("anthropic", "claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("anthropic", "claude-opus-4-5@20251101", "claude-opus-4-5"),
        # Used to take the price of claude-opus-5 ($5 / $25) by prefix: now its own ($4 / $20).
        ("anthropic", "claude-opus-5-5@20260901", "claude-opus-5-5"),
        ("openai_compatible", "anthropic/claude-sonnet-5.5", "claude-sonnet-5-5"),
        ("openai_compatible", "anthropic/claude-sonnet-4.5-20250929", "claude-sonnet-4-5"),
    ],
)
def test_router_prefixes_and_dated_snapshots(provider: str, model: str, known: str) -> None:
    assert pricing.tariff(provider, model, DAY) == pricing.tariff("openai_compatible", known, DAY)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("anthropic", "claude-opus-5-7"),  # not claude-opus-5
        ("anthropic", "claude-sonnet-5-5-preview"),
        ("openai", "gpt-5-pro"),  # not gpt-5
        ("openai", "gpt-5.4-mini-high"),
        ("openai", "gpt-5-2025"),  # not a full date
        ("openai", "gpt-4o-audio-preview"),
        ("openai", "gpt"),
        ("openai_compatible", "gemini-3.8"),
        ("openai_compatible", "gemini-3.8-flash-tts"),
        ("openai_compatible", "deepseek-flash-max"),
        ("mistral", "mistral-large"),
        ("mistral", "mistral-large-2411"),
        ("openai_compatible", "meta-llama/llama-3.3-70b-instruct"),
        ("openai_compatible", ""),
    ],
)
def test_an_unknown_model_never_borrows_a_price(provider: str, model: str) -> None:
    assert pricing.tariff(provider, model, DAY) is None
    assert pricing.price_for(provider, model, None, None, DAY) is None


# --- Price entered by the organization ------------------------------------------------------------


def test_the_price_entered_comes_first_with_the_model_cache_shares() -> None:
    own = pricing.price_for("openai", "gpt-6-astra", Decimal(3), Decimal(9), DAY)
    assert own is not None and not own.public
    assert (own.input, own.output) == (Decimal(3), Decimal(9))
    assert (own.cache_read, own.cache_write) == (Decimal("0.1"), Decimal("1.25"))
    assert own.long is None  # a flat price entered: no long-prompt price
    deepseek = pricing.price_for("openai_compatible", "deepseek-flash", Decimal(1), Decimal(1))
    assert deepseek is not None and deepseek.cache_read == Decimal("0.02")
    # Unknown model: a tenth for the cache reads, 1.25 times for the writes.
    unknown = pricing.price_for("ollama", "llama3", Decimal(0), Decimal(0), DAY)
    assert unknown is not None and (unknown.cache_read, unknown.cache_write) == (
        pricing.CACHE_READ,
        pricing.CACHE_WRITE,
    )
    assert pricing.cost(Usage(input_tokens=10**6, output_tokens=10**6), unknown) == 0


# --- Cache, provider by provider ------------------------------------------------------------------


def priced(provider: str, model: str) -> pricing.Price:
    found = pricing.price_for(provider, model, None, None, DAY)
    assert found is not None
    return found


def test_cache_as_each_provider_bills_it() -> None:
    # 1 M input billed in full, of which 400,000 written to the cache; 1 M read back; no output.
    usage = Usage(input_tokens=1_000_000, cached_tokens=1_000_000, cache_write_tokens=400_000)
    # Anthropic, 5-minute writes: 2 + 0.4 × 2 × 0.25 + 1 × 0.10 (Sonnet 5.5 hits: 5 %).
    assert pricing.cost(usage, priced("anthropic", "claude-sonnet-5-5")) == Decimal("2.3000")
    # OpenAI GPT-6: 30-minute writes at 1.25 × too; 10 + 0.4 × 10 × 0.25 + 1 × 1.
    assert pricing.cost(usage, priced("openai", "gpt-6-astra")) == Decimal("12.0000")
    # Earlier OpenAI models: no write surcharge, reads at their own share (half for gpt-4o).
    assert pricing.cost(usage, priced("openai", "gpt-4o")) == Decimal("3.7500")
    # Gemini, Mistral, DeepSeek: no write surcharge either.
    assert pricing.cost(usage, priced("openai_compatible", "gemini-3.8-flash")) == Decimal("0.8250")
    assert pricing.cost(usage, priced("mistral", "mistral-large-latest")) == Decimal("0.7700")
    assert pricing.cost(usage, priced("openai_compatible", "deepseek-v4-pro")) == Decimal("1.3640")


# --- Long prompts ---------------------------------------------------------------------------------


def test_a_long_prompt_is_billed_turn_by_turn() -> None:
    price = priced("anthropic", "claude-haiku-5-5")
    assert price.long_prompt == 100_000 and price.long is not None
    usage = Usage()
    usage.add(Turn("", [], "tool_use", input_tokens=60_000, output_tokens=1_000))
    # 100,000 tokens exactly: not over the threshold.
    usage.add(Turn("", [], "tool_use", input_tokens=40_000, cached_tokens=60_000))
    # 150,000 tokens with the cache: the whole request at the higher price, cache included.
    usage.add(
        Turn("", [], "end", input_tokens=50_000, cached_tokens=100_000, cache_write_tokens=50_000)
    )
    short = (60_000 + 40_000) * Decimal("0.10") + 1_000 * Decimal("0.50") + 60_000 * Decimal("0.01")
    long = 50_000 * Decimal("0.50") * Decimal("1.25") + 100_000 * Decimal("0.05")
    assert (short + long) / pricing.MILLION == Decimal("0.04735")
    assert pricing.cost(usage, price) == Decimal("0.0474")
    # Totals without the turns (a CLI, a usage built by hand): base price only.
    flat = Usage(usage.input_tokens, usage.output_tokens, usage.cached_tokens)
    flat.cache_write_tokens = usage.cache_write_tokens
    assert pricing.cost(flat, price) == Decimal("0.0184")


def test_long_prompt_thresholds_of_openai_and_gemini() -> None:
    usage = Usage()
    usage.add(Turn("", [], "end", input_tokens=272_001, output_tokens=0))
    assert pricing.cost(usage, priced("openai", "gpt-5.4")) == Decimal("1.3600")  # $5
    assert pricing.cost(usage, priced("openai", "gpt-5.4-mini")) == Decimal("0.2040")
    gemini = Usage()
    gemini.add(Turn("", [], "end", input_tokens=200_000, output_tokens=0))
    assert pricing.cost(gemini, priced("openai_compatible", "gemini-2.5-pro")) == Decimal("0.2500")
    gemini.add(Turn("", [], "end", input_tokens=200_001, output_tokens=0))
    assert pricing.cost(gemini, priced("openai_compatible", "gemini-2.5-pro")) == Decimal("0.7500")


def test_merged_usage_keeps_the_turns() -> None:
    first, second = Usage(), Usage()
    first.add(Turn("", [], "end", input_tokens=10))
    second.add(Turn("", [], "end", input_tokens=20, cached_tokens=5))
    first.merge(second)
    assert first.turn_tokens == [(10, 0, 0, 0), (20, 5, 0, 0)]


# --- Dated changes --------------------------------------------------------------------------------


def test_announced_changes_apply_on_their_date() -> None:
    argon = "gemini-4-argon"
    assert rate("openai_compatible", argon, date(2026, 11, 30)) == d("2", "10", "0.10")
    assert rate("openai_compatible", argon, date(2026, 12, 1)) == d("4", "20", "0.20")
    # Deprecated model, billed at the rates of the model it is routed to.
    pro = "gemini-2.5-pro"
    assert rate("openai_compatible", pro) == d("1.25", "10", "0.125")
    assert rate("openai_compatible", pro, date(2026, 12, 1)) == d("0.75", "3.75", "0.075")
    assert rate("openai_compatible", pro, date(2027, 1, 1)) == d("1.50", "7.50", "0.15")
    december = pricing.price_for("openai_compatible", pro, None, None, date(2026, 12, 1))
    assert december is not None and december.long is None  # no long-prompt price any more


def test_a_promotion_without_a_following_price_stops_being_known() -> None:
    assert rate("openai", "gpt-5.6-sol", date(2026, 11, 30)) == d("4", "20", "0.40")
    assert pricing.public_price("openai", "gpt-5.6-sol", date(2026, 12, 1)) is None
    assert pricing.price_for("openai", "gpt-5.6-sol", None, None, date(2026, 12, 1)) is None
    own = pricing.price_for("openai", "gpt-5.6-sol", Decimal(4), Decimal(20), date(2026, 12, 1))
    assert own is not None and own.cache_read == pricing.CACHE_READ


# --- Prudent price --------------------------------------------------------------------------------


def test_the_prudent_price_stays_at_the_level_of_the_most_expensive_usable_models() -> None:
    price = pricing.prudent_price(DAY)
    # Claude Fable 5.1 and GPT-6 Astra: $10 / $50. Not GPT-6 Astra's long-prompt price ($20 /
    # $75), nor OpenAI's "pro" models ($30 / $180, not in the table).
    assert (price.input, price.output) == (Decimal(10), Decimal(50))
    # Unchanged for existing instances: the same as before the per-provider tables.
    assert (price.cache_read, price.cache_write) == (Decimal("0.1"), Decimal("1.25"))
    assert price.public and price.cache_read == pricing.CACHE_READ and price.long is None
    assert pricing.prudent_price(date(2027, 6, 1)).output == Decimal(50)


def test_the_prudent_price_is_not_a_ceiling_for_restricted_models() -> None:
    # gpt-6-cyber ($20 / $100, Chat Completions) needs "Daybreak Red access and approval": not in
    # the table, so as not to double the cost counted for every unknown model. Its price is to
    # be entered; until then, it is counted at the prudent price, lower than its own.
    assert pricing.price_for("openai", "gpt-6-cyber", None, None, DAY) is None
    assert pricing.prudent_price(DAY).output < Decimal(100)
    own = pricing.price_for("openai", "gpt-6-cyber", Decimal(20), Decimal(100), DAY)
    assert own is not None and (own.input, own.output) == (Decimal(20), Decimal(100))


# --- Usage reported by OpenAI-compatible APIs -----------------------------------------------------


class Answer:
    status_code = 200

    def __init__(self, usage: dict[str, Any]) -> None:
        self.usage = usage

    def json(self) -> dict[str, Any]:
        return {
            "choices": [{"message": {"role": "assistant", "content": "Fini."}}],
            "usage": self.usage,
        }


def turn_for(usage: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Turn:
    monkeypatch.setattr("synaptane.ai.openai_compat.httpx2.post", lambda *_, **__: Answer(usage))
    config = AIConfig("openai", "gpt-6-astra", "sk-test")
    return OpenAICompatConversation(config, "système", "bonjour", []).step()


def test_openai_cache_writes_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    turn = turn_for(
        {
            "prompt_tokens": 15_000,
            "completion_tokens": 300,
            "total_tokens": 15_300,
            "prompt_tokens_details": {"cached_tokens": 12_000, "cache_write_tokens": 3_000},
        },
        monkeypatch,
    )
    assert (turn.input_tokens, turn.cached_tokens, turn.cache_write_tokens) == (
        3_000,
        12_000,
        3_000,
    )
    assert turn.output_tokens == 300


def test_deepseek_cache_hits_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    turn = turn_for(
        {"prompt_tokens": 70, "completion_tokens": 11, "prompt_cache_hit_tokens": 64},
        monkeypatch,
    )
    assert (turn.input_tokens, turn.cached_tokens, turn.output_tokens) == (6, 64, 11)


def test_thinking_tokens_left_out_of_completion_tokens_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = turn_for(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 520}, monkeypatch
    )
    assert (turn.input_tokens, turn.output_tokens) == (100, 420)
    assert turn_for({}, monkeypatch).output_tokens == 0


# --- Embeddings -----------------------------------------------------------------------------------


def test_embedding_prices_and_names() -> None:
    million = 1_000_000
    assert pricing.embedding_cost("gemini", "gemini-embedding-2", million) == Decimal("0.2")
    assert pricing.embedding_cost("gemini", "models/gemini-embedding-001", million) == Decimal(
        "0.15"
    )
    assert pricing.embedding_cost("openai", "text-embedding-ada-002", million) == Decimal("0.1")
    assert pricing.embedding_cost("mistral", "codestral-embed-2505", million) == Decimal("0.15")
    assert pricing.embedding_cost("voyage", "voyage-code-4", million) == Decimal("0.12")
    assert pricing.embedding_cost("voyage", "voyage-3", million) == Decimal("0.06")
    assert pricing.embedding_cost("voyage", "voyage-3-lite", million) == Decimal("0.02")
    assert pricing.embedding_cost("voyage", "voyage-context-4", million) == Decimal("0.12")
    assert pricing.embedding_cost("voyage", "voyage-law-2", million) == Decimal("0.12")
    assert pricing.embedding_cost("voyage", "voyage-context-3", million) == Decimal("0.18")
    assert pricing.embedding_cost("voyage", "voyage-lite-02-instruct", million) == Decimal("0.1")
    # Unknown model: the highest price of its own provider, never zero.
    assert pricing.embedding_cost("openai", "text-embedding-4", million) == Decimal("0.13")
    assert pricing.embedding_cost("mistral", "mistral-embed-2", million) == Decimal("0.15")
    assert pricing.embedding_cost("openai_compatible", "text-embedding-3-small", million) is None


def test_a_rate_without_cache_price_has_no_cache_discount() -> None:
    assert pricing.Rate(Decimal(2), Decimal(8)).cache_read == 1
    assert pricing.Rate(Decimal(0), Decimal(0), Decimal(0)).cache_read == 1
