"""The worker's side of the agents container: it sends it each subscription run.

With `SYNAPTANE_AGENTS_URL` (compose.yaml: `http://agents:8700`), the subscription CLIs no
longer run in the worker but in the `agents` container (`agents_runner`), which has neither
the data volume, nor the database, nor any secret of the instance. For a run, the worker:
1. starts the run's tool server (`cli_agents.tool_server`) on its own address on the network
   it shares with the agents container only (`callback_host`);
2. sends the agents container the run (provider, model, instructions, prompt, port and token
   of the tool server) and waits for its result: the CLI's kept output, bounded;
3. reads that result exactly as if the CLI had run here (`cli_agents.interpret`).

Authentication: a key shared by the worker and the agents container alone, in a file
(`SYNAPTANE_AGENTS_KEY_FILE`) of a volume mounted in these two containers only, written by
the worker the first time (`ensure_key`). It is never logged nor shown. The network adds to
it: the web container, exposed to the Internet, is not on the agents network (compose.yaml).

The worker also reports, in the data folder, the state of the agents container (reachable,
CLI versions, isolation contract): `synaptane doctor` and the health page, which run in the
web container and do not reach it, read it there (`read_state`).
"""

import json
import logging
import os
import random
import re
import secrets
import socket
import string
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

from synaptane.ai.base import AIError
from synaptane.ai.cli_agents import AGENTS_URL, Completed, agents_url
from synaptane.core.errors import Label

log = logging.getLogger("synaptane.ai.agents")

KEY_FILE = "SYNAPTANE_AGENTS_KEY_FILE"
KEY_MIN = 32  # characters of a key read from the file; shorter: refused
CONNECT_TIMEOUT = 10.0
MARGIN = 60.0  # seconds granted to the agents container beyond the run's own time limit
STATUS_TIMEOUT = 300.0  # login state: `auth status`, then a real mini-call (120 s at most)
INFO_TIMEOUT = 20.0
# Largest answer read from the agents container: its kept output is bounded (MAX_KEPT) and so
# are the tails; beyond, the answer is not one of its own.
MAX_ANSWER = 8 * 1024 * 1024
STATE_FILE = ".agents-state.json"
# The worker reports every REFRESH (sooner while something is wrong); a report older than
# STALE means the worker no longer does.
REFRESH = timedelta(minutes=10)
RETRY = timedelta(minutes=1)
STALE = timedelta(minutes=30)
CONTAINER, LOCAL = "container", "local"


# --- Shared key --------------------------------------------------------------------------------


def key_file() -> Path:
    value = os.environ.get(KEY_FILE, "").strip()
    if not value:
        raise AIError(
            "{url} is set but {key} is not: the worker cannot authenticate to the agents "
            "container (compose.yaml).",
            url=AGENTS_URL,
            key=KEY_FILE,
        )
    return Path(value)


def read_key(path: Path) -> str | None:
    """The shared key, or None if the file is missing, unreadable or too short."""
    try:
        key = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key if len(key) >= KEY_MIN else None


def ensure_key(path: Path) -> str:
    """The shared key, created the first time (by the worker: the agents container mounts the
    volume read-only). Written to a temporary file then linked into place: a reader never sees
    a half-written key, and of two workers starting at once, only one key wins."""
    if (key := read_key(path)) is not None:
        return key
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    # 0o440: readable by the group, for an installation without Docker where the runner runs
    # under another system account of the same group.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_urlsafe(48) + "\n")
        with suppress(FileExistsError):
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    key = read_key(path)
    if key is None:
        raise AIError("The key of the agents container cannot be read: {path}", path=str(path))
    return key


# --- Calls -----------------------------------------------------------------------------------


def callback_host(base: str) -> str:
    """The worker's address as seen by the agents container: the source address of the route
    to it (a UDP "connection" sends nothing). The tool server listens on that address only."""
    parts = urlsplit(base)
    host = parts.hostname or ""
    try:
        found = socket.getaddrinfo(host, parts.port or 80, type=socket.SOCK_DGRAM)
        family, _, _, _, address = found[0]
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect(address)
            local: str = probe.getsockname()[0]
    except OSError as error:
        raise AIError(
            "The agents container cannot be reached ({url}): {error}.",
            transient=True,
            url=base,
            error=error.strerror or type(error).__name__,
        ) from None
    return local


MAX_TEMPLATE = 2000
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,49}")
# Control characters (terminal escapes included), line breaks and tabs excepted.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _text(value: object, size: int) -> str:
    """A text of the agents container, cut to `size` characters, without control characters
    (`synaptane doctor` prints it in a terminal)."""
    return _CONTROL.sub("", str(value))[:size]


def _texts(value: object, size: int, count: int) -> tuple[str, ...]:
    return tuple(_text(v, size) for v in value[:count]) if isinstance(value, list) else ()


def _plain(template: str) -> bool:
    """Only `{name}` fields: no attribute, index, conversion or format specification, which a
    template coming from another container could use to read an object's attributes or to
    make a huge text (`{x:>999999999}`) wherever it is shown."""
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError:
        return False
    return all(
        name is None or (_FIELD.fullmatch(name) is not None and not spec and conversion is None)
        for _, name, spec, conversion in fields
    )


def _label(data: object) -> Label:
    """A message of the agents container (`Label.dumps` form); ValueError otherwise. Its
    template is formatted here and in the web container: plain fields and values only."""
    if not isinstance(data, dict) or not isinstance(data.get("template"), str):
        raise ValueError("not a message")
    template, params = data["template"], data.get("params") or {}
    if not isinstance(params, dict) or len(template) > MAX_TEMPLATE or not _plain(template):
        raise ValueError("not a message")
    values: dict[str, object] = {}
    for key, value in params.items():
        if not isinstance(value, str | int | float):
            raise ValueError("not a message")
        values[str(key)] = _text(value, MAX_TEMPLATE) if isinstance(value, str) else value
    try:
        return Label(_text(template, MAX_TEMPLATE), **values)
    except (KeyError, IndexError, ValueError, TypeError):
        raise ValueError("not a message") from None


class Busy(AIError):
    """The agents container already runs as many CLIs as it may: wait for a turn."""


def _error(data: object, status: int) -> AIError:
    """Error answered by the agents container (message to translate), or a generic one."""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        try:
            label = _label(data["error"])
            kind = Busy if data.get("busy") is True else AIError
            return kind(label.template, transient=bool(data.get("transient")), **label.params)
        except ValueError:
            pass  # not a message of the agents container: the generic error below
    if status == 401:
        return AIError(
            "The agents container refused the worker's key: both must read the same file ({key}).",
            key=KEY_FILE,
        )
    return AIError("The agents container answered with the error {status}.", status=status)


def _call(method: str, path: str, body: dict[str, Any] | None, timeout: float) -> Any:
    base = agents_url()
    if base is None:
        raise AIError("{url} is not set.", url=AGENTS_URL)
    stored = key_file()
    try:
        key = ensure_key(stored)
    except OSError as error:
        raise AIError(
            "The key of the agents container cannot be written ({path}): {error}.",
            path=str(stored),
            error=error.strerror or type(error).__name__,
        ) from None
    try:
        with (
            httpx2.Client(
                timeout=httpx2.Timeout(timeout, connect=CONNECT_TIMEOUT),
                follow_redirects=False,
                trust_env=False,  # no HTTP proxy between the worker and the agents container
            ) as client,
            client.stream(
                method, base + path, json=body, headers={"Authorization": f"Bearer {key}"}
            ) as response,
        ):
            content = bytearray()
            for chunk in response.iter_bytes():
                content += chunk
                if len(content) > MAX_ANSWER:
                    raise AIError(
                        "The answer of the agents container exceeds {size} MB.",
                        size=MAX_ANSWER // (1024 * 1024),
                    )
            status = response.status_code
    except httpx2.TimeoutException:
        raise AIError("The agents container did not answer in time.", transient=True) from None
    except httpx2.HTTPError as error:
        raise AIError(
            "The agents container cannot be reached ({url}): {error}. Is it running (docker "
            "compose ps agents)?",
            transient=True,
            url=base,
            error=type(error).__name__,
        ) from None
    try:
        data = json.loads(content) if content else None
    except ValueError:
        data = None
    if status != 200:
        raise _error(data, status)
    return data


# Waiting for a turn when the agents container is busy: a new try every BUSY_RETRY seconds
# (plus a random share, so that several workers do not come back together).
BUSY_RETRY = 10.0
# Longest wait of a connection test for each of its two calls (login check, then the run): it
# blocks the worker's fast lane (Git sync), so it does not wait for a tidy-up to end.
TEST_WAIT = 30.0


def wait_turn[T](call: Callable[[], T], wait: float) -> T:
    """`call`, tried again while the agents container is busy, for at most `wait` seconds:
    a run is not failed (nor counted as an attempt) because another one is in progress; past
    `wait`, the Busy error goes up, transient, and the job is retried later."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return call()
        except Busy:
            pause = BUSY_RETRY + random.uniform(0, BUSY_RETRY / 2)
            if time.monotonic() + pause > deadline:
                raise
            log.info("Agents container busy: next try in %.0f s.", pause)
            time.sleep(pause)


def run(
    provider: str,
    model: str,
    system: str,
    prompt: str,
    port: int,
    token: str,
    timeout: float,
    wait: float | None = None,
) -> Completed:
    """A run in the agents container; its CLI reaches the tool server at this worker's
    address, on `port`. Blocks until the end of the run; if the worker gives up (time limit,
    stop), the connection closes and the agents container stops the CLI. `wait`: longest
    wait for a turn, `timeout` by default."""
    data = wait_turn(
        lambda: _call(
            "POST",
            "/v1/run",
            {
                "provider": provider,
                "model": model,
                "system": system,
                "prompt": prompt,
                "port": port,
                "token": token,
                "timeout": int(timeout),
            },
            timeout + MARGIN,
        ),
        # By default as long as one run of this length: the one in progress has ended by then.
        timeout if wait is None else wait,
    )
    try:
        return Completed.from_json(data)
    except ValueError:
        raise AIError("The answer of the agents container cannot be read.") from None


def status(provider: str) -> Label:
    """Login state of a CLI, in the agents container (`cli_agents.local_login_status`)."""
    data = wait_turn(
        lambda: _call("POST", "/v1/status", {"provider": provider}, STATUS_TIMEOUT), TEST_WAIT
    )
    try:
        return _label(data.get("message") if isinstance(data, dict) else None)
    except ValueError:
        raise AIError("The answer of the agents container cannot be read.") from None


def info() -> dict[str, Any]:
    """What the agents container says of itself: version, CLIs, isolation contract, what it
    can still reach that it should not (`agents_runner.exposures`)."""
    data = _call("GET", "/v1/info", None, INFO_TIMEOUT)
    if not isinstance(data, dict):
        raise AIError("The answer of the agents container cannot be read.")
    return data


# --- State reported to the web container ------------------------------------------------------


@dataclass(frozen=True)
class CLIState:
    binary: str
    label: str
    installed: bool
    version: str = ""
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class State:
    """Where the subscription CLIs run, as the worker last saw it."""

    mode: str  # CONTAINER or LOCAL
    checked_at: datetime | None = None
    url: str = ""
    reachable: bool = False
    error: Label = Label("")
    version: str = ""
    clis: tuple[CLIState, ...] = ()
    contract_pending: bool = False  # the agents container has not finished checking yet
    exposures: tuple[str, ...] = ()  # what it can reach and should not

    @property
    def container(self) -> bool:
        return self.mode == CONTAINER

    @property
    def contract_broken(self) -> bool:
        return any(cli.installed and cli.problems for cli in self.clis)


def _clis(value: object) -> tuple[CLIState, ...]:
    found = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            found.append(
                CLIState(
                    binary=_text(item.get("binary", ""), 50),
                    label=_text(item.get("label", ""), 100),
                    installed=bool(item.get("installed")),
                    version=_text(item.get("version", ""), 100),
                    problems=_texts(item.get("problems"), 1000, 20),
                )
            )
    return tuple(found)


def probe_state() -> State:
    """State seen from this process: asked to the agents container if this process has its
    address (the worker), otherwise the CLI runs here."""
    now = datetime.now(UTC)
    base = agents_url()
    if base is None:
        return State(LOCAL, now)
    try:
        data = info()
    except AIError as error:
        return State(CONTAINER, now, base, error=error.label)
    contract = data.get("contract")
    return State(
        CONTAINER,
        now,
        base,
        reachable=True,
        version=_text(data.get("version", ""), 100),
        clis=_clis(contract),
        contract_pending=contract is None,
        exposures=_texts(data.get("exposures"), 300, 20),
    )


def write_state(data_dir: Path, state: State) -> None:
    """Into the data folder, shared with the web container (written then renamed)."""
    record = asdict(state)
    record["checked_at"] = state.checked_at.isoformat() if state.checked_at else None
    record["error"] = state.error.dumps() if state.error else ""
    target = data_dir / STATE_FILE
    temporary = target.with_name(target.name + ".tmp")
    with suppress(OSError):
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)


def read_state(data_dir: Path) -> State | None:
    """Last state reported by the worker; None if it never did (or an unreadable file)."""
    try:
        data = json.loads((data_dir / STATE_FILE).read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(data["checked_at"]) if data.get("checked_at") else None
        mode = data["mode"]
        error = data.get("error") or ""
        label = _label(json.loads(error)) if error else Label("")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if mode not in (CONTAINER, LOCAL) or (checked is not None and checked.tzinfo is None):
        return None
    return State(
        mode,
        checked,
        _text(data.get("url", ""), 300),
        bool(data.get("reachable")),
        label,
        _text(data.get("version", ""), 100),
        _clis(data.get("clis")),
        bool(data.get("contract_pending")),
        _texts(data.get("exposures"), 300, 20),
    )


def refresh(data_dir: Path) -> State:
    """Asks the agents container and records its state (worker maintenance)."""
    state = probe_state()
    write_state(data_dir, state)
    if state.container and not state.reachable:
        log.warning("Agents container unreachable: %s", state.error)
    return state
