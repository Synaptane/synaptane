"""Common provider interface: a conversation that moves forward in tool turns."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from synaptane.ai import quota
from synaptane.core.errors import Label

API_PROVIDERS = ("anthropic", "openai", "mistral", "ollama", "openai_compatible")
# Subscriptions: the official CLI, logged in on the server, runs the agent (cli_agents).
SUBSCRIPTION_PROVIDERS = ("claude_code", "codex")
PROVIDERS = API_PROVIDERS + SUBSCRIPTION_PROVIDERS
KEYLESS_PROVIDERS = ("ollama", "openai_compatible", *SUBSCRIPTION_PROVIDERS)
EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-5-5",  # tidy-up: good value for money
    "mistral": "mistral-large-latest",
}
DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "ollama": "http://localhost:11434/v1",
}


class AIError(Exception):
    """Failed call to the provider. `transient`: retrying later makes sense. `usage`: what was
    consumed before the failure (a CLI that ends in error may have worked and used up quota):
    it must be counted anyway. `label`: the message to translate (English template and
    `params`); `str(error)` is the English text."""

    def __init__(
        self,
        template: str,
        *,
        transient: bool = False,
        usage: "Usage | None" = None,
        **params: object,
    ) -> None:
        self.label = Label(template, **params)
        super().__init__(str(self.label))
        self.transient = transient
        self.usage = usage


@dataclass(frozen=True)
class AIConfig:
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    effort: str = "medium"  # Claude only: reasoning depth

    @property
    def endpoint(self) -> str:
        return (self.base_url or DEFAULT_BASE_URLS.get(self.provider, "")).rstrip("/")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall]
    stop: Literal["end", "tool_use", "max_tokens", "refusal"]
    input_tokens: int = 0  # input billed at full price (cache writes included)
    output_tokens: int = 0
    cached_tokens: int = 0  # input read back from the cache
    cache_write_tokens: int = 0  # share of `input_tokens` written to the cache (higher price)


@dataclass
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0  # share of `input_tokens` written to the cache (higher price)
    turns: int = 0
    notes: list[str] = field(default_factory=list)
    quota: dict[str, Any] | None = None  # subscription: used share of the windows (ai.quota)

    def add(self, turn: Turn) -> None:
        self.input_tokens += turn.input_tokens
        self.output_tokens += turn.output_tokens
        self.cached_tokens += turn.cached_tokens
        self.cache_write_tokens += turn.cache_write_tokens
        self.turns += 1

    def merge(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.turns += other.turns
        self.notes.extend(other.notes)
        self.quota = quota.merge(self.quota, other.quota)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def weighted(self) -> int:
        """Weighted tokens for the caps: input read back from the cache counts for a tenth."""
        return self.input_tokens + self.output_tokens + self.cached_tokens // 10


class Conversation(Protocol):
    """Provider-native history: `step` asks for a turn, `reply` answers it."""

    def step(self) -> Turn: ...

    def reply(self, results: list[ToolResult]) -> None: ...

    def nudge(self, text: str) -> None:
        """Prods the agent with a message (e.g. it stopped without calling a tool)."""
        ...
