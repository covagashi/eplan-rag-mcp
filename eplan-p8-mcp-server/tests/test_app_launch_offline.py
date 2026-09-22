"""
app_launch() - Audit #42 item 12.

GetActiveEplanServersOnLocalMachine (manager.get_active_servers()) reports no
PID, so it cannot tell a pre-existing EPLAN instance's remoting server apart
from the one app_launch just started. The old code discarded Popen's return
value (never learning the new pid), computed `already_running` and never used
it, then accepted whichever server/port turned up first - which, if another
EPLAN.exe was already up and reachable, was that instance, not the new one.

No EPLAN needed - subprocess.Popen, the pid/port helpers, and manager.get_active_servers
are all stubbed. A fake clock replaces time.sleep/time.time so the poll loop
resolves instantly regardless of wait_seconds.
"""

import os
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.join(os.path.dirname(HERE), "mcp_server")
for p in (MCP, os.path.join(MCP, "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

from api.actions import lifecycle  # noqa: E402


class _FakeClock:
    """time.time() returns an advancing counter; time.sleep() advances it."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _FakeClock()
    monkeypatch.setattr(lifecycle.time, "time", c.time)
    monkeypatch.setattr(lifecycle.time, "sleep", c.sleep)
    return c


@pytest.fixture
def fake_launch(monkeypatch):
    """Common plumbing: exe resolution and process spawn are stubbed."""
    monkeypatch.setattr(lifecycle, "_find_eplan_exe",
                        lambda version=None: (("C:/fake/eplan.exe", "2025"), None))
    proc = SimpleNamespace(pid=9999)
    monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda *a, **kw: proc)
    return proc


def _manager(connected=False, servers=None):
    return SimpleNamespace(
        connected=connected,
        ping=lambda: {"alive": False},
        get_active_servers=lambda: servers or [],
        connect=lambda port: {"success": True, "port": port},
    )


def test_nothing_running_before_uses_the_fast_path(fake_launch, clock, monkeypatch):
    """No pre-existing instance -> no ambiguity -> the original fast path."""
    monkeypatch.setattr(lifecycle, "get_manager",
                        lambda: _manager(servers=[{"port": "49153"}]))
    monkeypatch.setattr(lifecycle, "_eplan_pids", lambda: [])
    monkeypatch.setattr(lifecycle, "_eplan_listening_ports",
                        lambda only_pids=None, pids=None: pytest.fail("should not reach the filtered path"))
    result = lifecycle.app_launch(wait_seconds=10)
    assert result["success"] is True
    assert result["servers"] == [{"port": "49153"}]
    assert result["eplan_was_already_running"] is False
    assert result["new_pid"] == fake_launch.pid


def test_pre_existing_instance_is_not_connected_to(fake_launch, clock, monkeypatch):
    """
    The bug, reproduced: one EPLAN was already up on port 49152.
    get_active_servers() reports it (it has no way not to - no PID to filter
    by), but the fix must never trust that list when something was already
    running, and the new pid's own port never shows up.
    """
    monkeypatch.setattr(lifecycle, "get_manager",
                        lambda: _manager(servers=[{"port": "49152"}]))
    monkeypatch.setattr(lifecycle, "_eplan_pids", lambda: [1111])  # the old instance, unchanged
    seen_only_pids = []

    def fake_ports(only_pids=None, pids=None):
        seen_only_pids.append(set(only_pids) if only_pids is not None else None)
        return []  # the new instance's port never appears within the window

    monkeypatch.setattr(lifecycle, "_eplan_listening_ports", fake_ports)
    result = lifecycle.app_launch(wait_seconds=10)

    assert result["success"] is False
    assert result["eplan_was_already_running"] is True
    assert "1 EPLAN" in result["error"]
    assert "already running" in result["error"]
    # never blindly took servers[-1]["port"] == "49152" (the OLD instance)
    assert "servers" not in result or result.get("servers") == []
    # every filtered lookup was scoped to the new pid, never the old one
    assert seen_only_pids, "the filtered fallback was never even tried"
    for pids in seen_only_pids:
        assert 1111 not in pids
        assert fake_launch.pid in pids


def test_pre_existing_instance_new_port_is_found_and_used(fake_launch, clock, monkeypatch):
    """Same starting ambiguity, but this time the new process's own port shows
    up in time - that one must be used, filtered correctly by pid."""
    monkeypatch.setattr(lifecycle, "get_manager",
                        lambda: _manager(servers=[{"port": "49152"}]))
    monkeypatch.setattr(lifecycle, "_eplan_pids",
                        lambda: [1111, fake_launch.pid])  # old + the one we just started

    def fake_ports(only_pids=None, pids=None):
        assert only_pids is not None
        assert 1111 not in only_pids
        assert fake_launch.pid in only_pids
        return ["49999"]  # the NEW instance's real port

    monkeypatch.setattr(lifecycle, "_eplan_listening_ports", fake_ports)
    result = lifecycle.app_launch(wait_seconds=10)

    assert result["success"] is True
    assert result["fallback_ports"] == ["49999"]
    assert result["connect"]["port"] == "49999"


def test_nothing_running_before_and_nothing_ever_appears(fake_launch, clock, monkeypatch):
    monkeypatch.setattr(lifecycle, "get_manager", lambda: _manager(servers=[]))
    monkeypatch.setattr(lifecycle, "_eplan_pids", lambda: [])
    monkeypatch.setattr(lifecycle, "_eplan_listening_ports", lambda only_pids=None, pids=None: [])
    result = lifecycle.app_launch(wait_seconds=10)
    assert result["success"] is False
    assert result["eplan_was_already_running"] is False
    assert "Allow remote access" in result["error"]
