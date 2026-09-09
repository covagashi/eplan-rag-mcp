# API Data Access (Parts Database, Properties)

## The whole API is reachable — the restriction is compile-time only

This is the single most important thing to get right, and it is easy to state
backwards. There are **two** separate questions, and only the first has a
restrictive answer:

| | |
|---|---|
| Can I write `using Eplan.EplApi.X;`? | **Only for a fixed handful.** Everything else is CS0234. |
| Can I reach `Eplan.EplApi.X` from a script at all? | **Yes — all of it**, via runtime reflection. No API project, no extra licence. |

The script engine compiles against a **fixed assembly reference set** — `System`,
`Eplan.EplApi.Base`, `Eplan.EplApi.ApplicationFramework`, `Eplan.EplApi.Gui`,
`Eplan.EplApi.MasterData`, `Eplan.EplApi.Scripting`, `Eplan.IdentityClient`.
A `using` outside that set fails to compile: confirmed for
`Eplan.EplApi.DataModel`, `.HEServices` (2026-08) and `.EServices` (2026-09-08).
That is a *compiler* reference list, not a capability boundary: the assemblies
are in the same process and reflection sees all of them.

**Measured live, EPLAN 2025.0.3, `C:\Program Files\EPLAN\Platform\2025.0.3\Bin`,
2026-09-09** — sweep of `AppDomain.CurrentDomain.GetAssemblies()`:

- **38** `Eplan*` assemblies loaded in the process
- **26** `Eplan.EplApi.*` namespaces, **606** public types, **0** assemblies
  that threw on `GetTypes()`
- **6** further `Eplan.EplApi.*.dll` present but not preloaded
  (`MasterDatau`, `RecorderToolsu`, `RemoteClientu`, `Starteru`,
  `WebServicesu`, `WebServiceu`) — **all six loaded fine** via
  `Assembly.Load(name)`. **Zero refusals.**

So do not write "assume every `Eplan.EplApi.*` namespace is blocked". Assume
every namespace is **available**; only the `using` needs the reflection detour.

Largest namespaces by public type count: `DataModel` 214, `DataModel.E3D` 65,
`DataModel.Graphics` 52, `HEServices` 43, `ApplicationFramework` 40, `Base` 37,
`EServices` 22, `DataModel.Planning` 19, `DataModel.MasterData` 18,
`DataModel.EObjects` 17, `EServices.Ged` 12, `Gui` 12. (A floor, not a ceiling —
the count was taken before loading the six lazy assemblies, so
`Eplan.EplApi.MasterData` itself is not in it.)

Note `Eplan.EplApi.MasterDatau` is **not preloaded** yet `using
Eplan.EplApi.MasterData;` compiles — it is in the compiler's reference set and
loads on first use. Compile-time availability and runtime preloading are
independent; do not infer one from the other.

### Do not hardcode the assembly name — and do not assume the version rule

`Assembly.Load("Eplan.EplApi.DataModelu")` **fails on EPLAN 2027** with
`BadImageFormatException` (0x8007000B): there the managed object model is
`Eplan.EplApi.DataModelNetu` / `Eplan.EplApi.HEServicesNetu` and the
un-suffixed name is the mixed-mode **native** twin. Both names exist in the
2027 process, so a hardcoded name is a silent wrong-assembly pick.

But the naming is **not** simply "old = plain, new = Net": measured on
**2025.0.3 the loaded managed assembly is `Eplan.EplApi.DataModelu`**, there is
no `DataModelNetu` in the process, and nothing refuses to load. So the rule is
not a version cutoff you can memorise — resolve the type out of
`AppDomain.CurrentDomain.GetAssemblies()` first (EPLAN already has the managed
assembly loaded), and only fall back to `Assembly.Load` of candidate names,
newest scheme first. That works on every scheme without knowing which you are on.

### `Eplan.EplApi.EServices`

Third namespace in the CS0234 family, and where the itemized results of a check
run live (`PrjMessagesCollection`) — NOT the store
`get_system_messages`/`SysMessagesCollection` reads. See "Reading check-run
messages" below.

### `execute_custom_script`: what the wrapper already injects

The MCP `eplan_execute_custom_script` wrapper prepends exactly three usings:
`System`, `Eplan.EplApi.Base`, `Eplan.EplApi.Scripting`. Repeating them is a
harmless **CS0105** warning. Everything else you use — `System.IO`,
`System.Text`, `System.Reflection`, `System.Collections.Generic` — you must
declare yourself, or you get a wall of CS0246/CS0103. Declaring all of them and
eating the three CS0105 warnings is the safe default.

That tool now **reports compile errors properly** (`errorType:
"McpScriptNoResult"` with a `compile_errors` list carrying file, line, column
and CS#### text, plus `failedScriptPath`) — verified 2026-09-09. The older
failure mode, where a bad `using` surfaced only as `"Timeout waiting for script
results"`, is fixed on that path. A slow compile can still exceed the
foreground timeout and land as a background task; the diagnostics then arrive
with the task notification.

Incidentally `Eplan.EplApi.Scripting.StartAttribute` does **not** exist as a
type name even though `[Start]` resolves — use
`typeof(Eplan.EplApi.Base.BaseException).Assembly.Location` if you need the
install's `Bin` directory.

## Parts database (`MDPartsManagement`)

```csharp
using Eplan.EplApi.MasterData;

MDPartsManagement pm = new MDPartsManagement();
MDPartsDatabase database = pm.OpenDatabase();   // currently configured parts DB
if (database == null) throw new Exception("Could not open parts database");

foreach (MDPart part in database.Parts)
{
    if (part == null || string.IsNullOrEmpty(part.PartNr)) continue;
    // part.PartNr, part.Variant, part.Properties...
}
```

## Part properties

Access via `part.Properties.<PROPERTY_NAME>`. Values are `MDPropertyValue`: check `IsEmpty` before converting.

```csharp
string manufacturer = part.Properties.ARTICLE_MANUFACTURER_NAME.IsEmpty
    ? "" : part.Properties.ARTICLE_MANUFACTURER_NAME.ToString();
string erp = part.Properties.ARTICLE_ERPNR.IsEmpty
    ? "" : part.Properties.ARTICLE_ERPNR.ToString();
bool hasCE = !part.Properties.ARTICLE_CERTIFICATE_CE.IsEmpty
    && part.Properties.ARTICLE_CERTIFICATE_CE.ToBool();
string ul = part.Properties.ARTICLE_CERTIFICATE_UL.IsEmpty
    ? "" : part.Properties.ARTICLE_CERTIFICATE_UL.ToString();
```

Useful article properties:
- `ARTICLE_DESCR1/2/3` — descriptions (multilang; parse with the multilang parser in core-classes.md)
- `ARTICLE_MANUFACTURER_NAME`, `ARTICLE_ERPNR`
- `ARTICLE_CERTIFICATE_CE` (bool), `ARTICLE_CERTIFICATE_UL`
- `ARTICLE_EXTERNAL_DOCUMENT_1` … `ARTICLE_EXTERNAL_DOCUMENT_20` — legacy doc links
- `ARTICLE_EXTERNAL_DOCUMENT_URL[i]` / `ARTICLE_EXTERNAL_DOCUMENT_DESIGNATION[i]` — indexed doc links (newer)

### Indexed vs legacy external documents (fallback pattern)
Newer databases use the indexed `ARTICLE_EXTERNAL_DOCUMENT_URL[i]` properties; older ones the numbered `ARTICLE_EXTERNAL_DOCUMENT_n`. Robust code reads the indexed property and falls back to the numbered one when empty:

```csharp
for (int i = 1; i <= 20; i++)
{
    var pUrl = part.Properties.ARTICLE_EXTERNAL_DOCUMENT_URL[i];
    var pDesg = part.Properties.ARTICLE_EXTERNAL_DOCUMENT_DESIGNATION[i];
    if (pUrl.IsEmpty)
    {
        // fall back to ARTICLE_EXTERNAL_DOCUMENT_1 .. _20 by index
        switch (i)
        {
            case 1: pUrl = part.Properties.ARTICLE_EXTERNAL_DOCUMENT_1; break;
            case 2: pUrl = part.Properties.ARTICLE_EXTERNAL_DOCUMENT_2; break;
            // ... up to 20
        }
    }
    // document paths often contain $(MD_DOCUMENTS) -> resolve with PathMap.SubstitutePath
}
```

## Writing part properties (and creating/removing parts)

Writes go through the same property list and take effect **immediately** —
there is no save, commit or `Store()` step. Verified live on EPLAN 2026.

```csharp
MDPartsManagement pm = new MDPartsManagement();
using (MDPartsDatabase db = pm.OpenDatabase())
{
    MDPart part = db.AddPart("ZZ-TEST-001");        // throws if it exists

    // Assign through the property-id indexer, or the typed member.
    part.Properties[Eplan.EplApi.MasterData.Properties.MDPartsDatabaseItem.ARTICLE_MANUFACTURER]
        = "BOSAQ";
    part.Properties[Eplan.EplApi.MasterData.Properties.MDPartsDatabaseItem.ARTICLE_DESCR1]
        = "Circuit breaker";                        // MultiLangString-valued

    db.RemovePart(part);                            // permanent, no undo
}
```

`ARTICLE_DESCR1` and friends are multilanguage: a plain string is stored as
`??_??@Circuit breaker;` (no language assigned). Build a `MultiLangString`
if the language matters — see core-classes.md.

### `new MDPropertyValue("x")` does not exist

`MDPropertyValue` has **only a default constructor** — `CS1729: does not
contain a constructor that takes 1 arguments`. The string → `MDPropertyValue`
conversion that makes the assignment above work is compile-time only. To
build one, default-construct and `Set` it:

```csharp
var pv = new MDPropertyValue();
pv.Set("BOSAQ");            // Set(String) / Set(Double) / Set(Boolean) / ...
```

### Reaching a property by name at runtime: mind the ambiguity

Every `ARTICLE_*` member is declared **twice** on
`MDPartsDatabaseItemPropertyList` — once parameterless, once taking an `int`
index (for multi-value properties like `ARTICLE_CUSTOM_DATA_VALUE(i)`). So
the obvious reflection call throws `AmbiguousMatchException: Ambiguous match
found.` for *every* property, and `BindingFlags.DeclaredOnly` does not help
(both overloads are declared on the same type):

```csharp
// WRONG - always throws
var pi = part.Properties.GetType().GetProperty(name);

// RIGHT - pin the empty index-parameter list to select the plain overload
var pi = part.Properties.GetType().GetProperty(
    name,
    BindingFlags.Public | BindingFlags.Instance,
    null, null, Type.EmptyTypes, null);

var value = pi.GetValue(part.Properties, null);     // an MDPropertyValue

var pv = new MDPropertyValue();
pv.Set("BOSAQ");
pi.SetValue(part.Properties, pv, null);             // NOT a bare string:
                                                    // ArgumentException
```

Also note which names live where: `PartNr`, `Variant`, `ProductGroup`,
`ProductSubGroup` and `ProductTopGroup` are members of **`MDPart`**, while
descriptions, manufacturer, order number etc. are `ARTICLE_*` entries on
`MDPart.Properties`. A name-based lookup has to try both objects. There is no
`Description1` or `Manufacturer` on either — those are `ARTICLE_DESCR1` and
`ARTICLE_MANUFACTURER`.

## User-defined properties on parts

User-defined properties live on the part as `UserDefinedPropertyPositions`; each position has `IdentifyingName` (e.g. `"MLX.P025"`) and `Value` (multilang string):

```csharp
foreach (var pos in part.UserDefinedPropertyPositions)
{
    if (pos == null) continue;
    string ident = pos.IdentifyingName ?? "";
    if (ident.Equals("MLX.P025", StringComparison.OrdinalIgnoreCase))
    {
        var v = pos.Value;
        if (v != null) myValue = ParseMultiLang(v.ToString());
    }
}
```

## Symbol library enumeration (`SymbolLibrary` / `Symbol`)

Same reflection technique as the parts database, reached through
`Eplan.EplApi.DataModel.MasterData.SymbolLibrary`/`Symbol`/`SymbolVariant` -
no separate API license, works from a plain script.

```csharp
Type libType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolLibrary");
Type symType = FindType("Eplan.EplApi.DataModel.MasterData.Symbol");
ConstructorInfo libCtor = libType.GetConstructor(new Type[] { project.GetType(), typeof(string) });
object lib = libCtor.Invoke(new object[] { project, "IEC_symbol" });

// By exact name - the reliable route, see the gotcha below for "by index".
ConstructorInfo symByName = symType.GetConstructor(new Type[] { libType, typeof(string) });
object sym = symByName.Invoke(new object[] { lib, "Q_2L" });
```

### `Symbol(SymbolLibrary, int)` ids are SPARSE - `continue`, never `break`, on a construction miss

Walking a library by numeric index (`new Symbol(lib, i)` for `i` in `0..N`) is
the only way to enumerate names without already knowing them, but **the ids
are not contiguous**. Measured live (2026-09-08): on one project's `SPECIAL`
library the constructor throws for every `i` from 73 onward until 402
(`DCP2JICM`), then resolves again up to 634. A loop that does
`catch { break; }` (or `if (sym == null) break;`) on the first failure stops
enumerating at 73 and **silently drops every id above it** - it does not
raise, does not report a partial result, nothing distinguishes it from a
library that genuinely only has 73 symbols. `try/catch { continue; }` (and
`continue`, not `break`, on a null result) fixes it - bound the loop
(`i < 5000` is generous and cheap; a miss is just a fast reflection
exception) and let it run to the end:

```csharp
for (int i = 0; i < 5000; i++)
{
    object sym = null;
    try { sym = symByIdx.Invoke(new object[] { lib, i }); }
    catch { continue; }             // NOT break - see above
    if (sym == null) continue;      // NOT break
    if (PropText(sym, "IsValid") != "True") continue;
    // ... use sym ...
}
```

A caller-facing enumeration tool built on this walk should report a `matched`
count separate from what it returns, and should NOT report `truncated: false`
just because the walk reached the loop bound - that field is only honest once
the `break`s are gone.

### A symbol's real identity: `Symbol.Properties`, not its name

A symbol's short name (`Q2`, `F2`, `Q_2L`) is an internal identifier, not a
description - guessing what it draws or means from the name or from the
IEC-designation letter convention (`Q` = switching device, `F` = protection
device, etc.) is unreliable: `IEC_symbol/Q2` is a two-pole **rotary switch**,
not a breaker, and `F2` is a **fuse**, not a circuit breaker, despite both
prefixes suggesting "protection". `Symbol.Properties` (a `SymbolPropertyList`,
reached the same ambiguous-overload way as `MDPart.Properties` above) carries
the real, multilanguage answer:

```csharp
object props = TryRead(sym, "Properties", null);
string desc     = SafeText(TryRead(props, "SYMB_DESC", null));      // "en_US@Power circuit breaker, two-pole..."
string funcDesc = SafeText(TryRead(props, "FUNC_DESC", null));      // what each pin/connection means
string category = SafeText(TryRead(props, "FUNC_CATEGORY", null));  // "en_US@Safety switch;..."
string group    = SafeText(TryRead(props, "FUNC_CATEGORY_REGION", null)); // "en_US@Protection device;..."
```

Values are `;`-joined `lang_code@text` pairs (`en_US@...;es_ES@...;...`) - split
on `;` and match the language you want rather than assuming English is first.
Confirm a symbol's identity this way before placing it on a real page, not by
pattern-matching its name.

## Practical notes

- Wrap per-part processing in try/catch and continue the loop — a single corrupt part must not abort a full DB scan; log the part number.
- Materialize `database.Parts` into a `List<MDPart>` first if you need a count for a progress bar.
- Document paths from the parts DB frequently contain PathMap variables (`$(MD_DOCUMENTS)\...`) — always resolve with `PathMap.SubstitutePath` before using as a filesystem path, and skip `http(s)://` URLs when expecting files.
- Report progress/results with `new BaseException(msg, MessageLevel.Message).FixMessage()` so runs are traceable in EPLAN's message list.
- For project data (pages, devices, functions): prefer **actions** from scripts (`selectionset`, `edit`, property actions — see actions-reference.md). When an action cannot do the job (e.g. creating installation spaces headless), reach the object model via runtime reflection on the loaded assemblies (see `e3d-installation-spaces.md`), not via `using Eplan.EplApi.DataModel;` — that reference fails to compile.
