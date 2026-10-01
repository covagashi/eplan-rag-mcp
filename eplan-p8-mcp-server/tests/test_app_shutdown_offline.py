"""Issue #56: process exit and force apply exclusively to the connected instance."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from api.actions import lifecycle


@pytest.fixture
def setup(monkeypatch):
    target = SimpleNamespace(pid=46696, wait=Mock(return_value=True), terminate=Mock(), close=Mock())
    manager = SimpleNamespace(host="localhost", port="49152", client=SimpleNamespace(StopEplan=Mock(return_value=True)),
                              disconnect=Mock())
    monkeypatch.setattr(lifecycle, "_get_connected_manager", lambda: (manager, None))
    monkeypatch.setattr(lifecycle, "connected_process", Mock(return_value=target))
    monkeypatch.setattr(lifecycle, "_eplan_pids", Mock(side_effect=[[46696, 16924], [16924]]))
    monkeypatch.setattr(lifecycle.subprocess, "run", Mock(side_effect=AssertionError("must not taskkill")))
    return target, manager


def test_clean_exit_with_other_instance_is_success(setup):
    target, _ = setup
    result = lifecycle.app_shutdown(wait_seconds=20)
    assert result["success"] and result["exit_verified"]
    assert result["target_pid"] == 46696
    assert result["other_instances"] == [16924]
    assert result["force_killed"] == []
    target.terminate.assert_not_called()
    target.wait.assert_called_once_with(20)
    target.close.assert_called_once()


def test_lingering_target_is_not_killed_by_default(setup):
    target, _ = setup
    target.wait.return_value = False
    result = lifecycle.app_shutdown(wait_seconds=0)
    assert not result["success"]
    assert "force=True" not in result["error"]
    target.terminate.assert_not_called()


def test_force_terminates_only_retained_target(setup):
    target, _ = setup
    target.wait.side_effect = [False, True]
    result = lifecycle.app_shutdown(force=True)
    assert result["success"] and result["force_killed"] == [46696]
    target.terminate.assert_called_once()
    assert result["other_instances"] == [16924]


@pytest.mark.parametrize("failure", ["termination", "verification"])
def test_failed_force_does_not_claim_kill(setup, failure):
    target, _ = setup
    target.wait.return_value = False
    if failure == "termination":
        target.terminate.side_effect = OSError("Access denied")
    result = lifecycle.app_shutdown(force=True)
    assert not result["success"] and result["force_killed"] == []
    target.close.assert_called_once()


def test_unknown_identity_still_requests_stop_but_never_claims_exit(setup, monkeypatch):
    target, manager = setup
    monkeypatch.setattr(lifecycle, "connected_process", Mock(side_effect=RuntimeError("unknown owner")))
    result = lifecycle.app_shutdown(force=True)
    assert not result["success"] and not result["exit_verified"]
    assert result["target_pid"] is None
    manager.client.StopEplan.assert_called_once()
    target.terminate.assert_not_called()


def test_failed_process_listing_is_not_proof_of_exit(setup, monkeypatch):
    target, _ = setup
    target.wait.return_value = False
    monkeypatch.setattr(lifecycle, "_eplan_pids", lambda: [])
    assert not lifecycle.app_shutdown(wait_seconds=0)["success"]


def test_stop_exception_can_still_have_verified_exit(setup):
    _, manager = setup
    manager.client.StopEplan.side_effect = RuntimeError("connection closed")
    result = lifecycle.app_shutdown()
    assert result["success"] and "stop_eplan_error" in result
    manager.disconnect.assert_called_once()


@pytest.mark.parametrize("exited", [True, False])
def test_restart_depends_only_on_target_exit(setup, monkeypatch, exited):
    target, _ = setup
    target.wait.return_value = exited
    launch = Mock(return_value={"success": True})
    monkeypatch.setattr(lifecycle, "app_launch", launch)
    result = lifecycle.app_restart(reopen_project=False)
    assert result["success"] == exited
    assert launch.call_count == int(exited)
