"""
Convention profiles - what THIS company, for THIS client, actually does.

The primitives in schematic.py can place a device and draw a wire. They cannot
decide *which* device, tagged *how*, placed *where* - and those are not
properties of EPLAN. They are properties of a company, and usually of an
individual client. On the reference installation `NFPA_symbol_en_US` alone
offers SL, S, O, SSV, Q1, SWR, ONE; nothing in the API says which one this
customer means by "contactor".

Engineers already solve this with go-by projects: they keep a reference and copy
its patterns. This module lets the server do the same thing - read a project and
learn from it.

DESIGN, and the reasoning behind each decision:

1. LEARNING IS ADDITIVE, NEVER OVERWRITING. Every observation carries a count
   and its provenance (which project, which page, when). Reading a second
   project SHARPENS a profile instead of replacing it. That is what makes a
   profile more useful the longer it is used, rather than only reflecting
   whatever was read last - and it is what lets one profile serve many projects
   from the same client.

2. A QUERY RETURNS ALTERNATIVES, NOT A BARE ANSWER. `suggest()` gives the
   best-supported convention AND the runners-up with their support, so a caller
   can tell a settled convention ("47 of 48 contactors use this symbol") from a
   genuinely ambiguous one ("12 vs 11"). Collapsing that to one value would
   manufacture false confidence, which is the failure mode this whole layer
   exists to prevent.

3. PROFILES LIVE OUTSIDE THE REPOSITORY. A client's standards are customer
   data. The path comes from EPLAN_MCP_PROFILES (default under LOCALAPPDATA),
   the repo ships machinery and a generic example only, and a test asserts that
   nothing writes inside the tree.

4. THE FILE IS PLAIN, SORTED JSON. An engineer who spots a wrong inference must
   be able to open the file and fix it without going through a tool. Sorted keys
   and a stable shape keep it diffable, which matters because a profile
   legitimately belongs in a company's own private git repo.

5. NOTHING REQUIRES A PROFILE. Every caller degrades to current behaviour when
   no profile exists. This must be an accelerator, never a setup step.

Nothing here imports EPLAN or pythonnet, so all of it is testable with EPLAN
closed. Extraction from a live project lives in profiles.py.
"""

import io
import json
import os
import re
import time

__all__ = [
    "ProfileError",
    "PROFILE_ROOT",
    "profile_root",
    "profile_path",
    "list_profiles",
    "load_profile",
    "save_profile",
    "new_profile",
    "merge_observations",
    "suggest",
    "forget",
    "SCHEMA_VERSION",
    "CATEGORIES",
]


SCHEMA_VERSION = 1

# The observation categories a profile holds. Each is a mapping of
#   key -> [candidate, ...]
# where a candidate is {"value": ..., "count": int, "sources": [...], ...}.
#
# Deliberately open: a category this list does not name is still stored and
# returned. A future extractor should not need a change here to record something
# new, and an engineer hand-editing a profile should not have their addition
# silently dropped.
CATEGORIES = (
    "symbols",    # device kind -> which library/symbol/variant is used for it
    "tags",       # tag shape   -> the =plant+location-device patterns in use
    "pages",      # page aspect -> naming patterns, document types
    "geometry",   # measure     -> grid, spacing, column positions
    "parts",      # device kind -> part numbers actually assigned
)

# Keep provenance bounded: a profile learned from hundreds of pages should not
# grow without limit. The COUNT is the evidence; the source list is only there
# so a human can see where something came from.
_MAX_SOURCES_PER_CANDIDATE = 10

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


class ProfileError(Exception):
    """A profile could not be read, written or named."""


def profile_root():
    """
    Directory holding profile files.

    EPLAN_MCP_PROFILES if set, otherwise a per-user directory. Never inside the
    repository: a client's conventions are customer data and this repo is
    public. Resolved on every call rather than cached, so a test can point it
    somewhere else with monkeypatch.
    """
    env = os.environ.get("EPLAN_MCP_PROFILES")
    if env:
        return os.path.abspath(env)
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~"))
    return os.path.join(base, "eplan_mcp_profiles")


# Backwards-compatible alias for callers that want the value once.
PROFILE_ROOT = profile_root()


def _check_name(name):
    if not isinstance(name, str) or not _SAFE_NAME.match(name or ""):
        raise ProfileError(
            "Invalid profile name %r. Use letters, digits, space, dot, "
            "underscore or dash (1-64 chars); it becomes a filename." % (name,)
        )
    return name


def profile_path(name):
    return os.path.join(profile_root(), _check_name(name) + ".json")


def list_profiles():
    """Names of the profiles that exist, plus where they live."""
    root = profile_root()
    names = []
    if os.path.isdir(root):
        for entry in sorted(os.listdir(root)):
            if entry.lower().endswith(".json"):
                names.append(entry[:-5])
    return {"root": root, "profiles": names}


def new_profile(name, standard=None, description=None, now=None):
    """
    An empty profile.

    `standard` names the base convention this client's rules sit on top of -
    "NFPA", "IEC", or None when it is purely bespoke. It is recorded rather than
    enforced: a profile's job is to describe what a project DOES, and real
    projects deviate from the standard they claim.
    """
    stamp = now or time.strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "schema": SCHEMA_VERSION,
        "name": _check_name(name),
        "standard": standard,
        "description": description or "",
        "created": stamp,
        "updated": stamp,
        # Every project this profile has learned from, so a reader can tell how
        # much evidence sits behind it.
        "sources": [],
        "observations": {c: {} for c in CATEGORIES},
    }


def load_profile(name, missing_ok=False):
    """Read a profile. Returns None when missing and missing_ok."""
    path = profile_path(name)
    if not os.path.exists(path):
        if missing_ok:
            return None
        raise ProfileError(
            "No profile %r in %s. Create one with profile_learn(), or call "
            "list_profiles() to see what exists." % (name, profile_root())
        )
    try:
        with io.open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise ProfileError(
            "Profile %r at %s is not valid JSON (%s). It is meant to be "
            "hand-editable, so this usually means a manual edit went wrong - "
            "fix or delete the file." % (name, path, exc)
        )
    return _migrate(data, name)


def _migrate(data, name):
    """Tolerate an older or hand-edited file rather than refusing to load it."""
    if not isinstance(data, dict):
        raise ProfileError("Profile %r is not a JSON object." % name)
    data.setdefault("schema", SCHEMA_VERSION)
    data.setdefault("name", name)
    data.setdefault("standard", None)
    data.setdefault("description", "")
    data.setdefault("sources", [])
    obs = data.setdefault("observations", {})
    for cat in CATEGORIES:
        obs.setdefault(cat, {})
    return data


def save_profile(profile):
    """
    Write a profile atomically, sorted, with a trailing newline.

    Atomic because a profile can legitimately live in a company git repo and a
    half-written file there is worse than a stale one. Sorted so that learning
    the same thing twice produces no diff.
    """
    name = _check_name(profile.get("name"))
    root = profile_root()
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, name + ".json")
    tmp = path + ".tmp"
    text = json.dumps(profile, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path


def _candidate_key(value):
    """Stable identity for a candidate value, so counts accumulate correctly."""
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def merge_observations(profile, observations, source=None, now=None):
    """
    Fold new observations into a profile. THE core of continual learning.

    `observations` is {category: {key: [value, ...]}} - a plain list of what was
    seen, with repeats meaningful: seeing the same symbol on forty pages is
    forty observations and should outweigh a one-off.

    Existing candidates gain count; new ones are appended. NOTHING is removed,
    because absence in one project is not evidence against a convention seen in
    another - that asymmetry is deliberate. Use forget() to remove a bad read.

    Returns a summary of what changed, so a caller can report it rather than
    claiming a vague success.
    """
    stamp = now or time.strftime("%Y-%m-%dT%H:%M:%S")
    obs_root = profile.setdefault("observations", {})
    added, reinforced = 0, 0

    for category, keyed in (observations or {}).items():
        bucket = obs_root.setdefault(category, {})
        for key, values in (keyed or {}).items():
            if values is None:
                continue
            if not isinstance(values, (list, tuple)):
                values = [values]
            candidates = bucket.setdefault(str(key), [])
            index = {_candidate_key(c.get("value")): c for c in candidates
                     if isinstance(c, dict)}
            for value in values:
                ck = _candidate_key(value)
                existing = index.get(ck)
                if existing is None:
                    entry = {
                        "value": value,
                        "count": 1,
                        "firstSeen": stamp,
                        "lastSeen": stamp,
                        "sources": [source] if source else [],
                    }
                    candidates.append(entry)
                    index[ck] = entry
                    added += 1
                else:
                    existing["count"] = int(existing.get("count", 0)) + 1
                    existing["lastSeen"] = stamp
                    if source:
                        srcs = existing.setdefault("sources", [])
                        if source not in srcs:
                            srcs.append(source)
                            # Bounded: the count is the evidence, the source
                            # list is only for a human reading the file.
                            del srcs[:-_MAX_SOURCES_PER_CANDIDATE]
                    reinforced += 1
            # Most-supported first, then alphabetical so the file is stable.
            candidates.sort(key=lambda c: (-int(c.get("count", 0)),
                                           _candidate_key(c.get("value"))))

    if source:
        sources = profile.setdefault("sources", [])
        known = {s.get("source") for s in sources if isinstance(s, dict)}
        if source in known:
            for s in sources:
                if isinstance(s, dict) and s.get("source") == source:
                    s["lastLearned"] = stamp
                    s["timesLearned"] = int(s.get("timesLearned", 1)) + 1
        else:
            sources.append({"source": source, "firstLearned": stamp,
                            "lastLearned": stamp, "timesLearned": 1})

    profile["updated"] = stamp
    return {"added": added, "reinforced": reinforced}


def suggest(profile, category, key=None, limit=5):
    """
    What does this profile say about `category` (optionally one `key`)?

    ALWAYS returns alternatives with their support, never a bare answer.
    A caller must be able to tell "47 of 48 use this" from "12 versus 11";
    collapsing those to the same shape would manufacture confidence the
    evidence does not support.

    `confidence` is share-of-observations for that key, in 0..1. It says how
    consistent the observed projects were - NOT whether the convention is
    correct. A profile learned from one page can be 100% confident and wrong,
    which is why `count` and `sources` are returned alongside it.
    """
    bucket = (profile.get("observations") or {}).get(category) or {}
    if key is not None:
        keys = [str(key)] if str(key) in bucket else []
        if not keys:
            return {
                "category": category,
                "key": key,
                "known": False,
                "suggestions": [],
                "message": (
                    "Nothing learned for %r in %r. Known keys: %s"
                    % (key, category, ", ".join(sorted(bucket)) or "(none)")
                ),
            }
    else:
        keys = sorted(bucket)

    out = []
    for k in keys:
        candidates = [c for c in bucket.get(k, []) if isinstance(c, dict)]
        total = sum(int(c.get("count", 0)) for c in candidates) or 1
        ranked = sorted(candidates,
                        key=lambda c: (-int(c.get("count", 0)),
                                       _candidate_key(c.get("value"))))[:limit]
        out.append({
            "key": k,
            "observations": total,
            "best": ranked[0].get("value") if ranked else None,
            "confidence": round(int(ranked[0].get("count", 0)) / total, 3) if ranked else 0.0,
            "alternatives": [
                {
                    "value": c.get("value"),
                    "count": int(c.get("count", 0)),
                    "share": round(int(c.get("count", 0)) / total, 3),
                    "sources": list(c.get("sources") or []),
                    "lastSeen": c.get("lastSeen"),
                }
                for c in ranked
            ],
        })

    result = {"category": category, "known": bool(out), "suggestions": out}
    if key is not None and out:
        result["key"] = key
        result["best"] = out[0]["best"]
        result["confidence"] = out[0]["confidence"]
        if out[0]["observations"] < 3:
            result["caution"] = (
                "Only %d observation(s) behind this - it reflects one project, "
                "not an established convention. Learn from another go-by to "
                "confirm it." % out[0]["observations"]
            )
    return result


def forget(profile, category, key, value=None):
    """
    Remove a learned candidate - the correction path for a bad read.

    Needed because learning is otherwise purely additive: without this, one
    mis-parsed page would sit in the profile for ever. Removing the whole key is
    allowed (value=None) since an extractor bug usually pollutes a whole
    category rather than one entry.
    """
    bucket = (profile.get("observations") or {}).get(category)
    if not bucket or str(key) not in bucket:
        return {"removed": 0, "message": "Nothing learned for %r in %r." % (key, category)}
    if value is None:
        removed = len(bucket.pop(str(key)))
        return {"removed": removed, "message": "Dropped all of %r." % key}
    target = _candidate_key(value)
    before = bucket[str(key)]
    after = [c for c in before if _candidate_key(c.get("value")) != target]
    bucket[str(key)] = after
    if not after:
        bucket.pop(str(key))
    return {"removed": len(before) - len(after)}
