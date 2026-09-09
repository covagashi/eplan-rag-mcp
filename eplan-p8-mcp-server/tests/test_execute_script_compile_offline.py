"""execute_script must not report success for a script that never ran.

THE BUG THIS PINS. EPLAN's ExecuteScript returns success in well under a second
whether or not the C# compiled - a compile failure goes only to EPLAN's own
system-message tree, and the remote call never learns of it. So a broken script
came back as {"success": true, "message": "Executed directly: ..."} and the
caller had no way to tell it from a script that worked. Observed live on
2026-09-09: a generated introspection script was run three times, reported
success each time, and wrote no result at all.

execute_custom_script already had this covered, but only because it owns a
{{RESULT_PATH}} contract and can notice the file never appearing. An arbitrary
file has no such contract, so the fix reads EPLAN's message tree directly.

TWO TIERS HERE, both offline:

  Unit      - argument handling and the summariser, no EPLAN at all.
  Contract  - a FAKE EPLAN whose message tree is scripted per test. This is
              where the interesting cases live, because the contract is
              "attribute to this run only what this run caused", and a
              caller-supplied filename is not unique the way a generated
              script_<uuid>.cs is.

The live tier is tests/test_execute_script_live.py (skips unless EPLAN is
reachable) and tests/live-expectations/execute_script_compile.md.
"""

import pytest

from api.actions import scripts as scripts_mod
from api.actions import scripted as scripted_mod


COMPILE_BLOCK = [
    "Compiler errors or warnings in script C:\\tmp\\probe.cs :",
    "CS0246 (Row:7, Col:9): The type or namespace name 'StringBuilder' could not be found",
    "CS0103 (Row:96, Col:13): The name 'File' does not exist in the current context",
    "Script C:\\tmp\\probe.cs could not be compiled.",
]


@pytest.fixture
def eplan(monkeypatch):
    """A fake EPLAN: ExecuteScript always succeeds, the message tree is scripted.

    `eplan["messages"]` is the tree as it stands BEFORE the run, oldest first,
    in the shape get_system_messages returns. `eplan["emit_on_run"]` is what
    EPLAN appends to it when ExecuteScript is dispatched - which is how a
    compile error actually arrives, and the distinction the whole fix turns on:
    entries already in the tree belong to some earlier run, not to this one.
    `eplan["actions"]` records what was dispatched.
    """
    state = {"messages": [], "emit_on_run": [], "actions": []}

    class _Manager:
        def execute_action(self, action):
            state["actions"].append(action)
            state["messages"] = list(state["messages"]) + list(state["emit_on_run"])
            return {"success": True, "message": "Executed directly: " + action,
                    "action": action}

    monkeypatch.setattr(scripts_mod, "_get_connected_manager",
                        lambda: (_Manager(), None), raising=False)

    def fake_get_system_messages(min_level="Warning", max_messages=100):
        state["queried"] = True
        return {"success": True,
                "messages": [{"text": t, "level": "Error", "occurrences": 1}
                             for t in state["messages"]]}

    # Both helpers read the tree through scripted's own module global.
    monkeypatch.setattr(scripted_mod, "get_system_messages",
                        fake_get_system_messages, raising=False)
    return state


def _run(tmp_path, name="probe.cs"):
    f = tmp_path / name
    f.write_text("public class P { }", encoding="utf-8")
    return scripts_mod.execute_script(str(f))


# ---------------------------------------------------------------------------
# Unit: the summariser
# ---------------------------------------------------------------------------

def test_summary_prefers_the_real_error_over_cs0105():
    # CS0105 ("using directive appeared previously") fires on nearly every
    # script because EPLAN pre-imports namespaces the script also declares. If
    # it were allowed to lead, every report would name the one diagnostic that
    # is never the cause.
    errors = [
        "CS0105 (Row:1, Col:7): The using directive for 'System' appeared previously",
        "CS0246 (Row:7, Col:9): 'StringBuilder' could not be found",
    ]
    summary = scripted_mod.summarise_compile_errors(errors)
    assert "CS0246" in summary
    assert "CS0105" not in summary


def test_summary_falls_back_to_cs0105_when_it_is_all_there_is():
    errors = ["CS0105 (Row:1, Col:7): duplicate using"]
    assert "CS0105" in scripted_mod.summarise_compile_errors(errors)


def test_summary_falls_back_to_non_cs_lines():
    errors = ["Script C:\\tmp\\probe.cs could not be compiled."]
    assert "could not be compiled" in scripted_mod.summarise_compile_errors(errors)


# ---------------------------------------------------------------------------
# Contract: a clean run is passed through untouched
# ---------------------------------------------------------------------------

def test_clean_run_returns_the_action_result(eplan, tmp_path):
    result = _run(tmp_path)
    assert result["success"] is True
    assert "Executed directly" in result["message"]
    assert "compile_errors" not in result


def test_clean_run_is_not_confused_by_errors_about_other_scripts(eplan, tmp_path):
    eplan["messages"] = [
        "Compiler errors or warnings in script C:\\tmp\\something_else.cs :",
        "CS0246 (Row:1, Col:1): unrelated failure",
    ]
    result = _run(tmp_path)
    assert result["success"] is True, \
        "another script's compile error must not condemn this one"


# ---------------------------------------------------------------------------
# Contract: a compile failure is reported instead of a cheerful success
# ---------------------------------------------------------------------------

def test_compile_failure_is_reported_as_failure(eplan, tmp_path):
    eplan["emit_on_run"] = list(COMPILE_BLOCK)
    result = _run(tmp_path)
    assert result["success"] is False
    assert result["errorType"] == "McpScriptCompileError"


def test_compile_failure_names_the_real_diagnostic(eplan, tmp_path):
    eplan["emit_on_run"] = list(COMPILE_BLOCK)
    result = _run(tmp_path)
    # The message is the first thing a reader sees; it must not say the script
    # ran, and it must carry the CS number so the fix is one turn away.
    assert "did not compile" in result["message"].lower()
    assert "CS0246" in result["message"]
    assert "Executed directly" not in result["message"]


def test_compile_failure_keeps_the_full_block_and_the_path(eplan, tmp_path):
    eplan["emit_on_run"] = list(COMPILE_BLOCK)
    result = _run(tmp_path)
    assert result["compile_errors"] == COMPILE_BLOCK
    assert result["script_file"].endswith("probe.cs")


# ---------------------------------------------------------------------------
# Contract: attribute to THIS run only. A caller-supplied basename is reused;
# a generated script_<uuid>.cs never is. This is the case that makes a naive
# implementation report a script the user already fixed as still broken.
# ---------------------------------------------------------------------------

def test_second_run_of_a_fixed_script_is_not_condemned_by_the_first(eplan, tmp_path):
    eplan["emit_on_run"] = list(COMPILE_BLOCK)
    first = _run(tmp_path)
    assert first["success"] is False

    # User fixes the file and runs it again. EPLAN's tree still holds the old
    # block - nothing ever clears it - but this run adds nothing new.
    eplan["emit_on_run"] = []
    second = _run(tmp_path)
    assert second["success"] is True, \
        "stale entries from an earlier run must not fail the next one"


def test_a_second_genuine_failure_is_still_caught(eplan, tmp_path):
    eplan["emit_on_run"] = list(COMPILE_BLOCK)
    assert _run(tmp_path)["success"] is False

    # Still broken, differently: EPLAN appends a fresh block after the old one.
    eplan["emit_on_run"] = [
        "Compiler errors or warnings in script C:\\tmp\\probe.cs :",
        "CS1002 (Row:3, Col:1): ; expected",
        "Script C:\\tmp\\probe.cs could not be compiled.",
    ]
    second = _run(tmp_path)
    assert second["success"] is False
    assert "CS1002" in second["message"]
    assert "CS0246" not in second["message"], \
        "the report must describe THIS run's failure, not the previous one"


# ---------------------------------------------------------------------------
# Contract: never turn someone else's failure into a different one
# ---------------------------------------------------------------------------

def test_transport_failure_is_passed_through_unchanged(monkeypatch, tmp_path):
    class _Manager:
        def execute_action(self, action):
            return {"success": False, "message": "not connected"}

    monkeypatch.setattr(scripts_mod, "_get_connected_manager",
                        lambda: (_Manager(), None), raising=False)
    monkeypatch.setattr(
        scripted_mod, "get_system_messages",
        lambda **k: {"success": True, "messages": [
            {"text": t} for t in COMPILE_BLOCK]}, raising=False)

    result = _run(tmp_path)
    assert result["success"] is False
    assert result["message"] == "not connected", \
        "a connection fault must not be relabelled a compile error"
    assert "errorType" not in result


def test_unreadable_message_tree_does_not_fail_a_good_run(monkeypatch, tmp_path):
    class _Manager:
        def execute_action(self, action):
            return {"success": True, "message": "Executed directly"}

    monkeypatch.setattr(scripts_mod, "_get_connected_manager",
                        lambda: (_Manager(), None), raising=False)
    monkeypatch.setattr(scripted_mod, "get_system_messages",
                        lambda **k: {"success": False, "error": "tree unavailable"},
                        raising=False)
    # A diagnostic that cannot run must stay silent, not invent a verdict.
    assert _run(tmp_path)["success"] is True


def test_message_tree_exception_does_not_escape(monkeypatch, tmp_path):
    class _Manager:
        def execute_action(self, action):
            return {"success": True, "message": "Executed directly"}

    def boom(**kwargs):
        raise RuntimeError("tree exploded")

    monkeypatch.setattr(scripts_mod, "_get_connected_manager",
                        lambda: (_Manager(), None), raising=False)
    monkeypatch.setattr(scripted_mod, "get_system_messages", boom, raising=False)
    assert _run(tmp_path)["success"] is True


def test_diagnostic_does_not_recurse(monkeypatch, tmp_path):
    # Reading the tree itself runs a script. If that read times out it asks for
    # compile errors, which reads the tree... The guard flag is what stops it.
    calls = {"n": 0}

    class _Manager:
        def execute_action(self, action):
            return {"success": True, "message": "Executed directly"}

    def reentrant(**kwargs):
        calls["n"] += 1
        if calls["n"] < 5:
            scripted_mod._compile_errors_for("probe.cs")
        return {"success": True, "messages": []}

    monkeypatch.setattr(scripts_mod, "_get_connected_manager",
                        lambda: (_Manager(), None), raising=False)
    monkeypatch.setattr(scripted_mod, "get_system_messages", reentrant, raising=False)
    _run(tmp_path)
    assert calls["n"] < 5, "the recursion guard did not hold"
    assert scripted_mod._collecting_diagnostics is False, \
        "the guard flag must be cleared even on the nested path"


# ---------------------------------------------------------------------------
# Sandbox tier: the guard that limits what can be executed at all. execute_script
# turns a PATH into code execution, so the path is the payload.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "\\\\attacker\\share\\evil.cs",
    "//attacker/share/evil.cs",
])
def test_unc_path_is_refused_before_anything_runs(eplan, bad):
    result = scripts_mod.execute_script(bad)
    assert result["success"] is False
    assert "UNC" in result["error"]
    assert eplan["actions"] == [], "a refused path must never reach EPLAN"


@pytest.mark.parametrize("bad", [None, "", "   ", 42])
def test_empty_or_non_string_path_is_refused(eplan, bad):
    result = scripts_mod.execute_script(bad)
    assert result["success"] is False
    assert eplan["actions"] == []


def test_refusal_happens_before_the_message_tree_is_touched(eplan):
    scripts_mod.execute_script("\\\\attacker\\share\\evil.cs")
    assert "queried" not in eplan, \
        "a refused call should cost nothing, not a round trip to EPLAN"


# ---------------------------------------------------------------------------
# Smoke: the tool is still wired into the server surface
# ---------------------------------------------------------------------------

def test_execute_script_is_exported_and_documented():
    from api import actions
    assert "execute_script" in actions.__all__
    doc = actions.execute_script.__doc__
    # The docstring is the whole of a model's safety context at call time, and
    # it is now also the only place the weakened meaning of success is stated.
    assert "success=True" in doc
    assert "NOT that the script did what you wanted" in doc
