"""Corner-case tests for eplan_connection.py and api/actions/_base.py
that need no EPLAN: cs_escape completeness, version detection against a fake
install tree, singleton semantics, action parsing, and the script-wrapping
flow via a fake client."""

import os
import re

import pytest

import eplan_connection
from api.actions._base import _build_action


# ---------------------------------------------------------------------------
# cs_escape completeness
# ---------------------------------------------------------------------------

def test_cs_escape_none_and_non_string():
    assert eplan_connection.cs_escape(None) == ""
    assert eplan_connection.cs_escape(123) == "123"
    assert eplan_connection.cs_escape(True) == "True"


def test_cs_escape_unicode_line_terminators():
    # C# treats U+0085 (NEL), U+2028 (LS) and U+2029 (PS) as line terminators
    # in source code, so leaving them raw lets a value break out of its string
    # literal exactly like a raw newline would.
    nel, ls, ps = chr(0x85), chr(0x2028), chr(0x2029)
    out = eplan_connection.cs_escape(f"a{nel}b{ls}c{ps}d")
    assert nel not in out and ls not in out and ps not in out
    assert out == "a" + r"\u0085" + "b" + r"\u2028" + "c" + r"\u2029" + "d"


def test_cs_escape_leaves_printable_unicode_alone():
    assert eplan_connection.cs_escape("Größe µ 100%") == "Größe µ 100%"


# ---------------------------------------------------------------------------
# detect_installed_versions against a fake installation tree
# ---------------------------------------------------------------------------

def _make_install(root, full_version, coreclr=False):
    bin_dir = os.path.join(root, full_version, "Bin")
    os.makedirs(bin_dir)
    open(os.path.join(bin_dir, "Eplan.EplApi.RemoteClientu.dll"), "w").close()
    if coreclr:
        open(os.path.join(bin_dir, "Grpc.Net.Client.dll"), "w").close()


def test_detect_installed_versions_fake_tree(tmp_path, monkeypatch):
    root = str(tmp_path)
    _make_install(root, "2026.0.5")
    _make_install(root, "2026.0.10")          # newer patch of the same major
    _make_install(root, "2027.1.2", coreclr=True)
    os.makedirs(os.path.join(root, "NotAnInstall"))  # no marker DLL -> ignored

    monkeypatch.setattr(eplan_connection, "PLATFORM_ROOT", root)
    installs = eplan_connection.detect_installed_versions()

    assert [i["version"] for i in installs] == ["2027", "2026"]
    assert installs[0]["runtime"] == "coreclr"
    by_major = {i["version"]: i for i in installs}
    # numeric compare: 0.10 > 0.5 (string compare would get this wrong)
    assert by_major["2026"]["full_version"] == "2026.0.10"
    assert by_major["2026"]["runtime"] == "netfx"


def test_detect_installed_versions_missing_root(monkeypatch, tmp_path):
    monkeypatch.setattr(eplan_connection, "PLATFORM_ROOT",
                        str(tmp_path / "does-not-exist"))
    assert eplan_connection.detect_installed_versions() == []


# ---------------------------------------------------------------------------
# get_manager singleton semantics
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_singleton(monkeypatch):
    monkeypatch.setattr(eplan_connection, "_manager", None)


def test_get_manager_returns_same_instance(fresh_singleton):
    assert eplan_connection.get_manager() is eplan_connection.get_manager()


def test_get_manager_retargets_before_dlls_loaded(fresh_singleton):
    first = eplan_connection.get_manager("2026")
    second = eplan_connection.get_manager("2027")
    assert second is not first
    assert second.target_version == "2027"


def test_get_manager_keeps_loaded_version(fresh_singleton):
    first = eplan_connection.get_manager("2026")
    first._clr_initialized = True  # simulate DLLs already loaded
    second = eplan_connection.get_manager("2027")
    assert second is first
    assert second.target_version == "2026"


# ---------------------------------------------------------------------------
# execute_action plumbing via a fake client
# ---------------------------------------------------------------------------

class RecordingClient:
    SynchronousMode = False

    def __init__(self):
        self.actions = []
        self.captured_cs = None

    def ExecuteAction(self, action):
        self.actions.append(action)
        m = re.search(r'/ScriptFile:"([^"]+)"', action)
        if action.startswith("ExecuteScript") and m:
            with open(m.group(1), encoding="utf-8") as f:
                content = f.read()
            self.captured_cs = content
            rm = re.search(r'File\.WriteAllText\("([^"]+)"', content)
            result_path = rm.group(1).replace("\\\\", "\\")
            with open(result_path, "w", encoding="utf-8") as f:
                f.write('{"success": true, "parameters": {}}')


def _manager_with_fake():
    mgr = eplan_connection.EPLANConnectionManager()
    mgr.connected = True
    fake = RecordingClient()
    mgr.client = fake
    return mgr, fake


def test_execute_action_not_connected():
    mgr = eplan_connection.EPLANConnectionManager()
    result = mgr.execute_action("anything", quiet_mode=True)
    assert result == {"success": False, "message": "Not connected"}


def test_execute_action_direct_mode_bypasses_script():
    mgr, fake = _manager_with_fake()
    result = mgr.execute_action("XPrjActionProjectOpen /Project:x", quiet_mode=False)
    assert result["success"]
    assert fake.actions == ["XPrjActionProjectOpen /Project:x"]
    assert fake.SynchronousMode is True
    assert fake.captured_cs is None


@pytest.mark.parametrize("plumbing", ["RegisterScript", "ExecuteScript", "UnregisterScript"])
def test_script_plumbing_never_wrapped_even_in_quiet_mode(plumbing):
    # Wrapping these would recurse infinitely.
    mgr, fake = _manager_with_fake()
    mgr.execute_action(f'{plumbing} /ScriptFile:"X.cs"', quiet_mode=True)
    assert fake.actions == [f'{plumbing} /ScriptFile:"X.cs"']


def test_quiet_mode_parses_quoted_empty_and_plain_params():
    mgr, fake = _manager_with_fake()
    mgr.execute_action('act /A:"hello world" /B:plain /C:""', quiet_mode=True)
    cs = fake.captured_cs
    assert 'acc.AddParameter("A", "hello world");' in cs
    assert 'acc.AddParameter("B", "plain");' in cs
    assert 'acc.AddParameter("C", "");' in cs


def test_quiet_mode_generated_script_cleaned_up():
    mgr, fake = _manager_with_fake()
    mgr.execute_action('someAction /A:"x"', quiet_mode=True)
    base = os.path.dirname(os.path.abspath(eplan_connection.__file__))
    leftovers = [f for f in os.listdir(os.path.join(base, "scripts", "generated"))
                 if f.startswith("exec_action_")]
    results = [f for f in os.listdir(os.path.join(base, "scripts", "results"))
               if f.startswith("exec_result_")]
    assert leftovers == []
    assert results == []


def test_quiet_mode_never_registers_oneshot_script():
    # The generated wrapper has only a [Start] method. RegisterScript is for
    # persistent [DeclareAction]/[DeclareEventHandler]/[DeclareMenu] hooks and
    # answers a [Start]-only script with a modal "The script does not contain
    # attributes for loading." dialog - a blocking popup per call, which defeats
    # the point of QuietMode. ExecuteScript alone runs [Start].
    mgr, fake = _manager_with_fake()
    mgr.execute_action('someAction /A:"x"', quiet_mode=True)
    assert any(a.startswith("ExecuteScript") for a in fake.actions)
    assert not any(a.startswith("RegisterScript") for a in fake.actions)
    assert not any(a.startswith("UnregisterScript") for a in fake.actions)


def test_legacy_template_via_env(monkeypatch):
    monkeypatch.setenv("EPLAN_MCP_LEGACY_CLI", "1")
    mgr, fake = _manager_with_fake()
    result = mgr.execute_action('someAction /A:"x"', quiet_mode=True)
    assert result["success"]
    assert "cli-legacy" in fake.captured_cs
    assert "FindAction" not in fake.captured_cs


def test_quiet_mode_line_terminator_payload_stays_in_literal():
    # Regression companion to test_cs_escape_unicode_line_terminators: the
    # full pipeline must emit a script with no raw C# line terminator.
    mgr, fake = _manager_with_fake()
    ls = chr(0x2028)
    mgr.execute_action(f'someAction /P:"a{ls}b"', quiet_mode=True)
    assert ls not in fake.captured_cs


# ---------------------------------------------------------------------------
# _build_action
# ---------------------------------------------------------------------------

def test_build_action_bool_and_skipped_values():
    action = _build_action("MyAction", FLAG=True, OFF=False, EMPTY="", NONE=None, NUM=3)
    assert action == "MyAction /FLAG:1 /OFF:0 /NUM:3"


def test_build_action_quotes_values_with_spaces():
    action = _build_action("MyAction", NAME="hello world")
    assert action == 'MyAction /NAME:"hello world"'
