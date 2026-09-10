# Core EPLAN Classes

## PathMap — path variables

```csharp
using Eplan.EplApi.Base;

string projectPath = PathMap.SubstitutePath("$(PROJECTPATH)"); // project folder
string projectName = PathMap.SubstitutePath("$(PROJECTNAME)"); // name only
string fullProject = PathMap.SubstitutePath("$(P)");           // full .elk path
```

| Variable | Meaning |
|---|---|
| `$(PROJECTPATH)` | Project directory |
| `$(PROJECTNAME)` | Project name |
| `$(P)` | Full project path (.elk) |
| `$(TMP)` | Temp directory |
| `$(MD_SCRIPTS)` | Scripts master-data dir |
| `$(MD_DOCUMENTS)` | Documents master-data dir (common in part document links) |

Resolve any raw string that may contain variables:
```csharp
private string ResolvePath(string rawPath)
{
    if (string.IsNullOrEmpty(rawPath) || !rawPath.Contains("$(")) return rawPath;
    try { return PathMap.SubstitutePath(rawPath); } catch { return rawPath; }
}
```

## Progress — progress bars

```csharp
Progress oProgress = new Progress("SimpleProgress");   // or "EnhancedProgress"
oProgress.SetAllowCancel(true);
oProgress.SetAskOnCancel(true);
oProgress.SetNeededSteps(3);            // or BeginPart(100, "")
oProgress.SetTitle("My operation");
oProgress.ShowImmediately();

if (!oProgress.Canceled())
{
    oProgress.SetActionText("Step 1");
    oProgress.Step(1);
}
oProgress.EndPart(true);                // ALWAYS call, use finally
```

## Decider — EPLAN-native dialogs

```csharp
new Decider().Decide(
    EnumDecisionType.eOkDecision,
    "Message", "Title",
    EnumDecisionReturn.eOK, EnumDecisionReturn.eOK);
```
For list selection there is `ListSelectDecisionContext`. `System.Windows.Forms.MessageBox` is also fine in interactive scripts.

## Settings — read/write EPLAN settings

```csharp
var oSettings = new Eplan.EplApi.Base.Settings();

oSettings.SetStringSetting("USER.TrDMProject.UserData.Identification", "TEST", 0);
oSettings.SetBoolSetting("USER.EnfMVC.ContextMenuSetting.ShowExtended", true, 0);
oSettings.SetNumericSetting("USER.SYSTEM.GUI.LAST_PROJECTS_COUNT", 11, 0);

string s = oSettings.GetStringSetting("USER.TrDMProject.UserData.Longname", 0);
bool b   = oSettings.GetBoolSetting("USER.XUserSettingsGui.UseLoginName", 0);
int n    = oSettings.GetNumericSetting("USER.MF.PREVIEW.MINCOLWIDTH", 0);
```
Setting paths are hierarchical (`USER.*`, `STATION.*`, `PROJECT.*`). Find exact paths via the RAG or by exporting settings (`XSettingsExport`/`XSettingsImport`).

## MultiLangString — multilanguage texts

```csharp
MultiLangString name = new MultiLangString();
name.AddString(ISOCode.Language.L_en_US, "My Tab");
name.AddString(ISOCode.Language.L_de_DE, "Mein Tab");
```

Raw multilang values from properties serialize like `en_US@Text;es_ES@Texto` (sometimes wrapped in `{{ }}`). Parsing pattern (prefer English, fall back to first):

```csharp
private string ParseMultiLang(string rawText)
{
    if (string.IsNullOrEmpty(rawText)) return "";
    string fallback = "", english = "";
    foreach (string part in rawText.Split(new[] { ';' }, StringSplitOptions.RemoveEmptyEntries))
    {
        string val = part;
        int atIdx = part.IndexOf('@');
        if (atIdx >= 0)
        {
            string lang = part.Substring(0, atIdx).ToLower();
            val = part.Substring(atIdx + 1).Replace("{{", "").Replace("}}", "").Trim();
            if (string.IsNullOrEmpty(fallback)) fallback = val;
            if (lang.StartsWith("en")) english = val;
        }
        else
        {
            val = part.Replace("{{", "").Replace("}}", "").Trim();
            if (string.IsNullOrEmpty(fallback)) fallback = val;
        }
    }
    string result = !string.IsNullOrEmpty(english) ? english : fallback;
    return string.IsNullOrEmpty(result) ? rawText.Replace("{{", "").Replace("}}", "").Trim() : result;
}
```

## Ribbon and context menus (EPLAN 2022+)

Classic menu bar is gone since 2022 — use `Eplan.EplApi.Gui.RibbonBar` inside `[DeclareRegister]`:

```csharp
using Eplan.EplApi.Gui;

[DeclareRegister]
public void Register()
{
    RibbonBar ribbonBar = new RibbonBar();
    RibbonTab tab = ribbonBar.GetTab(TAB_NAME, true) ?? ribbonBar.AddTab(TAB_NAME);

    RibbonCommandGroup group = tab.AddCommandGroup("My group");
    group.AddCommand("My action", "MyActionName", new RibbonIcon(CommandIcon.Accumulator));

    // Custom icon from SVG + multilang labels/tooltips:
    RibbonIcon icon = ribbonBar.AddIcon(@"C:\icons\my.svg");
    group.AddCommand(commandText, "MyActionName", tooltip, description, icon);
}

[DeclareUnregister]
public void UnRegister()
{
    RibbonTab tab = new RibbonBar().GetTab(TAB_NAME, true);
    if (tab != null) tab.Remove();
}
```
`TAB_NAME` is a `MultiLangString`.

### Context menus

A right-click entry needs `[DeclareAction]` **plus** `[DeclareMenu]`, and the script must be
installed with `RegisterScript` — a `[Start]` method registers nothing.

```csharp
[DeclareAction("MyAction")]
public void OnClick() { MessageBox.Show("it works"); }

[DeclareMenu]
public void BuildMenu()
{
    // Fully qualify: ContextMenu is ambiguous with System.Windows.Forms.ContextMenu (CS0104).
    var loc  = new Eplan.EplApi.Gui.ContextMenuLocation("XPamDtTabSheetDialog", "1042");
    var menu = new Eplan.EplApi.Gui.ContextMenu();
    menu.AddMenuItem(loc, "it works", "MyAction", /*sepBefore*/ true, /*sepAfter*/ false);
}
```

**Finding the two location strings** — undocumented, and the only hard part. Turn on:

```
USER.EnfMVC.ContextMenuSetting.ShowIdentifier = true   // labels each menu "<DialogName>.<CtxId>"
USER.EnfMVC.ContextMenuSetting.ShowExtended   = true   // makes the label clickable -> copyable dialog
```

Right-click where the entry should go and EPLAN appends e.g. `XPamDtTabSheetDialog.1042`
to the menu itself. The id is simply the **Win32 control id** of the control under the
cursor, so it can also be read from a focus-chain dump.

Gotchas:
- `AddMenuItem` does **not** validate the location: a bogus dialog/id pair still returns
  `true`. The bool means "this item was new", not "the target exists" — only a visual
  check proves placement.
- `UnregisterScript` stops the action but leaves already-inserted items in place; use
  `RemoveMenuItem` or restart EPLAN.
- The script host references very little. `System`, `System.Windows.Forms`,
  `Eplan.EplApi.Scripting`, `ApplicationFramework`, `Base` and `Gui` are pre-injected
  (repeating them is CS0105), but `DataModel` and `HEServices` are **not referenced at
  all** (CS0234) — reach them by reflection.

**The action receives no context.** `ActionCallingContext.GetContextParameter()` is `null`,
so nothing tells the action which row or cell was clicked. EPLAN's grids are custom-drawn
`BCGPGridCtrl` with no window text, so `WM_GETTEXT` reads nothing either. What does work is
MSAA: `AccessibleObjectFromWindow(GetFocus(), OBJID_CLIENT, IID_IAccessible)` yields a
`GridControl` with one child per row (`accName` = "Row 5", `accValue` = the row's cells
joined with ", "). `accFocus` throws on that grid — scan children for the
`STATE_SYSTEM_FOCUSED` (0x4) bit instead. Two interop traps: the `out object` parameter
needs `[MarshalAs(UnmanagedType.IUnknown)]` (else `InvalidOleVariantTypeException`), and
late-bound IDispatch calls want `accName`, not `get_accName` (else COMException).

This reads the screen, not the data model: it reflects uncommitted grid edits, but it
depends on the control's accessibility and on column order. Prefer a self-describing
property grid (rows labelled `Name <id>`) over a positional one where possible.

## System messages

```csharp
// Emit into EPLAN's system message list (visible in the messages dialog):
new BaseException("Something happened", MessageLevel.Message).FixMessage();
new BaseException("Warning text", MessageLevel.Warning).FixMessage();
new BaseException("Error text", MessageLevel.Error).FixMessage();

// Show the dialog:
new CommandLineInterpreter().Execute("SystemErrDialog");
```

### Reading system messages incrementally (bookmark pattern)
```csharp
private static int lastBookmarkID = 0;

[DeclareEventHandler("onActionEnd.String.*")]
public void OnActionEnd(IEventParameter iEventParameter)
{
    SysMessagesCollection colSysMsg = new SysMessagesCollection(lastBookmarkID, MessageLevel.Error);
    if (colSysMsg.Count > 0)
    {
        BaseException last = colSysMsg.Cast<BaseException>().LastOrDefault();
        // ...process...
        lastBookmarkID = colSysMsg.BookmarkIDEnd;   // advance bookmark, avoids reprocessing
    }
}
```

### Bound the slice at both ends when you're capturing one operation

The two-arg constructor is **open at the top**: it returns everything from the
start bookmark onward, so anything EPLAN emits after your operation — including
from a later action in the same session — lands in it. If you want the messages
belonging to one call, take a second bookmark afterwards and use the three-arg
form.

```csharp
int start = 0, end = 0;
using (var m = new BaseException("start", MessageLevel.Message)) { start = m.GetBookmarkID(); }

// ...run the one thing you want messages for...

using (var m = new BaseException("end", MessageLevel.Message)) { end = m.GetBookmarkID(); }

var col = new SysMessagesCollection(start, end, MessageLevel.Message);
int total = col.Count;          // the real total, so truncation can be reported honestly
```

Filter your own marker messages out of the results, or they show up as content.

Two gotchas on this collection:

- **`Trace` and `Assert` are never added to it**, so reading at `MessageLevel.Trace`
  is not "wider" in any useful sense — it bounds the read, not what EPLAN stored.
- **`MessageLevel`'s numeric order is not a severity order**:
  `Trace=0, Message=1, Warning=2, Assert=3, Error=4, FatalError=5`. Sorting by
  enum value puts `Assert` — documented as the lowest level of error and not
  shown in the GUI — above `Warning`. Rank by an explicit list, not by value.

**For an action's own failure, prefer `acc.GetException()`** over bookmarking the
global tree — see *Executing Actions → how to find out WHY an action failed*. The
bookmark pattern above is for watching EPLAN generally, e.g. from an event handler.
