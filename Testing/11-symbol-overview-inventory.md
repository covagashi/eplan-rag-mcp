**SUPERSEDED 2026-09-08 (later the same day).** The enumeration-gap root
cause (`09`) is now fixed directly in `live_symbol_catalog`/
`live_routing_catalog` - `SPECIAL` enumerates 452 symbols on its own, no
report page needed. `eplan_live_read_pages_by_type`, the MCP tool this file
describes building as a workaround, has been **removed** - it existed only
because the real fix wasn't applied yet. `build_symbol_inventory.py` and the
redundancy-tiers workflow below are unaffected and still work the same way
against a `live_read_page`-shaped dump; only the recommended way to *produce*
that dump changed (plain `live_symbol_catalog` calls now, not a bulk report
read). Kept as the record of the second route that was found and why it
mattered before the root cause was located.

# Symbol Overview → Inventory: A Second Route Around the Enumeration Gap

Session 2026-09-08, project `Nuevo proyecto` (`Testing/SYMBOLS/Nuevo proyecto.elk`),
EPLAN 2025.0.3. The user built this project specifically as a symbol-visibility
aid: 104 pages of EPLAN's own built-in **Symbol Overview** report (`Insert >
Report > Symbol Overview` in the GUI; `PageType` = `"SymbolOverview"`), each
page laying out a handful of symbols in their four rotations plus label text,
already hand-trimmed of some duplicates/invisible entries before this session
started.

## Why this route matters

`09-symbol-catalog-enumeration-gap.md` found that `live_symbol_catalog`'s
depth-2 library listing stops at the first sparse `SymbolId` gap and reports
`truncated: false` anyway - a symbol above that gap is invisible to the
listing and its `contains` filter, though it resolves and places fine by
exact name. `10-image-to-schematic-pipeline.md` named the fix (`schematic.py`'s
depth-2 walk, `break` → `continue`) but also named this project's report pages
as an independent second route: a page a human (or EPLAN's own generator)
actually drew has no such blind spot, because nothing about it depends on
walking `SymbolId`s in order.

This session built the tooling to read that route at scale and turn it into
data.

## What was built

**`eplan_live_read_pages_by_type`** (new MCP tool, `schematic.py`) - reads
every page of one `PageType` in a single script execution, reusing
`live_read_page`'s own `ReadPage`/`DumpPlacement` serializer verbatim (spliced
in via the existing `_HELPERS_SCHEMATIC`) rather than one round trip per page.
Defaults to `page_type="SymbolOverview"`; `symbols_only=True` drops every
placement with no `symbol` (graphics, `PlaceHolderText`) before it comes back,
since those dominate a page's placement count and are noise for this purpose.
`types` filters further by CLR type on top of that. A `max_pages` cap fails
loud on a `page_type` that matches far more pages than intended, rather than
silently reading the whole project.

Before it existed as a tool, the same script (built by literally importing
`schematic.py`/`live.py` and calling `live._script()` with the real
`_HELPERS_SCHEMATIC`, so the scaffold was never hand-transcribed) was run once
through `execute_custom_script` to prove the approach - see the session
transcript. Verified live: **104/104 pages read, 0 skipped**, and the `types`+
`symbols_only` filters both confirmed correct (page `/1` narrowed from 54
placements to 17 when filtered to `types=["Function"]`, matching a hand count:
`DCP`+`DCP2`+`DCP2OL`+`DCPOL`×4 variants each + `PDL`×1 = 17).

Both the offline-generated C# (via the legacy `csc.exe`, C# 5, per
[[verify-generated-csharp-offline]]) and the live run against the real project
compiled/ran clean before this was promoted from a one-off script into a
permanent tool.

**`Testing/tools/build_symbol_inventory.py`** - turns the tool's output into:
- a long-format table (one row per pin) written to Parquet/CSV - the
  project-derived inventory `10` called for (`library`, `symbol`, `clrType`
  as a stand-in for `creates_clrType`, `variantNr`, `pin_index`,
  `pin_direction`, `offset_x`, `offset_y`, `page`)
- a three-tier, human-confirmed redundancy report (never auto-decides, per
  the "narrow, never decide" rule `10` already states for the image pipeline):
  - **Tier A** - the exact same symbol/variant placed on >1 page. Unambiguous.
  - **Tier B1/B2** - every pin at raw offset (0,0), split by whether the
    `clrType` is device-shaped (Function/Terminal/.... - high confidence odd)
    or routing-shaped (SymbolReference/InterruptionPoint/StrandConnector/... -
    low confidence, likely a legitimate bend point). The split exists because
    `SPECIAL/BR`, read live earlier this session, confirmed the routing case
    structurally: its two straight-through pins sit at (0,0) while only its
    third, jumped leg carries a real offset - a corner/T-node/bridge's bend
    point IS its location.
  - **Tier C** - different symbol names in the same library sharing a >=2-pin
    geometry signature. Confirmed noisy as expected on the large `IEC_symbol`
    library (278 groups, e.g. 218 symbols sharing a plain 2-pin footprint
    because they are electrically distinct contacts that happen to look
    alike) - capped and explicitly labelled a lead, not a verdict.

Both pieces are generic: neither hardcodes a library name or project, so the
same pipeline applies to a project built on `NFPA_symbol_en_US` (ANSI/JIC-style
US symbology) with no changes.

## Results on `Nuevo proyecto`

- **11,269 rows** written to `Testing/tools/.cache/symbol_inventory.parquet`.
- Tier A: **0** - EPLAN's report generator does not duplicate a symbol/variant
  across pages.
- Tier B1 (device-shaped, higher confidence): **36 rows = 9 symbols × 4
  variants** - `SPECIAL/DCP2OL`, `SPECIAL/DCPOL`, `SPECIAL/DCFP2OL`,
  `SPECIAL/DCFP2OL2`, `SPECIAL/SH2`, `IEC_symbol/XBD`, `IEC_symbol/XSD`,
  `IEC_symbol/MASSE`, `IEC_symbol/ANT`.
- Tier B2 (routing-shaped, low confidence): 28 rows across `BP`, `BPOL`,
  `CO`, `COST`, `CRST`, `TST`, `TS`, `SLST`.
- Tier C: 278 groups total, shown capped at 15.

A first pass cross-referenced the Tier B1 names against
`covaga/electrical-symbols-dataset` (`lookup_symbol_dataset.py`) and found
four of the nine (`DCPOL`, `DCP2OL`, `DCFP2OL`, `DCFP2OL2`) belong to the
dataset's "line only" device-connection-point family - symbols meant to sit
directly on a wire with no stub, which would make their (0,0) pins correct by
design rather than a defect. **The user's own correction on this point:** the
manual cleanup already done on these 104 pages was performed by visual
inspection in EPLAN, not against this dataset - so the dataset cross-reference
was a secondary lead, not the criterion actually used to confirm.

**Resolved (2026-09-08), by the user's own visual check in EPLAN** - recorded
in `Testing/tools/nuevo_proyecto_decisions.json` and applied via
`build_symbol_inventory.py --decisions`:

- **Excluded** (renders, but the geometry is real and useless -
  `SPECIAL/DCP2OL`, `SPECIAL/DCPOL` - plus their fluid-family siblings
  `SPECIAL/DCFP2OL`, `SPECIAL/DCFP2OL2`, excluded on the same call: "si es
  fluido descartalo también"). Despite matching the dataset's "line only"
  family (see above), the user's visual read overrides that prior: the
  drawing exists but is `"inutil"`.
- **Kept / verified_ok** (legitimate despite (0,0) pin geometry, same
  structural class as Tier B2's routing shapes): `SPECIAL/SH2`,
  `IEC_symbol/XBD`, `IEC_symbol/XSD`, `IEC_symbol/MASSE`, `IEC_symbol/ANT`.

The regenerated inventory (`Testing/tools/.cache/symbol_inventory.parquet`,
same 11,269 rows) carries a `status` column: 28 rows `excluded` (4 symbols ×
4 variants each, 1-2 pins per variant), 24 rows `verified_ok` (5 symbols × 4
variants each), 11,217 `unreviewed` (everything outside Tier B1 - Tier B2 and
Tier C were never put to the user for a per-symbol call).

## What this does NOT establish

- `live_symbol_catalog`'s depth-2 walk itself was not fixed here - `09`'s
  `break`→`continue` fix is still unapplied in the running server.
- Nothing in Tier B1/B2/C was excluded from the inventory yet. The parquet
  as written is unfiltered; a `status` column for confirmed exclusions is the
  natural next addition once the user has reviewed the candidates.
- This was measured on one project (`Nuevo proyecto`, `SPECIAL` +
  `IEC_symbol`). The claim that the same pipeline works unchanged against an
  NFPA/ANSI-library project is a design property (nothing library-specific in
  either script), not yet something run live against one.
