"""Provider selection and connection test."""

from collections.abc import Callable

from synaptane.ai.base import SUBSCRIPTION_PROVIDERS, AIConfig, AIError, Conversation, ToolSpec
from synaptane.core.errors import Label

ConversationFactory = Callable[[AIConfig, str, str, list[ToolSpec]], Conversation]


def open_conversation(
    config: AIConfig, system: str, first_message: str, tools: list[ToolSpec]
) -> Conversation:
    if config.provider == "anthropic":
        from synaptane.ai.anthropic_provider import AnthropicConversation

        return AnthropicConversation(config, system, first_message, tools)
    from synaptane.ai.openai_compat import OpenAICompatConversation

    return OpenAICompatConversation(config, system, first_message, tools)


PING = ToolSpec(
    name="report",
    description="Reports that the connection works.",
    input_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
)


def check_connection(config: AIConfig, factory: ConversationFactory = open_conversation) -> Label:
    """One real turn with a tool: checks the key, the model and tool calling. The message
    says what was checked (to translate)."""
    if config.provider in SUBSCRIPTION_PROVIDERS:
        return _check_subscription(config)
    conversation = factory(
        config,
        "You are checking a connection. Call the report tool with ok=true.",
        "Connection test.",
        [PING],
    )
    turn = conversation.step()
    if not any(call.name == "report" for call in turn.tool_calls):
        raise AIError("The model answers but does not call the tools: it cannot tidy.")
    return Label(
        "Connection successful ({model}, {tokens} tokens).",
        model=config.model,
        tokens=turn.input_tokens + turn.output_tokens,
    )


# A test blocks the worker's fast lane: it must not last as long as a run.
SUBSCRIPTION_TEST_TIMEOUT = 3 * 60  # seconds


def _check_subscription(config: AIConfig) -> Label:
    """CLI login, then a real tool call through the local MCP server.

    The CLI and its login only exist in the worker: this test runs there, as a job
    (`ai_settings.AI_TEST`), never in the web process."""
    from synaptane.ai import cli_agents

    status = cli_agents.login_status(config.provider)
    called: list[bool] = []

    def handler(name: str, arguments: dict[str, object]) -> str:
        called.append(name == PING.name)
        return "ok"

    run = cli_agents.run_agent(
        config,
        "You are checking a connection. Call the report tool with ok=true, then stop.",
        "Connection test.",
        [PING],
        handler,
        timeout=SUBSCRIPTION_TEST_TIMEOUT,
    )
    if not any(called):
        raise AIError("The CLI answers but does not call the tools of Synaptane.")
    return Label(
        "{status} Tool call successful ({tokens} tokens).", status=status, tokens=run.usage.total
    )
