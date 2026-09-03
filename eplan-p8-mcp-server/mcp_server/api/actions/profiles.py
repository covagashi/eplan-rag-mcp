"""
Learn a project's conventions from a go-by, and answer questions from them.

    eplan_profile_learn      read the open project, fold what it does into a profile
    eplan_profile_list       which profiles exist, and where
    eplan_profile_get        the whole profile, or one category
    eplan_profile_suggest    "what do we use for a contactor?" - ranked, with evidence
    eplan_profile_forget     remove a bad inference

Extraction deliberately reuses `live_read_page` from schematic.py rather than
adding a second reader: two readers would drift, and any device kind that
becomes visible to the schematic layer becomes learnable here for free.

WHAT A "DEVICE KIND" IS, and why it is inferred rather than configured.
EPLAN does not label a placement "contactor". What it has is the device tag,
whose letter prefix is the classification an engineer already uses: -K1 is a
contactor/relay, -Q1 a breaker, -M1 a motor. So the tag prefix IS the kind, and
it is derived from the objects on the page rather than from a table this module
would have to keep in step with every client's naming. Where a project uses a
prefix nobody standardised, it is still learned - as itself.

Nothing here decides anything for the caller. It reports what the reference
project did, with counts, and lets the caller choose.
"""

import os
import re

from .profile_store import (
    CATEGORIES,
    ProfileError,
    forget as _forget,
    list_profiles as _list,
    load_profile,
    merge_observations,
    new_profile,
    profile_root,
    save_profile,
    suggest as _suggest,
)
from .schematic import live_read_page
from .live import live_query_pages

__all__ = [
    "profile_learn",
    "profile_list",
    "profile_get",
    "profile_suggest",
    "profile_forget",
]


# A device tag's letter prefix - the part after the LAST "-", up to where the
# identifier starts.
#
# Calibrated against real tags from a production go-by rather than against the
# textbook "-K1" shape, because real ones do not all look like that:
#
#     +1162-MA1     -> MA      (letters then digits)
#     +-RC:1        -> RC      (letters then a colon)
#     +-TEST:A30    -> TEST    (a word, then a colon)
#     =AP+ST1-Q2    -> Q
#
# So: take letters following the last "-", stopping at a digit, a colon or the
# end. A prefix nobody standardised is still learned, as itself.
_TAG_PREFIX = re.compile(r"-([A-Za-z]{1,8})(?=[0-9:]|$)")

# Structure separators an EPLAN name is built from, used to describe a naming
# pattern without recording the customer-specific values themselves.
_STRUCTURE = re.compile(r"([=+\-/])")


def _kind_of(name):
    """
    Device kind from a tag, or None when the function has no tag yet.

    A freshly placed function is named "+" until tagged (measured on 2027), so
    this returns None for it rather than inventing a kind - an untagged device
    teaches nothing about vocabulary.
    """
    if not name or name.strip() in ("+", "-", "=", ""):
        return None
    m = _TAG_PREFIX.search(name)
    if m:
        return m.group(1).upper()
    return None


def _shape_of(name):
    """
    A naming PATTERN with the customer's values removed.

    "=AP+ST1-K12" -> "=A+A9-A9"   (letters->A, digits->9)

    Recording the shape rather than the literal name is what makes this useful:
    the pattern generalises to the next project, and it also avoids
    accumulating a customer's actual plant and location codes in a file that may
    be shared or committed.
    """
    if not name:
        return None
    out = []
    for ch in name:
        if ch.isalpha():
            out.append("A")
        elif ch.isdigit():
            out.append("9")
        else:
            out.append(ch)
    # Collapse runs so "K12" and "K345" describe the same shape.
    return re.sub(r"A{2,}", "A+", re.sub(r"9{2,}", "9+", "".join(out)))


def _round_to(value, step=0.001):
    try:
        return round(float(value) / step) * step
    except (TypeError, ValueError):
        return None


def _observe_page(page_state):
    """
    Turn one live_read_page result into observations.

    Returns {category: {key: [value, ...]}} with repeats meaningful - seeing the
    same symbol on forty pages must outweigh a one-off, so nothing is de-duped
    here.
    """
    obs = {c: {} for c in CATEGORIES}

    def add(category, key, value):
        obs[category].setdefault(str(key), []).append(value)

    placements = page_state.get("placements") or []

    # ---- page-level -------------------------------------------------------
    page_name = page_state.get("page")
    if page_name:
        shape = _shape_of(page_name)
        if shape:
            add("pages", "nameShape", shape)
    if page_state.get("pageType"):
        add("pages", "documentType", page_state["pageType"])
    if page_state.get("gridSize") is not None:
        add("geometry", "gridSize", _round_to(page_state["gridSize"]))

    xs = []
    for pl in placements:
        clr = pl.get("clrType")
        name = pl.get("name")
        sym = pl.get("symbol") or {}

        # ---- symbol vocabulary, keyed by the kind the tag implies ----------
        kind = _kind_of(name)
        if sym.get("library") and sym.get("name"):
            value = {
                "library": sym.get("library"),
                "symbol": sym.get("name"),
                "variantNr": sym.get("variantNr"),
            }
            # Keyed by device kind when known, otherwise by CLR type so an
            # untagged placement still teaches the vocabulary for its type.
            add("symbols", kind or ("clr:" + str(clr)), value)

        # ---- tag shapes ---------------------------------------------------
        if kind:
            shape = _shape_of(name)
            if shape:
                add("tags", kind, shape)
                add("tags", "any", shape)

        # ---- geometry -----------------------------------------------------
        loc = pl.get("location") or {}
        if clr == "Function" and loc.get("x") is not None:
            xs.append(float(loc["x"]))
            if loc.get("y") is not None:
                add("geometry", "deviceY", _round_to(loc["y"], 0.1))

    # Column pitch: the gaps between distinct device X positions on this page.
    # Learned as a distribution rather than an average, because a real page has
    # a rung pitch AND a wider gap between groups, and an average is neither.
    uniq = sorted(set(round(x, 2) for x in xs))
    for a, b in zip(uniq, uniq[1:]):
        gap = _round_to(b - a, 0.1)
        if gap and gap > 0:
            add("geometry", "deviceSpacingX", gap)

    return {c: v for c, v in obs.items() if v}


def profile_learn(profile: str, pages: list = None, page_limit: int = 25,
                  standard: str = None, description: str = None,
                  timeout_seconds: float = 120.0) -> dict:
    """
    Learn how the OPEN project builds schematics, into a named profile.

    Point this at a go-by - a reference project whose conventions you want new
    work to match - and it records what that project actually does: which symbol
    it uses for each device kind, what its device tags and page names look like,
    its grid and device spacing.

    LEARNING ACCUMULATES. Calling this again, on the same project or a different
    one, does not overwrite: counts increase and new candidates are added, so a
    profile sharpens with every project it sees and a one-off never outvotes the
    house norm. That is what lets one profile serve a client across many jobs.
    Use profile_forget() to remove something learned in error.

    Reads only. Nothing in the project is modified.

    Args:
        profile: Profile name, e.g. a client name. Created if new. Becomes a
            filename, so letters/digits/space/dot/dash/underscore only.
        pages: Page names to learn from. Omit to sample the project's own pages
            (up to page_limit).
        page_limit: How many pages to sample when `pages` is omitted
            (default 25). Learning is per-page, so a bigger sample means better
            evidence and a longer run.
        standard: Base standard this client sits on - "NFPA", "IEC", or omit.
            Recorded, not enforced: the profile's job is to describe what the
            project DOES, and real projects deviate from the standard they name.
        description: Free text, e.g. which client and which job this came from.
        timeout_seconds: Per-page script timeout.

    Returns:
        dict with "profile", "path", "pagesRead", "added", "reinforced" and a
        "summary" of what is now known per category.

        Profiles are stored OUTSIDE this repository - EPLAN_MCP_PROFILES, or a
        per-user directory - because a client's conventions are customer data.
        The file is plain sorted JSON and is meant to be hand-edited when it
        gets something wrong.
    """
    try:
        prof = load_profile(profile, missing_ok=True)
        if prof is None:
            prof = new_profile(profile, standard=standard, description=description)
        else:
            # Let a later call fill in metadata that was omitted first time,
            # without clobbering something already set.
            if standard and not prof.get("standard"):
                prof["standard"] = standard
            if description and not prof.get("description"):
                prof["description"] = description
    except ProfileError as exc:
        return {"success": False, "error": str(exc)}

    # Which pages?
    if pages:
        page_names = list(pages)
    else:
        listing = live_query_pages(limit=page_limit, timeout_seconds=timeout_seconds)
        if not listing.get("success"):
            return {"success": False,
                    "error": "Could not list the project's pages: %s"
                             % (listing.get("error") or listing.get("message")),
                    "hint": "Open the go-by project in EPLAN first."}
        inner = listing.get("results") or listing
        raw = inner.get("pages") or []
        page_names = [p.get("name") for p in raw if p.get("name")][:page_limit]

    if not page_names:
        return {"success": False,
                "error": "No pages to learn from. Open the go-by project in "
                         "EPLAN, or pass pages=[...] explicitly."}

    read_ok, failures, source_project, with_devices = 0, [], None, 0
    for name in page_names:
        # Devices only. A production page is mostly graphics - measured, one
        # Circuit page held 1887 placements whose first 40 were all PolyLine -
        # so an unfiltered read exhausts the limit on geometry and learns
        # nothing about the vocabulary, which is the whole point of this.
        state = live_read_page(name, include_pins=False, limit=500,
                               types=["Function"],
                               timeout_seconds=timeout_seconds)
        if not state.get("success"):
            failures.append({"page": name, "error": state.get("error")})
            continue
        read_ok += 1
        if state.get("matched"):
            with_devices += 1
        source_project = source_project or state.get("project")
        merge_observations(prof, _observe_page(state),
                           source=name if not source_project else
                           "%s :: %s" % (source_project, name))

    if not read_ok:
        return {"success": False,
                "error": "Every page failed to read; nothing was learned.",
                "failures": failures[:10]}

    try:
        path = save_profile(prof)
    except ProfileError as exc:
        return {"success": False, "error": str(exc)}

    counts = {}
    for cat, bucket in (prof.get("observations") or {}).items():
        if bucket:
            counts[cat] = {"keys": len(bucket),
                           "observations": sum(int(c.get("count", 0))
                                               for v in bucket.values()
                                               for c in v)}
    result = {
        "success": True,
        "profile": prof["name"],
        "path": path,
        "pagesRead": read_ok,
        "pagesRequested": len(page_names),
        "pagesWithDevices": with_devices,
        "summary": counts,
        "note": ("Learning is cumulative - run this against another go-by to "
                 "strengthen the profile rather than replace it."),
    }
    if failures:
        result["failures"] = failures[:10]
        result["failureCount"] = len(failures)
    if not with_devices:
        # Say so rather than reporting a confident-looking success over nothing.
        # The first pages of a real project are often ExternalDocument sheets
        # with no devices at all, so a default sample can legitimately miss.
        result["warning"] = (
            "None of the %d pages read contained a Function, so no symbol or "
            "tag vocabulary was learned - only page and geometry conventions. "
            "The first pages of a project are often external documents or "
            "cover sheets. Pass pages=[...] naming schematic pages, or raise "
            "page_limit." % read_ok
        )
    return result


def profile_list() -> dict:
    """
    List the convention profiles that exist, and where they are stored.

    Profiles live OUTSIDE this repository (EPLAN_MCP_PROFILES, else a per-user
    directory) because a client's conventions are customer data. Point
    EPLAN_MCP_PROFILES at a private repo to version and share them across a
    team.
    """
    try:
        listing = _list()
    except ProfileError as exc:
        return {"success": False, "error": str(exc)}
    out = dict(listing)
    out["success"] = True
    if not listing["profiles"]:
        out["note"] = ("No profiles yet. Open a go-by project and call "
                       "profile_learn(profile='<client>') to create one.")
    return out


def profile_get(profile: str, category: str = None) -> dict:
    """
    Read a profile back - everything, or one category.

    Args:
        profile: Profile name.
        category: One of symbols, tags, pages, geometry, parts. Omit for all.
            A whole profile learned from many projects is large, so prefer a
            category when you know which one you want.
    """
    try:
        prof = load_profile(profile)
    except ProfileError as exc:
        return {"success": False, "error": str(exc)}
    if category:
        bucket = (prof.get("observations") or {}).get(category)
        if bucket is None:
            return {"success": False,
                    "error": "Unknown category %r. Known: %s"
                             % (category, ", ".join(CATEGORIES))}
        return {"success": True, "profile": prof["name"],
                "category": category, "observations": bucket}
    return {"success": True, "profile": prof}


def profile_suggest(profile: str, category: str = "symbols",
                    key: str = None, limit: int = 5) -> dict:
    """
    Ask a profile what this client normally does.

    e.g. profile_suggest("acme", "symbols", key="K") -> which library/symbol
    their projects use for contactors, how many times it was seen, and what the
    alternatives were.

    ALWAYS returns alternatives with their support, never a bare answer, so you
    can tell a settled convention ("47 of 48") from an ambiguous one ("12 vs
    11") and ask the user when it matters. "confidence" is share-of-observations
    - it says how CONSISTENT the reference projects were, not whether the
    convention is right; a profile learned from one page can be 100% confident
    and wrong, which is why count and sources come back with it.

    Args:
        profile: Profile name.
        category: symbols | tags | pages | geometry | parts. Default symbols.
        key: The thing to ask about - a device-tag prefix ("K", "Q", "M") for
            symbols/tags/parts, or a measure ("gridSize", "deviceSpacingX") for
            geometry. Omit to get every key in the category.
        limit: Max alternatives per key (default 5).
    """
    try:
        prof = load_profile(profile)
    except ProfileError as exc:
        return {"success": False, "error": str(exc)}
    out = _suggest(prof, category, key=key, limit=limit)
    out["success"] = True
    out["profile"] = prof["name"]
    return out


def profile_forget(profile: str, category: str, key: str,
                   value: dict = None) -> dict:
    """
    Remove something a profile learned in error.

    Learning is otherwise purely additive, so without this one mis-read page
    would sit in the profile for ever. Omit `value` to drop everything under
    `key`, which is usually what you want when an extractor bug polluted a whole
    category rather than one entry.

    Args:
        profile: Profile name.
        category: symbols | tags | pages | geometry | parts.
        key: The key to correct.
        value: The specific candidate to remove. Omit to remove them all.
    """
    try:
        prof = load_profile(profile)
    except ProfileError as exc:
        return {"success": False, "error": str(exc)}
    result = _forget(prof, category, key, value)
    try:
        path = save_profile(prof)
    except ProfileError as exc:
        return {"success": False, "error": str(exc)}
    result["success"] = True
    result["profile"] = prof["name"]
    result["path"] = path
    return result
