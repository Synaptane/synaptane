"""Sign-in attempt throttling (in memory, per process).

Three counters over a sliding window, keyed on the **resolved** account (spelling variants
and the email address count as the same account):
- per account and IP address: the ordinary lockout (10 attempts);
- per account, all addresses: against distributed guessing (50 attempts); a single address
  therefore cannot lock someone else's account;
- per address: against trying many accounts (30 attempts).
A successful sign-in resets the account's counters.

Bounded memory: beyond MAX_KEYS keys, every key is pruned of its events that left the window,
and empty keys are forgotten (previously only keys already empty were, yet a key only empties
when it is read again). The next threshold doubles with what remains: pruning costs a scan
from time to time, never one per attempt.
"""

import threading
import time
from collections import deque
from collections.abc import Callable

WINDOW = 15 * 60  # seconds
MAX_PER_ACCOUNT_AND_ADDRESS = 10
MAX_PER_ACCOUNT = 50
MAX_PER_ADDRESS = 30
MAX_KEYS = 10_000


def _trim(events: deque[float], now: float, window: float) -> deque[float]:
    while events and events[0] <= now - window:
        events.popleft()
    return events


class _Windows:
    """Recent events per key, over a sliding window, with amortized pruning."""

    def __init__(self, window: float) -> None:
        self.window = window
        self.events: dict[str, deque[float]] = {}
        self.prune_at = MAX_KEYS

    def count(self, key: str, now: float) -> deque[float]:
        """Recent events of `key`, without creating the key (a read costs nothing)."""
        events = self.events.get(key)
        if events is None:
            return deque()
        if not _trim(events, now, self.window):
            del self.events[key]
        return events

    def add(self, key: str, now: float) -> None:
        _trim(self.events.setdefault(key, deque()), now, self.window).append(now)
        if len(self.events) > self.prune_at:
            self.prune(now)

    def prune(self, now: float) -> None:
        for key in [k for k, v in self.events.items() if not _trim(v, now, self.window)]:
            del self.events[key]
        self.prune_at = max(MAX_KEYS, 2 * len(self.events))


class LoginThrottle:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._windows = _Windows(WINDOW)

    def wait(self, account: str, address: str) -> int:
        """Seconds to wait before a new attempt (0: allowed)."""
        now = self._clock()
        waits = []
        with self._lock:
            for key, limit in (
                (f"ua:{account}|{address}", MAX_PER_ACCOUNT_AND_ADDRESS),
                ("u:" + account, MAX_PER_ACCOUNT),
                ("a:" + address, MAX_PER_ADDRESS),
            ):
                events = self._windows.count(key, now)
                if len(events) >= limit:
                    waits.append(int(events[0] + WINDOW - now) + 1)
        return max(waits, default=0)

    def failed(self, account: str, address: str) -> None:
        now = self._clock()
        with self._lock:
            for key in (f"ua:{account}|{address}", "u:" + account, "a:" + address):
                self._windows.add(key, now)

    def succeeded(self, account: str) -> None:
        with self._lock:
            events = self._windows.events
            events.pop("u:" + account, None)
            for key in [k for k in events if k.startswith(f"ua:{account}|")]:
                del events[key]


class RateLimit:
    """At most `limit` events per key over `window` seconds (in memory, per process)."""

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit, self.window, self._clock = limit, window, clock
        self._lock = threading.Lock()
        self._windows = _Windows(window)

    def allow(self, key: str) -> bool:
        """True (and the event counts) if the key has not reached its limit."""
        now = self._clock()
        with self._lock:
            if len(self._windows.count(key, now)) >= self.limit:
                return False
            self._windows.add(key, now)
            return True
