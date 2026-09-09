# Generating a project from an EEC One "typical" workbook

How to turn a `Typical <timestamp>.xlsm` sheet into a real EPLAN project by
inserting its macros yourself, without EEC One in the loop.

Field-proven 2026-09-09 on EPLAN 2025 (remote 49152), workbook
`Typical 20260907122428.xlsm`, macro root `V:\Macros\ProductoSTD`, product
family `7C0x`. Two sheets generated back to back: `L1.Z1@C0001_MULTI1`
(122 items, 38 pages) and `L1.Z1@C0002_MULTI1` (145 items, 39 pages,
51 340 objects) — 0 failures on both.

## Reading the workbook

One sheet per cabinet, named `<installation site>@<mounting location>_MULTI<n>`
(e.g. `L1.Z1@C0001_MULTI1`). Alongside them sit `RESUME`, `CONFIG_1`,
`TYPICAL EEC` (the column template), `AUX_1` and some `wksHidden_*` rule
sheets — none of which you need to insert anything.

### Columns

| Col | Meaning |
|---|---|
| A | Macro path, **relative to the macro root**, or a `####` marker |
| B–H | Structure identifiers: functional assignment, higher-level function, installation site, mounting location, HLF number, document type, user-defined |
| I | **Page name** |
| J | Representation type, as `Label <n>` |
| K | Macro variant letter (`A` = index 0) |
| L | `!` / `_` flag |
| M, N | X, Y — **in practice always empty**; see "Placement" below |
| O… | `PARAMETRO_n` / `PARAMETRO_VALOR_n`, then the electrical and general property pairs |

### Row 2 is special — and carries the whole structure

Row 2 is simultaneously the first macro row **and** the configuration header.
It is the **only** row with B–H filled, and those values apply to *every* page
the sheet generates. Rows 3+ carry only A, I, J, K, L.

It also holds the parameter set that produced this instance
(`FTD_VERSION`, `TYPE`, `NSI`, `LI_1..6`, `CS`, `CAL`, `TNEUTRAL_R`…) and, in
the first general-property value, a summary code like
`K3-X15C0L-1-CNNNNN-000Z`. Those are documentation of *why* these rows are
here — do **not** try to evaluate them.

### The sheet is already filtered — do not evaluate the conditions

Macro filenames encode their own inclusion rule: `_CS=L`, `_NSI≥1`, `_CAL=NO`,
`_TNEUTRAL_R≠IT`, and compound `Y(...)` = AND, `O(...)` = OR, e.g.
`040_{X06AC}_Y(CS=L;Y(O(LI_1=C;LI_1=S);O(TNEUTRAL_R=TT;TNEUTRAL_R=TNS)))`.

**These are already resolved.** The generated workbook lists only the macros
that passed. Confirm it cheaply: the macro folder on disk holds the
complementary variants (`CS=XL`, `NSI≥3`, `TNEUTRAL_R=IT`) that the sheet does
not list. Some conditions even reference parameters absent from row 2
(`SL_1`, `NLI` — derived in the hidden rules sheets), so evaluating them
yourself is not just wasted work, it is not possible from the sheet alone.

### `####` markers

- `####[X]` … `####` — a group sharing one reference point, X direction.
- `####[Y]` … `####` — same, Y direction.
- `####[script=<path>]` — a post-process script (last row). See below.

Inside a group the first macro is usually `..._(REF).ema`. Groups appear with
**distinct labels** (`{X02AA}`, `{X03BB}`, `{Y02AD}`), so each is placed once —
they are grouping markers, not repetition counts. Empty `####[X]`/`####` pairs
are reserved slots that produced nothing for this configuration; skip them.

Skip every `####` row when building the insertion list.

## Path resolution

`A` is relative to the macro root, and the extension is **often missing**. A
subset of rows also arrives in UPPERCASE — harmless on Windows, whose
filesystem is case-insensitive, so do not "fix" it.

```python
def resolve(a, root):
    base = os.path.join(root, a)
    if os.path.splitext(a)[1].lower() in (".ema", ".emp"):
        return base if os.path.exists(base) else None
    for ext in (".ema", ".emp"):                 # extension-less row
        if os.path.exists(base + ext):
            return base + ext
    return None
```

The extension **is** the row's kind: `.emp` = page macro (creates pages),
`.ema` = window macro (placed on a page).

Beware one lookalike character: the "less than" in conditions is `˂`
(U+02C2 MODIFIER LETTER LEFT ARROWHEAD), not `<` — chosen because `<` is
illegal in a filename. Match filenames byte-for-byte, never normalize.

## Validate the manifest before touching EPLAN

The model is self-checking, and the check is one line: **every page name
referenced by an `.ema` row must be created by some `.emp` row.** That
invariant is also *why* column I is the page name — it is how a window macro
addresses its target page.

```
pages   = [i.page for i in items if i.kind == "page"]
missing = [i.page for i in items if i.kind == "window" and i.page not in pages]
assert not missing
```

If `missing` is non-empty, your parse is wrong — stop, do not insert.

## Insertion

Everything below runs through the reflection scaffold in
`api-data-access.md` / `e3d-installation-spaces.md`: `LockingStep` held for the
whole run, `SelectionSet.GetCurrentProject(false)` for the project, `FindType`
+ `Activator` + `MethodInfo` for the rest. **No `using Eplan.EplApi.DataModel`
or `.HEServices`** — they do not compile in the script engine.

### The two overloads

```csharp
// Eplan.EplApi.HEServices.Insert
StorableObject[] PageMacro  (string empFile, Page insertAfter, Project prj,
                             bool overwrite, PageMacro.Enums.NumerationMode);
StorableObject[] WindowMacro(string emaFile, WindowMacro.Enums.RepresentationType,
                             int variant, Page page, PointD placement,
                             Insert.MoveKind, WindowMacro.Enums.NumerationMode);
```

Select them by parameter shape, then **take every enum and struct type off the
resolved `MethodInfo`'s own parameters** — never `FindType("...PointD")`:

```csharp
Type pmNumType = pm.GetParameters()[4].ParameterType;
Type repType   = wm.GetParameters()[1].ParameterType;
Type ptType    = wm.GetParameters()[4].ParameterType;   // PointD
Type mkType    = wm.GetParameters()[5].ParameterType;   // MoveKind
Type wmNumType = wm.GetParameters()[6].ParameterType;

object none       = Enum.ToObject(pmNumType, 1);   // NumerationMode.None
object mkRelative = Enum.ToObject(mkType, 2);      // MoveKind.Relative
```

Enum values worth memorising: `MoveKind` = `Absolute 1`, `Relative 2`.
`NumerationMode` = `Ignore 0`, `None 1` (= "do not modify", the default),
`Number 2`, `NumberWithQuestionMark 3`, `NumberPreferPrefix 4`.

### Placement — columns M/N are empty on purpose

Each macro is authored at its final position. Insert **every** window macro at
`PointD(0, 0)` with `MoveKind.Relative`, i.e. its own relative origin, and it
lands where the macro author put it. Do not compute offsets, do not try to
tile the `####[X]` groups.

```csharp
object pt = Activator.CreateInstance(ptType, new object[] { 0.0, 0.0 });
```

### Representation type comes from the `<n>` in column J

Parse the number out of `Multipolar <1>`; do **not** match the label. Several
distinct labels share one value — `Multi-line <1>` and `Multipolar <1>` are
both `1`, `Estructura de armario <6>` and `Panel layout <6>` are both `6` —
and the labels are localized, so they change with the UI language. Observed:
`1` multi-line/multipolar, `5` graphics, `6` panel layout / cabinet structure.

Variant: column K letter, `A` → `0`.

### A page macro keeps its OWN structure — you must rename it

`PageMacro(..., insertAfter: null, ...)` takes the structure identifiers from
the macro, so pages arrive named after the authoring project — the reference
run produced `=MLX++17+Hoja de configuración/3` for a macro that column I said
was page `015`. The docs' alternative (structure inherited from
`insertAfter`'s parent node) does not give you column I either.

So rename each returned page explicitly, from row 2's columns:

```csharp
object ppl = Activator.CreateInstance(pplType);   // PagePropertyList
SetProp(ppl, "DESIGNATION_PLANT",               plant);                // col C
SetProp(ppl, "DESIGNATION_PLACEOFINSTALLATION", placeOfInstallation);  // col D
SetProp(ppl, "DESIGNATION_LOCATION",            location);             // col E
SetProp(ppl, "DESIGNATION_DOCTYPE",             doctype);              // col G
SetProp(ppl, "PAGE_COUNTER",                    pageName);             // col I
pageType.GetMethod("SetName", new Type[] { pplType }).Invoke(page, new object[] { ppl });
```

Property-name mapping, since none of them is named after its column label:

| Sheet column | `PagePropertyList` member |
|---|---|
| C Higher-level function | `DESIGNATION_PLANT` |
| D Installation site | `DESIGNATION_PLACEOFINSTALLATION` |
| E Mounting location | `DESIGNATION_LOCATION` |
| G Document type | `DESIGNATION_DOCTYPE` |
| B Functional assignment | `DESIGNATION_FUNCTIONALASSIGNMENT` |
| H User-defined | `DESIGNATION_USERDEFINED` |
| I Page name | `PAGE_COUNTER` |

`PAGE_COUNTER` accepts a **string** through `PropertyValue`'s implicit
conversion, and that is what preserves a leading zero: pass `"015"`, not `15`.
Result: `&ECS=0++L1.Z1+C0001/015`.

If one `.emp` ever yields several pages, give the first column I and suffix the
rest (`015.1`, `015.2`). In the reference family each page name appears exactly
once as an `.emp`, so this never fired — but a silent name collision would.

### Index pages by counter within THIS run only

Window macros find their page by column I. Seeding that dictionary from the
pages already in the project **is a real bug the moment a second cabinet is
generated into the same project**: C0001 and C0002 both own a page whose
counter is `015`, so the C0002 window macros would silently land on C0001's
pages. Since every `.emp` for a sheet lives in that same sheet, register only
pages created during the run. Use the existing pages solely to pick the
`insertAfter` anchor so the new block appends at the end of the tree.

## Batching and resumability

The reference run: ~4 minutes and ~90 k placed objects for two cabinets. A
single sheet fits comfortably in one call at `timeout_seconds=600`.

If you do split it, split by **spreadsheet row range** (`FROM_ROW`/`TO_ROW`) —
that makes a batch resumable after a timeout, which matters because a timeout
never writes the result file. Catch per item, record `{row, file, error}`, and
keep going; one bad macro should not abort 140 good ones.

**Always guard the write with an explicit project-name check.** These scripts
create dozens of pages and any `RESET`-style clean-up is unrecoverable:

```csharp
if (projName != EXPECT_PROJECT)
    throw new Exception("Refusing to write: open project is '" + projName + "'.");
```

## The trailing script row

The last row is typically
`####[script=\\<server>\Eplan\Macros\ProductoSTD\_TOOLS\MLX_Numerar_paginas.cs]`
— a post-process that **renumbers every page**, discarding the column-I names
you just set. It is a separate, hard-to-reverse step: report it and let the
user decide, rather than running it as part of "insert the macros".

Note the row's UNC path (`\\<server>\Eplan\Macros\ProductoSTD`) and the local
macro root (`V:\Macros\ProductoSTD`) are the same share. Ask for the root
rather than deriving it.

## Verifying the result

`eplan_get_system_messages` will not show you the check results — run
`eplan_check_project`, then `eplan_live_read_check_messages`
(see [api-data-access.md](api-data-access.md), "Reading check-run messages").

Expected noise on a freshly generated project, and what it means:

| Id | Message | Reading |
|---|---|---|
| 2 / 3 | Interruption point without counterpart / without target | **Expected** while only some cabinets exist — the counterparts live in the sheets you have not generated yet |
| 1 | Placement outside the plot frame's evaluation area | Worth a look; came from MacroBox and PLC objects |
| 23 | Main function data ≠ part data | Macro/parts-database drift, pre-existing |
| 999 | "Verificación de calidad", empty text | Bulk of the messages and says nothing useful through `GetText()` — check the GUI dialog before reporting it as a finding |

During insertion the message tree also fills with one benign repeated warning
per part carrying a user-defined property whose display name differs from the
project's ("…tiene un nombre mostrado diferente…"). Not caused by the
insertion.

`PrjMessagesCollection.Count` again disagreed with the check summary
(241 vs 101), the same way it did at 3439 vs 3424. Treat the summary number as
the count and the collection as the content.
