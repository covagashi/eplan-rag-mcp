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

from api.actions import execute_script
from api.actions._base import _get_connected_manager


pytestmark = pytest.mark.live


def _eplan_available() -> bool:
    """True when EPLAN answers the remote-control channel.

    Ping rather than the manager's `connected` flag: that flag stays True after
    EPLAN stops answering. Observed 2026-09-09 - eplan_status reported
    connected while eplan_ping said alive=False, because a modal dialog had
    blocked the script engine. A live test gated on `connected` would have hung
    there instead of skipping.
    """
    try:
        manager, error = _get_connected_manager()
        if error or manager is None:
            return False
        return manager.ping().get("alive") is True
    except Exception:
        return False


requires_eplan = pytest.mark.skipif(
    not _eplan_available(),
    reason="no EPLAN answering the remote-control channel (ping said not alive)",
)


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


@requires_eplan
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


@requires_eplan
def test_a_broken_script_is_reported_as_a_compile_failure(tmp_path):
    script = _write(tmp_path, "mcp_live_broken.cs", BAD_SCRIPT)

    result = execute_script(script)

    assert result["success"] is False, \
        "a script that cannot compile must not come back as success"
    assert result["errorType"] == "McpScriptCompileError"
    assert result["script_file"] == script


@requires_eplan
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


@requires_eplan
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
