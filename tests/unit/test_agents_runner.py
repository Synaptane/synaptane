"""Agents container: the worker sends it the subscription runs (`agents_client`), it runs the
CLI apart (`agents_runner`). Real server on 127.0.0.1, real tool server, fake `claude` and
`codex` (tests/fake_cli.py): the whole protocol, without any login or model call."""

import json
import logging
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

from synaptane.ai import agents_client, agents_runner, cli_agents, cli_contract
from synaptane.ai.base import AIConfig, AIError, ToolSpec
from synaptane.cli import main
from tests import fake_cli
from tests.fake_cli import Fake, fake  # noqa: F401 — shared fixture

PING = ToolSpec("report", "Rapport.", {"type": "object", "properties": {"ok": {"type": "boolean"}}})
CLAUDE = AIConfig(provider="claude_code", model="")
CODEX = AIConfig(provider="codex", model="gpt-5.6")
TOKEN = "t" * 43


@dataclass
class Agents:
    url: str
    key_file: Path
    runner: agents_runner.Runner
    fake: Fake

    def key(self) -> str:
        return agents_client.ensure_key(self.key_file)

    def post(self, path: str, body: object, key: str | None = None) -> httpx2.Response:
        headers = {"Authorization": f"Bearer {key or self.key()}"}
        return httpx2.post(self.url + path, json=body, headers=headers, timeout=30)


@pytest.fixture
def agents(
    fake: Fake,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Agents]:
    """An agents container (max one CLI at a time), and this process as its worker."""
    key_file = tmp_path / "keys" / "key"
    key_file.parent.mkdir()
    monkeypatch.setenv("SYNAPTANE_AGENTS_KEY_FILE", str(key_file))
    runner = agents_runner.Runner(key_file, max_runs=1, check_contract=False)
    with fake_cli.serving(agents_runner.create_app(runner)) as url:
        monkeypatch.setenv("SYNAPTANE_AGENTS_URL", url)
        yield Agents(url, key_file, runner, fake)


def handler_for(calls: list[tuple[str, dict[str, object]]]) -> cli_agents.ToolHandler:
    def handler(name: str, arguments: dict[str, object]) -> str:
        calls.append((name, arguments))
        return "ok"

    return handler


@pytest.mark.parametrize("config", [CLAUDE, CODEX])
def test_a_run_goes_through_the_agents_container(agents: Agents, config: AIConfig) -> None:
    """The CLI runs in the agents container and reaches the run's tools at the worker's
    address; the worker reads its result as if the CLI had run here."""
    calls: list[tuple[str, dict[str, object]]] = []
    run = cli_agents.run_agent(config, "System.", "Prompt.", [PING], handler_for(calls))
    assert run.text == "Done: ok" and calls == [("report", {"ok": True})]
    assert run.usage.turns >= 1 and run.usage.output_tokens > 0
    # The key was created by the worker, readable by the owner and the group only.
    assert stat.S_IMODE(agents.key_file.stat().st_mode) == 0o440
    env = agents.fake.last("env")
    assert isinstance(env, dict)
    assert env["HOME"] == str(agents.fake.home)
    assert not {k for k in env if k.startswith("SYNAPTANE_AGENTS")}  # not even the key's path
    assert (env.get("ENABLE_TOOL_SEARCH") == "false") == (config is CLAUDE)
    given = (agents.fake.home / "last-input.txt").read_text()
    assert given == ("Prompt." if config is CLAUDE else "System.\n\n---\n\nPrompt.")
    assert agents.runner.running == 0


def test_the_cli_is_pointed_at_the_caller_only(agents: Agents) -> None:
    """The request gives a port, never an address: the CLI's tools are at the caller's
    address, whatever the request says."""
    agents.fake.configure(behavior="noise", lines=0)
    body = {
        "provider": "codex",
        "model": "",
        "system": "s",
        "prompt": "p",
        "port": 4321,
        "token": TOKEN,
        "timeout": 30,
        "host": "10.0.0.1",  # ignored
    }
    answer = agents.post("/v1/run", body)
    assert answer.status_code == 200, answer.text
    args = agents.fake.last("args")
    assert isinstance(args, list)
    assert 'mcp_servers.synaptane_tidy.url="http://127.0.0.1:4321/mcp"' in args
    done = cli_agents.Completed.from_json(answer.json())
    assert done.returncode == 0 and "agent_message" in done.stdout


def test_only_the_shared_key_opens_the_agents_container(
    agents: Agents, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, "synaptane.agents")
    health = httpx2.get(agents.url + "/healthz")
    assert health.status_code == 200 and health.text == "ok"  # says nothing more
    key = agents.key()
    assert httpx2.get(agents.url + "/v1/info").status_code == 401
    wrong = httpx2.get(agents.url + "/v1/info", headers={"Authorization": "Bearer " + "x" * 64})
    assert wrong.status_code == 401 and wrong.text == ""
    run = agents.post("/v1/run", {"provider": "claude_code"}, key="y" * 64)
    assert run.status_code == 401
    assert agents.fake.pids() == []  # no CLI launched
    good = httpx2.get(agents.url + "/v1/info", headers={"Authorization": f"Bearer {key}"})
    assert good.status_code == 200 and good.json()["max_runs"] == 1
    assert "missing or wrong key" in caplog.text
    assert key not in caplog.text and "x" * 64 not in caplog.text


def test_a_worker_with_another_key_is_refused(
    agents: Agents, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents.key()
    other = tmp_path / "other-key"
    monkeypatch.setenv("SYNAPTANE_AGENTS_KEY_FILE", str(other))
    with pytest.raises(AIError, match="refused the worker's key") as refused:
        cli_agents.run_agent(CLAUDE, "S", "P", [PING], handler_for([]))
    assert not refused.value.transient
    assert agents_client.ensure_key(other) not in str(refused.value)
    assert agents.fake.pids() == []


def test_without_its_key_the_agents_container_waits(tmp_path: Path) -> None:
    runner = agents_runner.Runner(tmp_path / "absent", 1, check_contract=False)
    with fake_cli.serving(agents_runner.create_app(runner)) as url:
        answer = httpx2.get(url + "/v1/info", headers={"Authorization": "Bearer " + "k" * 64})
    assert answer.status_code == 503
    assert answer.json()["transient"] is True
    assert "has no key yet" in answer.json()["error"]["template"]


def test_a_missing_key_setting_is_explained(
    agents: Agents, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SYNAPTANE_AGENTS_KEY_FILE")
    monkeypatch.setattr("synaptane.core.logs.configure", lambda: {})
    with pytest.raises(AIError, match="SYNAPTANE_AGENTS_KEY_FILE is not"):
        cli_agents.login_status("codex")
    assert main(["agents-runner"]) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "anthropic"},
        {"model": "--dangerously-skip-permissions"},
        {"model": 3},
        {"port": 0},
        {"port": True},
        {"port": "80"},
        {"token": "short"},
        {"token": "x" * 40 + " " + "y" * 10},
        {"timeout": 0},
        {"system": None},
        {"prompt": ["p"]},
    ],
)
def test_invalid_runs_are_refused(agents: Agents, change: dict[str, object]) -> None:
    body = {
        "provider": "claude_code",
        "model": "",
        "system": "s",
        "prompt": "p",
        "port": 4321,
        "token": TOKEN,
        "timeout": 30,
        **change,
    }
    answer = agents.post("/v1/run", body)
    assert answer.status_code == 400
    assert answer.json()["error"]["template"] == "Invalid request: {detail}"
    assert agents.fake.pids() == []


def test_requests_are_bounded(agents: Agents, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_runner, "MAX_BODY", 1000)
    big = {"provider": "codex", "prompt": "p" * 2000}
    assert agents.post("/v1/run", big).status_code == 413
    key = agents.key()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def chunks() -> Iterator[bytes]:  # no Content-Length: counted as it comes
        yield b'{"prompt": "'
        for _ in range(10):
            yield b"p" * 200
        yield b'"}'

    streamed = httpx2.post(agents.url + "/v1/run", content=chunks(), headers=headers)
    assert streamed.status_code == 413
    assert agents.post("/v1/status", ["not", "an", "object"]).status_code == 400
    assert agents.fake.pids() == []


def test_a_run_stops_at_its_time_limit(agents: Agents) -> None:
    agents.fake.configure(behavior="sleep", seconds=60)
    with pytest.raises(AIError, match="exceeded the maximum time") as late:
        cli_agents.run_agent(CODEX, "S", "P", [PING], handler_for([]), timeout=1)
    assert late.value.transient
    [pid] = agents.fake.pids()
    assert fake_cli.wait_for(lambda: not fake_cli.alive(pid))
    assert agents.runner.running == 0


def test_the_cli_stops_when_the_worker_hangs_up(agents: Agents) -> None:
    """The worker gave up (its own time limit, or it stopped): the connection closes, and
    the agents container stops the CLI at once instead of letting it use the quota."""
    agents.fake.configure(behavior="sleep", seconds=60)
    body = {
        "provider": "claude_code",
        "model": "",
        "system": "s",
        "prompt": "p",
        "port": 4321,
        "token": TOKEN,
        "timeout": 600,
    }
    headers = {"Authorization": f"Bearer {agents.key()}"}
    with pytest.raises(httpx2.TimeoutException):
        httpx2.post(agents.url + "/v1/run", json=body, headers=headers, timeout=1.5)
    assert fake_cli.wait_for(lambda: len(agents.fake.pids()) == 1)
    [pid] = agents.fake.pids()
    assert fake_cli.wait_for(lambda: not fake_cli.alive(pid) and agents.runner.running == 0)


def test_beyond_its_runs_the_agents_container_says_to_retry(agents: Agents) -> None:
    agents.runner.acquire()  # its only slot is taken
    try:
        with pytest.raises(AIError, match="already running 1 CLIs") as busy:
            cli_agents.run_agent(CLAUDE, "S", "P", [PING], handler_for([]))
        assert busy.value.transient
        with pytest.raises(AIError, match="already running"):
            cli_agents.login_status("codex")
    finally:
        agents.runner.release()
    assert agents.fake.pids() == []
    assert cli_agents.run_agent(CLAUDE, "S", "P", [PING], handler_for([])).text == "Done: ok"


def test_stopping_the_agents_container_stops_its_clis(agents: Agents) -> None:
    agents.fake.configure(behavior="sleep", seconds=60)
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(cli_agents.run_agent(CODEX, "S", "P", [PING], handler_for([])))
        except AIError as error:
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert fake_cli.wait_for(lambda: agents.runner.running == 1 and bool(agents.fake.pids()))
    agents.runner.stop_all()
    thread.join(timeout=20)
    [error] = outcome
    assert isinstance(error, AIError) and "interrupted" in str(error) and error.transient
    assert not fake_cli.alive(agents.fake.pids()[0])


def test_the_answer_only_carries_what_the_worker_reads(agents: Agents) -> None:
    """20 MB of the CLI's conversation: the answer keeps the result and the quota only."""
    agents.fake.configure(behavior="noise", lines=200, size=100_000)
    done = agents_client.run("claude_code", "", "S", "P", 4321, TOKEN, 60)
    assert done.returncode == 0 and done.had_output
    assert len(done.stdout) < 2000 and len(done.tail) <= cli_agents.TAIL
    assert '"rate_limit_event"' in done.stdout and '"result"' in done.stdout
    assert cli_agents.interpret("claude_code", done).text == "Done: None"


def test_a_cli_that_writes_too_much_is_stopped(
    agents: Agents, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_agents, "MAX_KEPT", 50_000)
    agents.fake.configure(behavior="flood", lines=100_000)
    with pytest.raises(AIError, match="wrote more than"):
        cli_agents.run_agent(CLAUDE, "S", "P", [PING], handler_for([]))
    assert fake_cli.wait_for(lambda: not fake_cli.alive(agents.fake.pids()[0]))


def test_a_huge_line_is_skipped_unread(
    fake: Fake,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool result echoed by the CLI: neither kept nor parsed; the following lines are."""
    monkeypatch.setattr(cli_agents, "MAX_LINE", 1000)
    fake.configure(behavior="huge-line", size=50_000)
    run = cli_agents.run_agent(CLAUDE, "S", "P", [PING], handler_for([]))
    assert run.text == "Done: None" and run.usage.turns == 2


def test_a_cli_missing_from_the_agents_container_is_reported(
    agents: Agents, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Published image: the agents container runs without any CLI."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    with pytest.raises(AIError, match="“claude” is not installed on the server") as missing:
        cli_agents.run_agent(CLAUDE, "S", "P", [PING], handler_for([]))
    assert not missing.value.transient
    with pytest.raises(AIError, match="“codex” is not installed"):
        cli_agents.login_status("codex")
    assert agents.runner.running == 0 and agents.runner.cancels == set()


def test_a_failing_cli_is_reported_with_its_message(agents: Agents) -> None:
    agents.fake.configure(behavior="fail")
    with pytest.raises(AIError, match="claude_code: fake failure: rate limit") as failed:
        cli_agents.run_agent(CLAUDE, "S", "P", [PING], handler_for([]))
    assert failed.value.transient  # rate limit: retried later


def test_login_state_comes_from_the_agents_container(
    agents: Agents, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_agents, "in_docker", lambda: True)
    assert cli_agents.login_status("codex") == "Logged in using ChatGPT"
    state = cli_agents.login_status("claude_code")
    assert state.template == "Claude Code answers. Quota used: {quota}."
    agents.fake.configure(behavior="error-result")
    with pytest.raises(AIError, match="login has expired: run “docker compose exec agents"):
        cli_agents.login_status("claude_code")
    agents.fake.configure(logged_in=False)
    with pytest.raises(AIError, match="docker compose exec agents codex login --device-auth"):
        cli_agents.login_status("codex")
    answer = agents.post("/v1/status", {"provider": "ollama"})
    assert answer.status_code == 400


def test_info_reports_contract_and_exposures(
    fake: Fake,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract is checked in the agents container, where the CLIs run; the worker
    records it for the web container."""
    reports = [
        cli_contract.Report("claude", "Claude Code (claude)", version="2.1.314"),
        cli_contract.Report("codex", "Codex (codex)", version="0.166.0", problems=["shell"]),
    ]
    monkeypatch.setattr(cli_contract, "check_all", lambda: reports)
    monkeypatch.setenv("SYNAPTANE_DATABASE_URL", "postgresql://synaptane:secret@postgres/x")
    key_file = tmp_path / "key"
    monkeypatch.setenv("SYNAPTANE_AGENTS_KEY_FILE", str(key_file))
    runner = agents_runner.Runner(key_file, 2)
    assert fake_cli.wait_for(lambda: runner.contract is not None)
    with fake_cli.serving(agents_runner.create_app(runner)) as url:
        monkeypatch.setenv("SYNAPTANE_AGENTS_URL", url)
        state = agents_client.probe_state()
    assert state.container and state.reachable and not state.contract_pending
    assert [(c.binary, c.version, c.problems) for c in state.clis] == [
        ("claude", "2.1.314", ()),
        ("codex", "0.166.0", ("shell",)),
    ]
    assert state.contract_broken
    assert "variable SYNAPTANE_DATABASE_URL" in state.exposures
    assert "secret" not in json.dumps(state.exposures)
    data = tmp_path / "data"
    data.mkdir()
    agents_client.write_state(data, state)
    assert agents_client.read_state(data) == state


def test_exposures_name_what_the_container_should_not_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in agents_runner.SECRET_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SYNAPTANE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli_agents, "in_docker", lambda: False)
    before = agents_runner.exposures()
    assert not [e for e in before if e.startswith(("variable", "master key"))]
    (tmp_path / "master.key").write_bytes(b"k" * 32)
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    found = agents_runner.exposures()
    assert f"master key ({tmp_path / 'master.key'})" in found
    assert "variable POSTGRES_PASSWORD" in found


def test_an_unreachable_agents_container(
    fake: Fake,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with fake_cli.serving(
        agents_runner.create_app(agents_runner.Runner(tmp_path / "k", 1, False))
    ) as url:
        pass  # stopped: nothing listens there any more
    monkeypatch.setenv("SYNAPTANE_AGENTS_URL", url)
    monkeypatch.setenv("SYNAPTANE_AGENTS_KEY_FILE", str(tmp_path / "k"))
    with pytest.raises(AIError, match="cannot be reached") as unreachable:
        cli_agents.run_agent(CLAUDE, "S", "P", [PING], handler_for([]))
    assert unreachable.value.transient
    state = agents_client.refresh(tmp_path)
    assert state.container and not state.reachable and "cannot be reached" in state.error
    assert agents_client.read_state(tmp_path) == state
    assert fake.pids() == []


def test_the_shared_key(tmp_path: Path) -> None:
    path = tmp_path / "key"
    assert agents_client.read_key(path) is None
    keys: list[str] = []
    threads = [
        threading.Thread(target=lambda: keys.append(agents_client.ensure_key(path)))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(keys)) == 1 and len(keys[0]) >= agents_client.KEY_MIN
    assert [p.name for p in tmp_path.iterdir()] == ["key"]  # no temporary file left
    assert agents_client.ensure_key(path) == keys[0]
    short = tmp_path / "short"
    short.write_text("abc")
    assert agents_client.read_key(short) is None


def test_the_worker_address_is_the_route_to_the_agents_container() -> None:
    assert agents_client.callback_host("http://127.0.0.1:8700") == "127.0.0.1"
    with pytest.raises(AIError, match="cannot be reached") as failed:
        agents_client.callback_host("http://agents.invalid:8700")
    assert failed.value.transient


def test_tool_server_listens_on_the_given_address_only() -> None:
    with cli_agents.tool_server([PING], handler_for([]), "127.0.0.1") as (url, token):
        port = int(url.rsplit(":", 1)[1].split("/")[0])
        assert url == f"http://127.0.0.1:{port}/mcp"
        # Another Host header (DNS rebinding), even with the token: refused.
        answer = httpx2.post(
            url,
            json={},
            headers={"Authorization": f"Bearer {token}", "Host": f"localhost:{port}"},
        )
        assert answer.status_code in (400, 421)
    assert cli_agents.host_port("fd00::1", 80) == "[fd00::1]:80"


def test_results_cross_the_wire_unchanged() -> None:
    done = cli_agents.Completed(3, "a", "b", "c", True, False, True, False)
    assert cli_agents.Completed.from_json(json.loads(json.dumps(done.to_json()))) == done
    broken: object
    for broken in (None, [], {"returncode": "0"}, {"returncode": 0, "stdout": 1}):
        with pytest.raises(ValueError):
            cli_agents.Completed.from_json(broken)


def test_agents_runner_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(agents_runner, "serve", lambda *args: seen.append(args))
    monkeypatch.setattr("synaptane.core.logs.configure", lambda: {})
    monkeypatch.setenv("SYNAPTANE_AGENTS_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setenv("SYNAPTANE_AGENTS_MAX_RUNS", "3")
    assert main(["agents-runner", "--host", "0.0.0.0", "--port", "8701", "--max-runs", "4"]) == 0
    assert seen == [("0.0.0.0", 8701, tmp_path / "key", 4)]


def test_unreadable_answers_of_the_agents_container() -> None:
    assert "error 502" in str(agents_client._error(None, 502))
    assert "refused the worker's key" in str(agents_client._error({}, 401))
    for broken in (
        {"error": {"template": "Hello {name}", "params": {"other": 1}}},  # name missing
        {"error": {"template": "x", "params": ["a"]}},
        {"error": {"template": 3}},
    ):
        assert "answered with the error 500" in str(agents_client._error(broken, 500))
    error = agents_client._error(
        {"error": {"template": "Busy {n}", "params": {"n": 2}}, "transient": True}, 503
    )
    assert str(error) == "Busy 2" and error.transient
