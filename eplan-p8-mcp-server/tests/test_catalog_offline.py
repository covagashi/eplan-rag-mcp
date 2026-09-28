"""Offline tests for api/actions/catalog.py and the generated action registry.

Everything here runs with NO EPLAN connection: the registry is a JSON file
shipped with the server, action_run(dry_run=True) never touches the wire, and
action_describe()/ribbon_catalog() must degrade to a plain dict instead of
raising or blocking. The registry regeneration test shells out to
tools/build_action_registry.py and is skipped when MFTools.xml is absent.
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from api.actions import catalog


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILDER = os.path.join(REPO_ROOT, "tools", "build_action_registry.py")

REQUIRED_KEYS = {
    "name", "description", "doc_url", "documented",
    "gui", "origin", "params", "wrapped_by",
}


@pytest.fixture(scope="module")
def registry():
    actions, meta, error = catalog._load_registry()
    assert error is None, error
    return actions, meta


@pytest.fixture(scope="module")
def gui_only_name(registry):
    """A real GUI-only action mined from MFTools, read off the registry."""
    actions, _ = registry
    candidates = sorted(
        name for name, e in actions.items()
        if not e.get("documented") and (e.get("gui") or {}).get("examples")
    )
    assert candidates, "registry has no GUI-only actions with examples"
    return candidates[0]


@pytest.fixture
def no_script(monkeypatch):
    """Trip-wire: any attempt to reach the script engine fails loudly."""
    def boom(*a, **kw):
        raise AssertionError("_execute_script must not be called with no connection")
    monkeypatch.setattr(catalog, "_execute_script", boom)


# ---------------------------------------------------------------------------
# registry shape
# ---------------------------------------------------------------------------

def test_registry_loads_and_is_non_empty(registry):
    actions, meta = registry
    assert len(actions) > 500
    assert meta["counts"]["total"] == len(actions)
    assert meta["eplan_version"]


def test_every_entry_has_the_required_keys(registry):
    actions, _ = registry
    for key, entry in actions.items():
        assert REQUIRED_KEYS <= set(entry), "{} missing {}".format(
            key, REQUIRED_KEYS - set(entry))
        assert entry["name"] == key
        assert isinstance(entry["documented"], bool)
        assert isinstance(entry["params"], list)
        assert isinstance(entry["wrapped_by"], list)
        assert isinstance(entry["origin"], list)
        gui = entry["gui"]
        assert {"categories", "command_ids", "examples"} <= set(gui)
        for p in entry["params"]:
            assert p.get("name"), "{} has a nameless param".format(key)
            assert p.get("source") in ("docs", "observed")


def test_registry_cache_is_not_a_live_reference(registry):
    """_compact / action_describe must never hand out the cache itself."""
    actions, _ = registry
    described = catalog.action_describe("backup")["registry"]
    described["params"].append({"name": "INJECTED", "source": "docs"})
    assert "INJECTED" not in [p["name"] for p in actions["backup"]["params"]]


# ---------------------------------------------------------------------------
# action_catalog: search
# ---------------------------------------------------------------------------

def test_search_finds_a_documented_action():
    result = catalog.action_catalog(search="backup", limit=200)
    assert result["success"] is True
    names = [a["name"] for a in result["actions"]]
    assert "backup" in names
    entry = next(a for a in result["actions"] if a["name"] == "backup")
    assert entry["documented"] is True
    assert "PROJECTNAME" in entry["params"]


def test_search_matches_parameter_names():
    result = catalog.action_catalog(search="ARCHIVENAME", limit=200)
    assert "backup" in [a["name"] for a in result["actions"]]


def test_search_finds_a_gui_only_action(gui_only_name):
    result = catalog.action_catalog(search=gui_only_name, limit=10000)
    names = [a["name"] for a in result["actions"]]
    assert gui_only_name in names
    entry = next(a for a in result["actions"] if a["name"] == gui_only_name)
    assert entry["documented"] is False
    assert entry["example"], "a GUI-mined action should carry an example command line"


def test_known_gui_only_action_is_present(registry):
    """Spot-check a specific MFTools-mined action (not in the official docs)."""
    actions, _ = registry
    entry = actions["XCabChangeAngleAction"]
    assert entry["documented"] is False
    assert entry["doc_url"] is None
    assert "used_actions" in entry["origin"]


def test_search_is_case_insensitive():
    assert catalog.action_catalog(search="BACKUP", limit=200)["count"] == \
           catalog.action_catalog(search="backup", limit=200)["count"]


def test_search_with_no_hits_is_empty_not_an_error():
    result = catalog.action_catalog(search="zzz-no-such-action-zzz")
    assert result["success"] is True
    assert result["count"] == 0
    assert result["actions"] == []


# ---------------------------------------------------------------------------
# action_catalog: filters
# ---------------------------------------------------------------------------

def test_documented_only_filter(registry):
    _, meta = registry
    result = catalog.action_catalog(documented_only=True, limit=10000)
    assert result["count"] == meta["counts"]["documented"]
    assert all(a["documented"] for a in result["actions"])


def test_wrapped_filter_partitions_the_registry(registry):
    actions, meta = registry
    yes = catalog.action_catalog(wrapped=True, limit=10000)
    no = catalog.action_catalog(wrapped=False, limit=10000)
    assert yes["count"] == meta["counts"]["wrapped"]
    assert yes["count"] + no["count"] == len(actions)
    assert all(a["wrapped_by"] for a in yes["actions"])
    assert not any(a["wrapped_by"] for a in no["actions"])


def test_category_filter():
    result = catalog.action_catalog(category="16", limit=10000)
    assert result["count"] > 0
    assert all("16" in a["categories"] for a in result["actions"])
    assert result["count"] < catalog.action_catalog(limit=1)["count"]


def test_category_accepts_an_int():
    assert catalog.action_catalog(category=16, limit=1)["count"] == \
           catalog.action_catalog(category="16", limit=1)["count"]


def test_filters_combine():
    result = catalog.action_catalog(search="pdf", documented_only=True, limit=10000)
    assert all(a["documented"] for a in result["actions"])
    assert result["count"] <= catalog.action_catalog(search="pdf", limit=1)["count"]


def test_invalid_limit_is_refused():
    result = catalog.action_catalog(limit="lots")
    assert result["success"] is False
    assert "limit" in result["error"].lower()


# ---------------------------------------------------------------------------
# truncation contract: the reported total survives the cut
# ---------------------------------------------------------------------------

def test_truncation_reports_the_full_total():
    small = catalog.action_catalog(documented_only=True, limit=5)
    big = catalog.action_catalog(documented_only=True, limit=10000)

    assert small["count"] == big["count"], "total must not shrink with limit"
    assert small["count"] > 5
    assert small["returned"] == 5
    assert len(small["actions"]) == 5
    assert small["truncated"] == small["count"] - 5

    assert big["returned"] == big["count"]
    assert big["truncated"] == 0
    assert len(big["actions"]) == big["count"]


def test_limit_zero_still_reports_the_total():
    result = catalog.action_catalog(search="backup", limit=0)
    assert result["count"] > 0
    assert result["returned"] == 0
    assert result["actions"] == []
    assert result["truncated"] == result["count"]


def test_registry_total_is_reported_alongside_matches(registry):
    actions, _ = registry
    result = catalog.action_catalog(search="backup", limit=1)
    assert result["registry"]["total_actions"] == len(actions)


# ---------------------------------------------------------------------------
# action_run: dry_run command building
# ---------------------------------------------------------------------------

def test_dry_run_builds_the_exact_command(no_script):
    result = catalog.action_run(
        "backup",
        {"TYPE": "PROJECT", "PROJECTNAME": "C:/My Projects/x.elk"},
        dry_run=True,
    )
    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["command"] == 'backup /TYPE:PROJECT /PROJECTNAME:"C:/My Projects/x.elk"'
    assert result["validation"]["action"] == "backup"
    assert result["validation"]["unknown_params_sent"] == []


def test_dry_run_resolves_casing_of_the_action_name(no_script):
    result = catalog.action_run("BACKUP", {"TYPE": "PROJECT"}, dry_run=True)
    assert result["success"] is True
    assert result["command"] == "backup /TYPE:PROJECT"
    assert result["validation"]["resolved_name"] == "backup"


def test_dry_run_with_no_params(no_script):
    result = catalog.action_run("compress", dry_run=True)
    assert result["success"] is True
    assert result["command"] == "compress"


# ---------------------------------------------------------------------------
# action_run: refusals
# ---------------------------------------------------------------------------

def test_unknown_action_is_refused_with_near_matches(no_script):
    result = catalog.action_run("bakcup", dry_run=True)
    assert result["success"] is False
    assert "bakcup" in result["error"]
    assert "backup" in result["near_matches"]


def test_empty_action_name_is_refused(no_script):
    result = catalog.action_run("   ", dry_run=True)
    assert result["success"] is False
    assert "name" in result["error"].lower()


def test_unknown_param_key_is_refused_and_named(no_script):
    result = catalog.action_run("backup", {"Type": "PROJECT"}, dry_run=True)
    assert result["success"] is False
    assert "Type" in result["error"]
    assert "TYPE" in result["known_params"]
    assert "TYPE" in result["suggestions"]["Type"]
    assert "casing" in result["suggestions"]["Type"]


def test_allow_unknown_params_lets_the_same_call_through(no_script):
    result = catalog.action_run(
        "backup", {"Type": "PROJECT"}, dry_run=True, allow_unknown_params=True
    )
    assert result["success"] is True
    assert result["command"] == "backup /Type:PROJECT"
    assert result["validation"]["unknown_params_sent"] == ["Type"]
    assert result["validation"]["allow_unknown_params"] is True


def test_action_with_no_registry_params_explains_the_escape_hatch(registry, no_script):
    actions, _ = registry
    name = sorted(n for n, e in actions.items() if not e["params"])[0]
    result = catalog.action_run(name, {"Anything": "1"}, dry_run=True)
    assert result["success"] is False
    assert result["known_params"] == []
    assert "allow_unknown_params" in result["hint"]
    ok = catalog.action_run(name, {"Anything": "1"}, dry_run=True,
                            allow_unknown_params=True)
    assert ok["command"] == "{} /Anything:1".format(name)


def test_action_name_key_collision_is_refused(no_script):
    result = catalog.action_run("backup", {"action_name": "x"}, dry_run=True,
                                allow_unknown_params=True)
    assert result["success"] is False
    assert "action_name" in result["error"]


def test_non_dict_params_is_refused(no_script):
    result = catalog.action_run("backup", params=["TYPE=PROJECT"], dry_run=True)
    assert result["success"] is False
    assert "dict" in result["error"]


def test_real_run_without_connection_reports_the_command():
    result = catalog.action_run("backup", {"TYPE": "PROJECT"})
    assert result["success"] is False
    assert result["command"] == "backup /TYPE:PROJECT"


# ---------------------------------------------------------------------------
# graceful degradation with no connection
# ---------------------------------------------------------------------------

def test_action_describe_degrades_to_registry_only(no_script):
    result = catalog.action_describe("backup")
    assert isinstance(result, dict)
    assert result["success"] is True
    assert result["in_registry"] is True
    assert result["registry"]["name"] == "backup"
    assert result["live"]["probed"] is False
    assert "connect" in result["live"]["reason"].lower()
    assert result["licensed_hint"]


def test_action_describe_resolves_casing(no_script):
    assert catalog.action_describe("BaCkUp")["name"] == "backup"


def test_action_describe_unknown_name_does_not_raise(no_script):
    result = catalog.action_describe("bakcup")
    assert result["success"] is True
    assert result["in_registry"] is False
    assert result["registry"] is None
    assert "backup" in result["near_matches"]


def test_action_describe_empty_name_is_refused(no_script):
    result = catalog.action_describe("")
    assert result["success"] is False


def test_ribbon_catalog_degrades_without_connection():
    result = catalog.ribbon_catalog()
    assert isinstance(result, dict)
    assert result["success"] is False
    assert result["error"]


def test_ribbon_catalog_propagates_the_script_engine_message(monkeypatch):
    monkeypatch.setattr(
        catalog, "_execute_script",
        lambda script, timeout=None: {"success": False, "message": "engine down"},
    )
    assert catalog.ribbon_catalog()["error"] == "engine down"


# ---------------------------------------------------------------------------
# registry regeneration is deterministic
# ---------------------------------------------------------------------------

def _default_mftools():
    spec = importlib.util.spec_from_file_location("_build_action_registry", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DEFAULT_MFTOOLS


def test_registry_regeneration_is_byte_identical(tmp_path):
    mftools = _default_mftools()
    if not os.path.exists(mftools):
        pytest.skip("MFTools.xml not present ({}) - EPLAN not installed".format(mftools))

    outs = []
    for i in (1, 2):
        out = tmp_path / "registry_{}.json".format(i)
        proc = subprocess.run(
            [sys.executable, BUILDER, "--mftools", mftools, "--out", str(out)],
            capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr
        outs.append(out.read_bytes())

    assert outs[0] == outs[1], "two builds of the registry differ"
    payload = json.loads(outs[0].decode("utf-8"))
    assert payload["_meta"]["sources"]["mftools_found"] is True
    # Underscore-prefixed top-level keys are metadata blocks (_meta,
    # _command_index), not actions - count only the action entries.
    action_keys = [k for k in payload if not k.startswith("_")]
    assert payload["_meta"]["counts"]["total"] == len(action_keys)


# ---------------------------------------------------------------------------
# ribbon_catalog view shaping
#
# The full live ribbon serialises to ~147,000 characters, past the tool-result
# size cap, so ribbon_catalog() drills down instead of dumping. These pin the
# three views and the tab-resolution join. They also guard a real bug found in
# review: the command-resolution loop used `tab` as its loop variable and
# shadowed the `tab` parameter, so every call raised
# "'dict' object has no attribute 'strip'".
# ---------------------------------------------------------------------------

_FAKE_RIBBON = {
    "success": True,
    "tabs": [
        {"tab": "Home", "identifier": "Start", "is_custom": False, "groups": [
            {"group": "Clipboard", "is_custom": False, "commands": [
                {"id": "35037", "text": "Paste", "action_command_line": "",
                 "is_custom": False},
                {"id": "999999", "text": "Mystery", "action_command_line": "",
                 "is_custom": False},
            ]},
        ]},
        {"tab": "Insert", "identifier": "Insert", "is_custom": False, "groups": [
            {"group": "Macros", "is_custom": False, "commands": [
                {"id": "35732", "text": "Page macro", "action_command_line": "",
                 "is_custom": False},
            ]},
        ]},
    ],
}


@pytest.fixture
def fake_ribbon(monkeypatch):
    import copy
    monkeypatch.setattr(
        catalog, "_execute_script",
        lambda script, timeout=30.0: {"success": True,
                                      "results": copy.deepcopy(_FAKE_RIBBON)})


def test_ribbon_catalog_default_view_is_a_compact_index(fake_ribbon):
    r = catalog.ribbon_catalog()
    assert r["success"] is True and r["view"] == "index"
    assert r["tabs"] == [
        {"tab": "Home", "identifier": "Start", "is_custom": False,
         "groups": 1, "commands": 2},
        {"tab": "Insert", "identifier": "Insert", "is_custom": False,
         "groups": 1, "commands": 1},
    ]
    # the index must not carry the per-command payload it exists to avoid
    assert "Paste" not in json.dumps(r)
    assert "hint" in r


def test_ribbon_catalog_tab_view_returns_only_that_tab(fake_ribbon):
    r = catalog.ribbon_catalog(tab="insert")           # case-insensitive
    assert r["view"] == "tab"
    assert [t["tab"] for t in r["tabs"]] == ["Insert"]
    r2 = catalog.ribbon_catalog(tab="Start")           # matches identifier too
    assert [t["tab"] for t in r2["tabs"]] == ["Home"]


def test_ribbon_catalog_unknown_tab_lists_the_real_ones(fake_ribbon):
    r = catalog.ribbon_catalog(tab="NoSuchTab")
    assert r["success"] is False
    assert "NoSuchTab" in r["error"]
    assert r["available_tabs"] == ["Home", "Insert"]


def test_ribbon_catalog_search_finds_a_button_across_tabs(fake_ribbon):
    r = catalog.ribbon_catalog(search="macro")
    assert r["view"] == "search" and r["count"] == 1
    hit = r["commands"][0]
    assert hit["text"] == "Page macro"
    assert hit["tab"] == "Insert" and hit["group"] == "Macros"
    assert "tabs" not in r


def test_ribbon_catalog_resolves_builtin_buttons_via_command_index(fake_ribbon):
    """EPLAN leaves action_command_line empty for built-ins; the command-id
    join against the registry is what makes those buttons runnable."""
    r = catalog.ribbon_catalog(tab="Home")
    cmds = {c["text"]: c for c in r["tabs"][0]["groups"][0]["commands"]}
    assert cmds["Paste"]["resolved_action"] == "GfDlgMgrActionIGfWind"
    assert cmds["Paste"]["resolved_action_command_line"] == \
        "GfDlgMgrActionIGfWind /function:Paste"
    # an id absent from MFTools.xml stays unresolved rather than guessing
    assert "resolved_action" not in cmds["Mystery"]
    # These counters describe the WHOLE ribbon walk, not the filtered view, so
    # they stay stable across index/tab/search calls: 2 resolved (Paste,
    # Page macro) and 1 unresolved (Mystery) across both fake tabs.
    assert r["resolved_from_command_index"] == 2
    assert r["unresolved_commands"] == 1


# ---------------------------------------------------------------------------
# _build_action parameter-injection guard
#
# Found in security review. EPLAN re-parses the command string with
#   /([a-zA-Z0-9_]+):("([^"]*)"|([^\s]*))
# so a stray double quote inside a value lets it close its own token and append
# further /PARAM pairs. That silently defeated the registry allowlist in
# action_run(), which catalog.py documents as the SAFER alternative to
# execute_raw_action - so the documented security property was false.
# ---------------------------------------------------------------------------

from api.actions._base import _build_action


def test_build_action_rejects_quote_smuggled_parameter():
    """The exact exploit from the review: a value that closes its own quote and
    appends an un-validated /EXPORTFILE."""
    with pytest.raises(ValueError) as excinfo:
        _build_action("XPdfExportAction",
                      PROJECTNAME='"x" /EXPORTFILE:C:/evil.pdf')
    assert "double quote" in str(excinfo.value)
    assert "PROJECTNAME" in str(excinfo.value)


def test_build_action_rejects_quote_breakout_that_overrides_a_valid_key():
    """Second variant: last-wins parsing let an injected key OVERRIDE one that
    had already been validated."""
    with pytest.raises(ValueError):
        _build_action("label", CONFIGSCHEME='a b" /PROJECTNAME:C:/other.elk')


def test_action_run_surfaces_the_injection_refusal_as_an_error_dict():
    """action_run must not leak a traceback - it returns the standard shape."""
    r = catalog.action_run("backup",
                           {"PROJECTNAME": '"x" /ARCHIVENAME:evil'},
                           dry_run=True)
    assert r["success"] is False
    assert "double quote" in str(r.get("error", "")).lower()


def test_build_action_still_quotes_spaces_and_keeps_prequoted_values():
    """The guard must not break the legitimate quoting behaviour."""
    # a value with spaces gets quoted
    assert _build_action("backup", PROJECTNAME="C:/My Projects/x.elk") == (
        'backup /PROJECTNAME:"C:/My Projects/x.elk"')
    # a well-formed pre-quoted token is passed through, not double-quoted
    assert _build_action("backup", PROJECTNAME='"C:/My Projects/x.elk"') == (
        'backup /PROJECTNAME:"C:/My Projects/x.elk"')
    # no spaces, no quotes -> untouched
    assert _build_action("backup", TYPE="PROJECT") == "backup /TYPE:PROJECT"
    # tabs are whitespace too and must be quoted rather than emitted bare,
    # otherwise the value would split into two tokens on re-parse
    assert _build_action("x", A="a	b") == 'x /A:"a	b"'
    # backslash paths are not mangled
    assert _build_action("backup", P="C:" + chr(92) + "temp") == (
        "backup /P:C:" + chr(92) + "temp")


# ---------------------------------------------------------------------------
# Ribbon-label search
#
# The point of this: people look for the words on the button. Nobody searches
# for "GedEditGuiPosDialogShow" - they search for "Coordinate input". Labels are
# captured from a live ribbon by tools/capture_ribbon_labels.py and joined onto
# actions through the command id, because EPLAN leaves
# RibbonCommand.ActionCommandLine empty for built-in buttons.
# ---------------------------------------------------------------------------

def test_registry_carries_ribbon_labels(registry):
    actions, meta = registry
    labelled = [e for e in actions.values() if (e.get("gui") or {}).get("labels")]
    assert len(labelled) > 100, "ribbon labels missing from the registry"
    assert meta["counts"].get("labelled_actions") == len(labelled)
    for entry in labelled:
        gui = entry["gui"]
        assert all(isinstance(x, str) and x for x in gui["labels"])
        assert all(" > " in p or p for p in gui["ribbon_paths"])


def test_search_finds_an_action_by_its_button_text():
    """The headline capability: search the GUI wording, get the action."""
    result = catalog.action_catalog(search="Coordinate input", limit=10)
    assert result["count"] >= 1
    names = [a["name"] for a in result["actions"]]
    assert "GedEditGuiPosDialogShow" in names, names
    hit = next(a for a in result["actions"] if a["name"] == "GedEditGuiPosDialogShow")
    assert "Coordinate input" in hit["labels"]
    # and the result tells the caller where to find it in the GUI
    assert hit["ribbon_paths"]


def test_search_matches_the_ribbon_path_too():
    result = catalog.action_catalog(search="3D layout space", limit=50)
    assert result["count"] >= 1
    assert any("3D layout space" in p
               for a in result["actions"] for p in a["ribbon_paths"])


def test_label_search_does_not_break_plain_name_search():
    """Adding labels to the haystack must not disturb existing behaviour."""
    assert "backup" in [a["name"] for a in
                        catalog.action_catalog(search="backup", limit=200)["actions"]]


def test_eplan_29_ribbon_catalog_fails_fast_with_discovery_guidance(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(catalog, "get_manager",
                        lambda: SimpleNamespace(connected=True, target_version="2.9"))
    def no_script(*args, **kwargs):
        raise AssertionError("2.9 must not compile a ribbon script")
    monkeypatch.setattr(catalog, "_execute_script", no_script)
    result = catalog.ribbon_catalog()
    assert result["success"] is False
    assert result["target_version"] == "2.9"
    assert "eplan_tools_search" in result["hint"]


def test_eplan_29_action_catalog_labels_are_marked_as_2027(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(catalog, "get_manager",
                        lambda: SimpleNamespace(connected=True, target_version="2.9"))
    result = catalog.action_catalog(search="backup", limit=1)
    assert result["success"]
    assert "2027" in result["version_note"]
