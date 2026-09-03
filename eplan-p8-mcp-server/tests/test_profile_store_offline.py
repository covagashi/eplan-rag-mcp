"""
The convention-profile store: continual learning, and where profiles live.

Two properties carry the whole design, and both are asserted here:

  1. LEARNING ACCUMULATES. Reading a second project must sharpen a profile, not
     replace it. If merging ever became last-write-wins, a profile would only
     ever describe whatever was read most recently - which is the opposite of
     the point.

  2. PROFILES STAY OUT OF THE REPOSITORY. A client's conventions are customer
     data and this repo is public.

Everything here runs with EPLAN closed.
"""

import io
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_SERVER = os.path.dirname(HERE)
MCP = os.path.join(REPO_SERVER, "mcp_server")
for p in (MCP, os.path.join(MCP, "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

from api.actions import profile_store as PS  # noqa: E402


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Point the store at a temp dir - never the real profile location."""
    monkeypatch.setenv("EPLAN_MCP_PROFILES", str(tmp_path))
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Where profiles live
# ---------------------------------------------------------------------------

def test_profile_root_honours_the_env_var(root):
    assert PS.profile_root() == os.path.abspath(root)


def test_profile_root_falls_back_to_a_per_user_directory(monkeypatch):
    monkeypatch.delenv("EPLAN_MCP_PROFILES", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert PS.profile_root().startswith(r"C:\Users\someone\AppData\Local")


def test_the_default_root_is_never_inside_this_repository(monkeypatch):
    """
    The guard that matters: a client's standards must not land in a public tree.
    """
    monkeypatch.delenv("EPLAN_MCP_PROFILES", raising=False)
    repo = os.path.abspath(os.path.dirname(REPO_SERVER))
    assert os.path.commonpath([os.path.abspath(PS.profile_root()), repo]) != repo


@pytest.mark.parametrize("bad", [
    "../escape", "sub/dir", "sub\\dir", "C:/abs", "", "   ", None, 5,
    "a" * 65, ".hidden",
])
def test_a_profile_name_that_is_not_a_bare_filename_is_refused(bad, root):
    with pytest.raises(PS.ProfileError):
        PS.profile_path(bad)


@pytest.mark.parametrize("ok", ["acme", "Acme Corp", "client_1", "a.b-c"])
def test_ordinary_client_names_are_accepted(ok, root):
    assert PS.profile_path(ok).endswith(".json")


# ---------------------------------------------------------------------------
# Continual learning - the core property
# ---------------------------------------------------------------------------

SYM_A = {"library": "NFPA_symbol_en_US", "symbol": "SL", "variantNr": 0}
SYM_B = {"library": "NFPA_symbol_en_US", "symbol": "Q1", "variantNr": 0}


def test_learning_the_same_thing_twice_increases_the_count_not_the_entries(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}}, source="job1")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}}, source="job2")
    candidates = prof["observations"]["symbols"]["K"]
    assert len(candidates) == 1, "the same observation created a duplicate entry"
    assert candidates[0]["count"] == 2


def test_a_second_project_adds_to_a_profile_rather_than_replacing_it(root):
    """If this ever became last-write-wins the whole design collapses."""
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A] * 5}}, source="job1")
    PS.merge_observations(prof, {"symbols": {"Q": [SYM_B] * 2}}, source="job2")
    assert set(prof["observations"]["symbols"]) == {"K", "Q"}
    assert prof["observations"]["symbols"]["K"][0]["count"] == 5


def test_a_rare_one_off_never_outranks_the_house_norm(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A] * 47}}, source="many")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_B]}}, source="odd")
    best = PS.suggest(prof, "symbols", key="K")
    assert best["best"] == SYM_A
    assert best["confidence"] > 0.9
    assert len(best["suggestions"][0]["alternatives"]) == 2, "the one-off must still be visible"


def test_candidates_are_ordered_by_support(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_B, SYM_A, SYM_A]}})
    values = [c["value"] for c in prof["observations"]["symbols"]["K"]]
    assert values[0] == SYM_A


def test_provenance_is_recorded_but_bounded(root):
    prof = PS.new_profile("acme")
    for i in range(40):
        PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}}, source="job%d" % i)
    entry = prof["observations"]["symbols"]["K"][0]
    assert entry["count"] == 40, "the count is the evidence and must be exact"
    assert len(entry["sources"]) <= PS._MAX_SOURCES_PER_CANDIDATE


def test_sources_track_how_often_each_project_was_learned_from(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}}, source="job1")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}}, source="job1")
    src = [s for s in prof["sources"] if s["source"] == "job1"][0]
    assert src["timesLearned"] == 2


def test_merge_reports_what_changed(root):
    prof = PS.new_profile("acme")
    first = PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}})
    second = PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}})
    assert first["added"] == 1 and first["reinforced"] == 0
    assert second["added"] == 0 and second["reinforced"] == 1


def test_an_unknown_category_is_still_stored(root):
    """A future extractor must not need a change to profile_store."""
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"wiring": {"colour": ["BK"]}})
    assert prof["observations"]["wiring"]["colour"][0]["value"] == "BK"


def test_merging_nothing_is_harmless(root):
    prof = PS.new_profile("acme")
    assert PS.merge_observations(prof, {}) == {"added": 0, "reinforced": 0}
    assert PS.merge_observations(prof, None) == {"added": 0, "reinforced": 0}


# ---------------------------------------------------------------------------
# suggest() must never manufacture confidence
# ---------------------------------------------------------------------------

def test_suggest_always_returns_alternatives_with_support(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A] * 12 + [SYM_B] * 11}})
    out = PS.suggest(prof, "symbols", key="K")
    alts = out["suggestions"][0]["alternatives"]
    assert len(alts) == 2
    assert abs(alts[0]["share"] - 12 / 23) < 0.01
    assert 0.5 < out["confidence"] < 0.55, "a near-tie must not look settled"


def test_a_thin_profile_says_so(root):
    """One observation can be 100% consistent and still be one page."""
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}})
    out = PS.suggest(prof, "symbols", key="K")
    assert out["confidence"] == 1.0
    assert "caution" in out and "one project" in out["caution"]


def test_a_well_supported_profile_carries_no_caution(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A] * 10}})
    assert "caution" not in PS.suggest(prof, "symbols", key="K")


def test_asking_about_something_unknown_lists_what_is_known(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}})
    out = PS.suggest(prof, "symbols", key="ZZ")
    assert out["known"] is False
    assert "K" in out["message"]


def test_suggest_with_no_key_returns_every_key(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A], "Q": [SYM_B]}})
    out = PS.suggest(prof, "symbols")
    assert {s["key"] for s in out["suggestions"]} == {"K", "Q"}


# ---------------------------------------------------------------------------
# Correction
# ---------------------------------------------------------------------------

def test_forget_removes_one_candidate(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A, SYM_B]}})
    result = PS.forget(prof, "symbols", "K", SYM_B)
    assert result["removed"] == 1
    assert [c["value"] for c in prof["observations"]["symbols"]["K"]] == [SYM_A]


def test_forget_without_a_value_drops_the_whole_key(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A, SYM_B]}})
    assert PS.forget(prof, "symbols", "K")["removed"] == 2
    assert "K" not in prof["observations"]["symbols"]


def test_forgetting_something_unknown_is_not_an_error(root):
    prof = PS.new_profile("acme")
    assert PS.forget(prof, "symbols", "nope")["removed"] == 0


# ---------------------------------------------------------------------------
# Persistence - the file is meant to be hand-edited and diffed
# ---------------------------------------------------------------------------

def test_round_trip(root):
    prof = PS.new_profile("acme", standard="NFPA")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}}, source="job1")
    PS.save_profile(prof)
    back = PS.load_profile("acme")
    assert back["standard"] == "NFPA"
    assert back["observations"]["symbols"]["K"][0]["count"] == 1


def test_the_file_is_sorted_so_relearning_the_same_thing_makes_no_diff(root):
    prof = PS.new_profile("acme", now="2026-01-01T00:00:00")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A], "Q": [SYM_B]}},
                          now="2026-01-01T00:00:00")
    first = io.open(PS.save_profile(prof), encoding="utf-8").read()

    prof2 = PS.load_profile("acme")
    PS.save_profile(prof2)
    second = io.open(PS.profile_path("acme"), encoding="utf-8").read()
    assert first == second, "a no-op save produced a diff"


def test_a_saved_profile_is_readable_json_a_human_can_edit(root):
    prof = PS.new_profile("acme")
    PS.merge_observations(prof, {"symbols": {"K": [SYM_A]}})
    text = io.open(PS.save_profile(prof), encoding="utf-8").read()
    assert text.endswith("\n")
    assert "\n  " in text, "not indented"
    json.loads(text)


def test_a_hand_edited_profile_missing_keys_still_loads(root):
    """An engineer will edit these by hand; do not refuse their file."""
    os.makedirs(PS.profile_root(), exist_ok=True)
    io.open(PS.profile_path("terse"), "w", encoding="utf-8").write('{"name": "terse"}')
    prof = PS.load_profile("terse")
    assert set(prof["observations"]) >= set(PS.CATEGORIES)


def test_corrupt_json_says_it_was_probably_a_manual_edit(root):
    os.makedirs(PS.profile_root(), exist_ok=True)
    io.open(PS.profile_path("broken"), "w", encoding="utf-8").write("{not json")
    with pytest.raises(PS.ProfileError) as exc:
        PS.load_profile("broken")
    assert "hand-editable" in str(exc.value)


def test_a_missing_profile_names_the_directory_it_looked_in(root):
    with pytest.raises(PS.ProfileError) as exc:
        PS.load_profile("nope")
    assert root in str(exc.value)


def test_missing_ok_returns_none(root):
    assert PS.load_profile("nope", missing_ok=True) is None


def test_list_profiles_reports_the_root_even_when_empty(root):
    out = PS.list_profiles()
    assert out["root"] == os.path.abspath(root) and out["profiles"] == []


def test_saving_leaves_no_temp_file_behind(root):
    prof = PS.new_profile("acme")
    PS.save_profile(prof)
    assert [f for f in os.listdir(root) if f.endswith(".tmp")] == []
