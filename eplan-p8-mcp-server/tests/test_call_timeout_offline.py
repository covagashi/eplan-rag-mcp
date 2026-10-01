"""A blocking remoting call must never be able to wedge the server.

Every EplanRemoteClient call except Connect() is an unbounded synchronous call
into the CLR. When EPLAN cannot service one - a human mid-edit, a modal dialog,
a project that will not close - it never returns, and the whole MCP server goes
with it: every later tool queues behind the wedged call and the only recovery is
restarting the server. Measured live 2026-09-11, StopEplan() blocked for 8m44s.

These tests pin the bound. They are offline: no EPLAN, no CLR.
"""
import sys
import time
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_server"))

from eplan_connection import call_with_timeout  # noqa: E402


def test_returns_the_value_when_the_call_completes():
    value, timed_out, error = call_with_timeout(lambda: 42, 5)
    assert (value, timed_out, error) == (42, False, None)


def test_reports_the_exception_rather_than_swallowing_it():
    value, timed_out, error = call_with_timeout(lambda: 1 / 0, 5)
    assert value is None
    assert timed_out is False
    assert isinstance(error, ZeroDivisionError)


def test_arguments_are_passed_through():
    value, timed_out, error = call_with_timeout(lambda a, b=0: a + b, 5, 1, b=2)
    assert (value, timed_out, error) == (3, False, None)


def test_a_blocking_call_gives_up_instead_of_hanging():
    release = threading.Event()
    start = time.time()
    try:
        value, timed_out, error = call_with_timeout(release.wait, 0.25)
        elapsed = time.time() - start

        assert timed_out is True
        assert value is None and error is None
        # The point of the fix: bounded, not "eventually".
        assert elapsed < 5, f"waited {elapsed:.1f}s on a call that never returns"
    finally:
        release.set()


def test_the_stuck_worker_does_not_keep_the_process_alive():
    """
    The worker cannot be interrupted - it is stuck in native code - so it is
    left running on purpose. It must at least be a daemon, or the leak turns
    into a process that will not exit.
    """
    release = threading.Event()
    before = {t.name for t in threading.enumerate()}
    try:
        call_with_timeout(release.wait, 0.25)
        leaked = [t for t in threading.enumerate() if t.name not in before]
        assert leaked, "expected the stuck worker to still be running"
        assert all(t.daemon for t in leaked)
    finally:
        release.set()


@pytest.mark.parametrize("timeout", [0, 0.01])
def test_a_tiny_timeout_still_returns(timeout):
    release = threading.Event()
    try:
        _, timed_out, _ = call_with_timeout(release.wait, timeout)
        assert timed_out is True
    finally:
        release.set()
