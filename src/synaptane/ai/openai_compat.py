"""Providers with an OpenAI-compatible API (Chat Completions + function calling): OpenAI,
Mistral, Ollama, and any compatible endpoint (vLLM, LM Studio, gateways…)."""

import json
from typing import Any

import httpx2

from synaptane.ai.base import AIConfig, AIError, ToolCall, ToolResult, ToolSpec, Turn

TIMEOUT = httpx2.Timeout(300.0, connect=15.0)


class OpenAICompatConversation:
    def __init__(
        self, config: AIConfig, system: str, first_message: str, tools: list[ToolSpec]
    ) -> None:
        if not config.endpoint:
            raise AIError("Missing provider address (base_url).")
        self.config = config
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": first_message},
        ]

    def step(self) -> Turn:
        headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
        try:
            response = httpx2.post(
                f"{self.config.endpoint}/chat/completions",
                json={"model": self.config.model, "messages": self.messages, "tools": self.tools},
                headers=headers,
                timeout=TIMEOUT,
            )
        except httpx2.TimeoutException:
            raise AIError(
                "{address} does not answer in time: is the service overloaded?",
                transient=True,
                address=self.config.endpoint,
            ) from None
        except httpx2.ConnectError:
            raise AIError(
                "Cannot reach {address} (connection refused or unknown address): check the "
                "address and that the service is running.",
                transient=True,
                address=self.config.endpoint,
            ) from None
        except httpx2.HTTPError as error:
            raise AIError(
                "Provider unreachable: {detail}", transient=True, detail=str(error)
            ) from None
        if response.status_code in (401, 403):
            raise AIError("Key refused by the provider.")
        if response.status_code == 429 or response.status_code >= 500:
            raise AIError(
                "Provider unavailable ({status}).", transient=True, status=response.status_code
            )
        if response.status_code >= 400:
            raise AIError(
                "Request refused ({status}): {detail}",
                status=response.status_code,
                detail=response.text[:300],
            )
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise AIError(
                "Unexpected response from the provider (not in the OpenAI format): check the "
                "API address and the model.",
                transient=True,
            ) from None
        self.messages.append(
            {k: v for k, v in message.items() if k in ("role", "content", "tool_calls")}
        )
        calls = []
        for call in message.get("tool_calls") or []:
            try:
                arguments = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                arguments = {"__invalid_json__": call["function"].get("arguments")}
            calls.append(ToolCall(call["id"], call["function"]["name"], arguments))
        usage = data.get("usage") or {}
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        reason = choice.get("finish_reason")
        # Truncation first: a truncated tool call must not be executed.
        stop = (
            "max_tokens"
            if reason == "length"
            else "refusal"
            if reason == "content_filter"
            else "tool_use"
            if calls
            else "end"
        )
        return Turn(
            text=message.get("content") or "",
            tool_calls=calls,
            stop=stop,  # type: ignore[arg-type]
            input_tokens=int(usage.get("prompt_tokens", 0)) - cached,
            output_tokens=int(usage.get("completion_tokens", 0)),
            cached_tokens=cached,
        )

    def reply(self, results: list[ToolResult]) -> None:
        for r in results:
            content = f"ERROR: {r.content}" if r.is_error else r.content
            self.messages.append({"role": "tool", "tool_call_id": r.call_id, "content": content})

    def nudge(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
