import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from synaptane.ai import cli_agents
from synaptane.ai.base import AIConfig, AIError, ToolSpec

CLAUDE = AIConfig(provider="claude_code", model="")
CODEX = AIConfig(provider="codex", model="gpt-5.6")
PING = ToolSpec("report", "Rapport.", {"type": "object", "properties": {"ok": {"type": "boolean"}}})


def test_claude_command_has_no_builtin_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    command = cli_agents._claude_command(CLAUDE, tmp_path / "system.md", tmp_path / "mcp.json")
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--system-prompt-file") + 1] == str(tmp_path / "system.md")
    assert "--strict-mcp-config" in command and "--model" not in command
    assert command[command.index("--allowedTools") + 1] == "mcp__synaptane_tidy"


def test_codex_command_disables_shell_and_web(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    command = cli_agents._codex_command(CODEX, "http://127.0.0.1:1/mcp", "TOKEN_ENV", tmp_path)
    assert command[-1] == "-"  # instructions and prompt through standard input
    disabled = {command[i + 1] for i, part in enumerate(command) if part == "--disable"}
    assert {"shell_tool", "unified_exec", "browser_use", "computer_use"} <= disabled
    assert 'web_search="disabled"' in command and "--ignore-user-config" in command
    assert command[command.index("-m") + 1] == "gpt-5.6"
    assert "TOKEN_ENV" in " ".join(command) and "Bearer" not in " ".join(command)


def test_environment_never_carries_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("SYNAPTANE_DATABASE_URL", "postgresql://secret")
    env = cli_agents._environment()
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SYNAPTANE_DATABASE_URL"} & env.keys()


@pytest.mark.parametrize("config", [CLAUDE, CODEX])
def test_claude_runs_without_tool_search(monkeypatch: pytest.MonkeyPatch, config: AIConfig) -> None:
    """Since 2.1.310, Claude Code offers the model a placeholder tool while it is active."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    seen: dict[str, str] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 1, "", "stop")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(AIError):
        cli_agents.run_agent(config, "System.", "Prompt.", [PING], lambda name, arguments: "ok")
    assert seen.get("ENABLE_TOOL_SEARCH") == ("false" if config is CLAUDE else None)


def test_usage_parsing() -> None:
    claude = json.dumps(
        {
            "type": "result",
            "result": "Fini",
            "num_turns": 7,
            "is_error": False,
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
        }
    )
    run = cli_agents._claude_usage("log\n" + claude)
    usage = run.usage
    assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens, usage.turns) == (
        10,
        90,
        5,
        7,
    )
    assert run.text == "Fini"
    with pytest.raises(AIError):
        cli_agents._claude_usage(
            json.dumps({"type": "result", "is_error": True, "result": "Quota atteint"})
        )
    events = [
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 50, "cached_input_tokens": 30, "output_tokens": 7},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Terminé"}},
    ]
    codex = cli_agents._codex_usage("\n".join(json.dumps(e) for e in events))
    assert (codex.usage.input_tokens, codex.usage.cached_tokens, codex.usage.output_tokens) == (
        20,
        30,
        7,
    )
    assert codex.text == "Terminé"
    with pytest.raises(AIError):
        cli_agents._codex_usage(json.dumps({"type": "error", "message": "usage limit"}))


def test_tool_server_requires_its_token_and_reports_rejections() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(name: str, arguments: dict[str, object]) -> str:
        calls.append((name, arguments))
        if arguments.get("ok") is False:
            raise cli_agents.ToolRejected("refusé")
        return "ok"

    async def use(url: str, token: str) -> list[tuple[bool, str]]:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(url, http_client=http) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            assert [t.name for t in (await session.list_tools()).tools] == ["report"]
            results = []
            for ok in (True, False):
                result = await session.call_tool("report", {"ok": ok})
                results.append((bool(result.is_error), result.content[0].text))  # type: ignore[union-attr]
            return results

    with cli_agents.tool_server([PING], handler) as (url, token):
        assert httpx2.post(url, json={}).status_code == 401
        assert asyncio.run(use(url, token)) == [(False, "ok"), (True, "refusé")]
    assert calls == [("report", {"ok": True}), ("report", {"ok": False})]


def test_cli_home_is_reserved_to_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI keeps its login in `SYNAPTANE_AGENT_HOME` (a volume of the worker only), not
    in Synaptane's HOME, which the data volume shares with the web container."""
    monkeypatch.setenv("HOME", str(tmp_path / "data" / "home"))
    monkeypatch.delenv("SYNAPTANE_AGENT_HOME", raising=False)
    assert cli_agents._environment()["HOME"] == str(tmp_path / "data" / "home")
    assert cli_agents.legacy_logins(tmp_path / "data") == []  # no reserved HOME: nothing old
    assert cli_agents.login_command("codex", docker=False) == "codex login --device-auth"

    monkeypatch.setenv("SYNAPTANE_AGENT_HOME", "/agents")
    assert cli_agents._environment()["HOME"] == "/agents"
    assert cli_agents.login_command("claude_code", docker=False) == "HOME=/agents claude auth login"
    # Under Docker, the worker container's HOME is already /agents (compose.yaml).
    assert cli_agents.login_command("claude_code", docker=True) == (
        "docker compose exec worker claude auth login"
    )

    old = tmp_path / "data" / "home"
    assert cli_agents.legacy_logins(tmp_path / "data") == []
    (old / ".claude").mkdir(parents=True)
    (old / ".claude.json").write_text("{}")
    assert cli_agents.legacy_logins(tmp_path / "data") == [old / ".claude"]
    (old / ".codex").mkdir()
    assert cli_agents.legacy_logins(tmp_path / "data") == [old / ".claude", old / ".codex"]
    assert cli_agents.legacy_cleanup(Path("/data"), docker=True) == (
        "docker compose exec app rm -rf /data/home/.claude /data/home/.claude.json "
        "/data/home/.codex"
    )


def test_codex_disables_every_tool_that_reaches_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Features found in Codex 0.160: execution, images from disk, sub-agents…"""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    command = cli_agents._codex_command(CODEX, "http://127.0.0.1:1/mcp", "TOKEN_ENV", tmp_path)
    disabled = {command[i + 1] for i, part in enumerate(command) if part == "--disable"}
    risky = {"unified_exec_tty", "shell_snapshot", "code_mode_host", "hooks", "view_image"}
    assert risky | {"multi_agent", "skill_mcp_dependency_install"} <= disabled


def test_login_command_and_message_fit_the_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployment: under Docker, the CLI logs in inside the `worker` container."""
    monkeypatch.delenv("SYNAPTANE_AGENT_HOME", raising=False)
    assert cli_agents.login_command("claude_code", docker=True) == (
        "docker compose exec worker claude auth login"
    )
    assert cli_agents.login_command("codex", docker=False) == "codex login --device-auth"

    class Result:
        stdout, stderr, returncode = json.dumps({"loggedIn": False}), "", 0

    monkeypatch.setattr(cli_agents, "in_docker", lambda: True)
    # The CLI is not necessarily installed where the tests run (CI): it is simulated.
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(AIError, match="docker compose exec worker claude auth login"):
        cli_agents.login_status("claude_code")


def test_claude_stream_gives_the_subscription_quota() -> None:
    """Claude Code streamed output: one quota reading per model call; the first and the last
    are kept (used share of the 5-hour and 7-day windows)."""
    from synaptane.ai import quota

    def reading(five: float, week: float) -> dict[str, object]:
        windows = {
            "five_hour": {"utilization": five, "resetsAt": 1791303600},
            "seven_day": {"utilization": week, "resetsAt": 1791392400},
        }
        info = {"status": "allowed", "rateLimitType": "five_hour", "unifiedWindows": windows}
        return {"type": "rate_limit_event", "rate_limit_info": info}

    events = [
        {"type": "system", "subtype": "init"},
        reading(0.03, 0.29),
        {"type": "assistant", "message": {"content": []}},
        reading(0.051, 0.295),
        {"type": "result", "result": "Fini", "num_turns": 2, "usage": {"output_tokens": 9}},
    ]
    run = cli_agents._claude_usage("\n".join(json.dumps(e) for e in events))
    assert run.text == "Fini" and run.usage.output_tokens == 9
    assert run.usage.quota == {
        "status": "allowed",
        "windows": {
            "five_hour": {"before": 3.0, "after": 5.1, "resets_at": 1791303600},
            "seven_day": {"before": 29.0, "after": 29.5, "resets_at": 1791392400},
        },
    }
    assert quota.summary(run.usage.quota) == "5 h: 3% → 5.1% · 7 days: 29% → 29.5%"
    assert quota.summary(run.usage.quota, "fr") == "5 h : 3 % → 5,1 % · 7 jours : 29 % → 29,5 %"
    assert quota.highest(run.usage.quota) == 29.5
    plain = cli_agents._claude_usage(json.dumps(events[-1]))
    assert plain.usage.quota is None and quota.summary(None) == ""
