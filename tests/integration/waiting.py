"""Bounded wait for a real HTTP server, started in a thread for the tests, to come up.

Without a timeout, a server that does not start (port already taken, application crashing at
startup) blocked the test until the global `pytest-timeout` deadline: five minutes of CI
lost, with no useful message.
"""

import threading
import time

import pytest
import uvicorn

STARTUP_TIMEOUT = 10.0  # seconds


def wait_until_started(
    server: uvicorn.Server, thread: threading.Thread, what: str, timeout: float = STARTUP_TIMEOUT
) -> None:
    """Wait until `server` listens; fail clearly after `timeout` seconds, or as soon as
    its thread stops (the server will never start)."""
    deadline = time.monotonic() + timeout
    while not server.started:
        if not thread.is_alive():
            pytest.fail(f"Le serveur {what} s'est arrêté avant d'avoir démarré.", pytrace=False)
        if time.monotonic() > deadline:
            pytest.fail(f"Le serveur {what} n'a pas démarré en {timeout:g} s.", pytrace=False)
        time.sleep(0.05)
