"""Estimated cost: cache writes are billed at 1.25 times the input price."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from synaptane.ai import pricing
from synaptane.ai.anthropic_provider import AnthropicConversation
from synaptane.ai.base import AIConfig, Turn, Usage


def test_cache_writes_cost_a_quarter_more_than_plain_input() -> None:
    price = pricing.price_for("anthropic", "claude-sonnet-5-5", None, None)
    assert price is not None and price.cache_write == Decimal("1.25")
    # 1 M input of which 400,000 written to cache, 1 M read back, 100,000 output ($2 / $10).
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cached_tokens=1_000_000)
    usage.cache_write_tokens = 400_000
    # 2 (input) + 0.2 (write surcharge: 0.4 × 2 × 0.25) + 0.2 (cache read) + 1 (output)
    assert pricing.cost(usage, price) == Decimal("3.4000")
    usage.cache_write_tokens = 0
    assert pricing.cost(usage, price) == Decimal("3.2000")  # the old calculation, without writes


def test_usage_adds_and_merges_cache_writes() -> None:
    usage = Usage()
    usage.add(Turn("", [], "end", input_tokens=500, cache_write_tokens=300))
    other = Usage(input_tokens=10)
    other.cache_write_tokens = 7
    usage.merge(other)
    assert (usage.input_tokens, usage.cache_write_tokens) == (510, 307)


def test_anthropic_turn_counts_cache_writes_apart() -> None:
    conversation = AnthropicConversation(
        AIConfig("anthropic", "claude-sonnet-5-5", "sk-ant-test"), "système", "bonjour", []
    )
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Fini.")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=120,
            output_tokens=30,
            cache_creation_input_tokens=2000,
            cache_read_input_tokens=5000,
        ),
    )

    def create(**_: Any) -> SimpleNamespace:
        return response

    conversation.client = SimpleNamespace(  # type: ignore[assignment]
        beta=SimpleNamespace(messages=SimpleNamespace(create=create))
    )
    turn = conversation.step()
    assert (turn.input_tokens, turn.cache_write_tokens, turn.cached_tokens) == (2120, 2000, 5000)
