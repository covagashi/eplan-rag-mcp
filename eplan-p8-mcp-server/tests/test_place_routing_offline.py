"""
Placing connection symbols: corners and T-nodes.

Three measured facts drive the design here.

1. A routing symbol is NOT a Function. `Function.Create` refuses one with
   `S511085Cannot create function`, which names no cause. The path that works is
   `SymbolVariant.Create(Page)`, returning a `SymbolReference`. So the two kinds
   of symbol need two tools, and each refuses the other's input by name.

2. `SymbolVariant.Create` takes no coordinate - the object is born at the page
   ORIGIN and must be moved. Leaving one there is the failure mode being
   guarded against, so the script verifies the move landed.

3. Variants of one symbol facing the same directions are NOT interchangeable.
   Measured on `SPECIAL_en_US/TLRU`, whose five `Down+Left+Right` variants put
   the pins in different PLACES: v8 has all three at the vertex, v0 pushes its
   Right pin one grid step out. So an unresolved variant is a refusal carrying
   the real geometry, not a silent pick of the lowest number.

Runs with EPLAN closed.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.join(os.path.dirname(HERE), "mcp_server")
for p in (MCP, os.path.join(MCP, "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

from api.actions import schematic as S  # noqa: E402


# One corner, one T-node type with two rival symbols, shaped like a real read.
CORNER = {
    "library": "SPECIAL_en_US", "symbol": "CO", "type": "Routing",
    "variants": [
        {"variantNr": 0, "directions": ["Right", "Down"],
         "pins": [{"direction": "Right", "offset": {"x": 0.0, "y": 0.0}},
                  {"direction": "Down", "offset": {"x": 0.0, "y": 0.0}}]},
        {"variantNr": 1, "directions": ["Right", "Up"],
         "pins": [{"direction": "Right", "offset": {"x": 0.0, "y": 0.0}},
                  {"direction": "Up", "offset": {"x": 0.0, "y": 0.0}}]},
    ],
}
TLRU = {
    "library": "SPECIAL_en_US", "symbol": "TLRU", "type": "TNodeDown",
    "variants": [
        {"variantNr": 0, "directions": ["Down", "Left", "Right"],
         "pins": [{"direction": "Down", "offset": {"x": 0.0, "y": 0.0}},
                  {"direction": "Left", "offset": {"x": 0.0, "y": 0.0}},
                  {"direction": "Right", "offset": {"x": 3.175, "y": 0.0}}]},
        {"variantNr": 8, "directions": ["Right", "Left", "Down"],
         "pins": [{"direction": "Right", "offset": {"x": 0.0, "y": 0.0}},
                  {"direction": "Left", "offset": {"x": 0.0, "y": 0.0}},
                  {"direction": "Down", "offset": {"x": 0.0, "y": 0.0}}]},
    ],
}
_UP_VARIANTS = [
    {"variantNr": 8, "directions": ["Right", "Left", "Up"],
     "pins": [{"direction": "Right", "offset": {"x": 0.0, "y": 0.0}},
              {"direction": "Left", "offset": {"x": 0.0, "y": 0.0}},
              {"direction": "Up", "offset": {"x": 0.0, "y": 0.0}}]},
]
TLRO = {"library": "SPECIAL_en_US", "symbol": "TLRO", "type": "TNodeUp",
        "variants": _UP_VARIANTS}
TLRO_1 = {"library": "SPECIAL_en_US", "symbol": "TLRO_1", "type": "TNodeUp",
          "variants": _UP_VARIANTS}


@pytest.fixture
def eplan(monkeypatch):
    """A fake EPLAN: catalog reads answer from `state`, writes are recorded."""
    state = {"symbols": [], "scripts": [], "placed": [], "walks": [],
             "project": "P1", "place_ok": True}
    S._routing_catalog_forget()

    def fake(script, timeout=30.0):
        state["scripts"].append(script)
        if "wantTypes" in script:                       # a catalog read
            state["walks"].append(script)
            return {"success": True, "results": {
                "success": True, "project": state["project"],
                "libraries": ["SPECIAL_en_US"],
                "symbols": [dict(s) for s in state["symbols"]]}}
        state["placed"].append(script)                   # a placement
        if not state["place_ok"]:
            return {"success": True, "results": {
                "success": False, "project": state["project"],
                "error": "no such symbol"}}
        return {"success": True, "results": {
            "success": True, "page": "+P/1", "handle": "h1",
            "project": state["project"],
            "symbolType": "Routing",
            "placed": {"location": {"x": 10.0, "y": 20.0},
                       "boundingBox": [{"x": 9.0, "y": 19.0},
                                       {"x": 11.0, "y": 21.0}],
                       "pins": [{"index": 0, "direction": "Right",
                                 "raw": {"x": 0.0, "y": 0.0}}]}}}

    monkeypatch.setattr(S, "_execute_script", fake)
    return state


# ---------------------------------------------------------------------------
# The two placement paths stay apart
# ---------------------------------------------------------------------------

def test_the_device_placer_names_the_real_problem_with_a_routing_symbol(eplan):
    """
    "S511085Cannot create function" says nothing a caller can act on. The
    preflight turns it into the tool they should have called.
    """
    eplan["symbols"] = [CORNER]
    S.live_place_symbol("+P/1", "SPECIAL_en_US", "CO", 10.0, 20.0)
    cs = eplan["scripts"][-1]
    # NOT `assert "ROUTING_TYPES" in cs` - that array is in the shared helpers
    # now, so it is present in every schematic script and proves nothing. Check
    # the preflight's own text.
    assert "Function.Create cannot" in cs
    assert "live_place_connection_symbol" in cs


def test_the_connection_placer_refuses_a_device(eplan):
    S.live_place_connection_symbol("+P/1", "LIB", "SL", 10.0, 20.0)
    cs = eplan["scripts"][-1]
    assert "a device, not a" in cs
    assert "live_place_symbol" in cs


def test_the_connection_placer_uses_symbolvariant_create_not_function_create(eplan):
    S.live_place_connection_symbol("+P/1", "LIB", "CO", 10.0, 20.0)
    cs = eplan["scripts"][-1]
    assert 'MethodByShape(varType, "Create", new string[] { "Page" }' in cs
    assert "Eplan.EplApi.DataModel.Function" not in cs


# ---------------------------------------------------------------------------
# The page origin
# ---------------------------------------------------------------------------

def test_a_symbol_born_at_the_origin_is_moved_and_the_move_is_verified(eplan):
    """
    Create(Page) takes no coordinate. An unmoved connection symbol sits on the
    page frame and autoconnects to whatever else is near (0,0), so the script
    checks that it actually landed.
    """
    S.live_place_connection_symbol("+P/1", "LIB", "CO", 10.0, 20.0)
    cs = eplan["scripts"][-1]
    assert 'GetWritable(sref.GetType(), "Location")' in cs
    move = cs.index("locProp.SetValue(sref")
    check = cs.index("The symbol did not move")
    assert move < check
    assert "still near the page origin" in cs


def test_an_unmovable_symbol_is_a_hard_error(eplan):
    S.live_place_connection_symbol("+P/1", "LIB", "CO", 10.0, 20.0)
    assert "Refusing to leave it there" in eplan["scripts"][-1]


def test_the_placement_is_scratch_guarded(eplan):
    S.live_place_connection_symbol("+P/1", "LIB", "CO", 10.0, 20.0)
    assert "GuardScratch(project," in eplan["scripts"][-1]


def test_writing_to_a_real_project_needs_the_flag(eplan):
    S.live_place_connection_symbol("+P/1", "LIB", "CO", 10.0, 20.0,
                                   allow_real_project=True)
    assert "GuardScratch(project, true," in eplan["scripts"][-1]


# ---------------------------------------------------------------------------
# Corners
# ---------------------------------------------------------------------------

def test_a_corner_is_looked_up_not_hardcoded(eplan):
    eplan["symbols"] = [CORNER]
    out = S.live_place_corner("+P/1", 10.0, 20.0, ["Right", "Down"])
    assert out["success"]
    assert out["chosen"]["symbol"] == "CO"
    assert out["chosen"]["variantNr"] == 0


def test_the_variant_is_derived_from_the_directions(eplan):
    eplan["symbols"] = [CORNER]
    assert S.live_place_corner("+P/1", 10.0, 20.0,
                               ["Right", "Up"])["chosen"]["variantNr"] == 1


def test_corner_direction_order_is_irrelevant(eplan):
    eplan["symbols"] = [CORNER]
    a = S.live_place_corner("+P/1", 10.0, 20.0, ["Right", "Down"])
    b = S.live_place_corner("+P/1", 10.0, 20.0, ["Down", "Right"])
    assert a["chosen"]["variantNr"] == b["chosen"]["variantNr"]


def test_a_corner_needs_exactly_two_directions(eplan):
    eplan["symbols"] = [CORNER]
    out = S.live_place_corner("+P/1", 10.0, 20.0, ["Right"])
    assert out["success"] is False
    assert "live_place_tnode" in out["error"]
    assert not eplan["scripts"]


def test_two_identical_directions_are_refused_as_a_straight_run(eplan):
    """A straight run needs no symbol at all - EPLAN autoconnects it."""
    eplan["symbols"] = [CORNER]
    out = S.live_place_corner("+P/1", 10.0, 20.0, ["Right", "Right"])
    assert out["success"] is False
    assert "autoconnects" in out["error"]
    assert not eplan["scripts"]


def test_a_project_with_no_matching_corner_says_so(eplan):
    eplan["symbols"] = [CORNER]
    out = S.live_place_corner("+P/1", 10.0, 20.0, ["Left", "Down"])
    assert out["success"] is False
    assert "live_routing_catalog" in out["error"]


# ---------------------------------------------------------------------------
# T-nodes
# ---------------------------------------------------------------------------

def test_a_tnode_is_chosen_by_TYPE_because_direction_is_not_a_variant(eplan):
    """
    The asymmetry with corners is EPLAN's: TNodeUp and TNodeDown are separate
    Symbol.Types, where a corner's four rotations are variants of one symbol.
    """
    assert S.TNODE_TYPE_BY_DIRECTION["Up"] == "TNodeUp"
    assert S.TNODE_TYPE_BY_DIRECTION["Down"] == "TNodeDown"
    eplan["symbols"] = [TLRU]
    S.live_place_tnode("+P/1", 10.0, 20.0, "Down", variant_nr=8)
    assert '"TNodeDown"' in eplan["scripts"][0]


def test_the_branch_direction_implies_the_other_two_legs(eplan):
    eplan["symbols"] = [TLRU]
    out = S.live_place_tnode("+P/1", 10.0, 20.0, "Down", variant_nr=8)
    assert sorted(out["chosen"]["directions"]) == ["Down", "Left", "Right"]


def test_a_bad_branch_direction_is_refused(eplan):
    out = S.live_place_tnode("+P/1", 10.0, 20.0, "Sideways")
    assert out["success"] is False
    assert not eplan["scripts"]


def test_two_rival_symbols_of_one_type_are_never_chosen_between(eplan):
    """TLRO and TLRO_1 are both TNodeUp. Picking one would be a coin flip."""
    eplan["symbols"] = [TLRO, TLRO_1]
    out = S.live_place_tnode("+P/1", 10.0, 20.0, "Up")
    assert out["success"] is False
    assert out["ambiguous"] is True
    assert {c["symbol"] for c in out["candidates"]} == {"TLRO", "TLRO_1"}
    assert not eplan["placed"], "it wrote despite being unable to choose"


def test_naming_the_symbol_resolves_a_rivalry(eplan):
    eplan["symbols"] = [TLRO, TLRO_1]
    out = S.live_place_tnode("+P/1", 10.0, 20.0, "Up",
                             symbol="TLRO_1", variant_nr=8)
    assert out["success"]
    assert out["chosen"]["symbol"] == "TLRO_1"


# ---------------------------------------------------------------------------
# Variants of one symbol are not interchangeable
# ---------------------------------------------------------------------------

def test_rival_variants_are_refused_with_their_real_geometry(eplan):
    """
    The refusal has to carry the pin OFFSETS. Saying only "5 variants match"
    leaves the caller no way to choose, and an earlier version of this message
    claimed they differed in pin ORDER - which is false for TLRU, whose pins
    sit in different PLACES.
    """
    eplan["symbols"] = [TLRU]
    out = S.live_place_tnode("+P/1", 10.0, 20.0, "Down")
    assert out["success"] is False
    assert out["ambiguous"] is True
    assert "NOT" in out["error"] and "different places" in out["error"]
    assert "+3.17" in out["error"], "the offset that distinguishes v0 is missing"
    assert "ORDER" not in out["error"]
    assert not eplan["placed"]


def test_an_explicit_variant_resolves_the_rivalry(eplan):
    eplan["symbols"] = [TLRU]
    out = S.live_place_tnode("+P/1", 10.0, 20.0, "Down", variant_nr=8)
    assert out["success"]
    assert out["chosen"]["variantNr"] == 8


def test_a_variant_that_does_not_face_the_right_way_is_refused(eplan):
    eplan["symbols"] = [TLRU]
    out = S.live_place_tnode("+P/1", 10.0, 20.0, "Down", variant_nr=5)
    assert out["success"] is False
    assert "does not face" in out["error"]
    assert not eplan["placed"]


def test_the_reason_for_the_choice_is_reported(eplan):
    eplan["symbols"] = [CORNER]
    out = S.live_place_corner("+P/1", 10.0, 20.0, ["Right", "Down"])
    assert "the only" in out["chosen"]["why"]
    assert "Routing" in out["chosen"]["why"]


def test_the_reason_does_not_claim_uniqueness_the_caller_overrode(eplan):
    """
    With symbol= or variant_nr= passed there may well have been rivals, and
    saying "the only one" would be a false statement about exactly the choice
    this tool refuses to make on its own.
    """
    eplan["symbols"] = [TLRO, TLRO_1]
    out = S.live_place_tnode("+P/1", 10.0, 20.0, "Up",
                             symbol="TLRO_1", variant_nr=8)
    why = out["chosen"]["why"]
    assert "the only" not in why
    assert "caller" in why
    assert "2" in why, "the number of rivals it was chosen from is missing"


# ---------------------------------------------------------------------------
# The usual injection and validation boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["{{RESULT_PATH}}", None, 5])
def test_a_hostile_page_never_reaches_a_script(bad, eplan):
    out = S.live_place_connection_symbol(bad, "LIB", "CO", 1.0, 2.0)
    assert out["success"] is False
    assert not eplan["scripts"]


def test_a_page_named_after_a_token_survives(eplan):
    """SNAP, VARNR and XVAL are token names; a page called one must not corrupt."""
    S.live_place_connection_symbol("+SNAP/1", "LIB", "CO", 1.0, 2.0)
    assert '"+SNAP/1"' in eplan["scripts"][-1]


def test_a_negative_variant_is_refused(eplan):
    out = S.live_place_connection_symbol("+P/1", "LIB", "CO", 1.0, 2.0,
                                         variant_nr=-1)
    assert out["success"] is False
    assert not eplan["scripts"]


def test_snapping_is_on_by_default_and_can_be_turned_off(eplan):
    S.live_place_connection_symbol("+P/1", "LIB", "CO", 1.0, 2.0)
    assert "bool SNAP" not in eplan["scripts"][-1]
    assert "if (true && grid > 0.0001)" in eplan["scripts"][-1]
    S.live_place_connection_symbol("+P/1", "LIB", "CO", 1.0, 2.0,
                                   snap_to_grid=False)
    assert "if (false && grid > 0.0001)" in eplan["scripts"][-1]


def test_a_successful_placement_offers_its_undo(eplan):
    out = S.live_place_connection_symbol("+P/1", "LIB", "CO", 1.0, 2.0)
    assert out["undo"]["tool"] == "eplan_live_remove_placement"
    assert out["undo"]["handle"] == "h1"


def test_a_failed_catalog_read_is_not_dressed_up_as_a_placement(monkeypatch):
    monkeypatch.setattr(S, "_execute_script", lambda script, timeout=30.0: {
        "success": False, "message": "no project open"})
    assert S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])["success"] is False


# ---------------------------------------------------------------------------
# The catalog walk is cached between placements
# ---------------------------------------------------------------------------

def test_repeat_placements_walk_the_libraries_once(eplan):
    eplan["symbols"] = [CORNER]
    S.live_place_corner("+P/1", 10.0, 20.0, ["Right", "Down"])
    S.live_place_corner("+P/1", 30.0, 20.0, ["Right", "Up"])
    S.live_place_corner("+P/1", 50.0, 20.0, ["Down", "Right"])
    assert len(eplan["walks"]) == 1
    assert len(eplan["placed"]) == 3


def test_a_cache_hit_answers_exactly_like_a_fresh_read(eplan):
    eplan["symbols"] = [CORNER]
    first = S.live_place_corner("+P/1", 10.0, 20.0, ["Right", "Up"])
    second = S.live_place_corner("+P/1", 30.0, 20.0, ["Right", "Up"])
    assert first["chosen"] == second["chosen"]
    assert second["chosen"]["variantNr"] == 1


def test_each_tnode_type_is_its_own_walk_and_corners_are_another(eplan):
    eplan["symbols"] = [CORNER, TLRU, TLRO]
    S.live_place_tnode("+P/1", 1.0, 2.0, "Down")
    S.live_place_tnode("+P/1", 3.0, 2.0, "Down")
    S.live_place_tnode("+P/1", 5.0, 2.0, "Up")
    S.live_place_corner("+P/1", 7.0, 2.0, ["Right", "Down"])
    assert len(eplan["walks"]) == 3


def test_a_refusal_does_not_poison_the_cache(eplan):
    """A walk that finds nothing is still a valid walk of THIS project."""
    eplan["symbols"] = []
    assert S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])["success"] is False
    assert S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])["success"] is False
    assert len(eplan["walks"]) == 1


def test_a_failed_read_is_never_cached(monkeypatch, eplan):
    calls = []

    def flaky(script, timeout=30.0):
        calls.append(script)
        return {"success": False, "message": "no project open"}

    monkeypatch.setattr(S, "_execute_script", flaky)
    S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])
    S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])
    assert len(calls) == 2


def test_opening_or_closing_a_project_invalidates_the_cache(eplan):
    from api.actions import _project_cache
    eplan["symbols"] = [CORNER]
    S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])
    _project_cache.bump()
    S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])
    assert len(eplan["walks"]) == 2


def test_the_cache_expires(eplan, monkeypatch):
    eplan["symbols"] = [CORNER]
    S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])
    now = S.time.monotonic()
    monkeypatch.setattr(S.time, "monotonic",
                        lambda: now + S.ROUTING_CATALOG_TTL_SECONDS + 1)
    S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])
    assert len(eplan["walks"]) == 2


def test_a_placement_that_lands_in_another_project_drops_the_cache(eplan):
    """The user switched projects in the GUI; the write-back reveals it."""
    eplan["symbols"] = [CORNER]
    S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])
    eplan["project"] = "P2"
    S.live_place_corner("+P/1", 3.0, 2.0, ["Right", "Down"])
    assert len(eplan["walks"]) == 1          # the hit was trusted for the write
    S.live_place_corner("+P/1", 5.0, 2.0, ["Right", "Down"])
    assert len(eplan["walks"]) == 2          # but not after it


def test_a_failed_placement_on_a_cache_hit_is_retried_from_a_fresh_read(eplan):
    eplan["symbols"] = [CORNER]
    S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])
    eplan["place_ok"] = False
    out = S.live_place_corner("+P/1", 3.0, 2.0, ["Right", "Down"])
    assert out["success"] is False
    assert len(eplan["walks"]) == 2
    assert len(eplan["placed"]) == 3         # the retry placed once more, not twice


def test_the_public_catalog_tool_always_reads_fresh_but_warms_the_cache(eplan):
    eplan["symbols"] = [CORNER]
    S.live_routing_catalog(symbol_type="Routing")
    S.live_routing_catalog(symbol_type="Routing")
    assert len(eplan["walks"]) == 2
    S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])
    assert len(eplan["walks"]) == 2


def test_the_cache_hands_out_copies_not_its_own_entry(eplan):
    """Direction filtering drops symbols; it must drop them from a copy."""
    other = dict(CORNER, symbol="CO_LD", variants=[
        {"variantNr": 0, "directions": ["Left", "Down"], "pins": []}])
    eplan["symbols"] = [CORNER, other]
    assert S.live_place_corner("+P/1", 1.0, 2.0, ["Right", "Down"])["success"]
    out = S.live_place_corner("+P/1", 3.0, 2.0, ["Left", "Down"])
    assert out["success"] and out["chosen"]["symbol"] == "CO_LD"
    assert len(eplan["walks"]) == 1
