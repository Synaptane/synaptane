"""Claude (Claude Code) and ChatGPT (Codex) subscriptions: the agent runs in the official CLI.

Synaptane never handles the subscription's credentials: the administrator logs the CLI in
once on the server, through the vendor's flow (`claude` then /login, `codex login`). For a
run, Synaptane:
1. exposes the run's tools through a local, ephemeral MCP server, protected by a single-use
   token;
2. launches `claude -p` or `codex exec` without any built-in tool (no shell, no files, no
   web): the agent can only act through those tools;
3. reads the consumption reported by the CLI.

The CLI keeps its login in its own `HOME` (`SYNAPTANE_AGENT_HOME`): under Docker, a volume
mounted only in the worker, which the Internet-facing web container does not see. What
guarantees the absence of built-in tools (command-line options, Codex features) is checked on
each image build by `cli_contract`.
"""

import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio.to_thread
import mcp_types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from synaptane.ai import quota
from synaptane.ai.base import AIConfig, AIError, ToolSpec, Usage
from synaptane.core.errors import Label

log = logging.getLogger("synaptane.ai.cli")

SERVER_NAME = "synaptane_tidy"
RUN_TIMEOUT = 45 * 60  # seconds
# Codex features turned off on each run: anything that executes, reads files, opens a browser
# or adds tools. An unknown name makes the CLI fail, and `cli_contract` checks on each image
# build that every name still exists and that every other enabled feature is known
# (`cli_contract.CODEX_ALLOWED`): a new feature must be classified.
CODEX_DISABLED = (
    "shell_tool",
    "shell_snapshot",  # launches the login shell to read its environment
    "unified_exec",
    "unified_exec_tty",
    "code_mode_host",  # execution of code written by the model
    "hooks",  # commands launched on events
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "in_app_browser",
    "in_app_local_automation",
    "view_image",  # reads an image from disk, at a path chosen by the model
    "image_generation",
    "multi_agent",  # sub-agents: their tools are not those of this run
    "goals",  # session goal tools, useless here
    "apps",
    "plugins",
    "plugin_sharing",
    "remote_plugin",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_suggest",
    "workspace_dependencies",
    "worktrees",
    "daemon_auto_start",
)
AGENT_HOME = "SYNAPTANE_AGENT_HOME"

ToolHandler = Callable[[str, dict[str, Any]], str]


class ToolRejected(Exception):
    """Tool refusal, sent back to the agent as an error (it can correct and retry)."""


# --- Local tool server -----------------------------------------------------------------------


class _BearerGuard:
    """Refuses any request without the run's token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if not secrets.compare_digest(headers.get(b"authorization", b""), self.expected):
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def tool_server(specs: list[ToolSpec], handler: ToolHandler) -> Iterator[tuple[str, str]]:
    """HTTP MCP server on 127.0.0.1, for the duration of the block. Returns (url, token)."""
    lock = threading.Lock()  # the tools modify shared state: one call at a time

    def call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        with lock:
            try:
                text, error = handler(name, arguments), False
            except ToolRejected as failure:
                text, error = str(failure), True
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], is_error=error
        )

    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=s.name, description=s.description, input_schema=s.input_schema)
                for s in specs
            ]
        )

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await anyio.to_thread.run_sync(call, params.name, dict(params.arguments or {}))

    server: Server[Any] = Server(
        SERVER_NAME, on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    port, token = _free_port(), secrets.token_urlsafe(32)
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[],
        ),
    )
    http = uvicorn.Server(
        uvicorn.Config(_BearerGuard(app, token), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=http.run, daemon=True, name="synaptane-tidy-tools")
    thread.start()
    deadline = time.monotonic() + 10
    while not http.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise AIError("The local tool server did not start.", transient=True)
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp", token
    finally:
        http.should_exit = True
        thread.join(timeout=10)


# --- Running the CLIs ------------------------------------------------------------------------


@dataclass
class CLIRun:
    usage: Usage
    text: str


LOGIN = {"claude_code": "claude auth login", "codex": "codex login --device-auth"}


def in_docker() -> bool:
    """True in the compose.yaml container (the CLIs log in within `worker`)."""
    return Path("/.dockerenv").exists()


def agent_home() -> Path | None:
    """`HOME` reserved for the subscription CLIs (`SYNAPTANE_AGENT_HOME`), if set.

    Under Docker, `/agents`, a volume of the worker only: the login is neither in the data
    volume nor visible from the web container. Without this variable, the CLI keeps the `HOME`
    of the system account running the worker."""
    value = os.environ.get(AGENT_HOME, "").strip()
    return Path(value) if value else None


def legacy_logins(data_dir: Path) -> list[Path]:
    """Logins left in the CLIs' former `HOME` (`/data/home`, before the worker volume). Only
    tests that the folders exist: Synaptane reads nothing there, and does not move them (only
    the official CLI handles the credentials)."""
    old = data_dir / "home"
    home = agent_home()
    if home is None or home == old:
        return []
    return [old / name for name in (".claude", ".codex") if (old / name).is_dir()]


def legacy_cleanup(data_dir: Path, docker: bool | None = None) -> str:
    """Command that deletes the former login, to be run by the administrator once the CLI is
    logged in again in the worker (`.claude.json`: Claude Code settings)."""
    old = data_dir / "home"
    paths = " ".join(str(old / name) for name in (".claude", ".claude.json", ".codex"))
    in_compose = in_docker() if docker is None else docker
    return f"docker compose exec app rm -rf {paths}" if in_compose else f"rm -rf {paths}"


def login_command(provider: str, docker: bool | None = None) -> str:
    """Command that logs in a subscription's CLI, depending on the installation.

    Under Docker, the `worker` container has the CLI volume as its `HOME` (compose.yaml): the
    plain command stores the login there. Elsewhere, the reserved `HOME` is given if set."""
    base = LOGIN[provider]
    if in_docker() if docker is None else docker:
        return f"docker compose exec worker {base}"
    home = agent_home()
    return f"HOME={home} {base}" if home else base


def _binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise AIError("The program “{name}” is not installed on the server.", name=name)
    return path


def _claude_command(config: AIConfig, system_file: Path, mcp_file: Path) -> list[str]:
    """The prompt comes through standard input, the instructions through a file: neither on
    the command line (visible in `ps`, limited to 128 KiB per argument)."""
    command = [
        _binary("claude"),
        "-p",
        "--system-prompt-file",
        str(system_file),
        "--tools",
        "",  # no built-in tool: no shell, no files, no web
        "--mcp-config",
        str(mcp_file),
        "--strict-mcp-config",
        "--allowedTools",
        f"mcp__{SERVER_NAME}",
        "--permission-mode",
        "dontAsk",
        # Streamed: the final result, plus the subscription's quota state (rate_limit_event).
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if config.model:
        command += ["--model", config.model]
    return command


def _codex_command(config: AIConfig, url: str, token_env: str, workdir: Path) -> list[str]:
    """Instructions and prompt come through standard input (`-`)."""
    command = [
        _binary("codex"),
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "-C",
        str(workdir),
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        f'mcp_servers.{SERVER_NAME}.url="{url}"',
        "-c",
        f'mcp_servers.{SERVER_NAME}.bearer_token_env_var="{token_env}"',
        "-c",
        f'mcp_servers.{SERVER_NAME}.default_tools_approval_mode="approve"',
    ]
    for feature in CODEX_DISABLED:
        command += ["--disable", feature]
    if config.model:
        command += ["-m", config.model]
    command.append("-")
    return command


def _claude_usage(stdout: str) -> CLIRun:
    """Claude Code streamed output: the final `result` event and the quota readings."""
    usage = Usage()
    data: dict[str, Any] | None = None
    readings: list[tuple[quota.Windows, str]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            data = event
        elif event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            windows = quota.claude_windows(info) if isinstance(info, dict) else {}
            if windows:
                readings.append((windows, str(info.get("status") or "")))
    if readings:
        usage.quota = quota.span(readings[0][0], readings[-1][0], readings[-1][1])
    if data is None:
        return CLIRun(usage, stdout[-2000:])
    tokens = data.get("usage") or {}
    usage.input_tokens = int(tokens.get("input_tokens", 0)) + int(
        tokens.get("cache_creation_input_tokens", 0)
    )
    usage.cached_tokens = int(tokens.get("cache_read_input_tokens", 0))
    usage.output_tokens = int(tokens.get("output_tokens", 0))
    usage.turns = int(data.get("num_turns", 0))
    if data.get("is_error"):
        message = str(data.get("result") or data.get("subtype"))[:500]
        raise AIError("Claude Code: {detail}", usage=usage, detail=message)  # tokens, quota counted
    return CLIRun(usage, str(data.get("result") or ""))


def _codex_usage(stdout: str) -> CLIRun:
    usage = Usage()
    text = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type", "")
        if kind == "turn.completed":
            tokens = event.get("usage") or {}
            cached = int(tokens.get("cached_input_tokens", 0))
            usage.input_tokens += int(tokens.get("input_tokens", 0)) - cached
            usage.cached_tokens += cached
            usage.output_tokens += int(tokens.get("output_tokens", 0))
            usage.turns += 1
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text = str(item.get("text") or text)
        elif kind in ("error", "turn.failed"):
            message = event.get("message") or (event.get("error") or {}).get("message")
            raise AIError("Codex: {detail}", detail=str(message)[:500])
    return CLIRun(usage, text)


def run_agent(
    config: AIConfig,
    system: str,
    prompt: str,
    specs: list[ToolSpec],
    handler: ToolHandler,
    timeout: int = RUN_TIMEOUT,
) -> CLIRun:
    """Runs a complete pass in the subscription's CLI."""
    with (
        tool_server(specs, handler) as (url, token),
        tempfile.TemporaryDirectory(prefix="synaptane-agent-") as scratch,
    ):
        workdir = Path(scratch)
        env = _environment()
        if config.provider == "claude_code":
            mcp_file = workdir / "mcp.json"
            mcp_file.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            SERVER_NAME: {
                                "type": "http",
                                "url": url,
                                "headers": {"Authorization": f"Bearer {token}"},
                            }
                        }
                    }
                )
            )
            mcp_file.chmod(0o600)
            system_file = workdir / "system.md"
            system_file.write_text(system)
            system_file.chmod(0o600)
            command, parse = _claude_command(config, system_file, mcp_file), _claude_usage
            given = prompt
        elif config.provider == "codex":
            env["SYNAPTANE_TIDY_TOKEN"] = token
            command = _codex_command(config, url, "SYNAPTANE_TIDY_TOKEN", workdir)
            parse = _codex_usage
            given = f"{system}\n\n---\n\n{prompt}"
        else:
            raise AIError("Unknown provider: {provider}", provider=config.provider)
        try:
            result = subprocess.run(
                command,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=given,
            )
        except subprocess.TimeoutExpired:
            raise AIError("The run exceeded the maximum time.", transient=True) from None
        if result.returncode != 0 and not result.stdout.strip():
            message = (result.stderr or "").strip()[-600:]
            transient = any(
                w in message.lower() for w in ("rate limit", "429", "overloaded", "timeout")
            )
            raise AIError(
                "{provider}: {detail}",
                transient=transient,
                provider=config.provider,
                detail=message or "failure without a message",
            )
        return parse(result.stdout)


def _environment() -> dict[str, str]:
    """Minimal environment: the CLI finds its login in its reserved `HOME`
    (`SYNAPTANE_AGENT_HOME`), failing that in the worker's."""
    keep = (
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "USER",
        "TMPDIR",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "DISABLE_AUTOUPDATER",
    )
    env = {k: v for k, v in os.environ.items() if k in keep}
    # An API key in the environment would bill the API instead of the subscription.
    for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(variable, None)
    home = agent_home()
    if home is not None:
        env["HOME"] = str(home)
    return env


PROBE_TIMEOUT = 120


def _claude_probe() -> Label:
    """Real mini-call (Haiku, no tool): "logged in" is not enough, a session may have expired
    without `auth status` noticing. Also gives the quota state."""
    command = [
        _binary("claude"),
        "-p",
        "Answer only: OK",
        "--model",
        "haiku",
        "--tools",
        "",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            env=_environment(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise AIError("Claude Code does not answer (timed out).", transient=True) from None
    try:
        run = _claude_usage(result.stdout)
    except AIError as error:
        if "authenticat" in str(error).lower() or "expired" in str(error).lower():
            raise AIError(
                "The Claude Code login has expired: run “{command}” again (steps on this page).",
                command=login_command("claude_code"),
            ) from None
        raise
    state = quota.summary(run.usage.quota)
    if state:
        return Label("Claude Code answers. Quota used: {quota}.", quota=state)
    return Label("Claude Code answers.")


def login_status(provider: str) -> Label:
    """Login state: real mini-call for Claude Code, state reported by the CLI for Codex
    (without ever reading their credentials)."""
    status = _require_logged_in(provider)
    return _claude_probe() if provider == "claude_code" else status


def _require_logged_in(provider: str) -> Label:
    """Login state reported by the CLI (without ever reading its credentials)."""
    command = {
        "claude_code": [_binary("claude"), "auth", "status"],
        "codex": [_binary("codex"), "login", "status"],
    }[provider]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, env=_environment())
    text = (result.stdout + result.stderr).strip()
    if provider == "claude_code":
        try:
            data = json.loads(text)
            if not data.get("loggedIn"):
                raise AIError(
                    "Claude Code is not logged in on the server: run “{command}” (steps on this "
                    "page).",
                    command=login_command("claude_code"),
                )
            return Label("Claude Code logged in ({method}).", method=data.get("authMethod", "?"))
        except ValueError:
            pass
    if result.returncode != 0 or "not logged in" in text.lower():
        raise AIError(
            "{provider} is not logged in on the server: run “{command}” (steps on this page). "
            "Detail: {detail}",
            provider=provider,
            command=login_command(provider),
            detail=text[:200],
        )
    return (
        Label(text.splitlines()[0]) if text else Label("{provider} logged in.", provider=provider)
    )
