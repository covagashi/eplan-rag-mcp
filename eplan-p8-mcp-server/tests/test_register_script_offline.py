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

WHY register_script DIFFS THE TREE instead of matching the filename the way
execute_script does. Measured live on 2025.0.3, 2026-09-09: the compile block
names the script in its header and footer, but the registration refusal is the
bare line

    "En el script no hay atributos disponibles para cargar."   (level Error)

with no path in it at all. Filename matching literally cannot see it - a first
attempt here did exactly that and the live test caught it. So register_script
snapshots the tree before the call and reports what appeared, which is sound
only because EPLAN's actions are synchronous.
"""

import pytest

from api.actions import scripts as scripts_mod
from api.actions import scripted as scripted_mod


COMPILE_BLOCK = [
    "Compiler errors or warnings in script C:\\tmp\\hooks.cs :",
    "CS1002 (Row:3, Col:1): ; expected",
    "Script C:\\tmp\\hooks.cs could not be compiled.",
]

# No CS number: it compiled, EPLAN just would not register it. This is the
# VERBATIM text EPLAN 2025.0.3 emits (Spanish UI, level Error) - note it does
# not name the script, which is the whole reason this tool diffs the tree.
NO_ATTRIBUTES_BLOCK = [
    "En el script no hay atributos disponibles para cargar.",
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


def test_pre_existing_complaints_do_not_condemn_this_run(eplan, tmp_path):
    # Entries already in the tree belong to some earlier call. EPLAN never
    # clears it, so without the before-snapshot every registration after the
    # first failure would inherit it forever.
    eplan["messages"] = list(NO_ATTRIBUTES_BLOCK)
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
        "The script does not contain attributes for loading."]
    assert _register(tmp_path)["errorType"] == "McpScriptRegisterFailed"


def test_refusal_is_caught_even_though_it_never_names_the_script(eplan, tmp_path):
    # The regression the live run exposed: the real message carries no path, so
    # any filename-based attribution reports success for a failed registration.
    eplan["emit_on_run"] = list(NO_ATTRIBUTES_BLOCK)
    script_name = "hooks.cs"
    assert script_name not in NO_ATTRIBUTES_BLOCK[0], \
        "fixture drift - this test is meaningless if the message names the file"
    assert _register(tmp_path, script_name)["success"] is False


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
# Contract: severity. Measured, not guessed - the refusal is logged at Error.
# ---------------------------------------------------------------------------

def test_tree_is_read_at_error_level(eplan, tmp_path):
    _register(tmp_path)
    assert eplan["levels"], "the message tree was never consulted"
    assert set(eplan["levels"]) == {"Error"}, \
        "the no-attributes refusal is logged at Error on 2025.0.3; reading " \
        "wider only admits unrelated warnings into the diff"


def test_before_and_after_reads_use_the_same_level(eplan, tmp_path):
    # Mismatched levels would diff different sets, making the delta fiction.
    _register(tmp_path)
    assert len(set(eplan["levels"])) == 1


def test_empty_tree_is_a_valid_baseline(eplan, tmp_path):
    # A freshly started EPLAN has an empty error tree. Treating that as "no
    # baseline" would silently disable the diagnostic exactly when it is most
    # likely to be needed.
    assert eplan["messages"] == []
    eplan["emit_on_run"] = list(NO_ATTRIBUTES_BLOCK)
    assert _register(tmp_path)["success"] is False


def _two_reads(monkeypatch, first, second):
    """Wire a fake whose message tree reads `first` then `second`."""
    reads = {"n": 0}

    class _Manager:
        def execute_action(self, action):
            return {"success": True, "message": "ok"}

    def two_reads(min_level="Warning", max_messages=100):
        reads["n"] += 1
        texts = first if reads["n"] == 1 else second
        return {"success": True, "messages": [{"text": t} for t in texts]}

    monkeypatch.setattr(scripts_mod, "_get_connected_manager",
                        lambda: (_Manager(), None), raising=False)
    monkeypatch.setattr(scripted_mod, "get_system_messages", two_reads, raising=False)


def test_a_slid_window_is_aligned_not_abandoned(monkeypatch, tmp_path):
    """The read keeps only the newest N, so a full window slides as it grows.

    An earlier version tested `before` for being a prefix of `after` and gave up
    when it was not - which, once the tree is longer than the window, is ALWAYS,
    so the diagnostic reported nothing precisely in the long-running sessions
    where it is most needed. Four live tests failed that way in the full suite
    while passing alone. Aligning on the overlap is the fix.
    """
    _two_reads(monkeypatch,
               ["old-1", "old-2"],
               ["old-2", NO_ATTRIBUTES_BLOCK[0]])   # slid by one, one new entry
    result = _register(tmp_path)
    assert result["success"] is False
    assert result["compile_errors"] == [NO_ATTRIBUTES_BLOCK[0]], \
        "the delta must be exactly what appeared, not the whole window"


def test_an_unalignable_tree_is_refused_rather_than_guessed(monkeypatch, tmp_path):
    # No shift lines the two reads up: the tree was cleared or rewritten, and
    # any 'delta' would be unrelated messages blamed on this call.
    _two_reads(monkeypatch,
               ["old-1", "old-2"],
               ["something", "entirely", "different"])
    assert _register(tmp_path)["success"] is True


def test_growth_without_sliding_is_still_detected(monkeypatch, tmp_path):
    # The window is not full: `before` really is a prefix of `after`.
    _two_reads(monkeypatch,
               ["old-1"],
               ["old-1", NO_ATTRIBUTES_BLOCK[0]])
    assert _register(tmp_path)["success"] is False


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
