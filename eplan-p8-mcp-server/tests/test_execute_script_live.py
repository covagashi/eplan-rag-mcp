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
import uuid

import pytest

from api.actions import (execute_raw_action, execute_script, register_script,
                         unregister_script)
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


# ---------------------------------------------------------------------------
# The round trip register_script could not prove on its own: that a registered
# [DeclareAction] hook is actually LIVE. Rows 6 and 7 above only prove the
# failures are caught; success=True meant no more than "EPLAN logged nothing".
# ---------------------------------------------------------------------------

DECLARE_SCRIPT = '''using System;
using System.IO;
using Eplan.EplApi.Scripting;

public class McpLiveProbeDeclare
{
    [DeclareAction("%(action)s")]
    public void MyAction()
    {
        File.AppendAllText(@"%(marker)s", "fired ");
    }
}
'''


def test_a_registered_declareaction_actually_fires(tmp_path):
    """Register a [DeclareAction], call it, and prove it ran - then that it stops.

    The one thing `register_script` cannot tell you about itself. Its
    success=True means only "EPLAN logged no complaint at Error level while
    registering"; nothing in rows 6-7 shows a hook that works. This calls the
    declared action and reads the file it writes, which is the only evidence
    available - see the baseline assertion below for why the action result is
    not evidence of anything.

    The action name carries a uuid. A registration PERSISTS for the rest of the
    EPLAN session, so a fixed name would let a run that died before its teardown
    leave a live hook behind, and the next run's baseline would then fire the
    PREVIOUS run's script - pointed at a tmp_path that no longer exists.
    """
    action_name = "McpLiveProbeAction_" + uuid.uuid4().hex[:12]
    marker = os.path.join(str(tmp_path), "fired.txt")
    script = _write(tmp_path, "mcp_live_declact.cs",
                    DECLARE_SCRIPT % {"action": action_name, "marker": marker})

    # Baseline: EPLAN does not know this action yet, and SAYS so. Measured
    # 2026-09-09 on 2025.0.3 - the tool surface reports
    #   errorType "Eplan.EplApi.Base.BaseException"
    #   "No se ha podido encontrar la accion 'X'. No esta incluida en el
    #    conjunto de funciones."
    # That reporting is not free and not universal: every wrapper in
    # api/actions/ goes through _base.QuietManagerWrapper, which forces
    # quiet_mode=True and so routes via ActionManager.FindAction, where a
    # missing action raises. A bare manager.execute_action(name) takes the
    # DIRECT path instead and returns {"success": true, "message": "Executed
    # directly: X"} for an action that does not exist - confirmed against a
    # garbage name. Worth keeping straight, because RegisterScript,
    # ExecuteScript and UnregisterScript are pinned to that direct path on
    # purpose (quiet_mode would recurse), which is exactly why those three
    # need the message-tree diff to learn anything at all.
    before = execute_raw_action(action_name)
    assert before["success"] is False,         "an action nobody has declared came back as success - "         "the not-found report this test reads is gone"
    assert not os.path.exists(marker),         "something fired before registration - a previous run leaked a hook?"

    registered = register_script(script)
    try:
        assert registered["success"] is True, registered

        # Now EPLAN resolves it, which is already evidence the attribute was
        # loaded - and the marker proves the body actually ran.
        fired = execute_raw_action(action_name)
        assert fired["success"] is True, fired
        assert os.path.exists(marker),             "register_script reported success but the declared action did not "             "fire - the hooks are not live and success=True is meaningless"
        assert open(marker).read() == "fired "
    finally:
        unregister_script(script)

    # And the hook is gone: the action stops resolving, and the marker keeps
    # exactly the one write from while it was registered.
    after = execute_raw_action(action_name)
    assert after["success"] is False,         "the action still resolves after unregister_script - the hook outlived it"
    assert open(marker).read() == "fired ",         "the action still fires after unregister_script - the hook outlived it"


def test_unregistering_a_path_that_was_never_registered_is_a_noop(tmp_path):
    """Open question until measured: error, or silence? It is silence.

    Both an unregistered-but-real script and a path with no file at all come
    back success=true. Worth pinning: teardown code that unregisters
    unconditionally - which this module's own tests do - would break the moment
    EPLAN started objecting.
    """
    never = _write(tmp_path, "mcp_live_never_registered.cs",
                   DECLARE_SCRIPT % {"action": "McpNeverRegistered",
                                     "marker": os.path.join(str(tmp_path), "x.txt")})

    assert unregister_script(never)["success"] is True
    assert unregister_script(os.path.join(str(tmp_path), "no_such_file.cs"))["success"] is True
