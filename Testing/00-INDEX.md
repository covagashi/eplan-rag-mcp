# Live Testing Notes — Schematic Authoring (Session 2026-09-04)

Replaces the old `Audit/` folder (a Spanish-language, action-by-action coverage
log for the general `eplan_action_*` catalog). This folder is narrower and more
concrete: it records what was actually **measured live** while building a real
control-circuit schematic ("puesta en marcha" / motor start) end-to-end through
the six `live_*` schematic-authoring primitives in `schematic.py`, on EPLAN
2025/2027, project `EJEMPLO` (cloned to a scratch project throughout — the
original was never written to).

Every claim below was produced by an actual tool call in this session and can
be re-verified the same way: place something, `live_read_page`/
`live_read_connections` it back, compare.

## Files

| File | Covers |
|---|---|
| [01-schematic-authoring-primitives.md](01-schematic-authoring-primitives.md) | The six primitives (`live_symbol_catalog` → `live_place_symbol` → `live_connect_pins` → `live_read_page` → `live_remove_placement`), plus `live_place_connected` and `live_set_device_tag`. What each returns, what "connected" actually means. |
| [02-routing-and-connections.md](02-routing-and-connections.md) | Corners, T-nodes, crosses (`Symbol.Type` taxonomy). How `generate_connections` + `live_read_connections` prove a circuit is real, not just drawn. A worked example: a self-holding motor-start rung with a verified parallel branch. |
| [03-coordinate-system-and-designations.md](03-coordinate-system-and-designations.md) | Confirming `+Y = up` on the page from placed reference terminals. The full four-part device designation (`=function++installation+location-device`) vs. the bare device-tag suffix most calls default to. |
| [04-interactive-vs-scripted-placement.md](04-interactive-vs-scripted-placement.md) | Two different ways to put a symbol on a page — the scripted `Function.Create` path (`live_place_symbol`) vs. the native GUI action `XEGActionInsertSymRef` (F3-equivalent) — their trade-offs, and a real device-drift bug caught by re-reading the page after a hang. |
| [05-external-symbol-dataset.md](05-external-symbol-dataset.md) | Cross-referencing a public HuggingFace symbol-description dataset against this project's live `IEC_symbol` library — where it helped, where it didn't, and what it confirmed. |
| [06-macrobox-reflection.md](06-macrobox-reflection.md) | A third placement kind the six primitives don't cover: `MacroBox`, invisible to `live_read_page`'s `Name` field. Found and verified `MacroBox.MacroName` by raw reflection, confirmed a write on an empty box, and flagged what that test does *not* prove for a populated one. |
| [07-page-to-ascii.md](07-page-to-ascii.md) | `tools/page_to_ascii.py` — renders a `live_read_page` result as a Dungeon-Crawl-style glyph map with a key block, so layout questions become visual instead of pairwise coordinate arithmetic. Routing glyphs are derived from pin directions, and every lossy step (overdrawn cells, non-straight connections) is reported rather than hidden. |
| [08-replicating-a-real-page.md](08-replicating-a-real-page.md) | Rebuilding a real production safety-circuit page from a photo. The contact ladders verified; four things broke. `live_place_symbol` cannot place objects EPLAN instantiates as `Terminal` (a device connection point is a `Function` and places fine — the catalog reports both as `symbolType: Function`, so it cannot be predicted) **and leaves an orphan at (0,0) after reporting failure**; a run between two interruption points produces no `Connection`; interruption points cannot be named; `live_set_device_tag` under-reports a merge. |
| [09-symbol-catalog-enumeration-gap.md](09-symbol-catalog-enumeration-gap.md) | `live_symbol_catalog`'s library listing stops after `SymbolId` 72 and reports `truncated: false` anyway. A symbol above that id is invisible to the listing and its `contains` filter, but resolves and places fine when named at depth 3. "Not in the catalog" is not evidence of absence. Root cause located: the depth-2 walk `break`s on the first sparse-id gap. |
| [10-image-to-schematic-pipeline.md](10-image-to-schematic-pipeline.md) | Design notes for turning a page image into placements with OCR + OpenCV against the symbol dataset. Because EPLAN autoconnects, no wire topology needs extracting — the wires become a verification oracle instead. What CV structurally cannot supply (`variant_nr`, `library`), and the project-derived parquet that would supply it. |
| [11-symbol-overview-inventory.md](11-symbol-overview-inventory.md) | Built the project-derived parquet `10` called for, via a second route around `09`'s enumeration gap: EPLAN's own Symbol Overview report pages, read in bulk by a new MCP tool (`eplan_live_read_pages_by_type`) and turned into a tidy inventory + three-tier, human-confirmed redundancy report by `tools/build_symbol_inventory.py`. Verified live on a 104-page project (11,269 rows); redundancy candidates pending the user's own confirmation. |

## Ground rules this session actually followed

- **Writes stayed scratch-only.** Every placement in these notes happened in a
  disposable clone (`eplan_scratch_project_create`), never the user's real
  `EJEMPLO.elk`.
- **A write is not "connected" until proven.** `live_place_symbol` /
  `live_place_connected` report facing pins on a shared axis — that is
  geometry, not a logical connection. Only `generate_connections` +
  `live_read_connections` settle whether EPLAN actually wired two devices.
- **Nothing here was read from a screen.** Every fact — including catching a
  device that had drifted position — came from diffing two JSON reads
  (`live_read_page`/`live_read_connections`) against each other, never from
  pixels. The ASCII maps in `07-page-to-ascii.md` are no exception: they are
  rendered *from* that same JSON, so they can show a layout mistake but can
  never show anything the JSON did not already contain.
