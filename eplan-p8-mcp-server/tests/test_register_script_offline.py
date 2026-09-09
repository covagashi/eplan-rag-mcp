"""register_script must not report success when EPLAN registered nothing.

Same structural blind spot as execute_script, with one extra failure mode.
RegisterScript returns success in ~0.45s regardless of outcome, and EPLAN's two
objections both go only to its own message tree:

  1. the C# did not compile                      -> CS#### lines
  2. the script has no loadable attributes       -> prose, no CS number

(2) is what a [Start]-only script gets. That is not hypothetical: the MCP's own
generated-script path used to RegisterScript before every ExecuteScript, and the
resulting complaint went unnoticed for a long time precisely because the remote
call kept saying success - see the comment in scripted._execute_script.

The two are reported as different errorTypes, decided by whether the block
CONTAINS a CS number, never by matching EPLAN's prose: the header and footer
around the CS lines are localised (this machine reports in Spanish) while the
CS codes are not. A test that matched on English text would pass here and fail
on the machine it was meant to protect.
"""

import pytest

from api.actions import scripts as scripts_mod
from api.actions import scripted as scripted_mod


COMPILE_BLOCK = [
    "Compiler errors or warnings in script C:\\tmp\\hooks.cs :",
    "CS1002 (Row:3, Col:1): ; expected",
    "Script C:\\tmp\\hooks.cs could not be compiled.",
]

# No CS number: it compiled, EPLAN just would not register it. Deliberately
# written in Spanish, because that is what this installation emits and the
# classifier must not care either way.
NO_ATTRIBUTES_BLOCK = [
    "El script C:\\tmp\\hooks.cs no contiene atributos para la carga.",
]


@pytest.fixture
def eplan(monkeypatch):
    """A fake EPLAN whose message tree is scripted per test.

    `emit_on_run` is appended when the action is dispatched - the distinction
    the fix turns on, since entries already present belong to an earlier run.
    """
    state = {"messages": [], "emit_on_run": [], "actions": [], "levels": []}

    class _Manager:
        def execute_action(self, action):
            state["actions"].append(action)
            state["messages"] = list(state["messages"]) + list(state["emit_on_run"])
            return {"success": True, "message": "Executed directly: " + action}

    monkeypatch.setattr(scripts_mod, "_get_connected_manager",
                        lambda: (_Manager(), None), raising=False)

    def fake_get_system_messages(min_level="Warning", max_messages=100):
        state["levels"].append(min_level)
        return {"success": True,
                "messages": [{"text": t, "level": "Warning", "occurrences": 1}
                             for t in state["messages"]]}

    monkeypatch.setattr(scripted_mod, "get_system_messages",
                        fake_get_system_messages, raising=False)
    return state


def _register(tmp_path, name="hooks.cs"):
    f = tmp_path / name
    f.write_text("public class H { }", encoding="utf-8")
    return scripts_mod.register_script(str(f))


# ---------------------------------------------------------------------------
# Contract: a clean registration is passed through
# ---------------------------------------------------------------------------

def test_clean_registration_returns_the_action_result(eplan, tmp_path):
    result = _register(tmp_path)
    assert result["success"] is True
    assert "compile_errors" not in result


def test_other_scripts_complaints_do_not_condemn_this_one(eplan, tmp_path):
    eplan["messages"] = ["El script C:\\tmp\\otro.cs no contiene atributos para la carga."]
    assert _register(tmp_path)["success"] is True


# ---------------------------------------------------------------------------
# Contract: the no-attributes refusal - the mode execute_script cannot have
# ---------------------------------------------------------------------------

def test_no_attributes_refusal_is_reported_as_failure(eplan, tmp_path):
    eplan["emit_on_run"] = list(NO_ATTRIBUTES_BLOCK)
    result = _register(tmp_path)
    assert result["success"] is False
    assert result["errorType"] == "McpScriptRegisterFailed"


def test_no_attributes_refusal_is_not_called_a_compile_error(eplan, tmp_path):
    # It compiled fine. Saying otherwise sends the reader to look for a syntax
    # error that is not there; the actual fix is to call execute_script instead.
    eplan["emit_on_run"] = list(NO_ATTRIBUTES_BLOCK)
    result = _register(tmp_path)
    assert "did not compile" not in result["message"].lower()
    assert "compiled but EPLAN refused it" in result["error"]
    assert result["compile_errors"] == NO_ATTRIBUTES_BLOCK


def test_classification_does_not_depend_on_the_message_language(eplan, tmp_path):
    # The same refusal in English must classify identically - the decision is
    # "does the block contain a CS number", not what the prose says.
    eplan["emit_on_run"] = [
        "The script C:\\tmp\\hooks.cs does not contain attributes for loading."]
    assert _register(tmp_path)["errorType"] == "McpScriptRegisterFailed"


# ---------------------------------------------------------------------------
# Contract: a compile failure is still a compile failure
# ---------------------------------------------------------------------------

def test_compile_failure_keeps_its_own_error_type(eplan, tmp_path):
    eplan["emit_on_run"] = list(COMPILE_BLOCK)
    result = _register(tmp_path)
    assert result["success"] is False
    assert result["errorType"] == "McpScriptCompileError"
    assert "CS1002" in result["message"]


# ---------------------------------------------------------------------------
# Contract: severity. The no-attributes complaint's level is UNCONFIRMED, so
# the tree is read at Warning - reading only Errors would miss it.
# ---------------------------------------------------------------------------

def test_tree_is_read_at_warning_level(eplan, tmp_path):
    _register(tmp_path)
    assert eplan["levels"], "the message tree was never consulted"
    assert set(eplan["levels"]) == {"Warning"}, \
        "register_script must not read at Error only - the no-attributes " \
        "complaint may be a Warning, and would then go unnoticed"


def test_snapshot_and_read_use_the_same_level(eplan, tmp_path):
    # Mismatched levels would count different sets, making the skip meaningless.
    _register(tmp_path)
    assert len(set(eplan["levels"])) == 1


# ---------------------------------------------------------------------------
# Contract: attribute to THIS run only
# ---------------------------------------------------------------------------

def test_a_fixed_script_is_not_condemned_by_the_previous_run(eplan, tmp_path):
    eplan["emit_on_run"] = list(NO_ATTRIBUTES_BLOCK)
    assert _register(tmp_path)["success"] is False

    eplan["emit_on_run"] = []
    assert _register(tmp_path)["success"] is True, \
        "EPLAN never clears its tree; stale entries must not fail the next run"


# ---------------------------------------------------------------------------
# Contract: never relabel someone else's failure
# ---------------------------------------------------------------------------

def test_transport_failure_is_passed_through(monkeypatch, tmp_path):
    class _Manager:
        def execute_action(self, action):
            return {"success": False, "message": "not connected"}

    monkeypatch.setattr(scripts_mod, "_get_connected_manager",
                        lambda: (_Manager(), None), raising=False)
    monkeypatch.setattr(
        scripted_mod, "get_system_messages",
        lambda **k: {"success": True,
                     "messages": [{"text": t} for t in NO_ATTRIBUTES_BLOCK]},
        raising=False)
    result = _register(tmp_path)
    assert result["message"] == "not connected"
    assert "errorType" not in result


# ---------------------------------------------------------------------------
# Sandbox tier: the path IS the payload for all three tools in this module
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn_name", ["register_script", "execute_script",
                                     "unregister_script"])
def test_every_tool_refuses_a_unc_path(eplan, fn_name):
    # The module docstring claims all of them do this. unregister_script did
    # not, so the claim was false for a third of the module.
    result = getattr(scripts_mod, fn_name)("\\\\attacker\\share\\evil.cs")
    assert result["success"] is False
    assert "UNC" in result["error"]
    assert eplan["actions"] == [], "a refused path must never reach EPLAN"


@pytest.mark.parametrize("fn_name", ["register_script", "execute_script",
                                     "unregister_script"])
def test_every_tool_refuses_an_empty_path(eplan, fn_name):
    for bad in (None, "", "   "):
        assert getattr(scripts_mod, fn_name)(bad)["success"] is False
    assert eplan["actions"] == []


# ---------------------------------------------------------------------------
# Smoke: still wired into the server surface, and still honest in the docstring
# ---------------------------------------------------------------------------

def test_tools_exported_and_documented():
    from api import actions
    for name in ("register_script", "execute_script", "unregister_script"):
        assert name in actions.__all__
    # Whitespace-normalised: a docstring is wrapped for reading, and a phrase
    # that straddles a line break should not fail a test about its meaning.
    doc = " ".join((actions.register_script.__doc__ or "").split())
    assert "success=True" in doc
    assert "not proof the hooks are live" in doc
