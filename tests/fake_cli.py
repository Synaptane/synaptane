"""Fake `claude` and `codex` for the tests (`fake_cli_program.py`): installed first in PATH,
with a fixture (`fake`), and helpers to serve the agents container in the test process."""

import json
import os
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PROGRAM = Path(__file__).with_name("fake_cli_program.py")


@dataclass
class Fake:
    """The fake CLIs installed for a test, and their `HOME`."""

    home: Path

    def configure(self, **settings: object) -> None:
        configure(self.home, **settings)

    def pids(self) -> list[int]:
        return pids(self.home)

    def last(self, name: str) -> object:
        return json.loads((self.home / f"last-{name}.json").read_text())


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    """Fake `claude` and `codex` first in PATH, `HOME` of the CLIs in the test's folder, no
    agents container (the CLI runs here unless the test says otherwise)."""
    folder = install(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SYNAPTANE_AGENT_HOME", str(home))
    monkeypatch.delenv("SYNAPTANE_AGENTS_URL", raising=False)
    monkeypatch.delenv("SYNAPTANE_AGENTS_KEY_FILE", raising=False)
    return Fake(home)


def install(folder: Path) -> Path:
    """Writes the fake `claude` and `codex` into `folder`; returns the folder."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "codex"):
        path = folder / name
        # This interpreter, rather than the program's `env python3` (other environments).
        body = PROGRAM.read_text().split("\n", 1)[1]
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return folder


def configure(home: Path, **settings: object) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "fake-cli.json").write_text(json.dumps(settings))


def pids(home: Path) -> list[int]:
    path = home / "pids"
    return [int(line) for line in path.read_text().split()] if path.exists() else []


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is dead too: it only waits for its parent to read its status.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


@contextmanager
def serving(app: Any) -> Iterator[str]:
    """Serves an ASGI application on 127.0.0.1 for the duration of the block; yields its
    address."""
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True, name="fake-agents")
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "server not started"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def wait_for(condition: Callable[[], bool], seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()
