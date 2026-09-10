#!/usr/bin/env python3
"""
Turn a bulk `SymbolOverview`-page dump into the project-derived symbol
inventory that 10-image-to-schematic-pipeline.md called for, plus a
redundancy-review report for a human to confirm against.

Background
----------
Testing/09-symbol-catalog-enumeration-gap.md found that `live_symbol_catalog`'s
depth-2 library listing stops at the first sparse SymbolId gap and does not say
so - it under-reports every library it touches. Testing/10 named the fix
(schematic.py's depth-2 walk, `break` -> `continue`) but also named a second,
independent route that never goes near that walk at all: EPLAN's own
`SymbolOverview` page type, generated once inside EPLAN and read back with the
same `live_read_page`/`ReadPage` serializer that already backs every `live_*`
tool. That is the route this script assumes - see
Testing/11-symbol-overview-inventory.md for how the dump itself was produced
(a single execute_custom_script call reusing schematic.py's own ReadPage/
DumpPlacement verbatim, looped over every SymbolOverview page in one project).

What this script does NOT do: decide what is redundant. It ranks candidates
and prints them for a human to confirm, the same rule 10 states for the image
pipeline ("narrow, never decide") - the project's owner is the one who knows
whether two symbols that render identically are actually the same thing or
two deliberately distinct library entries that happen to look alike.

Input
-----
The raw JSON written by execute_custom_script's SymbolOverview-dump script:
    {"success": true, "results": {"project", "pageCount", "pages": [
        {"page", "pageType", "placements": [
            {"clrType", "handle", "location", "symbol": {"library","name",
             "variantNr"}, "pins": [{"index","direction","raw":{"x","y"}}]},
            ...
        ]}, ...
    ]}}
Also accepts the same content without the {"success","results"} wrapper (a
plain `live_read_page`-shaped dump), so a single page's fixture works too.

Confirming candidates (--decisions)
------------------------------------
Nothing is ever excluded automatically. Once a human has looked at the
candidates (in EPLAN, visually - not by re-running this script), record the
call in a small JSON file and pass it as --decisions:

    {
      "SPECIAL/DCP2OL": {"status": "excluded", "reason": "renders but the ..."},
      "SPECIAL/SH2":    {"status": "verified_ok", "reason": "confirmed legit"}
    }

Keys are "library/symbol" (applies to every variantNr of that symbol - a
decision is about the symbol, not one rotation of it). Every row in the
output inventory gets a `status` column: "excluded" / "verified_ok" from the
file, or "unreviewed" for anything not mentioned. The redundancy report
echoes the recorded status inline next to each Tier A/B1/B2 entry so the
confirmed state stays visible alongside the evidence that produced it.

Output
------
- A long-format table, one row per pin (placements with zero pins get one row
  with the pin fields null), written to --out (Parquet if pandas+pyarrow are
  available, CSV otherwise - see write_inventory). Carries a `status` column
  (see --decisions above).
- A redundancy-candidate report on stdout (or --report), in two tiers:
    A. CROSS-PAGE DUPLICATES: the exact same (library, symbol, variantNr)
       placed on more than one SymbolOverview page. Unambiguous - the same
       variant cannot need two entries in a symbol catalog.
    B. ZERO-GEOMETRY, split B1/B2 by clrType: every pin's raw offset is
       (0, 0). B1 (devices - Function/Terminal/...) is the DCP2OL case found
       live on page /1: a device has no structural excuse for that, so it is
       likely decorative/label geometry or indistinguishable from another
       orientation. B2 (routing shapes - SymbolReference/InterruptionPoint/
       StrandConnector/...) is listed separately and flagged LOW confidence:
       a corner/T-node/bridge's bend point IS its location, so its
       through-legs legitimately sit at (0,0) - confirmed live this session
       on `SPECIAL/BR`, whose two straight-through pins read (0,0) while only
       its third, jumped leg carries a real offset.
    C. SAME-LIBRARY PIN-PATTERN COLLISION (best-effort, low confidence): two
       DIFFERENT symbol names in the same library whose variants carry an
       identical (direction, rounded offset) pin signature. Capped and
       clearly labelled - a single pin on one side is a common, legitimate
       shape (many terminals look alike), so this tier is noisy by nature and
       meant as a lead, not a verdict.

Usage
-----
    python build_symbol_inventory.py .cache/nuevo_proyecto_symbol_overview_dump.json \\
        --out .cache/symbol_inventory.parquet --report symbol_redundancy_report.md

Standard library + pandas/pyarrow.

NOTE: this script identifies symbols only by what EPLAN's own project reports
(library/name/pins) - it never guesses a symbol's MEANING from its name or
from an external dataset. An earlier version of this workflow cross-referenced
symbol names against a public HuggingFace dataset; that was dropped
(2026-09-08, user's instruction) after it produced two wrong reads in one
session - see [[eplan-electrical-symbols-dataset]] in memory. Confirm what a
symbol actually IS by placing it and looking (`eplan_export_graphics_pages` /
`eplan_preview_page`), or by asking the project's owner - never by name
pattern-matching.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROUND_NDIGITS = 3


def load_dump(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Unwrap execute_custom_script's {"success", "results", "audit_script"}
    # shape if present; otherwise assume this is already the results dict.
    if isinstance(data, dict) and "results" in data and "pages" not in data:
        data = data["results"]
    if "pages" not in data:
        raise SystemExit(
            "Input has no 'pages' key at the top level (after unwrapping "
            "'results' if present). Got keys: %s" % list(data.keys())
        )
    return data


def load_decisions(path) -> dict:
    """
    {"library/symbol": {"status": "excluded"|"verified_ok", "reason": "..."}}
    Not present -> every row gets status "unreviewed" (see decide()).
    """
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    bad = [k for k, v in raw.items()
           if not isinstance(v, dict) or v.get("status") not in
           ("excluded", "verified_ok")]
    if bad:
        raise SystemExit(
            "Bad --decisions entries (need {\"status\": \"excluded\"|"
            "\"verified_ok\", ...}) for: %s" % ", ".join(bad)
        )
    return raw


def decide(library, symbol, decisions: dict):
    """(status, reason) for one symbol, defaulting to ("unreviewed", None)."""
    entry = decisions.get("%s/%s" % (library, symbol))
    if not entry:
        return "unreviewed", None
    return entry["status"], entry.get("reason")


def iter_placements(dump: dict):
    """Yield (page_name, placement_dict) for every placement in the dump."""
    for page in dump.get("pages", []):
        page_name = page.get("page")
        for pl in page.get("placements", []):
            yield page_name, pl


def pin_signature(pl: dict):
    """
    A hashable summary of a placement's pin geometry: sorted
    (direction, round(x), round(y)) triples. Two placements sharing this
    signature look identical on the page regardless of symbol name.
    """
    pins = pl.get("pins") or []
    sig = []
    for p in pins:
        raw = p.get("raw") or {}
        x = round(float(raw.get("x", 0.0)), ROUND_NDIGITS)
        y = round(float(raw.get("y", 0.0)), ROUND_NDIGITS)
        sig.append((p.get("direction"), x, y))
    return tuple(sorted(sig, key=lambda t: (str(t[0]), t[1], t[2])))


def is_zero_geometry(sig) -> bool:
    return len(sig) > 0 and all(x == 0.0 and y == 0.0 for _, x, y in sig)


def build_rows(dump: dict, decisions: dict = None):
    """
    Long-format rows, one per pin (placements with no pins get one row with
    pin_* fields set to None so they are not silently dropped from the table -
    the same "absence is recorded, not hidden" rule the C# side follows).
    """
    decisions = decisions or {}
    rows = []
    for page_name, pl in iter_placements(dump):
        sym = pl.get("symbol")
        if not sym:
            # Graphics, PlaceHolderText, etc. - nothing a live_* placement
            # call could target by name. Not part of this inventory.
            continue
        status, reason = decide(sym.get("library"), sym.get("name"), decisions)
        base = {
            "page": page_name,
            "clrType": pl.get("clrType"),
            "library": sym.get("library"),
            "symbol": sym.get("name"),
            "variantNr": sym.get("variantNr"),
            "handle": pl.get("handle"),
            "status": status,
            "status_reason": reason,
        }
        pins = pl.get("pins") or []
        if not pins:
            row = dict(base)
            row.update(pin_index=None, pin_direction=None,
                       offset_x=None, offset_y=None)
            rows.append(row)
            continue
        for p in pins:
            raw = p.get("raw") or {}
            row = dict(base)
            row.update(
                pin_index=p.get("index"),
                pin_direction=p.get("direction"),
                offset_x=raw.get("x"),
                offset_y=raw.get("y"),
            )
            rows.append(row)
    return rows


def find_cross_page_duplicates(dump: dict):
    """Tier A: same (library, symbol, variantNr) placed on >1 page."""
    seen = defaultdict(set)
    for page_name, pl in iter_placements(dump):
        sym = pl.get("symbol")
        if not sym:
            continue
        key = (sym.get("library"), sym.get("name"), sym.get("variantNr"))
        seen[key].add(page_name)
    return {k: sorted(v) for k, v in seen.items() if len(v) > 1}


# clrTypes where every pin sitting at (0, 0) is STRUCTURALLY NORMAL rather
# than suspicious: a corner/T-node/bridge's bend point is its own location,
# so its through-legs legitimately have zero offset - Testing/07's
# page_to_ascii.py already treats SymbolReference/InterruptionPoint this way
# (STRUCTURAL_TYPES/IP_TYPES), and the live BR/CO read in this same session
# confirmed it: BR's two straight-through legs sit at raw (0,0) while only
# its third, "jumped" leg carries a real (0, +2) offset. A device-shaped
# clrType (Function, Terminal) has no such excuse - ALL its pins landing on
# the insertion point (DCP2OL, DCPOL) is what actually flagged as odd in
# Testing/09/10.
ROUTING_LIKE_CLRTYPES = {
    "SymbolReference", "InterruptionPoint", "PotentialDefinitionPoint",
    "ConnectionDefinitionPoint", "ConnectionDefinition",
    "StrandConnector", "StrandInterConnector",
}


def find_zero_geometry(dump: dict):
    """
    Tier B: every pin on the placement sits at raw offset (0, 0).

    Split by clrType: device-shaped placements (Function, Terminal, ...) are
    returned as `devices` (high confidence - see ROUTING_LIKE_CLRTYPES
    comment); routing-shaped placements are returned as `routing` (low
    confidence - likely a legitimate bend point, not a defect).
    """
    devices, routing = [], []
    for page_name, pl in iter_placements(dump):
        sym = pl.get("symbol")
        if not sym or not pl.get("pins"):
            continue
        sig = pin_signature(pl)
        if not is_zero_geometry(sig):
            continue
        clr = pl.get("clrType")
        entry = (page_name, sym.get("library"), sym.get("name"),
                 sym.get("variantNr"), clr)
        (routing if clr in ROUTING_LIKE_CLRTYPES else devices).append(entry)
    return devices, routing


def find_pattern_collisions(dump: dict, cap: int = 40):
    """
    Tier C: different symbol NAMES in the same library sharing a pin
    signature with >=2 pins (>=2 to cut single-pin noise - see module
    docstring). Capped at `cap` groups so a noisy result cannot swamp the
    report; the true count is still reported.
    """
    by_sig = defaultdict(set)  # (library, sig) -> {symbol names}
    for _page_name, pl in iter_placements(dump):
        sym = pl.get("symbol")
        if not sym:
            continue
        sig = pin_signature(pl)
        if len(sig) < 2 or is_zero_geometry(sig):
            continue
        by_sig[(sym.get("library"), sig)].add(sym.get("name"))
    collisions = {k: sorted(v) for k, v in by_sig.items() if len(v) > 1}
    total = len(collisions)
    items = sorted(collisions.items(), key=lambda kv: -len(kv[1]))[:cap]
    return items, total


def write_inventory(rows, out_path: Path):
    try:
        import pandas as pd
    except ImportError:
        import csv
        with open(out_path.with_suffix(".csv"), "w", newline="",
                  encoding="utf-8") as f:
            if not rows:
                return out_path.with_suffix(".csv")
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print("pandas/pyarrow not installed - wrote CSV instead of Parquet.",
              file=sys.stderr)
        return out_path.with_suffix(".csv")

    df = pd.DataFrame(rows)
    try:
        df.to_parquet(out_path, index=False)
        return out_path
    except ImportError:
        csv_path = out_path.with_suffix(".csv")
        df.to_csv(csv_path, index=False)
        print("pyarrow not installed - wrote CSV instead of Parquet.",
              file=sys.stderr)
        return csv_path


def _status_tag(library, symbol, decisions: dict) -> str:
    status, reason = decide(library, symbol, decisions)
    if status == "unreviewed":
        return ""
    return " [%s%s]" % (status, (" - %s" % reason) if reason else "")


def render_report(dump: dict, dupes, zero_geo_devices, zero_geo_routing,
                   collisions, collisions_total, decisions: dict = None) -> str:
    decisions = decisions or {}
    lines = []
    lines.append("# Symbol redundancy review - %s" % dump.get("project", "?"))
    lines.append("")
    lines.append("Generated from %d `SymbolOverview` page(s). Nothing here "
                  "was removed automatically - confirm each group, then "
                  "re-run with the confirmed exclusions." %
                  dump.get("pageCount", len(dump.get("pages", []))))
    lines.append("")

    lines.append("## Tier A - cross-page duplicates (%d)" % len(dupes))
    lines.append("")
    lines.append("The exact same library/symbol/variant placed on more than "
                  "one page. Unambiguous: a symbol catalog needs each "
                  "variant listed once.")
    lines.append("")
    if dupes:
        for (lib, name, vnr), pages in sorted(dupes.items()):
            lines.append("- `%s/%s` v%s -> pages %s%s" %
                          (lib, name, vnr, ", ".join(pages),
                           _status_tag(lib, name, decisions)))
    else:
        lines.append("(none found)")
    lines.append("")

    lines.append("## Tier B1 - zero-geometry DEVICES (%d, higher confidence)"
                  % len(zero_geo_devices))
    lines.append("")
    lines.append("Every pin sits at raw offset (0, 0) on a device-shaped "
                  "clrType (Function, Terminal, ...). Matches the "
                  "`SPECIAL/DCP2OL` case found live on page /1 (Testing/09, "
                  "Testing/10): a device has no structural excuse for every "
                  "pin landing on its own insertion point, so this is "
                  "likely decorative/label geometry, or an orientation "
                  "indistinguishable from another without a real pin to "
                  "anchor it.")
    lines.append("")
    if zero_geo_devices:
        for page, lib, name, vnr, clr in zero_geo_devices:
            lines.append("- `%s/%s` v%s (%s) on page %s%s" %
                          (lib, name, vnr, clr, page,
                           _status_tag(lib, name, decisions)))
    else:
        lines.append("(none found)")
    lines.append("")

    lines.append("## Tier B2 - zero-geometry ROUTING shapes (%d, low "
                  "confidence - likely NOT a defect)" % len(zero_geo_routing))
    lines.append("")
    lines.append("Same test, but on a routing-shaped clrType (corner/"
                  "T-node/bridge/strand connector). A corner's bend point "
                  "IS its location, so its through-legs legitimately carry "
                  "offset (0, 0) - confirmed live in this session: `BR`'s "
                  "two straight-through pins sit at (0,0) while only its "
                  "third, jumped leg carries a real offset. Listed for "
                  "completeness, not as a redundancy lead.")
    lines.append("")
    if zero_geo_routing:
        for page, lib, name, vnr, clr in zero_geo_routing:
            lines.append("- `%s/%s` v%s (%s) on page %s%s" %
                          (lib, name, vnr, clr, page,
                           _status_tag(lib, name, decisions)))
    else:
        lines.append("(none found)")
    lines.append("")

    lines.append("## Tier C - same-library pin-pattern collisions "
                  "(showing %d of %d, low confidence)" %
                  (len(collisions), collisions_total))
    lines.append("")
    lines.append("Different symbol NAMES in the same library whose variants "
                  "share an identical >=2-pin geometry. This is a LEAD, not "
                  "a verdict - two genuinely distinct symbols can legitimately "
                  "share a footprint (e.g. two 2-pin devices that only differ "
                  "by what they mean electrically, not by shape). Confirm "
                  "each one against `live_symbol_catalog(symbol=...)` before "
                  "treating it as a duplicate.")
    lines.append("")
    if collisions:
        for (lib, sig), names in collisions:
            sig_txt = ", ".join("%s(%.2f,%.2f)" % (d, x, y) for d, x, y in sig)
            shown = names[:8]
            more = "" if len(names) <= 8 else " (+%d more)" % (len(names) - 8)
            lines.append("- `%s`: %d symbols sharing pins [%s] -- e.g. %s%s" %
                          (lib, len(names), sig_txt, " / ".join(shown), more))
    else:
        lines.append("(none found)")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", help="Path to the SymbolOverview bulk-dump JSON "
                                  "(or a single live_read_page JSON).")
    ap.add_argument("--out", default=".cache/symbol_inventory.parquet",
                     help="Inventory output path (Parquet if pandas+pyarrow "
                          "are installed, else CSV). Default: "
                          "%(default)s")
    ap.add_argument("--report", default=None,
                     help="Write the redundancy report to this path too "
                          "(always printed to stdout).")
    ap.add_argument("--collision-cap", type=int, default=15,
                     help="Max Tier C groups to print (default 15 - this "
                          "tier is expected to be mostly noise on a large "
                          "IEC-style library where many genuinely distinct "
                          "symbols share a simple footprint; raise it only "
                          "if you actually want to work through more).")
    ap.add_argument("--decisions", default=None,
                     help="Path to a JSON file of confirmed calls (see the "
                          "module docstring, 'Confirming candidates'). Every "
                          "row gets a status column; entries not mentioned "
                          "stay 'unreviewed'.")
    args = ap.parse_args()

    dump = load_dump(Path(args.dump))
    decisions = load_decisions(args.decisions)
    rows = build_rows(dump, decisions)
    dupes = find_cross_page_duplicates(dump)
    zero_geo_devices, zero_geo_routing = find_zero_geometry(dump)
    collisions, collisions_total = find_pattern_collisions(
        dump, cap=args.collision_cap)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = write_inventory(rows, out_path)

    report = render_report(dump, dupes, zero_geo_devices, zero_geo_routing,
                            collisions, collisions_total, decisions)
    print(report)
    n_excluded = sum(1 for r in rows if r["status"] == "excluded")
    n_ok = sum(1 for r in rows if r["status"] == "verified_ok")
    n_unreviewed = len(rows) - n_excluded - n_ok
    print("\n(%d rows written to %s -- %d excluded, %d verified_ok, %d "
          "unreviewed)" % (len(rows), written, n_excluded, n_ok, n_unreviewed),
          file=sys.stderr)

    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
        print("Report also written to %s" % args.report, file=sys.stderr)


if __name__ == "__main__":
    main()
