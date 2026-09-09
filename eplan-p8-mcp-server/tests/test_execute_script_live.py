"""E2E: execute_script against a REAL EPLAN.

The offline suite pins the logic against a fake message tree. It cannot prove
the one thing that actually matters here: that EPLAN's real compile diagnostics
land in the tree in the shape the parser expects, with the script's filename in
them. That shape is EPLAN's, not ours, and it is what the fix depends on - so a
localised EPLAN, a different release, or a changed message format would break
the fix silently while every offline test stays green.

RUNNING IT. Every test here skips unless an EPLAN is reachable on the
remote-control channel, so the file is safe to leave in the default suite and
in CI:

    python -m pytest tests/test_execute_script_live.py -v

Skips are the expected outcome on a machine without EPLAN. To see them:

    python -m pytest tests/test_execute_script_live.py -v -rs

WHAT IT WRITES. Nothing in any project. Both scripts are generated into
tmp_path, and the successful one only writes a file back into tmp_path. No
project, master data or settings are touched, so this needs no scratch project
and is safe against whatever happens to be open.

Observed results belong in tests/live-expectations/execute_script_compile.md.
"""

import os

import pytest

from api.actions import execute_script, register_script, unregister_script
from eplan_connection import get_manager


pytestmark = pytest.mark.live


@pytest.fixture(scope="module", autouse=True)
def live_eplan():
    """Connect for this module only, and hand the connection back as found.

    A FIXTURE and not a `skipif`, which is where the first version went wrong.
    `skipif` is evaluated during COLLECTION, so connecting there mutates the
    shared singleton manager before a single test runs - and four
    "degrades without a connection" tests in test_catalog_offline.py then failed,
    having been collected as offline tests and executed against a live EPLAN.
    They passed in isolation and failed in the full suite, which is the worst
    shape a test failure can have. A module-scoped fixture connects when this
    module actually starts and restores the prior state afterwards.

    Connecting at all is necessary: pytest runs in its own process, whose
    manager starts disconnected and never auto-connects, so gating on the
    existing connection would skip every test on a machine where EPLAN is
    running perfectly - a live tier that can never run is decoration.

    `connect()` auto-detects the port rather than assuming DEFAULT_PORT: EPLAN
    came back on 49153 after a restart on 2026-09-09, having been on 49152 all
    day.

    Then ping, rather than trusting the manager's `connected` flag - that flag
    stays True after EPLAN stops answering. Observed the same day: eplan_status
    reported connected while eplan_ping said alive=False, because a modal dialog
    had blocked the script engine. Gating on `connected` would have hung here
    instead of skipping.
    """
    manager = get_manager()
    was_connected = manager.connected
    if not was_connected:
        try:
            manager.connect()
        except Exception:
            pass

    alive = False
    try:
        alive = manager.connected and manager.ping().get("alive") is True
    except Exception:
        alive = False

    if not alive:
        if not was_connected:
            try:
                manager.disconnect()
            except Exception:
                pass
        pytest.skip("no EPLAN answering the remote-control channel "
                    "(ping said not alive)")

    yield manager

    if not was_connected:
        try:
            manager.disconnect()
        except Exception:
            pass


GOOD_SCRIPT = '''using System;
using System.IO;
using Eplan.EplApi.Scripting;

public class McpLiveProbeOk
{
    [Start]
    public void Run()
    {
        File.WriteAllText(@"%s", "ran");
    }
}
'''

# CS0246: a type that does not exist. Chosen over a syntax error because it is
# the failure mode that actually bit - a missing `using` for a type the script
# engine does not pre-import.
BAD_SCRIPT = '''using System;
using Eplan.EplApi.Scripting;

public class McpLiveProbeBroken
{
    [Start]
    public void Run()
    {
        NoSuchTypeAnywhere x = new NoSuchTypeAnywhere();
        Console.WriteLine(x);
    }
}
'''


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)



def test_a_working_script_still_reports_success(tmp_path):
    marker = os.path.join(str(tmp_path), "ok.txt")
    # The generated path lands in a C# @"..." verbatim literal, where a doubled
    # backslash stays doubled - so it must NOT be escaped. Getting this wrong is
    # how a probe silently wrote nothing on 2026-09-09.
    script = _write(tmp_path, "mcp_live_ok.cs", GOOD_SCRIPT % marker)

    result = execute_script(script)

    assert result["success"] is True, result
    assert "compile_errors" not in result
    assert os.path.exists(marker), \
        "EPLAN reported success but the script left no trace - the very " \
        "failure this tool is supposed to stop reporting as success"



def test_a_broken_script_is_reported_as_a_compile_failure(tmp_path):
    script = _write(tmp_path, "mcp_live_broken.cs", BAD_SCRIPT)

    result = execute_script(script)

    assert result["success"] is False, \
        "a script that cannot compile must not come back as success"
    assert result["errorType"] == "McpScriptCompileError"
    assert result["script_file"] == script



def test_the_real_diagnostic_reaches_the_caller(tmp_path):
    script = _write(tmp_path, "mcp_live_diag.cs", BAD_SCRIPT)

    result = execute_script(script)

    assert result["success"] is False
    blob = " ".join(result.get("compile_errors") or []) + result.get("message", "")
    # The CS number is what makes the error actionable in one turn. Asserted
    # rather than the message text, which is localised - this machine's EPLAN
    # reports in Spanish.
    assert "CS0246" in blob, result
    assert os.path.basename(script) in blob, \
        "the diagnostic must be attributable to THIS script file"



def test_a_fixed_script_stops_being_reported_as_broken(tmp_path):
    """The regression the skip_matches snapshot exists to prevent.

    A caller-supplied filename is reused across runs, and EPLAN never clears its
    message tree. Without the before-snapshot, the second run inherits the first
    run's errors and a script the user just fixed keeps being called broken.
    """
    script = _write(tmp_path, "mcp_live_fixed.cs", BAD_SCRIPT)
    assert execute_script(script)["success"] is False

    marker = os.path.join(str(tmp_path), "fixed.txt")
    _write(tmp_path, "mcp_live_fixed.cs", GOOD_SCRIPT % marker)

    result = execute_script(script)
    assert result["success"] is True, \
        "the previous run's compile errors are still being attributed to this one"
    assert os.path.exists(marker)


# ---------------------------------------------------------------------------
# register_script: the second failure mode, which has no offline equivalent.
# EPLAN's "no attributes for loading" complaint is real prose from a real
# install; the offline suite can only assert we classify a block we invented.
# ---------------------------------------------------------------------------


def test_registering_a_start_only_script_is_reported_as_refused(tmp_path):
    """A [Start]-only script has nothing to register, and EPLAN says so.

    This is THE case that went unnoticed for a long time: RegisterScript
    returned success in ~0.45s while EPLAN complained in its own UI.

    It is also the test that corrected the implementation. The first version of
    register_script attributed complaints by filename, the way execute_script
    does, and this test failed against a real EPLAN because the refusal carries
    NO path - measured 2026-09-09 on 2025.0.3, Spanish UI:

        "En el script no hay atributos disponibles para cargar."  (level Error)

    Hence the tree diff. If this ever fails again, check first whether EPLAN
    still emits that line at all: silence there means the tool is blind again.
    """
    marker = os.path.join(str(tmp_path), "reg.txt")
    script = _write(tmp_path, "mcp_live_startonly.cs", GOOD_SCRIPT % marker)

    result = register_script(script)

    assert result["success"] is False, \
        "a script with no loadable attributes must not register 'successfully'"
    assert result["errorType"] == "McpScriptRegisterFailed", result
    # It compiled - calling it a compile error would send the reader hunting
    # for a syntax error that is not there.
    blob = " ".join(result.get("compile_errors") or [])
    assert blob.strip(), "the refusal reached us empty - nothing to report"
    assert "CS" not in blob, result
    # Deliberately NOT asserting the script name appears: it does not, and an
    # assertion that it should is what sent the first implementation wrong.

    # Leave nothing behind even though nothing should have registered.
    unregister_script(script)



def test_registering_a_script_that_does_not_compile_is_a_compile_error(tmp_path):
    script = _write(tmp_path, "mcp_live_regbroken.cs", BAD_SCRIPT)

    result = register_script(script)

    assert result["success"] is False
    assert result["errorType"] == "McpScriptCompileError", result
    assert "CS0246" in " ".join(result.get("compile_errors") or [])
