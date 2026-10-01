"""Windows process ownership/handle tests; never opens or terminates a real process."""
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import process_control as control


@pytest.mark.parametrize("listeners,expected", [
    ("TCP 0.0.0.0:49152 0.0.0.0:0 LISTENING 123", 123),
    ("TCP [::]:49152 [::]:0 LISTENING 123\nTCP 0.0.0.0:49152 0.0.0.0:0 LISTENING 123", 123),
    ("TCP 0.0.0.0:49153 0.0.0.0:0 LISTENING 123", None),
    ("TCP 0.0.0.0:49152 0.0.0.0:0 ESTABLISHED 123", None),
    ("TCP 127.0.0.1:49152 0.0.0.0:0 LISTENING 123\nTCP [::1]:49152 [::]:0 LISTENING 456", None),
])
def test_listener_requires_unique_owner(monkeypatch, listeners, expected):
    monkeypatch.setattr(control.subprocess, "run", Mock(return_value=SimpleNamespace(stdout=listeners)))
    if expected is None:
        with pytest.raises(RuntimeError):
            control._listener_pid("49152")
    else:
        assert control._listener_pid("49152") == expected


def test_listener_query_failure_is_not_empty_success(monkeypatch):
    monkeypatch.setattr(control.subprocess, "run", Mock(side_effect=OSError("query failed")))
    with pytest.raises(OSError):
        control._listener_pid(49152)


def test_remote_target_never_looks_at_local_processes(monkeypatch):
    lookup = Mock(side_effect=AssertionError("local lookup"))
    monkeypatch.setattr(control, "_listener_pid", lookup)
    with pytest.raises(RuntimeError, match="Remote"):
        control.connected_process("remote-workstation", 49152, True)
    lookup.assert_not_called()


def test_changed_owner_releases_handle(monkeypatch):
    process = SimpleNamespace(wait=Mock(return_value=False), close=Mock())
    monkeypatch.setattr(control, "_listener_pid", Mock(side_effect=[123, 456]))
    monkeypatch.setattr(control, "WindowsProcess", Mock(return_value=process))
    with pytest.raises(RuntimeError, match="changed"):
        control.connected_process("localhost", 49152)
    process.close.assert_called_once()


@pytest.fixture
def api(monkeypatch):
    def image_name(handle, flags, path, size):
        path.value = "C:/EPLAN/EPLAN.exe"
        return True
    api = SimpleNamespace(OpenProcess=Mock(return_value=98765),
                          QueryFullProcessImageNameW=Mock(side_effect=image_name),
                          WaitForSingleObject=Mock(return_value=258),
                          TerminateProcess=Mock(return_value=True), CloseHandle=Mock(return_value=True))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: api, raising=False)
    return api


@pytest.mark.parametrize("force", [False, True])
def test_rights_and_operations_are_bound_to_original_handle(api, force):
    process = control.WindowsProcess(123, allow_terminate=force)
    assert api.OpenProcess.call_args.args == (0x101000 | int(force), False, 123)
    assert not process.wait(0)
    api.WaitForSingleObject.return_value = 0
    assert process.wait(1)
    if force:
        process.terminate()
        api.TerminateProcess.assert_called_once_with(98765, 1)
    process.close()
    process.close()
    api.CloseHandle.assert_called_once_with(98765)
    assert all(call.args[0] == 98765 for call in api.WaitForSingleObject.call_args_list)


def test_non_eplan_owner_is_rejected_and_handle_closed(api):
    def wrong_image(handle, flags, path, size):
        path.value = "C:/other.exe"
        return True
    api.QueryFullProcessImageNameW.side_effect = wrong_image
    with pytest.raises(RuntimeError, match="not EPLAN"):
        control.WindowsProcess(123, True)
    api.CloseHandle.assert_called_once_with(98765)
    api.TerminateProcess.assert_not_called()


def test_already_exited_process_is_rejected(api):
    api.WaitForSingleObject.return_value = 0
    with pytest.raises(RuntimeError, match="already exited"):
        control.WindowsProcess(123)
    api.CloseHandle.assert_called_once_with(98765)
