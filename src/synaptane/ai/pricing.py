"""Estimated cost of a run, in euros.

Price entered by the organization (€ per million tokens, input and output), or else the
model's public price when known: public amounts are in dollars and counted one for one in
euros, hence "estimated". Subscriptions (Claude Code, Codex) have no pay-per-use cost.

Public prices are kept per provider (`PUBLIC_PRICES`), each block with the page it was read
from and the date. They are the standard, pay-as-you-go prices of the provider's own API: no
batch, priority or regional surcharge, no free allowance, no volume discount. A model is looked
up in the table of its provider first, then in the others, by its exact name (a Claude model
called through the OpenAI provider at a custom address, a gateway, Gemini or DeepSeek behind an
OpenAI-compatible API); never for Ollama (local) or a subscription. Models with restricted
access (approval by the provider) are not in the tables: their price is to be entered.

Prompt cache, as each provider bills it (`Rate`):
- input read back from the cache is billed at the model's cached-input price (a tenth of the
  input price for most models; `cache_read` is that share);
- input written to the cache costs more only where the provider says so: 1.25 times the input
  price at Anthropic (5-minute duration, the only one Synaptane asks for: one hour would cost
  2 times) and for OpenAI's GPT-5.6 and later models (30-minute default lifetime); no surcharge
  elsewhere.
With a price entered by the organization, the cache shares of the model's public price apply
to it (a tenth and 1.25 times for a model not in the tables).

Some prices depend on the size of each request's prompt (input, cache reads and writes
included): above `long_prompt` tokens, that request is billed at the `long` rate (OpenAI over
272K, Gemini Pro over 200K, Claude Haiku 5.5 over 100K tokens). The cost is then computed turn
by turn (`Usage.turn_tokens`). Announced price changes are dated (`Tariff.since`); a
promotional price whose end is announced without the following price stops being known at
that date.

A model with no known price is never counted as zero when a budget is set: it is counted at
the highest base price of the tables (`prudent_price`, see `budget.run_price`), that is of the
models open to every customer. It is not a ceiling: a restricted model may cost more
(`gpt-6-cyber`, $20 / $100), and its price must then be entered.

Model names: case and spaces are ignored, and so is a router or namespace prefix
(`openai/gpt-5`, `models/gemini-2.5-pro`); a dated snapshot is billed like its model
(`gpt-5-2025-08-07`, `claude-haiku-4-5-20251001`), and OpenRouter's dotted Claude names are
recognized (`anthropic/claude-sonnet-5.5`). Nothing else: an unknown model never takes the
price of a known model whose name starts the same way (`gpt-5-pro` is not `gpt-5`).
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from synaptane.ai.base import SUBSCRIPTION_PROVIDERS, Usage

MILLION = Decimal(1_000_000)
CACHE_READ = Decimal("0.1")  # default: input read back from the cache, share of the input price
CACHE_WRITE = Decimal("1.25")  # default (Anthropic): input written to the cache, 5 minutes
NO_SURCHARGE = Decimal(1)


@dataclass(frozen=True)
class Rate:
    """Public price of a model, in dollars per million tokens."""

    input: Decimal
    output: Decimal
    cached: Decimal | None = None  # input read back from the cache; None: no cache price
    write: Decimal = NO_SURCHARGE  # input written to the cache, share of the input price

    @property
    def cache_read(self) -> Decimal:
        """Input read back from the cache, share of the input price (1: no discount known)."""
        if self.cached is None or not self.input:
            return NO_SURCHARGE
        return self.cached / self.input


@dataclass(frozen=True)
class Tariff:
    """Price of a model from `since` (None: already in force). `rate` None: no known price
    from that date (promotion ending, following price not published)."""

    rate: Rate | None
    since: date | None = None
    long_prompt: int = 0  # requests whose prompt exceeds this many tokens: `long` rate
    long: Rate | None = None


def _p(
    input: str,
    output: str,
    cached: str | None = None,
    *,
    write: str = "1",
    since: date | None = None,
    long_prompt: int = 0,
    long: tuple[str, str, str] | None = None,
) -> tuple[Tariff, ...]:
    """One tariff, amounts as written on the provider's page; tariffs are added up (`+`) when
    a change is announced."""
    surcharge = Decimal(write)
    rate = Rate(
        Decimal(input), Decimal(output), Decimal(cached) if cached is not None else None, surcharge
    )
    higher = Rate(Decimal(long[0]), Decimal(long[1]), Decimal(long[2]), surcharge) if long else None
    return (Tariff(rate, since, long_prompt, higher),)


def _ended(since: date) -> tuple[Tariff, ...]:
    return (Tariff(None, since),)


_ANTHROPIC_WRITE = "1.25"  # 5-minute cache write

# Anthropic. Sources: https://platform.claude.com/docs/en/about-claude/pricing (prices, cache
# multipliers, long context) and https://platform.claude.com/docs/en/about-claude/model-deprecations
# (active or deprecated models only: Claude Opus 4 and 4.1 are retired), read on 2026-11-08.
# Columns: input, output, cache hits; 5-minute cache writes at 1.25 times the input.
ANTHROPIC: dict[str, tuple[Tariff, ...]] = {
    "claude-fable-5-1": _p("10", "50", "0.25", write=_ANTHROPIC_WRITE),
    "claude-mythos-5-1": _p("10", "50", "0.25", write=_ANTHROPIC_WRITE),
    "claude-fable-5": _p("10", "50", "1", write=_ANTHROPIC_WRITE),
    "claude-mythos-5": _p("10", "50", "1", write=_ANTHROPIC_WRITE),
    "claude-opus-5-5": _p("4", "20", "0.20", write=_ANTHROPIC_WRITE),
    "claude-opus-5": _p("5", "25", "0.50", write=_ANTHROPIC_WRITE),
    "claude-opus-4-8": _p("5", "25", "0.50", write=_ANTHROPIC_WRITE),
    "claude-opus-4-7": _p("5", "25", "0.50", write=_ANTHROPIC_WRITE),
    "claude-opus-4-6": _p("5", "25", "0.50", write=_ANTHROPIC_WRITE),
    "claude-opus-4-5": _p("5", "25", "0.50", write=_ANTHROPIC_WRITE),
    "claude-sonnet-5-5": _p("2", "10", "0.10", write=_ANTHROPIC_WRITE),
    "claude-sonnet-5": _p("2", "10", "0.20", write=_ANTHROPIC_WRITE),
    "claude-sonnet-4-6": _p("3", "15", "0.30", write=_ANTHROPIC_WRITE),
    "claude-sonnet-4-5": _p("3", "15", "0.30", write=_ANTHROPIC_WRITE),
    # A request whose prompt is over 100,000 tokens (cache reads and writes included) pays the
    # higher prices.
    "claude-haiku-5-5": _p(
        "0.10",
        "0.50",
        "0.01",
        write=_ANTHROPIC_WRITE,
        long_prompt=100_000,
        long=("0.50", "2.50", "0.05"),
    ),
    "claude-haiku-4-5": _p("1", "5", "0.10", write=_ANTHROPIC_WRITE),
}

_OPENAI_WRITE = "1.25"  # GPT-5.6 and later: 30-minute cache write
_OPENAI_LONG = 272_000  # "Short context: ≤272K input tokens"

# OpenAI. Sources: https://developers.openai.com/api/docs/pricing ("Standard", short and long
# context; the long-context prices come from the page's own price table) and
# https://developers.openai.com/api/docs/guides/prompt-caching (cache writes), read on
# 2026-11-08. Columns: input, output, cached input. Only models that answer through Chat
# Completions (each model's page): the "pro" models (gpt-5.5-pro, gpt-5.4-pro, gpt-5.2-pro,
# gpt-5-pro, o3-pro) and gpt-5.3-codex only answer through the Responses API, which Synaptane
# does not use.
OPENAI: dict[str, tuple[Tariff, ...]] = {
    "gpt-6-astra": _p(
        "10", "50", "1.00", write=_OPENAI_WRITE, long_prompt=_OPENAI_LONG, long=("20", "75", "2")
    ),
    "gpt-6.1-sol": _p(
        "2", "10", "0.10", write=_OPENAI_WRITE, long_prompt=_OPENAI_LONG, long=("4", "15", "0.20")
    ),
    "gpt-6.1-terra": _p(
        "1", "5", "0.05", write=_OPENAI_WRITE, long_prompt=_OPENAI_LONG, long=("2", "7.5", "0.10")
    ),
    "gpt-6-sol": _p(
        "2", "10", "0.20", write=_OPENAI_WRITE, long_prompt=_OPENAI_LONG, long=("4", "15", "0.40")
    ),
    "gpt-6-luna": _p(
        "0.10",
        "0.50",
        "0.01",
        write=_OPENAI_WRITE,
        long_prompt=_OPENAI_LONG,
        long=("0.20", "0.75", "0.02"),
    ),
    # Promotional price "available until November 30, 2026"; the next one is not published.
    "gpt-5.6-sol": _p(
        "4", "20", "0.40", write=_OPENAI_WRITE, long_prompt=_OPENAI_LONG, long=("8", "30", "0.80")
    )
    + _ended(date(2026, 12, 1)),
    "gpt-5.6-terra": _p(
        "2", "12", "0.20", write=_OPENAI_WRITE, long_prompt=_OPENAI_LONG, long=("4", "18", "0.40")
    ),
    "gpt-5.6-luna": _p(
        "0.20",
        "1.20",
        "0.02",
        write=_OPENAI_WRITE,
        long_prompt=_OPENAI_LONG,
        long=("0.40", "1.80", "0.04"),
    ),
    "gpt-5.5": _p("5", "30", "0.50", long_prompt=_OPENAI_LONG, long=("10", "45", "1")),
    "gpt-5.4": _p("2.50", "15", "0.25", long_prompt=_OPENAI_LONG, long=("5", "22.5", "0.50")),
    "gpt-5.4-mini": _p("0.75", "4.50", "0.075"),
    "gpt-5.4-nano": _p("0.20", "1.25", "0.02"),
    "gpt-5.2": _p("1.75", "14", "0.175"),
    "gpt-5.1": _p("1.25", "10", "0.125"),
    "gpt-5": _p("1.25", "10", "0.125"),
    "gpt-5-mini": _p("0.25", "2", "0.025"),
    "gpt-5-nano": _p("0.05", "0.40", "0.005"),
    "gpt-4.1": _p("2", "8", "0.50"),
    "gpt-4.1-mini": _p("0.40", "1.60", "0.10"),
    "gpt-4o": _p("2.50", "10", "1.25"),
    "gpt-4o-mini": _p("0.15", "0.60", "0.075"),
    "o3": _p("2", "8", "0.50"),
    "chat-latest": _p("5", "30", "0.50"),
}

_GEMINI_LONG = 200_000  # "prompts <= 200k tokens"
_GEMINI_3_8_FLASH = _p("0.75", "3.75", "0.075") + _p("1.50", "7.50", "0.15", since=date(2027, 1, 1))

# Google Gemini, through its OpenAI-compatible API (or a gateway). Source:
# https://ai.google.dev/gemini-api/docs/pricing (now https://aistudio.google.com/docs/pricing),
# "Paid Tier", "Standard" tab, text input, read on 2026-11-08. Columns: input, output
# (thinking included), context caching (implicit caching: no storage cost). Announced changes
# are dated. gemini-2.5-pro and gemini-2.5-flash, deprecated, are billed from December 1, 2026
# at the rates of the model they are then routed to (Gemini 3.8 Flash, Gemini 3.5 Flash-Lite:
# the same as today for gemini-2.5-flash).
GEMINI: dict[str, tuple[Tariff, ...]] = {
    "gemini-4-argon": _p("2", "10", "0.10") + _p("4", "20", "0.20", since=date(2026, 12, 1)),
    "gemini-3.8-flash": _GEMINI_3_8_FLASH,
    "gemini-3.6-flash": _p("0.75", "3.75", "0.075")
    + _p("1.50", "7.50", "0.15", since=date(2027, 1, 1)),
    "gemini-3.5-flash-lite": _p("0.30", "2.50", "0.03"),
    "gemini-3.1-pro-preview": _p(
        "2", "12", "0.20", long_prompt=_GEMINI_LONG, long=("4", "18", "0.40")
    ),
    "gemini-3.1-pro-preview-customtools": _p(
        "2", "12", "0.20", long_prompt=_GEMINI_LONG, long=("4", "18", "0.40")
    ),
    "gemini-3.1-flash-lite": _p("0.25", "1.50", "0.025"),
    "gemini-3-flash-preview": _p("0.50", "3", "0.05"),
    "gemini-2.5-pro": _p(
        "1.25", "10", "0.125", long_prompt=_GEMINI_LONG, long=("2.50", "15", "0.25")
    )
    + _p("0.75", "3.75", "0.075", since=date(2026, 12, 1))
    + _p("1.50", "7.50", "0.15", since=date(2027, 1, 1)),
    "gemini-2.5-flash": _p("0.30", "2.50", "0.03"),
    "gemini-2.5-flash-lite": _p("0.10", "0.40", "0.01"),
}

# Mistral. Sources: https://docs.mistral.ai/inference/pricing ("Standard", USD), identifiers
# from each model's page (https://docs.mistral.ai/models/…), cache:
# https://docs.mistral.ai/inference/conversations/advanced/prompt-caching (cached tokens at
# 10 % of the input price), read on 2026-11-08. Columns: input, output, cached input.
# `mistral-large-latest`: undetermined. A "-latest" alias "points to the latest version, across
# generations" (https://docs.mistral.ai/inference/model-lifecycle), Mistral Large 4; but the page
# of Mistral Large 3 (https://docs.mistral.ai/models/mistral-large-3-25-12, deprecated on
# 2026-10-31, no price shown any more) still lists it among its names, and Large 4's page does
# not. Counted at the higher of the two: Large 4 ($0.7 / $2.1); Large 3 was $0.5 / $1.5 ("Mistral
# Large costs $0.5 /M tokens in and $1.5 /M tokens out", https://mistral.ai/pricing, FAQ).
# Mistral Medium 3.5 (`mistral-medium-latest`) and Large 3 (`mistral-large-2512`), deprecated,
# have no published price: left out.
_MISTRAL_LARGE_4 = _p("0.7", "2.1", "0.07")
_MISTRAL_SMALL_4 = _p("0.15", "0.6", "0.015")
_MINISTRAL_14B = _p("0.2", "0.2", "0.02")
_MINISTRAL_8B = _p("0.15", "0.15", "0.015")
_MINISTRAL_3B = _p("0.1", "0.1", "0.01")
_CODESTRAL = _p("0.3", "0.9", "0.03")
MISTRAL: dict[str, tuple[Tariff, ...]] = {
    "mistral-large-4": _MISTRAL_LARGE_4,
    "mistral-large-4-0": _MISTRAL_LARGE_4,
    "mistral-large-latest": _MISTRAL_LARGE_4,
    "mistral-small-2603": _MISTRAL_SMALL_4,
    "mistral-small-latest": _MISTRAL_SMALL_4,
    "ministral-14b-2512": _MINISTRAL_14B,
    "ministral-14b-latest": _MINISTRAL_14B,
    "ministral-8b-2512": _MINISTRAL_8B,
    "ministral-8b-latest": _MINISTRAL_8B,
    "ministral-3b-2512": _MINISTRAL_3B,
    "ministral-3b-latest": _MINISTRAL_3B,
    "codestral-2508": _CODESTRAL,
    "codestral-latest": _CODESTRAL,
}

# DeepSeek, through its OpenAI-compatible API. Source:
# https://api-docs.deepseek.com/quick_start/pricing, read on 2026-11-08. Columns: input (cache
# miss), output, input (cache hit). Peak-hour prices (01:00-04:00 and 06:00-10:00 UTC, Monday
# to Friday): off peak, a run costs half, the estimate stays on the high side. The legacy names
# deepseek-v4-flash and deepseek-v4-flash-vision-exp are "billed at the Flash price".
_DEEPSEEK_FLASH = _p("0.3", "1.2", "0.006")
DEEPSEEK: dict[str, tuple[Tariff, ...]] = {
    "deepseek-flash": _DEEPSEEK_FLASH,
    "deepseek-v4-flash": _DEEPSEEK_FLASH,
    "deepseek-v4-flash-vision-exp": _DEEPSEEK_FLASH,
    "deepseek-v4-pro": _p("1.32", "3.96", "0.044"),
}

PUBLIC_PRICES: dict[str, dict[str, tuple[Tariff, ...]]] = {
    "anthropic": ANTHROPIC,
    "openai": OPENAI,
    "gemini": GEMINI,
    "mistral": MISTRAL,
    "deepseek": DEEPSEEK,
}
# Table looked up first for each chat provider; then, by exact name, all the others: any of
# them may sit behind a custom address (the OpenAI provider pointed at Anthropic's
# OpenAI-compatible endpoint, a gateway: OpenRouter, LiteLLM), and Gemini and DeepSeek behind an
# OpenAI-compatible API. The names never overlap between tables. Ollama runs locally and
# subscriptions are paid apart: no public price (providers missing here).
PROVIDER_TABLES: dict[str, tuple[str, ...]] = {
    provider: (own, *(t for t in PUBLIC_PRICES if t != own))
    for provider, own in (("anthropic", "anthropic"), ("openai", "openai"), ("mistral", "mistral"))
}
PROVIDER_TABLES["openai_compatible"] = tuple(PUBLIC_PRICES)

# Embeddings: public prices per million input tokens, in dollars.
EMBEDDING_PRICES: dict[str, dict[str, Decimal]] = {
    # https://developers.openai.com/api/docs/pricing ("Embedding"), read on 2026-11-08.
    "openai": {
        "text-embedding-3-small": Decimal("0.02"),
        "text-embedding-3-large": Decimal("0.13"),
        "text-embedding-ada-002": Decimal("0.10"),
    },
    # https://ai.google.dev/gemini-api/docs/pricing ("Gemini Embedding", "Gemini Embedding 2"
    # text input), read on 2026-11-08.
    "gemini": {
        "gemini-embedding-001": Decimal("0.15"),
        "gemini-embedding-2": Decimal("0.20"),
    },
    # https://docs.mistral.ai/inference/pricing ("Embedding models"), identifiers from
    # https://docs.mistral.ai/models/mistral-embed-23-12 and …/codestral-embed-25-05, read on
    # 2026-11-08.
    "mistral": {
        "mistral-embed": Decimal("0.1"),
        "mistral-embed-2312": Decimal("0.1"),
        "codestral-embed": Decimal("0.15"),
        "codestral-embed-2505": Decimal("0.15"),
    },
    # https://docs.voyageai.com/docs/pricing (text embeddings, beyond the free tokens; "Older
    # models" included; multimodal models, outside `/v1/embeddings`, left out), read on
    # 2026-11-08.
    "voyage": {
        "voyage-4-large": Decimal("0.12"),
        "voyage-4": Decimal("0.06"),
        "voyage-4-lite": Decimal("0.02"),
        "voyage-code-4": Decimal("0.12"),
        "voyage-3-large": Decimal("0.18"),
        "voyage-code-3": Decimal("0.18"),
        "voyage-3.5": Decimal("0.06"),
        "voyage-3.5-lite": Decimal("0.02"),
        "voyage-context-4": Decimal("0.12"),  # contextualized embeddings endpoint
        "voyage-finance-2": Decimal("0.12"),
        "voyage-law-2": Decimal("0.12"),
        "voyage-code-2": Decimal("0.12"),
        "voyage-context-3": Decimal("0.18"),
        "voyage-multilingual-2": Decimal("0.12"),
        "voyage-large-2-instruct": Decimal("0.12"),
        "voyage-large-2": Decimal("0.12"),
        "voyage-3": Decimal("0.06"),
        "voyage-3-lite": Decimal("0.02"),
        "voyage-2": Decimal("0.1"),
        "voyage-02": Decimal("0.1"),
        "voyage-01": Decimal("0.1"),
        "voyage-lite-01": Decimal("0.1"),
        "voyage-lite-01-instruct": Decimal("0.1"),
        "voyage-lite-02-instruct": Decimal("0.1"),
    },
}
# Embedding providers billed per use (Ollama and compatible APIs: unknown).
PAID_EMBEDDINGS = tuple(EMBEDDING_PRICES)


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    public: bool  # default public price (estimate), not the one entered
    cache_read: Decimal = CACHE_READ
    cache_write: Decimal = CACHE_WRITE
    long_prompt: int = 0  # requests whose prompt exceeds this many tokens: the `long` price
    long: "Price | None" = None


# Dated snapshot: OpenAI (-2025-08-07) or Anthropic (-20251001, @20251001 on Google Cloud).
_SNAPSHOT = re.compile(r"(?:-\d{4}-\d{2}-\d{2}|[-@]\d{8})$")


def _names(model: str) -> Iterator[str]:
    """The model's name, then the other forms under which the tables may know it."""
    name = model.strip().lower().rsplit("/", 1)[-1]  # router or namespace: openai/…, models/…
    yield name
    bare = _SNAPSHOT.sub("", name)
    if bare != name:
        yield bare
    if bare.startswith("claude-") and "." in bare:
        yield bare.replace(".", "-")  # OpenRouter: claude-sonnet-5.5


def _in_force(tariffs: tuple[Tariff, ...], on: date) -> Tariff | None:
    current = None
    for candidate in tariffs:
        if candidate.since is None or candidate.since <= on:
            current = candidate
    return current if current is not None and current.rate is not None else None


def tariff(provider: str, model: str, on: date | None = None) -> Tariff | None:
    """Public tariff in force (today, UTC, by default) for this model with this provider, or
    None: unknown."""
    day = on or datetime.now(UTC).date()
    tables = [PUBLIC_PRICES[t] for t in PROVIDER_TABLES.get(provider, ())]
    for table in tables:
        for name in _names(model):
            if name in table:
                return _in_force(table[name], day)
    return None


def public_price(provider: str, model: str, on: date | None = None) -> Rate | None:
    """Public price in force of this model (short prompts), or None."""
    found = tariff(provider, model, on)
    return found.rate if found else None


def _applied(rate: Rate) -> Price:
    return Price(rate.input, rate.output, True, rate.cache_read, rate.write)


def price_for(
    provider: str,
    model: str,
    price_input: Decimal | None,
    price_output: Decimal | None,
    on: date | None = None,
) -> Price | None:
    """Applicable price, or None: subscription, or unknown model with no price entered. The
    price entered by the organization comes first, with the cache shares of the model's public
    price (and no long-prompt price)."""
    if provider in SUBSCRIPTION_PROVIDERS:
        return None
    known = tariff(provider, model, on)
    rate = known.rate if known else None
    if price_input is not None and price_output is not None:
        return Price(
            price_input,
            price_output,
            public=False,
            cache_read=rate.cache_read if rate else CACHE_READ,
            cache_write=rate.write if rate else CACHE_WRITE,
        )
    if known is None or rate is None:
        return None
    if known.long is None:
        return _applied(rate)
    return Price(
        rate.input,
        rate.output,
        True,
        rate.cache_read,
        rate.write,
        known.long_prompt,
        _applied(known.long),
    )


def prudent_price(on: date | None = None) -> Price:
    """Highest base price of the public tables (and the cache at the ordinary price): to count a
    run whose model has no known price while a budget is set. The tables only hold models open
    to every customer that Synaptane can use: not OpenAI's "pro" models (Responses API only), nor
    models with restricted access (`gpt-6-cyber`, $20 / $100, on approval). Long-prompt prices are
    left out. The most expensive models are then at the same level ($10 / $50 per million tokens
    on 2026-11-08: Claude Fable 5.1, GPT-6 Astra). Not a ceiling: a restricted model priced
    higher needs its price entered."""
    day = on or datetime.now(UTC).date()
    rates = [
        found.rate
        for table in PUBLIC_PRICES.values()
        for tariffs in table.values()
        if (found := _in_force(tariffs, day)) is not None and found.rate is not None
    ]
    highest = max(rates, key=lambda r: (r.output, r.input))
    return Price(highest.input, highest.output, public=True)


def embedding_cost(provider: str, model: str, tokens: int) -> Decimal | None:
    """Estimated cost of `tokens` embedded tokens, or None: local or unknown provider (Ollama,
    compatible API). A paid model missing from the table is counted at the highest price of its
    provider, never zero."""
    if provider not in PAID_EMBEDDINGS or tokens <= 0:
        return None
    table = EMBEDDING_PRICES[provider]
    rate = next((table[n] for n in _names(model) if n in table), max(table.values()))
    return (Decimal(tokens) * rate / MILLION).quantize(Decimal("0.000001"))


def _amount(tokens: tuple[int, int, int, int], price: Price) -> Decimal:
    """(input billed at full price, read from the cache, written to the cache, output) at this
    price, per million. Cache writes are already in the input (full price): only the surcharge
    is added."""
    input_tokens, cached, written, output = tokens
    return (
        Decimal(input_tokens) * price.input
        + Decimal(written) * price.input * (price.cache_write - 1)
        + Decimal(cached) * price.input * price.cache_read
        + Decimal(output) * price.output
    )


def cost(usage: Usage, price: Price | None) -> Decimal | None:
    if price is None:
        return None
    total = _amount(
        (usage.input_tokens, usage.cached_tokens, usage.cache_write_tokens, usage.output_tokens),
        price,
    )
    if price.long is not None:
        # Requests with a long prompt (input and cache): the difference, turn by turn.
        for turn in usage.turn_tokens:
            if turn[0] + turn[1] > price.long_prompt:
                total += _amount(turn, price.long) - _amount(turn, price)
    return (total / MILLION).quantize(Decimal("0.0001"))
