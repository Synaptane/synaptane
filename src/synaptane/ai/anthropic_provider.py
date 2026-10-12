"""Claude through the official `anthropic` SDK (manual tool loop, shared by all providers).

- Adaptive thinking by default on recent models; depth set by `effort`.
- Server-side fallback (`fallbacks: "default"`): if a request is refused by a safety filter,
  the API replays it on a fallback model instead of failing.
- The history is kept as is (response blocks sent back in full).
- Strict tools (`strict: true`) when the model accepts them: the schemas are closed.
"""

from typing import Any, cast

import anthropic
from anthropic.types.beta import BetaMessageParam, BetaOutputConfigParam, BetaToolParam

from synaptane.ai.base import AIConfig, AIError, ToolCall, ToolResult, ToolSpec, Turn

MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Server-side fallback: only these models, and only on the Claude API itself.
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
NO_EFFORT_MODELS = ("claude-haiku",)  # `effort` refused


def request_options(config: AIConfig) -> dict[str, Any]:
    """Claude-specific parameters, depending on the model and the endpoint (gateway or not)."""
    model = config.model.strip().lower()
    options: dict[str, Any] = {}
    if not model.startswith(NO_EFFORT_MODELS):
        options["output_config"] = cast(BetaOutputConfigParam, {"effort": config.effort})
    if not config.base_url and any(
        model == m or model.startswith(m + "-") for m in FALLBACK_MODELS
    ):
        options["betas"] = [FALLBACK_BETA]
        options["fallbacks"] = "default"
    return options


# Strict tools (arguments always match the schema): models that accept them.
STRICT_MODELS = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)


def _closed(schema: object) -> bool:
    """Every object of the schema is closed (`additionalProperties: false`), a condition of
    strict mode; otherwise the API would refuse the request."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
            return False
        return all(_closed(v) for v in schema.values())
    if isinstance(schema, list):
        return all(_closed(v) for v in schema)
    return True


def tool_param(config: AIConfig, tool: ToolSpec) -> BetaToolParam:
    """Tool definition for the API. `strict: true` guarantees valid arguments (no invented
    parameter, no wrong type): only on the Claude API itself (a gateway may not know it), for
    a model that accepts it and a closed schema."""
    param: BetaToolParam = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }
    model = config.model.strip().lower()
    if not config.base_url and model.startswith(STRICT_MODELS) and _closed(tool.input_schema):
        param["strict"] = True
    return param


class AnthropicConversation:
    def __init__(
        self, config: AIConfig, system: str, first_message: str, tools: list[ToolSpec]
    ) -> None:
        self.config = config
        self.client = anthropic.Anthropic(
            api_key=config.api_key, base_url=config.base_url or None, max_retries=3
        )
        self.system = system
        self.tools = [tool_param(config, t) for t in tools]
        self.messages: list[BetaMessageParam] = [{"role": "user", "content": first_message}]

    def step(self) -> Turn:
        try:
            response = self.client.beta.messages.create(
                model=self.config.model,
                max_tokens=MAX_TOKENS,
                system=self.system,
                tools=self.tools,
                messages=self.messages,
                **request_options(self.config),
                # Append-only history: each turn reads the previous one back from the cache.
                cache_control={"type": "ephemeral"},
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
            raise AIError("Anthropic key refused: {detail}", detail=error.message) from None
        except anthropic.NotFoundError as error:
            raise AIError(
                "Model not found ({model}): {detail}", model=self.config.model, detail=error.message
            ) from None
        except anthropic.BadRequestError as error:
            raise AIError("Request refused by Anthropic: {detail}", detail=error.message) from None
        except anthropic.RateLimitError:
            raise AIError("Anthropic rate limit reached.", transient=True) from None
        except anthropic.APIStatusError as error:
            transient = error.status_code >= 500
            raise AIError(
                "Anthropic error {status}", transient=transient, status=error.status_code
            ) from None
        except anthropic.APIConnectionError:
            raise AIError("Anthropic unreachable.", transient=True) from None

        # Append-only history: the full content is sent back on the next turn.
        self.messages.append({"role": "assistant", "content": response.content})
        calls = [
            ToolCall(block.id, block.name, dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        ]
        text = "".join(block.text for block in response.content if block.type == "text")
        stop = {
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
            "refusal": "refusal",
        }.get(str(response.stop_reason), "end")
        if calls and stop == "end":
            stop = "tool_use"  # a truncation (max_tokens) or a refusal stays visible
        # Cache writes: counted in the input, and separately for their higher price
        # (1.25 × the input with the 5-minute duration of automatic caching).
        written = response.usage.cache_creation_input_tokens or 0
        return Turn(
            text=text,
            tool_calls=calls,
            stop=stop,  # type: ignore[arg-type]
            input_tokens=response.usage.input_tokens + written,
            output_tokens=response.usage.output_tokens,
            cached_tokens=response.usage.cache_read_input_tokens or 0,
            cache_write_tokens=written,
        )

    def reply(self, results: list[ToolResult]) -> None:
        # All results of a turn in a single user message.
        content: list[Any] = [
            {
                "type": "tool_result",
                "tool_use_id": r.call_id,
                "content": r.content,
                "is_error": r.is_error,
            }
            for r in results
        ]
        self.messages.append({"role": "user", "content": content})

    def nudge(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
