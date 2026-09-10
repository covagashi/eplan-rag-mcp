# Image → Schematic: What a Pipeline Would Actually Need

Design notes, not an implementation. Written after being asked whether OCR +
OpenCV against `covaga/electrical-symbols-dataset` (which carries a PNG per
symbol) could turn a photo of a page into placements.

Short answer: the identification half works better than it has any right to,
the construction half is smaller than it looks, and the two things CV cannot
supply are exactly the two that decided every outcome in
`08-replicating-a-real-page.md`.

## The correction that shrinks the whole problem: EPLAN wires itself

The first draft of this design had a stage for extracting wire topology —
`HoughLinesP` over the line mask, junction detection, net building. **That
stage should not exist.**

Nothing in this session ever drew a wire. `02-routing-and-connections.md`
records the rule and `08` exercised it: two facing pins on a shared axis
autoconnect, and `generate_connections` materialises the `Connection` objects.
Six real connections came out of placing symbols at coordinates and nothing
else. Corners and T-nodes are not lines either — they are placed symbols, so
they fall out of the same detection stage as the devices.

So the pipeline's output is **symbol identity + variant + millimetre
position**, full stop. The wire pixels are not needed to build anything.

They are still worth extracting, for a different job. The lines in the image
are the drawing's *intended* topology; `live_read_connections` afterwards is
the *achieved* topology. Diffing the two is what catches the failure this
session actually hit — the 0V column in `08` that produced no connection at
all, silently, with no error from `generate_connections`. Wires as an oracle,
never as an input.

## Stages

| stage | classical tooling | output |
|---|---|---|
| separate glyphs from wires | binarise, then morphological opening with a long horizontal kernel and again with a long vertical one; subtract both | symbol/text blobs |
| crop candidates | connected components on what remains | crops |
| identify | `cv2.matchTemplate` against the dataset PNGs, over the 8 orientations | `short_name`, `number`, score |
| read labels | Tesseract on the text blobs | `-K4.011`, `23`/`24`, `A₂ 1,5 mm²` |
| pixels → mm | `cv2.getPerspectiveTransform` | page coordinates |
| verify (after building) | `HoughLinesP` on the discarded line mask, vs `live_read_connections` | topology diff |

No training anywhere. Line removal by morphology is the standard table- and
form-extraction technique, and a schematic suits it better than a table does,
because the wires really are pure straight runs.

**The calibration stage is where the four reference terminals earn their
keep.** `03-coordinate-system-and-designations.md` records them placed by hand
at `(0,292)`, `(0,16)`, `(420,292)`, `(420,16)` to settle which way +Y points.
Four points with known millimetre positions is exactly a homography: the
pixel→mm transform stops being an estimate and becomes solved, including any
perspective if the input is a photo rather than a render.

## What CV cannot supply, and why it is the part that matters

**`variant_nr`.** Rotation is recoverable — it is whichever of the eight
orientations scored best. Rotation is not the problem. `SPECIAL/TLRU` has
five variants that **render identically** and differ only in pin offsets: v0
puts its Right pin at `(+2, 0)`, v8 puts all three legs at `(0, 0)`. In `08`
that choice decided whether the T-node landed on the pin or beside it. The
same holds for `IEC_symbol/S` v0 vs v2 — same picture, pin indices swapped, so
`pin[0]` means the opposite terminal.

**`library`.** The dataset carries `short_name` and `number` but not which
EPLAN library the symbol lives in. `live_symbol_catalog` at depth 3 needs the
library name to resolve anything.

Both gaps are closable, but not with pixels — see below.

## The missing artefact: a project-derived parquet

The public dataset answers *what does this symbol mean* and *what is it
called*. It cannot answer *how do I place it*. A second parquet, derived from
a live project, would carry what the first one structurally cannot:

| column | why |
|---|---|
| `library` | required by every `live_*` call; absent from the public dataset |
| `symbol` | join key to `short_name` |
| `symbolType` | what `live_symbol_catalog` reports |
| `creates_clrType` | `Function` or `Terminal` — see below |
| `variantNr` | one row per variant |
| `pin_index`, `pin_designation`, `offset_x`, `offset_y`, `direction` | one row per pin |

Two things this unlocks immediately:

**It closes the variant gap the CV cannot.** If the detection stage records
*where the wire stubs attach* to a cropped symbol — which the line mask gives
for free, since attachment points are where removed lines met a kept blob —
those measured offsets can be matched against each candidate variant's pin
offsets. `TLRU` v0 (Right pin at `+2`) and v8 (all at `0`) become
distinguishable by 2mm of geometry rather than by pictogram. That turns an
impossible discrimination into an ordinary one.

**It pre-empts the `Terminal` trap.** `08` finding 2: `IEC_symbol/X` reports
`symbolType: "Function"` exactly like `SPECIAL/DCP2JICM`, but EPLAN
instantiates it as `Terminal`, `Function.Create` throws, and — the dangerous
part — a stray object is left at `(0, 0)`. That distinction is invisible ahead
of time and can only be learned by trying. A `creates_clrType` column records
what a single probe found out, once, so nothing has to rediscover it by
stranding an orphan on a production page.

## Building it depends on a one-line fix — DONE (2026-09-08)

Both `break`s (this one and the identical bug in `live_routing_catalog`) are
now `continue`. Verified live: `SPECIAL` enumerates 452 symbols instead of 73.
The project-derived parquet described below is no longer blocked by this.

## (original note, kept for the record)

Enumerating a library is currently impossible, and `09-symbol-catalog-enumeration-gap.md`
records the symptom. The cause is in `schematic.py`, in the depth-2 walk:

```csharp
// Walk by INDEX - proven to enumerate a library exhaustively, and it
// stops at the first index that does not resolve.
for (int i = 0; i < 5000; i++)
{
    object sym = null;
    try { sym = symCtorInt.Invoke(new object[] { lib, i }); }
    catch { break; }
    if (sym == null) break;
    if (PropText(sym, "IsValid") != "True") continue;
```

The loop bound is fine and it already knows how to skip an invalid symbol with
`continue`. But `SymbolId`s are **sparse** — `SPECIAL` runs 0…72 and then
nothing until at least 402 (`DCP2JICM`) — and `catch { break; }` aborts at 73.
The comment's claim that this enumerates exhaustively is the false premise;
it was presumably validated against a library with contiguous ids.

Changing both `break`s to `continue` makes the walk complete. `IsValid` and
the null check already handle the gaps, and the 5000 bound already caps the
cost.

**Do this before the parquet.** With the walk fixed, the project parquet is a
straightforward enumeration: every library, every symbol, every variant, every
pin. Without it, the only route is probing the public dataset's ~33,500 names
one depth-3 call at a time — which works, since depth 3 resolved `DCP2JICM`
that the listing denied, but costs four orders of magnitude more calls to
learn the same thing.

## The rule this pipeline has to inherit

It must **narrow, never decide**. Not emit `live_place_symbol(...)`; emit
ranked candidates with scores and millimetre coordinates, to be reconciled
against a depth-3 `live_symbol_catalog` call before anything is written.

That is the same rule `lookup_symbol_dataset.py` states in its own docstring
("this script's job is narrowing candidates, not proving a name resolves") and
the same one `page_to_ascii.py` follows when it refuses to draw a connection
it cannot justify as axis-aligned. It is not caution for its own sake: in this
session that refusal is what made the missing 0V column visible. A confident
score attached to an invented variant is precisely the kind of output that
looks right and is not.
