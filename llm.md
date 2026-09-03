# LLM Operating Guide — EPLAN AI Automation Toolkit

This file tells you (the LLM) what this toolkit lets you **do** and **configure**.
It assumes you are connected through one or more of the MCP servers in this repo.

---

## 1. What you are connected to

There are up to **four MCP servers**, each a different capability:

| MCP server | Kind | What it lets you do |
|------------|------|---------------------|
| `eplan` (local) | Action server | **Control a running EPLAN Electric P8 instance** — open/close projects, export, import, reports, checks, renumber, parts DB, settings, run C# scripts, etc. |
| `eplan-rag` (remote) | Knowledge | **Look up the EPLAN P8 API** (2026 docs, actions/classes/properties/parameters) via **semantic search** (Vectorize + bge-base). |
| `eplan-wiki-2027` (remote) | Knowledge | **Look up the EPLAN P8 API** (2027 docs) via **keyword/full-text search** (SQLite FTS5 + bm25). Prefer this over `eplan-rag` when you already know or can guess the exact class/method/property name — measured head-to-head, FTS5 wins that case and semantic search wins only when the query shares no vocabulary with the docs at all. |
| `eecpro-rag` (remote) | Knowledge | **Look up the EPLAN EEC Pro 2026** documentation via semantic search. |

If `eplan-rag`/`eplan-wiki-2027` aren't connected, you can still query them over REST:
`POST https://rag2026.covaga.xyz/search` (semantic, 2026) or
`POST https://rag2027.covaga.xyz/search` (keyword, 2027), body `{"query": "...", "topK": 5}`.
Use one of these whenever you are unsure of an exact action name or parameter —
**do not guess EPLAN action parameters**.

---

## 2. The local `eplan` action server

It exposes **210 tools** (full tool-by-tool reference: [the project wiki](https://github.com/covagashi/eplan-rag-mcp/wiki)):

- **8 connection/utility tools**: `eplan_versions`, `eplan_servers`,
  `eplan_connect`, `eplan_status`, `eplan_ping`, `eplan_test`,
  `eplan_disconnect`, `eplan_list_extensions`.
- **198 EPLAN action tools** → `eplan_<action>` (e.g. `eplan_open_project`).
  Includes 5 discovery tools (`eplan_settings_list_children`,
  `eplan_list_schemes`, `eplan_list_report_templates`, `eplan_list_layers`,
  `eplan_list_enums`) that enumerate real EPLAN catalogs instead of guessing,
  4 live-DataModel tools (`eplan_live_query_functions`,
  `eplan_live_query_pages`, `eplan_live_set_function_text`,
  `eplan_live_set_connection_designations`) that read/edit the open project's
  object model via runtime reflection (see §4 below), 6 schematic-authoring
  tools on that same reflection scaffold (`eplan_live_symbol_catalog`,
  `eplan_live_create_page`, `eplan_live_place_symbol`,
  `eplan_live_connect_pins`, `eplan_live_read_page`,
  `eplan_live_remove_placement`) that CREATE a schematic rather than only
  reading one - every write returns the page read-back as proof and an undo
  handle, and writes refuse a project outside the scratch root unless
  `allow_real_project=True`, and 5 convention-profile tools
  (`eplan_profile_learn`, `eplan_profile_list`, `eplan_profile_get`,
  `eplan_profile_suggest`, `eplan_profile_forget`) that learn how a GO-BY
  project builds schematics - which symbol it uses for each device kind, its
  tag and page-naming shapes, its grid and spacing - so generated work matches
  the house standard. Learning accumulates rather than overwriting, so a
  profile sharpens with each project it sees; profiles are stored OUTSIDE the
  repo (EPLAN_MCP_PROFILES) because a client's conventions are customer data, application lifecycle
  control (`eplan_app_launch`, `eplan_app_shutdown`, `eplan_app_restart` —
  full exit/relaunch/reconnect/reopen cycles for unattended add-in
  deploy-test loops), scratch project fixtures
  (`eplan_scratch_project_create` / `_discard` / `_list` — disposable clones
  of a template project; deletion is confined to the scratch root), and
  `eplan_get_system_messages` (read EPLAN's system message tree — the same
  errors/warnings the user sees in the GUI's system messages dialog).
- **4 Asset Administration Shell tools** → `aas_<action>`
  (`aas_export_part`, `aas_export_project`, `aas_inspect_package`,
  `aas_import_parts`) for AAS/AASX digital-twin export and import.

The EPLAN version is auto-detected (newest installed). If the user wants a
specific version, call `eplan_versions` to list what is installed, then
`eplan_connect(version="2026")`. Decide the version BEFORE the first connect —
once one version's DLLs are loaded, switching requires restarting the server.

Every action runs inside a C# script in EPLAN's process under `QuietMode`
(no dialogs). It is silent, safe for unattended/batch use, and returns values
EPLAN wrote back to the calling context (e.g. `PROJECT`, `PAGES`).

Each tool already carries its own description and parameter schema (generated
from the Python docstring + type hints). **Read the tool's own description before
calling it** — this guide is the map, the tool schemas are the territory.

### Result shape

Tools return JSON. Actions typically return:

```json
{ "success": true, "parameters": { "PROJECT": "C:\\...\\Proj.elk" } }
```

`success: false` with a `message`/`error` means the EPLAN action itself failed —
read the message; it usually points at a bad parameter or a missing precondition
(e.g. no project open).

`success: true` from a directly-executed utility (`ExecuteScript`) only means
EPLAN accepted the call. Generated scripts are one-shot `[Start]`-only classes
run via `ExecuteScript` alone — they are deliberately **not** passed through
`RegisterScript`/`UnregisterScript` (that pair is for installing a script's
persistent `[DeclareAction]`/`[DeclareEventHandler]`/`[DeclareMenu]` hooks,
which these scripts don't have; registering them anyway just produced a
spurious "script does not contain attributes for loading" warning in
EPLAN's own UI and two wasted remote-API round-trips per call).

---

## 3. Standard workflow

1. **Check / connect first.** Call `eplan_status` (or `eplan_servers` →
   `eplan_connect`). Almost every action needs an open connection. Port and
   EPLAN version are auto-detected; pass `version` only if the user asks for a
   specific one. If `eplan_servers` returns `[]` the connection usually still
   works — that empty list is a known limitation (especially right after EPLAN
   starts), not a failure: `eplan_connect` falls back to the TCP ports
   EPLAN.exe actually listens on (via netstat), then the default 49152. To
   reach EPLAN on another machine pass `host` (port required then).
2. **Pick the project context.** Most actions take an optional `project_name`. If
   omitted, EPLAN uses the **currently selected/open** project. Use
   `eplan_get_current_project` to confirm what that is.
3. **Call `eplan_<action>`** with the parameters from the tool schema.
4. **Verify unknowns via the RAG** before constructing raw actions or custom
   scripts.
5. **Report results** from the returned JSON honestly (including `success: false`).

---

## 4. What you can DO (capability map)

All of these exist as `eplan_*` tools:

- **Projects:** open, close, get current, compress, synchronize, upgrade, set
  language, switch type, project management tasks.
- **Backup / restore:** projects and master data.
- **Export:** PDF (project/pages), DXF/DWG (project/pages, by scheme), graphics
  (PNG/TIF/…), PXF/EPJ, 3D.
- **Import:** PXF projects, DXF/DWG into pages or as macros, PDF comments, 3D.
- **Print:** project or pages.
- **Check / verify:** project, pages, parts (with verification schemes).
- **Generate:** connections, cables.
- **Reports / evaluations:** update reports, model views, copper unfolds,
  drilling views.
- **Search:** devices, texts, all properties, page data, project data.
- **Navigation / editing:** open page, go to device, open layout space, close
  pages, get selected pages, page/macro preview, navigate to EEC.
- **Renumber:** devices, pages, cables, terminals, connections.
- **Translate:** translate project, export missing translations, remove language.
- **Device lists, labels, graphical layers, macros.**
- **Settings & properties:** import/export settings, set settings, get/set
  project / page / object properties, user properties.
- **Parts:** export/import parts lists, part selection, data source, full parts
  management API export/import.
- **PLC:** bus data export/import via converters.
- **Workspace:** open/save/clean (needs the EPLAN GUI/mainframe).
- **Data exchange:** connections/functions/pages export for external editing,
  data-configuration import/export, potential/pipeline definitions, subprojects,
  master data operations.
- **Cabinet / 3D:** cabinet weight, segment filling, topology, pre-planning data,
  segment templates.
- **Production:** NC data, production wiring.
- **Ribbon & add-ons:** export/import ribbon bar, load API module, register/
  unregister add-on, and `execute_raw_action` for any action not wrapped.
- **Scripted advanced APIs (run as C#):** direct **parts database** queries
  (`parts_db_*`), **typed settings** get/set
  (`settings_get/set_string|bool|int|double`), **PathMap** variable substitution,
  and `execute_custom_script` to run arbitrary C# inside EPLAN.
- **Live DataModel (read/edit the open project via reflection):**
  `eplan_live_query_functions`, `eplan_live_query_pages` (read, with substring
  filter + result limit), `eplan_live_set_function_text` (write `FUNC_TEXT`,
  defaults to one function at a time, returns the previous value),
  `eplan_live_set_connection_designations` (write the indexed
  `FUNC_CONNECTIONDESIGNATION` property, re-reads after writing to confirm).
  These reach `Eplan.EplApi.DataModel`/`HEServices` types via
  `AppDomain.CurrentDomain.GetAssemblies()` + `Assembly.Load` fallback instead
  of a static `using`, because that `using` doesn't compile in EPLAN's script
  engine (CS0234) and, separately, the managed assembly names changed
  (`Eplan.EplApi.DataModelu` → `...DataModelNetu`) starting with EPLAN 2025/2027
  — a hardcoded old name throws `BadImageFormatException` on newer installs.

### Escape hatches

- `eplan_execute_raw_action("ActionName /PARAM:value ...")` — run any EPLAN
  action string directly (still wrapped in QuietMode). Use after confirming the
  syntax with the RAG.
- `eplan_execute_custom_script(<C# code>, timeout_seconds=30.0)` — run a full
  C# script with access to `Eplan.EplApi.*`. Write results to
  `{{RESULT_PATH}}` as JSON. Raise `timeout_seconds` for scripts that walk
  large collections (e.g. reflection over every function/page in a big
  project); the default is tuned for small scripts, not bulk enumeration.

---

## 5. What you can CONFIGURE

- **Target EPLAN version:** auto-detected (newest installed). Override per
  session with `eplan_connect(version="2026")`; list options with
  `eplan_versions`. Non-standard install path: set the `EPLAN_PLATFORM_ROOT`
  environment variable. Switching versions after DLLs are loaded requires a
  server restart.
- **Add a new action / tool:** implement a function in
  `api/actions/<module>.py`, export it in that package's
  `__init__.py` `__all__`, restart. It auto-registers as `eplan_<name>`. The
  docstring + type hints become the tool description and schema you will see.
- **EPLAN settings at runtime:** via `eplan_set_setting` /
  `eplan_set_project_setting` (action params `set`/`value`/`index`) or the
  typed `eplan_settings_set_*` scripted tools.
- **The MCP registration itself:** `claude mcp add eplan -- python .../server.py`.

---

## 6. Caveats & gotchas

- **Connect before acting.** Unconnected calls return a "Not connected" error.
- **`project_name` is optional** — omitting it uses the selected project. Pass the
  full `.elk` path to be explicit. Windows paths must be escaped (`\\`) or use `/`.
- **GUI-only actions** behave poorly headless/under QuietMode: `redraw_ged`
  (returns FALSE in QuietMode) and the `workspace` actions (need a mainframe).
- **Property actions on project/page** (`get/set_project_property`,
  `get/set_page_property`) act on the **current project / selected page(s)** and
  use `PropertyId`/`PropertyIndex`/`PropertyValue` — they do not take a project
  or page name.
- **`selectionset`** valid `TYPE` values are `PROJECT`, `PROJECTS`, `PAGES`,
  `LAYOUTSPACES` only.
- **Don't invent parameters.** When unsure, query the RAG (`rag2026.covaga.xyz`)
  for the authoritative action page.
- **Custom C# scripts can't `using Eplan.EplApi.DataModel;`** — that statement
  doesn't compile in EPLAN's script engine (CS0234). Reach DataModel/HEServices
  types via reflection instead (see the `live_*` tools in §4), and don't
  hardcode the managed assembly name — it's `...Netu`-suffixed on EPLAN
  2025/2027, not the pre-2025 name.

---

## 7. Safety — confirm before destructive actions

Treat these as outward/irreversible and **confirm with the user first** unless
explicitly authorized: deleting pages or device/parts lists, closing a project
with unsaved changes, renumbering (devices/pages/cables/terminals/connections),
restore/backup overwrites, settings changes, `set_*_property`, raw actions, and
custom C# scripts. Read-only actions (status, search, get current project,
exports to new files, parts DB queries) are safe to run as needed.
