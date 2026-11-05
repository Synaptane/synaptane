"""Prometheus metrics: the text format, the requests answered by this process, and the
process itself (CPU, memory, open files).

No client library: the text format is a few lines per metric
(https://prometheus.io/docs/instrumenting/exposition_formats/). The web server is a single
process (`synaptane serve`), so the request counters kept in memory are those of the whole
instance; they start again from zero when it restarts, as Prometheus expects of a counter.
"""

import math
import os
import threading
import time
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from pathlib import Path

from starlette.types import ASGIApp, Message, Receive, Scope, Send

Labels = Mapping[str, str]
# Upper bounds of the request duration buckets, in seconds.
BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
# Probes and the metrics themselves: not counted, they would drown the rest.
UNCOUNTED = ("/metrics", "/healthz", "/readyz")
OAUTH = ("/authorize", "/token", "/register", "/revoke", "/.well-known/", "/oauth/")
STARTED = time.time()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _labels(labels: Labels) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


class Exposition:
    """The text of `/metrics`, one metric family after the other."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(
        self, name: str, kind: str, help_text: str, samples: Iterable[tuple[Labels, float]]
    ) -> None:
        """A family (`gauge`, or `counter` whose name ends in `_total`) and its samples; none
        is allowed."""
        self.lines.append(f"# HELP {name} {_escape(help_text)}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            self.lines.append(f"{name}{_labels(labels)} {_number(value)}")

    def one(self, name: str, kind: str, help_text: str, value: float, **labels: str) -> None:
        self.add(name, kind, help_text, [(labels, value)])

    def histogram(
        self, name: str, help_text: str, series: Iterable[tuple[Labels, "Histogram"]]
    ) -> None:
        self.lines.append(f"# HELP {name} {_escape(help_text)}")
        self.lines.append(f"# TYPE {name} histogram")
        for labels, histogram in series:
            total = 0
            for bound, count in zip((*BUCKETS, math.inf), histogram.counts, strict=True):
                total += count
                bucket = {**labels, "le": _number(bound)}
                self.lines.append(f"{name}_bucket{_labels(bucket)} {total}")
            self.lines.append(f"{name}_sum{_labels(labels)} {_number(histogram.sum)}")
            self.lines.append(f"{name}_count{_labels(labels)} {total}")

    def extend(self, other: "Exposition") -> None:
        self.lines.extend(other.lines)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


class Histogram:
    def __init__(self) -> None:
        self.counts = [0] * (len(BUCKETS) + 1)  # the last one: beyond the largest bound
        self.sum = 0.0

    def observe(self, seconds: float) -> None:
        self.counts[bisect_left(BUCKETS, seconds)] += 1
        self.sum += seconds


def area(path: str) -> str:
    """Part of the application a request is for: few values, so few series."""
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    if path.startswith("/api/"):
        return "api"
    if path.startswith("/static/"):
        return "static"
    if path.startswith(OAUTH):
        return "oauth"
    return "web"


class Requests:
    """Requests answered by this process: count by area and status class, and duration."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str], int] = {}
        self._durations: dict[str, Histogram] = {}

    def record(self, path: str, status: int, seconds: float) -> None:
        where = area(path)
        code = f"{status // 100}xx"
        with self._lock:
            self._counts[where, code] = self._counts.get((where, code), 0) + 1
            self._durations.setdefault(where, Histogram()).observe(seconds)

    def write(self, out: Exposition) -> None:
        with self._lock:
            counts = sorted(self._counts.items())
            durations = [
                ({"area": where}, _copy(histogram))
                for where, histogram in sorted(self._durations.items())
            ]
        out.add(
            "synaptane_http_requests_total",
            "counter",
            "HTTP requests answered, by area (web, api, mcp, oauth, static) and status class.",
            [({"area": where, "code": code}, count) for (where, code), count in counts],
        )
        out.histogram(
            "synaptane_http_request_duration_seconds",
            "Time to answer an HTTP request, by area.",
            durations,
        )


def _copy(histogram: Histogram) -> Histogram:
    copy = Histogram()
    copy.counts, copy.sum = list(histogram.counts), histogram.sum
    return copy


class MeasuredRequests:
    """ASGI middleware: counts each request and its duration (until the response is sent)."""

    def __init__(self, app: ASGIApp, requests: Requests) -> None:
        self.app, self.requests = app, requests

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path in UNCOUNTED:
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500  # an exception that escapes: the server answers 500

        async def sending(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, sending)
        finally:
            self.requests.record(path, status, time.perf_counter() - start)


def write_process(out: Exposition) -> None:
    """The usual `process_*` metrics: CPU, resident memory (Linux), open files (Linux)."""
    out.one(
        "process_cpu_seconds_total", "counter", "CPU time used by the process.", time.process_time()
    )
    out.one(
        "process_start_time_seconds",
        "gauge",
        "Start of the process, in seconds since the Unix epoch.",
        STARTED,
    )
    try:
        resident = int(Path("/proc/self/statm").read_text().split()[1])
        out.one(
            "process_resident_memory_bytes",
            "gauge",
            "Resident memory of the process.",
            resident * os.sysconf("SC_PAGE_SIZE"),
        )
        out.one(
            "process_open_fds", "gauge", "Open file descriptors.", len(os.listdir("/proc/self/fd"))
        )
    except (OSError, ValueError, IndexError):  # not Linux
        pass
