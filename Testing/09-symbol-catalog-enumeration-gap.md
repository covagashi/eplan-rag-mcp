**RESOLVED 2026-09-08.** Both `break`s named in "Root cause" below are now
`continue` in `schematic.py` (`live_symbol_catalog`'s depth-2 walk and
`live_routing_catalog`'s walk, which had the identical bug). Verified live
after an MCP server restart: `SPECIAL` now reports 452 symbols (up from 73),
`DCP2JICM` (id 402) is in the listing, and `truncated: false` is honest. The
finding below is kept as the record of how this was found and why it
mattered - see also `11-symbol-overview-inventory.md` for the workaround tool
this fix made unnecessary (removed the same day), and
`claude-skills/eplan-development/skills/eplan-development/references/api-data-access.md`
for the promoted general-purpose version of this gotcha.

# `live_symbol_catalog` Under-Reports Libraries — and Says It Didn't

**A "not in the catalog" answer from this tool is not evidence the symbol does
not exist.** The depth-2 library listing stops after `SymbolId` 72 and reports
`truncated: false` anyway.

## How it surfaced

The user asked for `DCP2JICM` to be placed. Checking first, as the discipline
in `01-schematic-authoring-primitives.md` requires:

```
live_symbol_catalog(library="SPECIAL", contains="DCP")
  -> 11 symbols, matched: 11, truncated: false     # no DCP2JICM
live_symbol_catalog(library="IEC_symbol", contains="DCP")
  -> 0 symbols
live_symbol_catalog(library="SPECIAL", limit=400)
  -> 73 symbols, matched: 73, truncated: false     # still no DCP2JICM
```

Three reads, all agreeing, none flagging incompleteness. The reasonable
conclusion — the one drawn, and stated to the user — was that `DCP2JICM` is
an ANSI/JIC symbol absent from this IEC project.

That was wrong. Asking for it *by name* works:

```
live_symbol_catalog(library="SPECIAL", symbol="DCP2JICM")
  -> symbolType: "Function", 8 variants, v0 pins (0,+4) and (0,-1)
```

and so does placing it:

```
live_place_symbol(page="+MANDO/4", library="SPECIAL", symbol="DCP2JICM",
                  x=0, y=0, variant_nr=0)
  -> success, handle 28/17/199032/0, clrType "Function"
```

## The mechanism: depth-2 `index` is the real `SymbolId`, and it stops at 72

The unfiltered `SPECIAL` listing returns exactly 73 entries with `index`
0…72, contiguous, no gaps. Those indices are not list positions — they are
EPLAN's own numeric symbol ids. Checked against
`covaga/electrical-symbols-dataset` (see `05-external-symbol-dataset.md`),
nine for nine:

| symbol | catalog `index` | dataset `number` |
|---|---|---|
| `BP` | 8 | 8 |
| `DCP5` | 20 | 20 |
| `DCP2` | 22 | 22 |
| `PLCCPNG` | 33 | 33 |
| `MC` | 44 | 44 |
| `BPIN` | 48 | 48 |
| `CO` | 70 | 70 |
| `TLRO` | 72 | 72 |
| **`DCP2JICM`** | **absent** | **402** |

So the enumeration walks ids 0 through 72 and stops, while the library holds
ids reaching at least 402. Everything above 72 is invisible to depth 2 and to
the `contains` filter, which operates on that same truncated set.

This retroactively explains something recorded earlier and left unexplained:
`02-routing-and-connections.md` notes that the dataset's 19 `CDP*` variants
(`CDP`=308, `CDPU`=338, the `CDPCP2F1`–`F5` piping family) "had not been
enumerated from this project directly" — even though page `+MANDO/1` has a
plain `CDP` placed on it. They were never going to appear: every one of them
is above id 72.

## What to do instead

1. **Never conclude a symbol is absent from a depth-2 listing.** The listing
   is a sample of low-id symbols, not an inventory. Only a depth-3 call
   (`symbol=` by exact name) answers "does this exist", and it answers
   authoritatively — it resolved `DCP2JICM` that the listing denied.
2. **Get candidate names from the dataset, then confirm at depth 3.** This
   inverts the guidance in `05-external-symbol-dataset.md`, which treated the
   dataset as a prior to be confirmed against the live catalog. For any symbol
   with an id above 72 the dataset is not merely a prior — it is the only
   route to the *name*, because the live listing will never show it. The live
   check still happens, just at depth 3 rather than depth 2.
3. **Treat `truncated: false` here as unverified.** It is reporting on the
   walk, not on the library.

## Root cause

Located in `schematic.py`'s depth-2 walk. It constructs `Symbol(lib, i)` for
`i` in `0..5000` and aborts on the first index that does not construct:

```csharp
    try { sym = symCtorInt.Invoke(new object[] { lib, i }); }
    catch { break; }
    if (sym == null) break;
    if (PropText(sym, "IsValid") != "True") continue;
```

`SymbolId`s are sparse, so the first gap — id 73 in `SPECIAL` — ends the walk
while ids up to at least 402 remain. The comment above the loop asserts it
"enumerates a library exhaustively", which is the false premise; it holds only
for a library with contiguous ids.

Both `break`s should be `continue`. The `IsValid` test already skips
non-symbols the same way, and the 5000 bound already caps the cost, so the
machinery for tolerating gaps is present — the exception path just takes the
wrong branch. Not applied here: this file records findings, and changing
server code mid-session leaves the running MCP server stale until restarted.

## Scope of the claim

Measured on `SPECIAL` in `EJEMPLO_scratch_20260904_120740` (EPLAN 2025), which
returned 73 entries. `IEC_symbol` was not enumerated exhaustively enough to
confirm the same cut-off applies there, though the `contains="DCP"` result of
zero — against a library that has to contain more than the listing shows —
points the same way. The number 72 may be a per-library artefact rather than a
constant; what is established is that the listing is incomplete and does not
say so.
