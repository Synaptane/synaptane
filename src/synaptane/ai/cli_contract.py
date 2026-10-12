"""Contract of the subscription CLIs: what guarantees the agent has no shell, files or web.

With a subscription, the tidy-up agent runs in the official CLI (`cli_agents`). That it only
has Synaptane's tools rests solely on command-line options: `--tools ""` and
`--strict-mcp-config` for Claude Code, `--disable …` for Codex. A version upgrade can rename
an option or a feature, change its meaning, or add a feature enabled by default: the agent
would then get a shell back in the worker, and a prompt injection through the inbox would be
enough to read the server.

This module checks it **without login or model call**, in an empty temporary `HOME` (no login
is read, no user setting applies):
1. Claude Code's help still mentions the options used, with the same meaning;
2. every Codex feature turned off still exists, and every other enabled feature is known;
3. above all, the exact command of a run is launched against a **fake local provider** (API
   address on 127.0.0.1, dummy key) that records the request and answers with an error: this
   reveals the tools the CLI actually offers the model. Only those of Synaptane's MCP server
   are allowed (plus, for Codex, `CODEX_TOOLS_ALLOWED`). This check is what showed that
   `codex features list` reports `unified_exec` as enabled while the `exec_command` tool is no
   longer offered: the feature list is not enough.

It is run by `synaptane check-clis`, by the CI on each built image, by the CLI upgrade
workflow before it opens its PR, and reported by `synaptane doctor`.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from synaptane.ai import cli_agents
from synaptane.ai.base import AIConfig, AIError, ToolSpec
from synaptane.ai.cli_agents import CODEX_DISABLED

TIMEOUT = 60  # seconds, per command

# Options of `_claude_command`, and the forms under which the help mentions them.
CLAUDE_FLAGS: dict[str, tuple[str, ...]] = {
    "--tools": ("--tools",),
    "--strict-mcp-config": ("--strict-mcp-config",),
    "--permission-mode": ("--permission-mode",),
    "--allowedTools": ("--allowedTools",),
    "--mcp-config": ("--mcp-config",),
    "--output-format": ("--output-format",),
    # Hidden option: the help only mentions it within "--system-prompt[-file]".
    "--system-prompt-file": ("--system-prompt-file", "--system-prompt[-file]"),
}
# Meaning of the values passed: if the help no longer states it, it may have changed.
CLAUDE_MEANINGS = (
    ('use "" to disable all tools', 'according to the help, `--tools ""` no longer disables tools'),
    ('"dontask"', 'the "dontAsk" permission mode has disappeared from the help'),
)

# Codex features enabled by default, reviewed and judged free of execution, file access and
# web access (Codex 0.160.0). Any other enabled feature fails the contract: it must be
# classified here, or turned off in `cli_agents.CODEX_DISABLED` (when in doubt, turned off).
# "removed" features are ignored: they can no longer be set.
CODEX_ALLOWED = frozenset(
    {
        "auth_elicitation",  # authentication request from an MCP server
        "compaction_image_budget",  # image size during context summarization
        "content_item_kinds",  # message format
        "enable_request_compression",  # compression of requests to OpenAI
        "fast_mode",  # model service tier
        "guardian_approval",  # automatic review of approvals: restricts, opens nothing
        "guardian_reuse_parent_compaction",
        "in_app_chat",  # desktop app
        "in_app_dictation",  # desktop app
        "in_app_updates",  # desktop app
        "mentions_v2",  # mentions in the typed text
        "realtime_conversation",  # voice conversation, interactive
        "sleep_tool",  # waiting
        "system_proxy_fallback",  # system network proxy
        "tool_call_mcp_elicitation",  # questions from an MCP server
        "unbounded_connection_retries",  # network retries
        "write_stdin_approval",  # approval specific to unified_exec, turned off
    }
)

# Built-in tools that Codex still offers the model with the options of a run, and why they are
# harmless. No option removes them (Codex 0.160.0).
CODEX_TOOLS_ALLOWED = {
    "tool_search": "search among the declared MCP tools, Synaptane's only (sources checked)",
    "list_mcp_resources": "resources of the declared MCP servers: Synaptane alone, no resource",
    "list_mcp_resource_templates": "same",
    "read_mcp_resource": "same",
    "request_user_input": "question to the user, never answered in non-interactive mode",
    "apply_patch": "file writing, refused by the read-only sandbox "
    "(--sandbox read-only, approval_policy never)",
}
MCP_PREFIX = f"mcp__{cli_agents.SERVER_NAME}__"
PROBE_TOOL = ToolSpec(
    name="report",
    description="Reports that the check works.",
    input_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
)
PROBE_KEY = "synaptane-contract-not-a-key"


@dataclass(frozen=True)
class Feature:
    name: str
    stage: str  # "stable", "experimental", "under development", "removed"…
    enabled: bool


@dataclass
class Report:
    """Contract result for one CLI."""

    binary: str
    label: str
    installed: bool = True
    version: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.installed and not self.problems


def parse_features(text: str) -> dict[str, Feature]:
    """Output of `codex features list`: "name  stage  true|false" per line."""
    features: dict[str, Feature] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[-1] not in ("true", "false"):
            continue  # warnings, empty lines
        features[parts[0]] = Feature(parts[0], " ".join(parts[1:-1]), parts[-1] == "true")
    return features


def _documented(help_text: str, forms: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![\w-]){re.escape(form)}(?![\w-])", help_text) for form in forms)


def _environment(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment of a check: empty `HOME` (no login, no user setting), no real API key, no
    side traffic."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "LANG": "C.UTF-8",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        **(extra or {}),
    }


def _run(
    command: list[str],
    home: Path,
    stdin: str | None = None,
    extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if stdin is None:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=_environment(home, extra),
            cwd=home,
            stdin=subprocess.DEVNULL,
        )
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env=_environment(home, extra),
        cwd=home,
        input=stdin,
    )


def _version(binary: str, home: Path) -> str:
    lines = (_run([binary, "--version"], home).stdout or "").strip().splitlines()
    return lines[-1] if lines else ""


# --- Fake provider: the tools actually offered to the model --------------------------------


class _Recorder(BaseHTTPRequestHandler):
    """Keeps the body of each request and answers with an error: the CLI stops at the first
    turn, without any model being called."""

    received: list[dict[str, object]]

    def _answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = None
        if isinstance(body, dict):
            self.received.append(body)
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.end_headers()
        error = {"type": "invalid_request_error", "message": "Synaptane check."}
        self.wfile.write(json.dumps({"type": "error", "error": error}).encode())

    do_GET = do_POST = _answer

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def fake_provider() -> Iterator[tuple[str, list[dict[str, object]]]]:
    """Fake API provider on 127.0.0.1, for the duration of the block. Returns (url, requests)."""
    received: list[dict[str, object]] = []
    handler = type("Recorder", (_Recorder,), {"received": received})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, args=(0.05,), daemon=True, name="synaptane-contract"
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", received
    finally:
        server.shutdown()
        server.server_close()


Tool = Mapping[str, object]


def offered_tools(received: list[dict[str, object]]) -> list[list[Tool]]:
    """Tools of each request that offers some (Messages API as well as Responses)."""
    offered: list[list[Tool]] = []
    for body in received:
        tools = body.get("tools")
        if isinstance(tools, list):
            offered.append([t for t in tools if isinstance(t, dict)])
    return offered


def tool_name(tool: Tool) -> str:
    """Name of a tool; failing that its type (Codex's `tool_search` has no name)."""
    return str(tool.get("name") or tool.get("type"))


def _search_sources(tool: Tool) -> list[str]:
    """Sources announced by Codex's tool search (deferred MCP tools)."""
    found = re.search(r"following sources:\s*((?:\n\s*- [^\n]+)+)", str(tool.get("description")))
    return [line.strip()[2:].strip() for line in found[1].strip().splitlines()] if found else []


def judge_tools(offered: Sequence[Sequence[Tool]], allowed: Collection[str] = ()) -> list[str]:
    """Problems: built-in tool not allowed, a tool source other than Synaptane, Synaptane's
    tools missing, or nothing observed."""
    if not offered:
        return ["the CLI sent no request to the fake provider: tools not checked"]
    tools = [tool for listed in offered for tool in listed]
    names = {tool_name(tool) for tool in tools}
    problems = []
    extra = sorted(n for n in names if not n.startswith(MCP_PREFIX) and n not in allowed)
    if extra:
        problems.append(
            f"built-in tools offered to the model: {', '.join(extra)} (only Synaptane's should "
            "be: the agent could read the server)"
        )
    # Codex does not present the MCP tools upfront: `tool_search` finds them, and announces
    # where they come from. Only Synaptane's server must appear there.
    sources = {source for tool in tools for source in _search_sources(tool)}
    others = sorted(sources - {cli_agents.SERVER_NAME})
    if others:
        problems.append(f"tool sources other than Synaptane: {', '.join(others)}")
    if not any(n.startswith(MCP_PREFIX) for n in names) and cli_agents.SERVER_NAME not in sources:
        problems.append(
            "Synaptane's tools (MCP) are not offered to the model: the MCP configuration is no "
            "longer understood"
        )
    return problems


def _probe_command(provider: str, home: Path, api: str, url: str, token: str) -> list[str]:
    """Exact command of a run (`cli_agents`), pointed at the fake provider."""
    config = AIConfig(provider=provider, model="")
    if provider == "claude_code":
        mcp_file = home / "mcp.json"
        server = {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}
        mcp_file.write_text(json.dumps({"mcpServers": {cli_agents.SERVER_NAME: server}}))
        system_file = home / "system.md"
        system_file.write_text("Check.")
        return cli_agents._claude_command(config, system_file, mcp_file)
    command = cli_agents._codex_command(config, url, "SYNAPTANE_TIDY_TOKEN", home)
    fake = (
        f'{{name="synaptane-contract", base_url="{api}/v1", env_key="SYNAPTANE_CONTRACT_KEY", '
        'wire_api="responses", request_max_retries=0, stream_max_retries=0}'
    )
    assert command[-1] == "-"  # instructions through standard input, last
    return [
        *command[:-1],
        "-c",
        f"model_providers.synaptane_contract={fake}",
        "-c",
        'model_provider="synaptane_contract"',
        "-m",
        "gpt-5.5",
        "-",
    ]


def probe(provider: str, home: Path) -> tuple[list[list[Tool]], str]:
    """Runs the command of a run against the fake provider, with the real local tool server.
    Returns the tools offered to the model and the end of the output (diagnostic)."""
    with (
        cli_agents.tool_server([PROBE_TOOL], lambda name, arguments: "ok") as (url, token),
        fake_provider() as (api, received),
    ):
        command = _probe_command(provider, home, api, url, token)
        extra = (
            {"ANTHROPIC_BASE_URL": api, "ANTHROPIC_API_KEY": PROBE_KEY}
            if provider == "claude_code"
            else {"SYNAPTANE_TIDY_TOKEN": token, "SYNAPTANE_CONTRACT_KEY": PROBE_KEY}
        )
        result = _run(command, home, stdin="Check.", extra=extra)
        return offered_tools(received), (result.stderr or result.stdout or "").strip()[-300:]


def _check_offered(report: Report, home: Path, provider: str, allowed: Collection[str]) -> None:
    offered, output = probe(provider, home)
    problems = judge_tools(offered, allowed)
    if not offered and output:
        problems[0] += f" (output: {output})"
    report.problems += problems


# --- Checks per CLI ------------------------------------------------------------------------


def check_claude(home: Path) -> Report:
    report = Report("claude", "Claude Code (claude)")
    binary = shutil.which("claude")
    if binary is None:
        report.installed = False
        return report
    report.version = _version(binary, home)
    result = _run([binary, "--help"], home)
    text = result.stdout + result.stderr
    if result.returncode != 0 or not text.strip():
        report.problems.append(f"`claude --help` failed: {text.strip()[-300:]}")
        return report
    missing = [flag for flag, forms in CLAUDE_FLAGS.items() if not _documented(text, forms)]
    if missing:
        report.problems.append(
            f"options missing from the help: {', '.join(missing)} (renamed or removed: "
            "the agent could get its built-in tools back)"
        )
    flat = " ".join(text.split()).lower()
    report.problems += [problem for phrase, problem in CLAUDE_MEANINGS if phrase not in flat]
    _check_offered(report, home, "claude_code", ())
    return report


def check_codex(home: Path) -> Report:
    report = Report("codex", "Codex (codex)")
    binary = shutil.which("codex")
    if binary is None:
        report.installed = False
        return report
    (home / ".codex").mkdir(exist_ok=True)
    report.version = _version(binary, home)
    result = _run([binary, "features", "list"], home)
    features = parse_features(result.stdout)
    if result.returncode != 0 or not features:
        output = (result.stdout + result.stderr).strip()[-300:]
        report.problems.append(f"`codex features list` unreadable: {output}")
        return report
    missing = [name for name in CODEX_DISABLED if name not in features]
    if missing:
        report.problems.append(
            f"features to turn off not found: {', '.join(missing)} (renamed? Codex then refuses "
            "to start; if another one takes over their role, the agent would get a shell back)"
        )
    # `features list` does not always tell the real state (`unified_exec` stays enabled there
    # once turned off, but the tool is no longer offered): only unclassified features count
    # here, and the fake provider tells what is really offered.
    unknown = sorted(
        f.name
        for f in features.values()
        if f.enabled
        and f.stage != "removed"
        and f.name not in CODEX_DISABLED
        and f.name not in CODEX_ALLOWED
    )
    if unknown:
        report.problems.append(
            f"unclassified enabled features: {', '.join(unknown)}. If one executes, reads "
            "files, opens the web or adds tools, add it to cli_agents.CODEX_DISABLED; "
            "otherwise, to cli_contract.CODEX_ALLOWED"
        )
    if not missing:  # otherwise Codex refuses the options: a run would not start
        _check_offered(report, home, "codex", CODEX_TOOLS_ALLOWED)
    return report


def check_all() -> list[Report]:
    """Contract of each CLI; a missing CLI is reported (`installed` false), not checked."""
    reports = []
    for check in (check_claude, check_codex):
        with tempfile.TemporaryDirectory(prefix="synaptane-cli-contract-") as scratch:
            try:
                reports.append(check(Path(scratch)))
            except (OSError, subprocess.SubprocessError, AIError) as error:
                name = check.__name__.removeprefix("check_")
                reports.append(Report(name, name, problems=[f"cannot run the check: {error}"]))
    return reports
