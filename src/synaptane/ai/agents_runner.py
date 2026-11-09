"""`synaptane agents-runner`: the agents container, where the subscription CLIs run.

Claude Code and Codex run here, apart from the worker: this container (compose.yaml,
`agents`) mounts only the CLIs' logins (`/agents`, their `HOME`), receives no secret of the
instance, and is on two networks only: the one it shares with the worker alone, and an
outgoing one (Anthropic, OpenAI). Neither the data volume (master key, repositories), nor
the database, nor the web container are within its reach. A flaw in a CLI, or a prompt
injection that would get past its options, no longer reaches the rest of the server.

The worker sends each run (`agents_client`); this server:
- accepts only requests bearing the key shared with the worker (`SYNAPTANE_AGENTS_KEY_FILE`,
  read again at each request); `/healthz` alone answers without it, and says nothing;
- launches the CLI exactly as the worker would (`cli_agents.run_cli`: same options, same
  environment, throwaway working folder), pointed at the tool server of the worker, at the
  address the request comes from: a caller cannot point the CLI elsewhere;
- runs at most `max_runs` CLIs at a time, 1 by default (beyond: 503, the worker waits for its
  turn), each for
  at most `cli_agents.RUN_TIMEOUT`, and stops the CLI as soon as the worker hangs up;
- answers with the kept output of the CLI (bounded, `cli_agents.MAX_KEPT`), never with its
  logins;
- checks the isolation contract of its CLIs (`cli_contract`) at startup, here, where they run.
"""

import json
import logging
import os
import re
import secrets
import socket
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from synaptane import version_label
from synaptane.ai import cli_agents, cli_contract
from synaptane.ai.agents_client import read_key
from synaptane.ai.base import SUBSCRIPTION_PROVIDERS, AIError
from synaptane.core.errors import Label

log = logging.getLogger("synaptane.agents")

DEFAULT_PORT = 8700
# One CLI at a time by default: the CLIs of the agents container share its account, so one of
# them, compromised, could read the run token of another running at the same time (its
# environment in /proc, its MCP configuration) and use that run's tools, those of another
# space or organization. More at once trades that separation for less waiting.
DEFAULT_MAX_RUNS = 1
MAX_BODY = 16 * 1024 * 1024  # instructions and prompt of a run
POLL = 0.5  # seconds between two checks that the worker is still waiting
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
# Secrets of the instance that must never reach this container (compose.yaml).
SECRET_VARIABLES = (
    "SYNAPTANE_DATABASE_URL",
    "SYNAPTANE_METRICS_TOKEN",
    "POSTGRES_PASSWORD",
    "SYNAPTANE_BACKUP_AGE_RECIPIENTS",
    "SYNAPTANE_BACKUP_REMOTES",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


class Refused(Exception):
    def __init__(
        self, status: int, label: Label, transient: bool = False, busy: bool = False
    ) -> None:
        super().__init__(str(label))
        self.status, self.label, self.transient, self.busy = status, label, transient, busy


def _answer(error: Refused | AIError, status: int | None = None) -> JSONResponse:
    busy = isinstance(error, Refused) and error.busy
    if isinstance(error, Refused):
        label, transient, status = error.label, error.transient, error.status
    else:
        label, transient = error.label, error.transient
    answer = {"error": json.loads(label.dumps(limit=2000)), "transient": transient}
    if busy:
        answer["busy"] = True  # the worker waits for its turn (agents_client.wait_turn)
    return JSONResponse(answer, status_code=status or 422)


class _KeyGuard:
    """Refuses any request without the shared key (read again each time: a key re-created by
    the worker is taken into account without a restart). `/healthz` excepted."""

    def __init__(self, app: ASGIApp, key_file: Path) -> None:
        self.app, self.key_file = app, key_file

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        key = read_key(self.key_file)
        if key is None:
            refusal = Refused(
                503,
                Label(
                    "The agents container has no key yet: the worker creates it ({path}) "
                    "at its first call.",
                    path=str(self.key_file),
                ),
                transient=True,
            )
            await _answer(refusal)(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if not secrets.compare_digest(headers.get(b"authorization", b""), f"Bearer {key}".encode()):
            client = scope.get("client") or ("?", 0)
            log.warning("Request refused from %s: missing or wrong key.", client[0])
            await PlainTextResponse("", status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def _body(request: Request) -> dict[str, Any]:
    """JSON body of at most MAX_BODY bytes."""
    declared = request.headers.get("content-length")
    if declared and (not declared.isdigit() or int(declared) > MAX_BODY):
        raise Refused(413, Label("Request too large."))
    content = bytearray()
    async for chunk in request.stream():
        content += chunk
        if len(content) > MAX_BODY:
            raise Refused(413, Label("Request too large."))
    try:
        data = json.loads(content)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise Refused(400, Label("Invalid request: {detail}", detail="JSON object expected"))
    return data


def _invalid(detail: str) -> Refused:
    return Refused(400, Label("Invalid request: {detail}", detail=detail))


def _provider(data: dict[str, Any]) -> str:
    provider = data.get("provider")
    if provider not in SUBSCRIPTION_PROVIDERS:
        raise _invalid("unknown provider")
    return str(provider)


def exposures() -> list[str]:
    """What this container can reach and should not: secrets of the instance in its
    environment, the master key, the Docker socket, the database. Empty in compose.yaml;
    reported by `synaptane doctor` and the health page otherwise."""
    found = [f"variable {name}" for name in SECRET_VARIABLES if os.environ.get(name)]
    data_dir = Path(os.environ.get("SYNAPTANE_DATA_DIR") or "/data")
    for path in (data_dir / "master.key", Path("/data/master.key")):
        if os.access(path, os.R_OK):
            found.append(f"master key ({path})")
            break
    if Path("/var/run/docker.sock").exists():
        found.append("Docker socket (/var/run/docker.sock)")
    if cli_agents.in_docker():
        try:
            socket.getaddrinfo("postgres", 5432)
            found.append("database (postgres)")
        except OSError:
            pass
    return found


class Runner:
    """State of the agents container: run slots, isolation contract."""

    def __init__(self, key_file: Path, max_runs: int, check_contract: bool = True) -> None:
        self.key_file = key_file
        self.max_runs = max_runs
        self.slots = threading.BoundedSemaphore(max_runs)
        self.running = 0
        self.cancels: set[threading.Event] = set()  # one per run in progress
        self._lock = threading.Lock()
        self.contract: list[cli_contract.Report] | None = None
        self.exposures = exposures()
        for exposure in self.exposures:
            log.warning("The agents container is not isolated: it can reach the %s.", exposure)
        if check_contract:
            threading.Thread(
                target=self._check, daemon=True, name="synaptane-agents-contract"
            ).start()

    def _check(self) -> None:
        reports = cli_contract.check_all()
        for report in reports:
            if report.installed and report.problems:
                log.warning(
                    "Isolation contract broken for %s: %s", report.label, "; ".join(report.problems)
                )
        self.contract = reports

    def acquire(self) -> None:
        if not self.slots.acquire(blocking=False):
            raise Refused(
                503,
                Label(
                    "The agents container is already running as many CLIs as it may ({n}): "
                    "the run will be retried.",
                    n=self.max_runs,
                ),
                transient=True,
                busy=True,
            )
        with self._lock:
            self.running += 1

    def release(self) -> None:
        with self._lock:
            self.running -= 1
        self.slots.release()

    def stop_all(self) -> None:
        """The server stops: the CLIs in progress too (the worker will retry the runs)."""
        with self._lock:
            for cancel in self.cancels:
                cancel.set()


Handler = Callable[[Request], Awaitable[Response]]


def _guarded(handler: Handler) -> Handler:
    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except Refused as refusal:
            return _answer(refusal)
        except AIError as error:
            return _answer(error)

    return wrapped


async def _watch(request: Request, cancel: threading.Event, done: anyio.Event) -> None:
    """Sets `cancel` as soon as the worker hangs up (it gave up, or it stopped)."""
    while not done.is_set():
        if await request.is_disconnected():
            cancel.set()
            return
        await anyio.sleep(POLL)


def create_app(runner: Runner) -> ASGIApp:
    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def run(request: Request) -> Response:
        data = await _body(request)
        provider = _provider(data)
        model = data.get("model") or ""
        if not isinstance(model, str) or (model and not MODEL.fullmatch(model)):
            raise _invalid("model")
        system, prompt = data.get("system"), data.get("prompt")
        if not isinstance(system, str) or not isinstance(prompt, str):
            raise _invalid("system and prompt must be texts")
        port, timeout, token = data.get("port"), data.get("timeout"), data.get("token")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise _invalid("port")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise _invalid("token")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            raise _invalid("timeout")
        if request.client is None:
            raise _invalid("unknown caller address")
        # The tools are at the caller's address, never at an address of its choosing.
        url = f"http://{cli_agents.host_port(request.client.host, port)}/mcp"
        limit = min(timeout, cli_agents.RUN_TIMEOUT)
        runner.acquire()
        cancel, done = threading.Event(), anyio.Event()
        with runner._lock:
            runner.cancels.add(cancel)
        log.info("Run %s started (at most %d s).", provider, limit)
        results: list[cli_agents.Completed] = []
        failures: list[AIError] = []  # raised outside the task group: not as an ExceptionGroup
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(_watch, request, cancel, done)
                try:
                    results.append(
                        await anyio.to_thread.run_sync(
                            lambda: cli_agents.run_cli(
                                provider, model, system, prompt, url, token, limit, cancel
                            )
                        )
                    )
                except AIError as error:
                    failures.append(error)
                finally:
                    done.set()
                    group.cancel_scope.cancel()
        finally:
            with runner._lock:
                runner.cancels.discard(cancel)
            runner.release()
        if failures:
            log.info("Run %s not started: %s", provider, failures[0])
            raise failures[0]
        completed = results[0]
        outcome = (
            "interrupted (worker gone, or stopping)"
            if completed.cancelled
            else "timed out"
            if completed.timed_out
            else "output too large"
            if completed.overflow
            else f"code {completed.returncode}"
        )
        log.info("Run %s finished: %s.", provider, outcome)
        return JSONResponse(completed.to_json())

    async def status(request: Request) -> Response:
        provider = _provider(await _body(request))
        runner.acquire()
        try:
            message = await anyio.to_thread.run_sync(cli_agents.local_login_status, provider)
        finally:
            runner.release()
        return JSONResponse({"message": json.loads(message.dumps(limit=2000))})

    async def info(request: Request) -> Response:
        contract = runner.contract
        return JSONResponse(
            {
                "version": version_label(),
                "max_runs": runner.max_runs,
                "running": runner.running,
                "contract": None if contract is None else [asdict(r) for r in contract],
                "exposures": runner.exposures,
            }
        )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        runner.stop_all()

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", healthz),
            Route("/v1/run", _guarded(run), methods=["POST"]),
            Route("/v1/status", _guarded(status), methods=["POST"]),
            Route("/v1/info", _guarded(info), methods=["GET"]),
        ],
    )
    return _KeyGuard(app, runner.key_file)


def serve(host: str, port: int, key_file: Path, max_runs: int) -> None:
    """Runs the agents container's server until it is stopped."""
    cli_agents._RUNNER.set()
    runner = Runner(key_file, max_runs)
    log.info(
        "Agents container listening on %s (at most %d CLIs at a time).",
        cli_agents.host_port(host, port),
        max_runs,
    )
    uvicorn.run(
        create_app(runner),
        host=host,
        port=port,
        log_config=None,
        access_log=False,  # one line per run is enough; nothing of a request is logged
        timeout_graceful_shutdown=10,
    )
