# 3D — Installation spaces (layout spaces) via RemoteClient scripts

## What works

Creating an **installation space (layout space)** headless (no GUI dialog, no
user input) **works**, but **not** with a direct `using Eplan.EplApi.DataModel;`
reference.

| Approach | Result |
|---|---|
| `using Eplan.EplApi.DataModel;` + `new SelectionSet().GetCurrentProject(false)` | ❌ CS0234 — the EPLAN script engine compiles scripts against a **fixed assembly set** (`Base`, `ApplicationFramework`, `Gui`, `MasterData`, `IdentityClient` + System). Direct DataModel/HEServices `using` directives **fail to compile** (silently in the generated-script path, CS0234 in the log). |
| `ActionManager.Actions` enumeration | ❌ Hangs the script engine (no result within 60 s). Use `ActionManager.FindAction(name)` only. |
| `XCabCreateInstallationSpace` action | ⚠️ Interactive only: opens `XDeviceTagDlg` asking for the device tag; under QuietMode the dialog is suppressed and **nothing is created**. Good for letting a user pick a name, not for automation. |
| `selectionset /TYPE:LAYOUTSPACES` | ⚠️ Read-only, returns only the **selected** layout space. Fine for "what is the user looking at", not for enumerating spaces. |
| **Runtime reflection on the loaded DataModel/HEServices assemblies** | ✅ **This is the technique that works** — resolve types by scanning loaded assemblies, not `Assembly.Load` of a hardcoded name (see below; the hardcoded name breaks on 2027). |

## Why reflection works

The managed object-model assemblies are already loaded in the EPLAN process and
can be reached at runtime inside a script, even though the **compile-time**
`using` directives are rejected. Everything reachable through them can then be
driven via `System.Reflection` (which is in the default script reference set).

### Resolve the assembly by scanning, not by name (2027)

`Assembly.Load("Eplan.EplApi.DataModelu")` / `...HEServicesu")` work on 2025 but
**throw `BadImageFormatException` (0x8007000B, "an attempt was made to load a
program with an incorrect format") on EPLAN 2027**. On 2027 the managed
assemblies are **`Eplan.EplApi.DataModelNetu`** and
**`Eplan.EplApi.HEServicesNetu`**; the un-suffixed names still exist in the
process but are the mixed-mode **native** twins, so `Assembly.Load` silently
picks the wrong one. (Same 'u'-suffix confusion as `Eplan.EplApi.Gui`, where the
DLL name ends in 'u' but the namespace does not.)

Version-proof replacement — scan what is already loaded and fall back to the
`*Netu` names first:

```csharp
static Assembly[] _asms;
static Type FindType(string fullName)
{
    if (_asms == null) _asms = AppDomain.CurrentDomain.GetAssemblies();
    foreach (Assembly a in _asms)
    {
        try { Type t = a.GetType(fullName); if (t != null) return t; }
        catch { }
    }
    string[] candidates = new string[] {
        "Eplan.EplApi.DataModelNetu", "Eplan.EplApi.HEServicesNetu",
        "Eplan.EplApi.DataModelu",    "Eplan.EplApi.HEServicesu" };
    foreach (string c in candidates)
    {
        try
        {
            Type t = Assembly.Load(c).GetType(fullName);
            if (t != null) { _asms = AppDomain.CurrentDomain.GetAssemblies(); return t; }
        }
        catch { }
    }
    throw new Exception("Could not resolve type " + fullName);
}
```

Verified on EPLAN 2027 (.NET 8): `LockingStep` resolves out of
`Eplan.EplApi.DataModelNetu`, and the recipe below then enumerates a live
project (56 pages / 4181 functions on the test project) and writes to it.

## Proven recipe (environment: EPLAN 2025.0.x, RemoteClient on localhost:49152)

```csharp
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.Base;
using Eplan.EplApi.Scripting;

// reflectively reach the object model with FindType() from the section above.
// Do NOT Assembly.Load a hardcoded assembly name - it breaks on 2027.

// 1) Locking context is REQUIRED. Without it every project access throws
//    NoLockingStepException: S063110 "No se ha generado ningún objeto de la
//    clase 'LockingStep'".
var lsType = FindType("Eplan.EplApi.DataModel.LockingStep");
object lockStep = Activator.CreateInstance(lsType);   // ctor() exists, Dispose() exists
try
{
    // 2) Current project WITHOUT ProjectManager (which also needs the lock)
    var ssType = FindType("Eplan.EplApi.HEServices.SelectionSet");
    object ss = Activator.CreateInstance(ssType);
    var getCur = ssType.GetMethod("GetCurrentProject", new Type[] { typeof(bool) });
    object project = getCur.Invoke(ss, new object[] { false });
    // project.ProjectFullName -> read property via reflection

    // 3) Enumerate existing spaces  (project.InstallationSpaces)
    var spaces = (System.Collections.IEnumerable)
        project.GetType().GetProperty("InstallationSpaces").GetValue(project, null);
    foreach (var s in spaces)
        Console.WriteLine(s.GetType().GetProperty("VisibleName").GetValue(s, null));

    // 4) Create the space  (static InstallationSpace.Create(Project, String, List))
    var isType = FindType("Eplan.EplApi.DataModel.E3D.InstallationSpace");
    var create = isType.GetMethods(BindingFlags.Public | BindingFlags.Static)
        .First(m => m.Name == "Create" && m.GetParameters().Length == 3);
    object space = create.Invoke(null, new object[] { project, "MySpaceName", null });
    // space.VisibleName -> read back; it's a live object, persists on save
}
finally
{
    lsType.GetMethod("Dispose").Invoke(lockStep, null);
}
```

Key reflection member names that are present and verified via runtime
introspection (2025.0.3):

- `Eplan.EplApi.DataModel.ProjectManager` → props `CurrentProject`, `OpenProjects`
  (both need the `LockingStep`; with it, works).
- `Eplan.EplApi.DataModel.Project` → `InstallationSpaces`, `ProjectFullName`,
  `ProjectName`, `Pages`, `Properties`, `Settings`.
- `Eplan.EplApi.DataModel.E3D.InstallationSpace` → static
  `Create(Project, String, List<...>)` **and** instance
  `Create(Project, String)` / `Create(Project)`; `VisibleName`, `Name`,
  `Properties`. A static 3-arg `Create` returns the live installation space.
- `Eplan.EplApi.HEServices.SelectionSet` → `GetCurrentProject(bool)`,
  `OpenedInstallationSpaces`, `SelectedProjects`, `Selection`.

## Pitfalls

- Do **not** put `using Eplan.EplApi.DataModel;` / `...DataModel.E3D;` /
  `...HEServices;` in the script source. That is the exact CS0234 trap, and a
  script that fails to compile never runs — it never reaches the line that
  writes a result file. If the host driving the script works by polling for that file, a compile error looks
  **identical to a hang**: you get a plain timeout, not a compiler message, on
  the caller side. **Before spending time on RAM/size/locking theories for a
  timed-out script, check EPLAN's own message tree first** (`SysMessagesCollection`,
  see pitfalls.md #4) for a `CS####`-prefixed entry naming the generated `.cs`
  file — that is where the real error is sitting the whole time. Confirmed
  live (2026-09-03): the same script timed out identically against a
  400-installation-space project and a 4-page one, with free RAM ranging
  ~2–3 GB — none of that mattered, the cause was the `using` directive both
  times. Collection size (hundreds of pages/spaces) is not itself a source of
  slowness once a script actually compiles — enumeration is fast.
- The EPLAN script engine also rejects modern C# syntax: index initializers
  (`new Dictionary<...> { ["k"] = v }`) / dictionary-initializer braces fail
  with CS1525 even where the same code compiles in VS. **Write `MethodInfo`/
  dictionary member entries as separate `x["k"] = v;` statements.**
- **Escape Windows paths before embedding them in the generated C#**: a raw
  `"D:\x\A-B..."` collapses `\d`, `\A`, `\1` into invalid escape sequences
  (CS1009). Emit `\\` for every backslash. If your runner has an escaping helper, use it;
  if not, write one, because this bites every generated script.
- Guard the `LockingStep` lifetime: construct before touching the project,
  `Dispose()` in `finally`. A project opened in the GUI and also accessed
  headless through this path coexists fine on EPLAN 2025.
- Wrap each reflective call in try/catch and flatten `InnerException` chains —
  EPLAN wraps failures in `TargetInvocationException`; the real message
  (`NoLockingStepException`) sits two levels down.
- Introspecting `Type.GetType(...)` with a full-name string and `Enum.GetNames`
  on nested EPLAN enum types can hang the script engine (timeout); read the
  enum type from the method's `ParameterType` and build the value with
  `Enum.ToObject(type, 0)` instead.
- Once the space exists, **opening it** in the GED still requires the standard
  GUI/action path (`edit` is treated as a page name; `selectionset` only reports
  the current selection).

## Inserting a 3D (window) macro into a space — headless

The interactive `XGedStartInteractionAction /Name:XMIaInsertMacro` interaction
(3 vertical steps, needs a human click to place) is **not** the only way. The
object model exposes `Eplan.EplApi.HEServices.Insert3D`, reachable by the same
runtime-reflection technique, with a **filename overload** that inserts a macro
from disk with no dialogs:

```csharp
// Eplan.EplApi.HEServices.Insert3D  (via FindType, see above)
// verified method:  StorableObject[] WindowMacro(
//   String strFileName, Int32 nVariant,
//   Placement3D oParent, Matrix3D oMatrix,
//   MoveKind nMoveCondition, NumerationMode nNumerationMode)

object insert = Activator.CreateInstance(insertType);          // new Insert3D()
// resolve the 6-param overload whose 1st param is String
MethodInfo wanted = insertType.GetMethods(...)
    .First(m => m.Name == "WindowMacro" && m.GetParameters().Length == 6
             && m.GetParameters()[0].ParameterType == typeof(string));

// args in order:
//   strFileName      = full path of the .ema (must be `\\`-escaped above)
//   nVariant         = 0
//   oParent          = the InstallationSpace (it IS-A E3D.Placement3D)
//   oMatrix          = Activator.CreateInstance(System.Windows.Media.Media3D.Matrix3D) // identity
//   nMoveCondition   = Enum.ToObject(param.ParameterType, 0)   // Insert3D+MoveKind
//   nNumerationMode  = Enum.ToObject(param.ParameterType, 0)   // MasterData.WindowMacro+Enums+NumerationMode

object placed = wanted.Invoke(insert, args);   // returns the placed objects
```

Other overloads take a `WindowMacro` (MasterData) object or `PointMate`/`Mate`
pairs for snapping; the **filename overload** is the only one that needs no
pre-loaded macro object.

### Full headless recipe (verified end-to-end 2026-08 on EPLAN 2025.0.3)

1. `LockingStep` (reflection) + `SelectionSet.GetCurrentProject(false)`.
2. Find the target space: iterate `project.InstallationSpaces`, match
   `VisibleName`.
3. `InstallationSpace.Create(project, name)` if missing.
4. `Insert3D.WindowMacro(fileName, 0, space, identityMatrix, 0-enum, 0-enum)`.
5. Done — the macro is placed (returns the placed `Placement3D[]`; 1 object for
   a single-macro space), no user clicks, no dialogs.