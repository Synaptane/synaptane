"""Model-specific details of embeddings: task prefixes and prices."""

from decimal import Decimal

from synaptane.ai import pricing
from synaptane.ai.embeddings import prefixes


def test_voyage_models_get_the_prompts_of_their_input_type() -> None:
    query, document = prefixes("voyage-4-large")
    assert query == "Represent the query for retrieving supporting documents: "
    assert document == "Represent the document for retrieval: "
    assert prefixes("voyage-3.5-lite") == (query, document)


def test_models_without_task_prefix_are_unchanged() -> None:
    assert prefixes("text-embedding-3-small") == ("", "")
    assert prefixes("bge-m3") == ("", "")


def test_voyage_4_models_have_their_public_price() -> None:
    million = 1_000_000
    assert pricing.embedding_cost("voyage", "voyage-4-large", million) == Decimal("0.12")
    assert pricing.embedding_cost("voyage", "voyage-4", million) == Decimal("0.06")
    assert pricing.embedding_cost("voyage", "voyage-4-lite", million) == Decimal("0.02")
