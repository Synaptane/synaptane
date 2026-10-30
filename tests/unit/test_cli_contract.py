"""Subscription CLI contract: simulated outputs, never the real CLIs.

`shutil.which` and `subprocess.run` are simulated: these tests do not depend on `claude` or
`codex` being present on the machine (that is what had turned the CI red for 17 hours).
The requests of the simulated "CLI", however, go to the real local fake provider.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx2
import pytest

from synaptane.ai import cli_agents, cli_contract
from synaptane.cli import main

FIXTURE = Path(__file__).parents[1] / "fixtures" / "codex-features-0.166.0.txt"
REPORT = {"type": "function", "name": "mcp__synaptane_tidy__report"}
SEARCH = {
    "type": "tool_search",
    "execution": "client",
    "description": "# Tool discovery\n\nYou have access to tools from the following sources:\n"
    "- synaptane_tidy\nSome of the tools may not have been provided to you upfront.",
}
CODEX_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "name": "list_mcp_resources"},
    {"type": "function", "name": "request_user_input"},
    {"type": "custom", "name": "apply_patch"},
    SEARCH,
]
CLAUDE_HELP = """Usage: claude [options] [command] [prompt]
  --allowedTools, --allowed-tools <tools...>  Tools to allow
  --bare  Minimal mode: --system-prompt[-file], --append-system-prompt[-file], --mcp-config
  --mcp-config <configs...>             Load MCP servers from JSON files or strings
  --output-format <format>              Output format (only works with --print)
  --permission-mode <mode>              Permission mode (choices: "acceptEdits", "auto",
                                        "bypassPermissions", "manual",
                                        "dontAsk", "plan")
  --strict-mcp-config                   Only use MCP servers from --mcp-config
  --tools <tools...>                    Specify the list of available tools from
                                        the built-in set. Use "" to disable all
                                        tools, "default" to use all tools.
"""


class Done:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def simulate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    help_text: str = CLAUDE_HELP,
    features: str | None = None,
    claude_tools: list[dict[str, Any]] | None = None,
    codex_tools: list[dict[str, Any]] | None = None,
) -> list[list[str]]:
    """Simulated CLIs: help, feature list, and a run that sends its tools to the fake
    provider (as the real CLI would). Returns the commands that were run."""
    features = FIXTURE.read_text() if features is None else features
    launched: list[list[str]] = []
    real_run = subprocess.run

    def run(command: list[str], **kwargs: Any) -> Any:
        if Path(command[0]).name == "git":  # Synaptane version (`--version`), not a CLI
            return real_run(command, **kwargs)
        launched.append(command)
        env: dict[str, str] = kwargs["env"]
        assert env["HOME"] != str(Path.home())  # never the person's HOME (logins)
        assert "OPENAI_API_KEY" not in env and env.get("ANTHROPIC_API_KEY") in (
            None,
            cli_contract.PROBE_KEY,
        )
        name = Path(command[0]).name
        if "--version" in command:
            return Done(f"{name} 9.9.9")
        if command[1:] == ["--help"]:
            return Done(help_text)
        if command[-2:] == ["features", "list"]:
            return Done("WARNING: proceeding\n" + features)
        if name == "claude":
            assert env["ENABLE_TOOL_SEARCH"] == "false"  # the environment of a run
            url = env["ANTHROPIC_BASE_URL"] + "/v1/messages"
            tools = [REPORT] if claude_tools is None else claude_tools
        else:
            joined = " ".join(command)
            found = re.search(r'base_url="([^"]+)"', joined)
            assert found and kwargs["input"]
            url, tools = (
                found[1] + "/responses",
                CODEX_TOOLS if codex_tools is None else codex_tools,
            )
        httpx2.post(url, json={"model": "x", "tools": tools})
        return Done(stderr="erreur du faux fournisseur", returncode=1)

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(subprocess, "run", run)
    return launched


def test_parse_features_reads_the_real_output() -> None:
    features = cli_contract.parse_features(FIXTURE.read_text())
    assert features["shell_tool"] == cli_contract.Feature("shell_tool", "stable", True)
    assert features["agent_message_board"].stage == "under development"
    assert not features["memories"].enabled


def test_lists_match_codex_of_the_image() -> None:
    """Image version: every disabled feature exists, every enabled feature is classified."""
    features = cli_contract.parse_features(FIXTURE.read_text())
    assert set(cli_agents.CODEX_DISABLED) <= features.keys()
    assert features.keys() >= cli_contract.CODEX_ALLOWED
    active = {f.name for f in features.values() if f.enabled and f.stage != "removed"}
    assert active - set(cli_agents.CODEX_DISABLED) - cli_contract.CODEX_ALLOWED == set()
    assert not set(cli_agents.CODEX_DISABLED) & cli_contract.CODEX_ALLOWED


def test_judge_tools() -> None:
    allowed = cli_contract.CODEX_TOOLS_ALLOWED
    assert cli_contract.judge_tools([[REPORT]]) == []
    assert cli_contract.judge_tools([CODEX_TOOLS], allowed) == []
    shell = cli_contract.judge_tools([[REPORT, {"name": "Bash"}, {"name": "Read"}]])
    assert len(shell) == 1 and "Bash, Read" in shell[0]
    apps = dict(SEARCH, description=SEARCH["description"].replace("tidy\n", "tidy\n- gmail\n"))
    assert "gmail" in cli_contract.judge_tools([[apps]], allowed)[0]
    lost = cli_contract.judge_tools([[{"name": "request_user_input"}]], allowed)
    assert "MCP configuration" in lost[0]
    assert "no request" in cli_contract.judge_tools([])[0]


def test_fake_provider_records_and_refuses() -> None:
    with cli_contract.fake_provider() as (url, received):
        answer = httpx2.post(url + "/v1/messages", json={"tools": [REPORT]})
        httpx2.post(url + "/v1/other", content=b"pas du json")
    assert answer.status_code == 400 and answer.json()["error"]["type"] == "invalid_request_error"
    assert cli_contract.offered_tools(received) == [[REPORT]]


def test_contract_holds_with_the_expected_clis(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launched = simulate(monkeypatch)
    reports = cli_contract.check_all()
    assert [(r.binary, r.ok, r.version) for r in reports] == [
        ("claude", True, "claude 9.9.9"),
        ("codex", True, "codex 9.9.9"),
    ]
    # The probe runs the exact command of a run, pointed at the fake provider.
    claude = next(c for c in launched if "-p" in c)
    assert claude[claude.index("--tools") + 1] == "" and "--strict-mcp-config" in claude
    codex = next(c for c in launched if "exec" in c)
    assert {"shell_tool", "view_image"} <= {
        codex[i + 1] for i, p in enumerate(codex) if p == "--disable"
    }
    assert codex[-1] == "-" and 'model_provider="synaptane_contract"' in codex
    assert main(["check-clis"]) == 0
    assert "[ok] Claude Code (claude)" in capsys.readouterr().out


def test_contract_fails_when_the_clis_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    help_text = CLAUDE_HELP.replace("--strict-mcp-config", "--only-mcp-config").replace(
        'Use "" to disable all', 'Use "" for the defaults of'
    )
    features = FIXTURE.read_text().replace("shell_snapshot ", "shell_snapshot_v3 ")
    features += "magic_terminal                           stable             true\n"
    simulate(
        monkeypatch,
        help_text=help_text,
        features=features,
        claude_tools=[REPORT, {"name": "Bash"}],
    )
    claude, codex = cli_contract.check_all()
    assert any("--strict-mcp-config" in p for p in claude.problems)
    assert any('--tools ""' in p for p in claude.problems)
    assert any("built-in tools offered to the model: Bash" in p for p in claude.problems)
    assert any("not found: shell_snapshot " in p for p in codex.problems)
    assert any(
        "unclassified enabled features: magic_terminal, shell_snapshot_v3" in p
        for p in codex.problems
    )
    assert main(["check-clis"]) == 1
    output = capsys.readouterr()
    assert "[FAILED] Codex (codex)" in output.out and "Contract broken" in output.err


def test_codex_offering_a_shell_breaks_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    simulate(monkeypatch, codex_tools=[*CODEX_TOOLS, {"type": "function", "name": "exec_command"}])
    _, codex = cli_contract.check_all()
    assert len(codex.problems) == 1 and "exec_command" in codex.problems[0]


def test_absent_clis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    reports = cli_contract.check_all()
    assert [(r.installed, r.ok) for r in reports] == [(False, False), (False, False)]
    assert main(["check-clis"]) == 1  # the image must contain both
    assert main(["check-clis", "--installed-only"]) == 0


def test_a_cli_that_cannot_run_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")

    def broken(command: list[str], **kwargs: Any) -> Done:
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr(subprocess, "run", broken)
    reports = cli_contract.check_all()
    assert all("cannot run the check" in r.problems[0] for r in reports)
