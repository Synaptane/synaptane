#!/usr/bin/env python3
"""Fake `claude` and `codex` (tests/fake_cli.py, scripts/e2e-compose.sh): the same arguments,
the same outputs, no network other than the run's tool server, no login.

Installed (or mounted) as executables named `claude` and `codex`. What they do is read from
`$HOME/fake-cli.json` (the CLI's environment only keeps HOME and PATH): `behavior` among
"tool" (calls the `report` tool when the command gives a tool server, then ends; default),
"sleep" (waits `seconds`), "flood" (writes `lines` result events), "noise" (writes `lines`
assistant events of `size` bytes, then the result), "huge-line" (one line of `size` bytes,
then the result), "fail" (exits with code 2 and a message on the error output),
"error-result" (Claude: a result in error, session expired), "script" (writes `stdout` and
`stderr`, exits with `exit`); `logged_in` (default true). Each process appends its pid to
`$HOME/pids` and leaves its arguments, environment and standard input in `$HOME/last-*`.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def call_tool(name: str, args: list[str]) -> str:
    """Calls the run's `report` tool, as the CLI would (MCP over HTTP, with the token)."""
    if name == "claude":
        config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
        server = config["mcpServers"]["synaptane_tidy"]
        url, headers = server["url"], dict(server["headers"])
    else:
        prefix = "mcp_servers.synaptane_tidy.url="
        url = next(a for a in args if a.startswith(prefix))[len(prefix) :].strip('"')
        headers = {"Authorization": "Bearer " + os.environ["SYNAPTANE_TIDY_TOKEN"]}
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json, text/event-stream"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "report", "arguments": {"ok": True}},
    }
    request = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as answer:
        text: str = json.loads(answer.read())["result"]["content"][0]["text"]
        return text


def main() -> int:
    home = Path(os.environ.get("HOME", "."))
    settings_file = home / "fake-cli.json"
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    with open(home / "pids", "a") as pids:
        pids.write(f"{os.getpid()}\n")
    name, args = Path(sys.argv[0]).name, sys.argv[1:]
    logged_in = settings.get("logged_in", True)

    if name == "claude" and args[:2] == ["auth", "status"]:
        emit({"loggedIn": logged_in, "authMethod": "claude.ai"})
        return 0
    if name == "codex" and args[:2] == ["login", "status"]:
        print("Logged in using ChatGPT" if logged_in else "Not logged in")
        return 0 if logged_in else 1
    if args[:1] == ["--version"]:
        print(f"0.0.1 (fake {name})")
        return 0

    given = sys.stdin.read()
    behavior = settings.get("behavior", "tool")
    (home / "last-input.txt").write_text(given)
    (home / "last-args.json").write_text(json.dumps(args))
    (home / "last-env.json").write_text(json.dumps(dict(os.environ)))

    if behavior == "script":  # the given outputs and exit code, nothing else
        sys.stdout.write(settings.get("stdout", ""))
        sys.stdout.flush()
        sys.stderr.write(settings.get("stderr", ""))
        return int(settings.get("exit", 0))
    if behavior == "fail":
        sys.stderr.write("fake failure: rate limit reached\n")
        return 2
    if behavior == "sleep":
        time.sleep(float(settings.get("seconds", 60)))
    if behavior == "flood":
        for _ in range(int(settings.get("lines", 1000))):
            emit({"type": "result", "result": "x" * 1000, "num_turns": 1, "usage": {}})
    if behavior == "noise":
        for _ in range(int(settings.get("lines", 10))):
            content = "y" * int(settings.get("size", 1000))
            emit({"type": "assistant", "message": {"content": content}})
    if behavior == "huge-line":
        filler = "z" * int(settings.get("size", 100))
        sys.stdout.write('{"type": "result", "result": "' + filler + '"}\n')

    called = None
    if behavior == "tool" and (name == "codex" or "--mcp-config" in args):
        called = call_tool(name, args)

    if name == "claude":
        emit({"type": "system", "subtype": "init"})
        windows = {"five_hour": {"utilization": 0.1, "resetsAt": 1791303600}}
        info = {"status": "allowed", "unifiedWindows": windows}
        emit({"type": "rate_limit_event", "rate_limit_info": info})
        usage = {"input_tokens": 10, "output_tokens": 5}
        error = behavior == "error-result"
        result = "Session expired: please authenticate" if error else f"Done: {called}"
        emit(
            {"type": "result", "result": result, "num_turns": 2, "is_error": error, "usage": usage}
        )
    else:
        usage = {"input_tokens": 50, "cached_input_tokens": 30, "output_tokens": 7}
        emit({"type": "turn.completed", "usage": usage})
        emit(
            {"type": "item.completed", "item": {"type": "agent_message", "text": f"Done: {called}"}}
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
