"""
Turning a page read into observations, and the type filter that makes it possible.

Both of these were shaped by what a REAL production go-by turned out to look
like, not by what a textbook example looks like. The two corrections are worth
stating because each one silently produced an empty or useless profile:

  * A schematic page is mostly GRAPHICS. One measured Circuit page held 1887
    placements whose first 40 were all PolyLine, so an unfiltered read exhausts
    its limit before reaching a single device and the page looks empty.

  * Real device tags are not "-K1". The measured ones include "+1162-MA1",
    "+-RC:1" and "+-TEST:A30", so a prefix pattern written for the textbook
    shape classifies almost nothing.

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

from api.actions import profiles as P  # noqa: E402
from api.actions import schematic as S  # noqa: E402


# ---------------------------------------------------------------------------
# Device kind from a real tag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tag,kind", [
    # Measured on a production project.
    ("+1162-MA1", "MA"),
    ("+1162-MA2", "MA"),
    ("+-RC:1", "RC"),
    ("+-TEST:A30", "TEST"),
    ("+-TEST:PORT 1", "TEST"),
    # Textbook shapes must keep working.
    ("-K1", "K"),
    ("=AP+ST1-Q2", "Q"),
    ("+A1-FU12", "FU"),
])
def test_device_kind_from_a_real_tag(tag, kind):
    assert P._kind_of(tag) == kind


@pytest.mark.parametrize("tag", ["+", "-", "=", "", None, "   "])
def test_an_untagged_function_teaches_no_kind(tag):
    """
    A freshly placed function is named "+" until tagged (measured on 2027).
    Inventing a kind for it would poison the vocabulary with a phantom device.
    """
    assert P._kind_of(tag) is None


def test_a_prefix_nobody_standardised_is_still_learned():
    """The profile describes what a client DOES, not what a standard says."""
    assert P._kind_of("+X9-WEIRD1") == "WEIRD"


# ---------------------------------------------------------------------------
# Name shapes: the pattern generalises, the customer's values do not leak
# ---------------------------------------------------------------------------

def test_a_shape_keeps_structure_and_drops_the_values():
    shape = P._shape_of("=AP+ST1-K12")
    assert "AP" not in shape and "ST" not in shape and "12" not in shape
    assert "=" in shape and "+" in shape and "-" in shape


def test_tags_that_differ_only_by_number_share_a_shape():
    assert P._shape_of("+1162-MA1") == P._shape_of("+1162-MA2")
    assert P._shape_of("-K1") == P._shape_of("-K9")


def test_a_page_name_shape_matches_the_measured_one():
    """Real page names on the go-by look like '&ADD#BA01/1'."""
    assert P._shape_of("&ADD#BA01/1") == P._shape_of("&ADD#BC01/6")


def test_shape_of_nothing_is_none():
    assert P._shape_of(None) is None


# ---------------------------------------------------------------------------
# Observation extraction
# ---------------------------------------------------------------------------

def _page(placements, name="&ADD#BC01/1", page_type="Circuit", grid=1.27):
    return {"page": name, "pageType": page_type, "gridSize": grid,
            "placements": placements}


# The exact shape DumpPlacement emits: the symbol block's key is "name", not
# "symbol". Copied from a real live read rather than written from memory - the
# first version of this fixture used "symbol" and quietly tested nothing.
FUSE = {
    "clrType": "Function", "name": "+1162-FU1",
    "location": {"x": 60.0, "y": 200.0},
    "symbol": {"library": "Symbol Library - East River", "name": "F1",
               "variantNr": 3},
}


def test_a_tagged_function_teaches_symbol_and_tag_keyed_by_kind():
    obs = P._observe_page(_page([FUSE]))
    assert obs["symbols"]["FU"][0]["library"] == "Symbol Library - East River"
    assert "FU" in obs["tags"]
    assert "any" in obs["tags"], "an overall tag shape is useful too"


def test_repeats_are_kept_because_frequency_is_the_evidence():
    obs = P._observe_page(_page([FUSE, FUSE, FUSE]))
    assert len(obs["symbols"]["FU"]) == 3, (
        "de-duping here would make forty pages count the same as one"
    )


def test_an_untagged_function_still_teaches_vocabulary_under_its_type():
    anon = dict(FUSE, name="+")
    obs = P._observe_page(_page([anon]))
    assert "FU" not in obs.get("symbols", {})
    assert any(k.startswith("clr:") for k in obs["symbols"])


def test_page_type_and_grid_are_learned():
    obs = P._observe_page(_page([FUSE]))
    assert obs["pages"]["documentType"] == ["Circuit"]
    assert obs["geometry"]["gridSize"] == [1.27]


def test_device_spacing_is_learned_as_a_distribution_not_an_average():
    """
    A real page has a rung pitch AND wider gaps between groups. An average is
    neither, so the gaps are recorded individually and let counts decide.
    """
    a = dict(FUSE, location={"x": 10.0, "y": 200.0})
    b = dict(FUSE, location={"x": 20.0, "y": 200.0})
    c = dict(FUSE, location={"x": 30.0, "y": 200.0})
    d = dict(FUSE, location={"x": 100.0, "y": 200.0})
    obs = P._observe_page(_page([a, b, c, d]))
    gaps = obs["geometry"]["deviceSpacingX"]
    assert 10.0 in gaps and 70.0 in gaps


def test_a_page_with_no_devices_teaches_only_page_level_facts():
    obs = P._observe_page(_page([], page_type="ExternalDocument"))
    assert "symbols" not in obs and "tags" not in obs
    assert obs["pages"]["documentType"] == ["ExternalDocument"]


def test_a_placement_with_no_symbol_is_skipped_not_recorded_as_empty():
    poly = {"clrType": "PolyLine", "name": None, "location": {"x": 1.0, "y": 2.0}}
    obs = P._observe_page(_page([poly]))
    assert "symbols" not in obs


# ---------------------------------------------------------------------------
# The type filter on live_read_page
# ---------------------------------------------------------------------------

@pytest.fixture
def capture(monkeypatch):
    seen = {}

    def fake(script, timeout=30.0):
        seen["cs"] = script
        return {"success": True, "results": {"success": True}}

    monkeypatch.setattr(S, "_execute_script", fake)
    return seen


def test_read_page_without_types_filters_nothing(capture):
    S.live_read_page("P")
    assert "ReadPage(page, 200, true, null)" in capture["cs"]


def test_read_page_with_types_emits_a_csharp_array(capture):
    S.live_read_page("P", types=["Function"])
    assert 'new string[] { "Function" }' in capture["cs"]


def test_a_single_type_string_is_accepted(capture):
    S.live_read_page("P", types="Function")
    assert 'new string[] { "Function" }' in capture["cs"]


def test_type_names_are_escaped(capture):
    S.live_read_page("P", types=['Fun"ction'])
    assert 'Fun\\"ction' in capture["cs"]


def test_a_result_path_token_in_a_type_is_refused(capture):
    result = S.live_read_page("P", types=["{{RESULT_PATH}}"])
    assert result["success"] is False
    assert "cs" not in capture


def test_the_true_page_total_is_reported_alongside_the_filtered_count(capture):
    """
    A filtered read must never look like an empty page - that is exactly the
    confusion the filter exists to remove.
    """
    S.live_read_page("P", types=["Function"])
    cs = capture["cs"]
    assert 'd["placementCount"] = total;' in cs
    assert 'd["matched"] = matched;' in cs


def test_writes_report_their_page_after_unfiltered(capture):
    """A write's own proof should show everything it affected, not a subset."""
    S.live_place_symbol("P", "LIB", "SL", 1.0, 2.0)
    assert "ReadPage(page, 200, true, null)" in capture["cs"]
