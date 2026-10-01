"""Launch must connect only to the process it created, including concurrent starts."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from api.actions import lifecycle


@pytest.fixture
def setup(monkeypatch):
    proc = SimpleNamespace(pid=9999, poll=Mock(return_value=None))
    manager = SimpleNamespace(connected=False, disconnect=Mock(), connect=Mock(return_value={"success": True}),
                              get_active_servers=Mock(return_value=[{"port": "49153"}]))
    monkeypatch.setattr(lifecycle, "_find_eplan_exe", lambda v: (("C:/fake/EPLAN.exe", "2025"), None))
    monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(lifecycle, "get_manager", lambda: manager)
    monkeypatch.setattr(lifecycle, "_eplan_pids", lambda: [1111])
    now = [0]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return proc, manager


@pytest.mark.parametrize("already_running", [[], [1111], [1111, 2222]])
def test_only_spawned_process_ports_are_used(setup, monkeypatch, already_running):
    proc, manager = setup
    monkeypatch.setattr(lifecycle, "_eplan_pids", lambda: already_running)
    def ports(only_pids=None):
        assert only_pids == {9999}
        return ["49999"]
    monkeypatch.setattr(lifecycle, "_eplan_listening_ports", ports)
    result = lifecycle.app_launch(wait_seconds=10)
    assert result["success"]
    manager.connect.assert_called_once_with(port="49999")
    manager.get_active_servers.assert_not_called()


@pytest.mark.parametrize("ports", [[], ["49152", "49154"]])
def test_missing_or_ambiguous_owned_port_never_uses_other_instance(setup, monkeypatch, ports):
    _, manager = setup
    monkeypatch.setattr(lifecycle, "_eplan_listening_ports", lambda only_pids: ports)
    assert not lifecycle.app_launch(wait_seconds=10)["success"]
    manager.connect.assert_not_called()


def test_process_exits_before_endpoint_is_ready(setup, monkeypatch):
    proc, manager = setup
    proc.poll.return_value = 0
    monkeypatch.setattr(lifecycle, "_eplan_listening_ports", Mock(side_effect=AssertionError("dead process")))
    assert not lifecycle.app_launch(wait_seconds=10)["success"]
    manager.connect.assert_not_called()


def test_port_disappears_before_connect(setup, monkeypatch):
    _, manager = setup
    monkeypatch.setattr(lifecycle, "_eplan_listening_ports", Mock(side_effect=[["49152"], []]))
    assert not lifecycle.app_launch(wait_seconds=10)["success"]
    manager.connect.assert_not_called()


def test_port_changes_during_connect_are_not_reported_as_success(setup, monkeypatch):
    _, manager = setup
    monkeypatch.setattr(lifecycle, "_eplan_listening_ports", Mock(side_effect=[["49152"], ["49152"], []]))
    assert not lifecycle.app_launch(wait_seconds=10)["success"]
    manager.disconnect.assert_called_once()
