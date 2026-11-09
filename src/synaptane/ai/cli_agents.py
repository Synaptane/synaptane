"""Claude (Claude Code) and ChatGPT (Codex) subscriptions: the agent runs in the official CLI.

Synaptane never handles the subscription's credentials: the administrator logs the CLI in
once on the server, through the vendor's flow (`claude auth login`, `codex login`). For a
run, Synaptane:
1. exposes the run's tools through an ephemeral MCP server of the worker, protected by a
   single-use token;
2. launches `claude -p` or `codex exec` without any built-in tool (no shell, no files, no
   web): the agent can only act through those tools;
3. reads the consumption reported by the CLI.

Where the CLI runs (`agents_client`):
- with `SYNAPTANE_AGENTS_URL` (compose.yaml), in a container of its own, `agents`, which
  sees neither the data volume nor the database nor any secret of the instance: the worker
  sends it each run (`agents_runner`) and the CLI reaches the tools at the worker's address
  on their private network;
- otherwise, in the worker itself (installation without Docker, older Compose file): less
  isolated, reported by `synaptane doctor` and the health page.

Either way, the CLI keeps its login in its own `HOME` (`SYNAPTANE_AGENT_HOME`, `/agents` in
the image), and what guarantees the absence of built-in tools (command-line options, Codex
features) is checked on each image build by `cli_contract`, which runs where the CLI runs.
"""

import atexit
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    "browser_annotation_api",  # websites opening annotation tools in the desktop app
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
    "plugin_discovery_prefetch",  # downloads the plugin catalog at startup
)
# Codex features turned on: the descriptions of the skills of its home (`$CODEX_HOME/skills`,
# `~/.agents/skills`) are no longer added to the instructions. `--ignore-user-config` does not
# leave them out, and a compromised run could leave some there for the next ones. Checked by
# `cli_contract` (a skill planted in the HOME of the check must not reach the model).
CODEX_ENABLED = ("omit_skill_descriptions",)
# Claude Code: deferred tool loading ("tool search") turned off. Since 2.1.310 it keeps itself
# active with a placeholder tool offered to the model, and it can add its own search tool: the
# model must see Synaptane's tools only, all of them upfront. Checked by `cli_contract`.
CLAUDE_ENVIRONMENT = {"ENABLE_TOOL_SEARCH": "false"}
# Claude Code: no setting source. Its login (`~/.claude/.credentials.json`, `~/.claude.json`) is
# not a setting and is still read. Checked by `cli_contract` (a hook and instructions planted in
# the HOME of the check must have no effect).
NO_SETTINGS = ("--setting-sources", "")
# Codex: instructions it adds to every run from its home (`--ignore-user-config` leaves out
# `config.toml`, not these), with no option to leave them out. Synaptane never writes them: a
# run is refused while one is there (`codex_instructions`).
CODEX_INSTRUCTIONS = ("AGENTS.md", "AGENTS.override.md")
AGENT_HOME = "SYNAPTANE_AGENT_HOME"
# Address of the agents container (`agents_runner`); unset: the CLI runs in the worker.
AGENTS_URL = "SYNAPTANE_AGENTS_URL"

# Output of a run, read as it comes: only the events Synaptane reads are kept (final result,
# quota, consumption, errors), up to MAX_KEPT bytes, beyond which the run is stopped; a line
# longer than MAX_LINE (a page read by a tool, echoed by the CLI) is skipped without being
# parsed. Memory stays bounded whatever the CLI writes, and so does the agents container's
# answer to the worker.
MAX_KEPT = 4 * 1024 * 1024
MAX_LINE = 8 * 1024 * 1024
TAIL = 4000  # characters of the raw output and of the error output kept for diagnostics
STOP_GRACE = 5.0  # seconds between SIGTERM and SIGKILL of a CLI that is stopped
_KEPT_EVENTS = frozenset({"result", "rate_limit_event", "turn.completed", "turn.failed", "error"})
# Cheap pre-filter before parsing a line: one of these must appear in a kept event.
_MARKERS = (
    b'"result"',
    b'"rate_limit_event"',
    b'"turn.completed"',
    b'"turn.failed"',
    b'"error"',
    b'"agent_message"',
)

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


def _free_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as probe:
        probe.bind((host, 0))
        port: int = probe.getsockname()[1]
        return port


def host_port(host: str, port: int) -> str:
    """`host:port`, with brackets around an IPv6 address."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@contextmanager
def tool_server(
    specs: list[ToolSpec], handler: ToolHandler, host: str = "127.0.0.1"
) -> Iterator[tuple[str, str]]:
    """HTTP MCP server for the duration of the block. Returns (url, token).

    `host`: address it listens on, 127.0.0.1 when the CLI runs here; with the agents
    container, the worker's address on their private network (`agents_client.callback_host`),
    and on that one only: neither the web container nor the database's network reach it."""
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
    port, token = _free_port(host), secrets.token_urlsafe(32)
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host_port(host, port)],
            allowed_origins=[],
        ),
    )
    http = uvicorn.Server(
        uvicorn.Config(_BearerGuard(app, token), host=host, port=port, log_level="warning")
    )
    thread = threading.Thread(target=http.run, daemon=True, name="synaptane-tidy-tools")
    thread.start()
    deadline = time.monotonic() + 10
    while not http.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise AIError("The local tool server did not start.", transient=True)
        time.sleep(0.05)
    try:
        yield f"http://{host_port(host, port)}/mcp", token
    finally:
        http.should_exit = True
        thread.join(timeout=10)


# --- Running the CLIs ------------------------------------------------------------------------


@dataclass
class CLIRun:
    usage: Usage
    text: str


LOGIN = {"claude_code": "claude auth login", "codex": "codex login --device-auth"}
# Set by `agents_runner.serve`: this process is the agents container's runner.
_RUNNER = threading.Event()


def in_docker() -> bool:
    """True in a container of compose.yaml."""
    return Path("/.dockerenv").exists()


def agents_url() -> str | None:
    """Address of the agents container (`SYNAPTANE_AGENTS_URL`, the worker's environment in
    compose.yaml); None: the CLI runs in this process's machine and account."""
    value = os.environ.get(AGENTS_URL, "").strip().rstrip("/")
    return value or None


def login_service(container: bool | None = None) -> str:
    """Compose service where the CLI is signed in: `agents` when the CLIs run in their own
    container (this process is its runner, or the worker that sends it the runs), otherwise
    `worker`. `container`: the mode, when known from elsewhere (web container: the state the
    worker reported, `agents_client.read_state`)."""
    if container is None:
        container = _RUNNER.is_set() or agents_url() is not None
    return "agents" if container else "worker"


def agent_home() -> Path | None:
    """`HOME` reserved for the subscription CLIs (`SYNAPTANE_AGENT_HOME`), if set.

    Under Docker, `/agents`, the `synaptane-agents` volume, mounted only where the CLI runs
    (the agents container; the worker with an older Compose file): the login is neither in
    the data volume nor visible from the web container. Without this variable, the CLI keeps
    the `HOME` of the system account running it."""
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
    logged in again (`.claude.json`: Claude Code settings)."""
    old = data_dir / "home"
    paths = " ".join(str(old / name) for name in (".claude", ".claude.json", ".codex"))
    in_compose = in_docker() if docker is None else docker
    return f"docker compose exec app rm -rf {paths}" if in_compose else f"rm -rf {paths}"


def login_command(provider: str, docker: bool | None = None, container: bool | None = None) -> str:
    """Command that logs in a subscription's CLI, depending on the installation.

    Under Docker, the service where the CLI runs (`login_service`) has the CLI volume as its
    `HOME` (compose.yaml): the plain command stores the login there. Elsewhere, the reserved
    `HOME` is given if set."""
    base = LOGIN[provider]
    if in_docker() if docker is None else docker:
        return f"docker compose exec {login_service(container)} {base}"
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
        # No setting source (user, project, local): neither the hooks, environment and
        # permissions of `~/.claude/settings.json`, nor `~/.claude/CLAUDE.md`, skills or agents
        # that a compromised run would have left in the CLI's HOME, kept from run to run.
        *NO_SETTINGS,
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
    for feature in CODEX_ENABLED:
        command += ["--enable", feature]
    if config.model:
        command += ["-m", config.model]
    command.append("-")
    return command


def _claude_usage(stdout: str, fallback: str = "") -> CLIRun:
    """Claude Code streamed output: the final `result` event and the quota readings.
    `fallback`: what to show when there is no result (end of the raw output)."""
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
        return CLIRun(usage, (fallback or stdout)[-2000:])
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
        if not isinstance(event, dict):
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


# --- Process: bounded output, time limit, cancellation ----------------------------------------


@dataclass
class Completed:
    """What a CLI process left: the kept events (`stdout`, one JSON per line), the end of its
    raw output and of its error output, and how it ended. Same form in the worker and in the
    agents container's answer."""

    returncode: int
    stdout: str = ""
    tail: str = ""
    stderr: str = ""
    had_output: bool = False  # something other than blanks on the standard output
    timed_out: bool = False
    cancelled: bool = False
    overflow: bool = False  # more than MAX_KEPT bytes of kept events: stopped

    def to_json(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "tail": self.tail,
            "stderr": self.stderr,
            "had_output": self.had_output,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "overflow": self.overflow,
        }

    @classmethod
    def from_json(cls, data: object) -> "Completed":
        """Back from `to_json` (answer of the agents container); ValueError otherwise."""
        if not isinstance(data, dict) or not isinstance(data.get("returncode"), int):
            raise ValueError("not a run result")
        texts = {k: data.get(k, "") for k in ("stdout", "tail", "stderr")}
        flags = {
            k: data.get(k, False) for k in ("had_output", "timed_out", "cancelled", "overflow")
        }
        if not all(isinstance(v, str) for v in texts.values()) or not all(
            isinstance(v, bool) for v in flags.values()
        ):
            raise ValueError("not a run result")
        return cls(data["returncode"], **texts, **flags)


def _kept(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    kind = event.get("type")
    if kind in _KEPT_EVENTS:
        return True
    item = event.get("item")
    return (
        kind == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message"
    )


class _Output:
    """Standard output of the CLI, read in pieces: kept events, raw tail, bounded memory."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.size = 0
        self.overflow = False
        self.seen = False
        self._line = bytearray()
        self._skipping = False  # line longer than MAX_LINE: ignored up to its end
        self._tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.seen = self.seen or bool(chunk.strip())
        self._tail += chunk
        del self._tail[: -TAIL * 4]
        start = 0
        while (end := chunk.find(b"\n", start)) >= 0:
            self._piece(chunk[start:end])
            self._end_of_line()
            start = end + 1
        self._piece(chunk[start:])

    def close(self) -> None:
        self._end_of_line()

    @property
    def tail(self) -> str:
        return self._tail.decode("utf-8", "replace")[-TAIL:]

    def _piece(self, piece: bytes) -> None:
        if self._skipping or not piece:
            return
        self._line += piece
        if len(self._line) > MAX_LINE:
            self._line.clear()
            self._skipping = True

    def _end_of_line(self) -> None:
        line = bytes(self._line)
        self._line.clear()
        if self._skipping:
            self._skipping = False
            return
        if self.overflow or not any(marker in line for marker in _MARKERS):
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not _kept(event):
            return
        self.size += len(line) + 1
        if self.size > MAX_KEPT:
            self.overflow = True
            return
        self.lines.append(line.decode("utf-8", "replace"))


# CLIs running in this process: stopped if it exits during a run (worker stopped from a
# terminal or by its service manager), as their own session keeps them from the signal.
_RUNNING: set[subprocess.Popen[bytes]] = set()
_RUNNING_LOCK = threading.Lock()


@atexit.register
def _stop_running() -> None:
    with _RUNNING_LOCK:
        running = list(_RUNNING)
    for process in running:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Stops the CLI and everything it started (its own session): SIGTERM, then SIGKILL."""
    for sent in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sent)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=STOP_GRACE)
            return
        except subprocess.TimeoutExpired:
            continue


def execute(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    given: str,
    timeout: float,
    cancel: threading.Event | None = None,
) -> Completed:
    """Runs a CLI: `given` on its standard input, at most `timeout` seconds, stopped as soon
    as `cancel` is set (the worker gave up) or its kept output exceeds MAX_KEPT. The CLI runs
    in a session of its own, stopped as a whole: nothing it started survives the run."""
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise AIError(
            "The CLI could not be started: {error}", error=error.strerror or type(error).__name__
        ) from None
    assert process.stdin and process.stdout and process.stderr
    with _RUNNING_LOCK:
        _RUNNING.add(process)
    try:
        return _follow(process, given, timeout, cancel)
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(process)


def _follow(
    process: subprocess.Popen[bytes],
    given: str,
    timeout: float,
    cancel: threading.Event | None,
) -> Completed:
    """Feeds, reads and watches a CLI started by `execute`, until it ends or is stopped."""
    output = _Output()
    errors = bytearray()

    def write() -> None:
        assert process.stdin
        # OSError: the CLI stopped reading (it ended, or it was stopped).
        with suppress(OSError):
            process.stdin.write(given.encode())
        with suppress(OSError):
            process.stdin.close()

    def read_output() -> None:
        assert process.stdout
        while chunk := os.read(process.stdout.fileno(), 65536):
            output.feed(chunk)
        output.close()

    def read_errors() -> None:
        assert process.stderr
        while chunk := os.read(process.stderr.fileno(), 65536):
            errors.extend(chunk)
            del errors[: -TAIL * 4]

    threads = [
        threading.Thread(target=target, daemon=True, name=f"synaptane-cli-{name}")
        for name, target in (("in", write), ("out", read_output), ("err", read_errors))
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    timed_out = cancelled = False
    while True:
        try:
            process.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            pass
        if output.overflow:
            break
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if time.monotonic() > deadline:
            timed_out = True
            break
    # Also when the CLI ended by itself: whatever it left running in its session goes too.
    _stop(process)
    for thread in threads:
        thread.join(timeout=STOP_GRACE)
    if not any(thread.is_alive() for thread in threads):  # never under a reading thread
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    return Completed(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout="\n".join(output.lines),
        tail=output.tail,
        stderr=errors.decode("utf-8", "replace")[-TAIL:],
        had_output=output.seen,
        timed_out=timed_out,
        cancelled=cancelled,
        overflow=output.overflow,
    )


def interpret(provider: str, done: Completed) -> CLIRun:
    """Result of a run, or the AIError that tells what went wrong."""
    if done.timed_out:
        raise AIError("The run exceeded the maximum time.", transient=True)
    if done.cancelled:
        raise AIError("The run was interrupted.", transient=True)
    if done.overflow:
        raise AIError(
            "The CLI wrote more than {size} MB of results: the run was stopped.",
            size=MAX_KEPT // (1024 * 1024),
        )
    if done.returncode != 0 and not done.had_output:
        message = done.stderr.strip()[-600:]
        transient = any(
            w in message.lower() for w in ("rate limit", "429", "overloaded", "timeout")
        )
        raise AIError(
            "{provider}: {detail}",
            transient=transient,
            provider=provider,
            detail=message or "failure without a message",
        )
    if provider == "claude_code":
        return _claude_usage(done.stdout, done.tail)
    return _codex_usage(done.stdout)


# --- A run -----------------------------------------------------------------------------------


def run_cli(
    provider: str,
    model: str,
    system: str,
    prompt: str,
    url: str,
    token: str,
    timeout: float,
    cancel: threading.Event | None = None,
) -> Completed:
    """Launches the CLI of a run here, pointed at the tool server `url` (token `token`), in a
    throwaway working folder. Called by the worker (CLI in the worker) or by the agents
    container's runner (`agents_runner`)."""
    config = AIConfig(provider=provider, model=model)
    with tempfile.TemporaryDirectory(prefix="synaptane-agent-") as scratch:
        workdir = Path(scratch)
        env = _environment()
        if provider == "claude_code":
            mcp_file = workdir / "mcp.json"
            mcp_file.touch(0o600)
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
            system_file = workdir / "system.md"
            system_file.touch(0o600)
            system_file.write_text(system)
            env.update(CLAUDE_ENVIRONMENT)
            command, given = _claude_command(config, system_file, mcp_file), prompt
        elif provider == "codex":
            if found := codex_instructions(env):
                raise AIError(
                    "Codex would add the instructions of {paths} to every run: delete them "
                    "(Synaptane never writes them, a compromised run may have).",
                    paths=", ".join(map(str, found)),
                )
            env["SYNAPTANE_TIDY_TOKEN"] = token
            command = _codex_command(config, url, "SYNAPTANE_TIDY_TOKEN", workdir)
            given = f"{system}\n\n---\n\n{prompt}"
        else:
            raise AIError("Unknown provider: {provider}", provider=provider)
        return execute(command, cwd=workdir, env=env, given=given, timeout=timeout, cancel=cancel)


def codex_instructions(env: dict[str, str]) -> list[Path]:
    """Instruction files of Codex's home (`CODEX_HOME`, otherwise `$HOME/.codex`) that it would
    add to every run. Only their existence is tested: nothing is read."""
    home = env.get("CODEX_HOME") or str(Path(env.get("HOME") or "~").expanduser() / ".codex")
    return [Path(home) / name for name in CODEX_INSTRUCTIONS if (Path(home) / name).exists()]


def run_agent(
    config: AIConfig,
    system: str,
    prompt: str,
    specs: list[ToolSpec],
    handler: ToolHandler,
    timeout: int = RUN_TIMEOUT,
    wait: float | None = None,
) -> CLIRun:
    """Runs a complete pass in the subscription's CLI: in the agents container if the worker
    has its address, otherwise here. `wait`: longest wait for a turn when the agents container
    is busy (`agents_client.wait_turn`), `timeout` by default."""
    from synaptane.ai import agents_client

    if config.provider not in LOGIN:
        raise AIError("Unknown provider: {provider}", provider=config.provider)
    remote = agents_url()
    host = agents_client.callback_host(remote) if remote else "127.0.0.1"
    with tool_server(specs, handler, host) as (url, token):
        if remote:
            port = urlsplit(url).port or 80
            done = agents_client.run(
                config.provider, config.model, system, prompt, port, token, timeout, wait
            )
        else:
            done = run_cli(config.provider, config.model, system, prompt, url, token, timeout)
    return interpret(config.provider, done)


def _environment() -> dict[str, str]:
    """Minimal environment: the CLI finds its login in its reserved `HOME`
    (`SYNAPTANE_AGENT_HOME`), failing that in the `HOME` of the process."""
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
        *NO_SETTINGS,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    with tempfile.TemporaryDirectory(prefix="synaptane-probe-") as scratch:
        done = execute(
            command, cwd=Path(scratch), env=_environment(), given="", timeout=PROBE_TIMEOUT
        )
    if done.timed_out:
        raise AIError("Claude Code does not answer (timed out).", transient=True)
    try:
        run = interpret("claude_code", done)
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
    (without ever reading their credentials). In the agents container if the worker has its
    address, otherwise here."""
    if agents_url():
        from synaptane.ai import agents_client

        return agents_client.status(provider)
    return local_login_status(provider)


def local_login_status(provider: str) -> Label:
    """`login_status` where this process runs (the worker, or the agents container)."""
    status = _require_logged_in(provider)
    return _claude_probe() if provider == "claude_code" else status


def _require_logged_in(provider: str) -> Label:
    """Login state reported by the CLI (without ever reading its credentials)."""
    # Only the provider's CLI is looked for: the other one may be missing.
    name, *arguments = {
        "claude_code": ["claude", "auth", "status"],
        "codex": ["codex", "login", "status"],
    }[provider]
    command = [_binary(name), *arguments]
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
