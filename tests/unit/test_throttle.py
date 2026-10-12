import pytest

from synaptane.core import throttle as throttle_module
from synaptane.core.throttle import (
    MAX_PER_ACCOUNT,
    MAX_PER_ACCOUNT_AND_ADDRESS,
    MAX_PER_ADDRESS,
    WINDOW,
    LoginThrottle,
    RateLimit,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_account_is_blocked_from_one_address_then_released() -> None:
    clock = Clock()
    throttle = LoginThrottle(clock)
    for _ in range(MAX_PER_ACCOUNT_AND_ADDRESS):
        assert throttle.wait("id:alice", "10.0.0.1") == 0
        throttle.failed("id:alice", "10.0.0.1")
    assert throttle.wait("id:alice", "10.0.0.1") > 0
    # A single address does not lock the account for the others (no denial of service).
    assert throttle.wait("id:alice", "10.9.9.9") == 0
    clock.now += WINDOW + 1
    assert throttle.wait("id:alice", "10.0.0.1") == 0


def test_distributed_guessing_is_blocked() -> None:
    throttle = LoginThrottle(Clock())
    for n in range(MAX_PER_ACCOUNT):
        throttle.failed("id:alice", f"10.0.{n // 250}.{n % 250}")
    assert throttle.wait("id:alice", "10.9.9.9") > 0  # same account, all addresses
    assert throttle.wait("id:bob", "10.9.9.9") == 0


def test_address_is_blocked_across_accounts() -> None:
    throttle = LoginThrottle(Clock())
    for n in range(MAX_PER_ADDRESS):
        throttle.failed(f"login:user{n}", "203.0.113.7")
    assert throttle.wait("login:nouveau", "203.0.113.7") > 0
    assert throttle.wait("login:nouveau", "203.0.113.8") == 0


def test_success_resets_the_account() -> None:
    throttle = LoginThrottle(Clock())
    for _ in range(MAX_PER_ACCOUNT_AND_ADDRESS - 1):
        throttle.failed("id:alice", "10.0.0.1")
    throttle.succeeded("id:alice")
    throttle.failed("id:alice", "10.0.0.1")
    assert throttle.wait("id:alice", "10.0.0.1") == 0


def test_old_active_keys_are_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempts on varied names and addresses do not grow memory endlessly, even if no key
    is read again (so never emptied by a read)."""
    monkeypatch.setattr(throttle_module, "MAX_KEYS", 30)
    clock = Clock()
    throttle = LoginThrottle(clock)
    for n in range(10):
        throttle.failed(f"login:ancien{n}", f"10.0.0.{n}")
    clock.now += WINDOW + 1
    for n in range(40):
        throttle.failed(f"login:nouveau{n}", "10.1.1.1")
    remaining = throttle._windows.events
    assert not any("ancien" in key or "10.0.0." in key for key in remaining)
    assert len(remaining) == 40 * 2 + 1  # only recent keys remain
    assert throttle.wait("login:autre", "10.1.1.1") > 0  # recent attempts still count


def test_reading_creates_no_key() -> None:
    throttle = LoginThrottle(Clock())
    throttle.wait("login:personne", "10.0.0.1")
    assert throttle._windows.events == {}


def test_rate_limit_forgets_old_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(throttle_module, "MAX_KEYS", 5)
    clock = Clock()
    limit = RateLimit(1, 60, clock)
    for n in range(5):
        assert limit.allow(f"ancien{n}")
    clock.now += 61
    for n in range(6):
        assert limit.allow(f"nouveau{n}")
    assert not any(key.startswith("ancien") for key in limit._windows.events)
    assert not limit.allow("nouveau0")
