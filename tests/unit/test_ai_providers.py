import json
from typing import Any

import httpx2
import pytest

from synaptane.ai.base import AIConfig, AIError, ToolResult, ToolSpec
from synaptane.ai.openai_compat import OpenAICompatConversation
from synaptane.ai.providers import check_connection

TOOL = ToolSpec("report", "x", {"type": "object", "properties": {}})


def fake_post(responses: list[httpx2.Response], seen: list[dict[str, Any]]) -> Any:
    def post(url: str, **kwargs: Any) -> httpx2.Response:
        seen.append({"url": url, **kwargs})
        return responses.pop(0)

    return post


def chat(message: dict[str, Any], reason: str = "tool_calls") -> httpx2.Response:
    body = {
        "choices": [{"message": message, "finish_reason": reason}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }
    return httpx2.Response(200, json=body)


def test_openai_compatible_tool_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "report", "arguments": json.dumps({"ok": True})},
    }
    responses = [
        chat({"role": "assistant", "content": None, "tool_calls": [call]}),
        chat({"role": "assistant", "content": "fini"}, "stop"),
    ]
    monkeypatch.setattr(httpx2, "post", fake_post(responses, seen))
    config = AIConfig(provider="mistral", model="mistral-large-latest", api_key="mk")
    conversation = OpenAICompatConversation(config, "système", "bonjour", [TOOL])
    turn = conversation.step()
    assert turn.stop == "tool_use" and turn.tool_calls[0].input == {"ok": True}
    assert (turn.input_tokens, turn.output_tokens) == (12, 3)
    assert seen[0]["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert seen[0]["headers"] == {"Authorization": "Bearer mk"}
    conversation.reply([ToolResult("c1", "ok")])
    assert conversation.step().stop == "end"
    assert seen[1]["json"]["messages"][-2] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "ok",
    }


@pytest.mark.parametrize(
    ("status", "transient"), [(401, False), (429, True), (503, True), (400, False)]
)
def test_openai_compatible_errors(
    monkeypatch: pytest.MonkeyPatch, status: int, transient: bool
) -> None:
    monkeypatch.setattr(httpx2, "post", fake_post([httpx2.Response(status, text="nope")], []))
    conversation = OpenAICompatConversation(AIConfig("openai", "gpt", "k"), "s", "u", [TOOL])
    with pytest.raises(AIError) as error:
        conversation.step()
    assert error.value.transient is transient


def check_connection_requires_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "report", "arguments": "{}"}}
    monkeypatch.setattr(
        httpx2, "post", fake_post([chat({"role": "assistant", "tool_calls": [call]})], [])
    )
    assert "Connexion réussie" in check_connection(AIConfig("ollama", "llama", ""))
    monkeypatch.setattr(
        httpx2, "post", fake_post([chat({"role": "assistant", "content": "Bonjour !"}, "stop")], [])
    )
    with pytest.raises(AIError):
        check_connection(AIConfig("ollama", "llama", ""))


def test_claude_options_follow_the_model() -> None:
    from synaptane.ai.anthropic_provider import request_options

    sonnet = request_options(AIConfig("anthropic", "claude-sonnet-5-5", "k"))
    assert sonnet["fallbacks"] == "default" and "output_config" in sonnet
    haiku = request_options(AIConfig("anthropic", "claude-haiku-4-5-20251001", "k"))
    assert "output_config" not in haiku and "fallbacks" not in haiku
    gateway = request_options(AIConfig("anthropic", "claude-opus-5-5", "k", "https://gw/v1"))
    assert "fallbacks" not in gateway  # a gateway may reject the beta
