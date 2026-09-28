# EPLAN MCP Server

Remote control of **EPLAN Electric P8** from an LLM (e.g. Claude) via MCP (Model Context Protocol).

The server connects to a running EPLAN instance through the EPLAN Remote Client API
(pythonnet / CLR) and exposes **199 MCP tools** in the default `full` mode: 8 connection/utility
tools, **187 `eplan_*` action tools** (183 typed wrappers plus the 4
action-catalog/dispatch tools), and **4 Asset Administration Shell tools**
(`aas_*`) for AAS/AASX digital-twin export and import.

The EPLAN version is **auto-detected** (newest installed under
`C:\Program Files\EPLAN\Platform`); the LLM can override it per session via
`eplan_connect(version=...)`.

Every action is wrapped in a dynamically generated C# script executed **inside
EPLAN's process** under `QuietMode` (`QuietModes.ShowNoDialogs`). Completely
silent, prevents blocking dialogs, and reads return values back from the
`ActionCallingContext` — safe for batch / headless automation.

---

## Architecture

```
LLM (Claude)  -->  MCP Protocol  -->  FastMCP Server (server.py)
                                           |
                                           v
                                  C# Script Generator
                                 (QuietModeStep wrapper)
                                           |
                                           v
                                 EPLAN Process (P8)
```

How execution works internally (`eplan_connection.py::execute_action(action, quiet_mode=True)`):

1. Parse the action name and parameters.
2. Generate a `.cs` script that runs the action inside
   `using (var qm = new QuietModeStep(QuietModes.ShowNoDialogs)) { ... }`.
3. `ExecuteScript` → read a JSON result file. Deliberately NOT `RegisterScript`
   first: registration installs a script's *persistent* hooks
   (`[DeclareAction]` / `[DeclareEventHandler]` / `[DeclareMenu]`), and a
   generated wrapper has only a `[Start]` method, which `ExecuteScript` compiles
   and runs by itself. Registering one achieved nothing, made EPLAN log "The
   script does not contain attributes for loading." once per call, and cost two
   extra remote round-trips (median 0.39s → 0.22s without it).
4. Return `{"success": bool, "parameters": {...}}`, where `parameters` are the
   values EPLAN wrote back into the `ActionCallingContext` (e.g. `PROJECT`, `PAGES`).

The script-management utilities themselves (`RegisterScript`, `ExecuteScript`,
`UnregisterScript`) always run directly to avoid infinite recursion.

---

## Directory Structure

```
eplan-p8-mcp-server/
├── mcp_server/
│   ├── api/
│   │   └── actions/              # QuietMode python action wrappers
│   │       ├── _base.py          # QuietManagerWrapper, _build_action
│   │       ├── scripted.py       # advanced APIs via C# (parts DB, typed settings, PathMap)
│   │       ├── __init__.py       # re-exports every action and defines __all__
│   │       └── *.py              # one module per action category
│   ├── scripts/
│   │   ├── generated/            # temporary C# scripts (auto-created, auto-deleted)
│   │   └── results/              # temporary JSON results (auto-created, auto-deleted)
│   ├── server.py                 # MCP server: connection tools + dynamic action registration
│   ├── eplan_connection.py       # connection manager + QuietMode wrapper + version auto-detection
│   ├── requirements.txt
│   └── README.md                 # This file
└── tools/
    ├── validate_actions.py       # cross-check wrappers against the 2027 EPLAN wiki
    └── action_validation_report.md
```

> Note: both `eplan_connection.py` and `api/actions/scripted.py` write their
> temporary scripts/results under the single `scripts/generated` and
> `scripts/results` folders.

---

## Requirements

- **EPLAN Electric P8** installed (2024, 2025, 2026, or 2027)
- **Python 3.10+** (64-bit, to match EPLAN's process)
- Dependencies: `pip install -r requirements.txt` (`pythonnet`, `mcp`,
  `basyx-python-sdk` for the `aas_*` tools)

---

## Installation for Claude Code

### 1. Install dependencies

```bash
cd eplan-p8-mcp-server\mcp_server
pip install -r requirements.txt
```

### 2. Register the MCP server

```bash
claude mcp add eplan -- python YOUR_PATH\eplan-p8-mcp-server\mcp_server\server.py
claude mcp list   # "eplan" should appear
```

Or add it manually to `%USERPROFILE%\.claude\settings.json`:

```json
{
  "mcpServers": {
    "eplan": {
      "command": "python",
      "args": ["YOUR_PATH\\eplan-p8-mcp-server\\mcp_server\\server.py"]
    }
  }
}
```

By default the server publishes every tool. If the ~49,000 tokens of tool
definitions per request matter to you, set `EPLAN_MCP_MODE=discovery` — see
[Discovery mode](#discovery-mode-eplan_mcp_mode).

### 3. Connect

Start EPLAN, open Claude Code, and say `connect to eplan`.

---

## Remote topology (EPLAN on another machine)

The MCP server **must run on the machine where EPLAN is installed**: it loads
EPLAN's DLLs locally and exchanges generated C# script files and JSON result
files with EPLAN via local paths (`scripts/generated/`, `scripts/results/`).
You cannot run the server on machine A against an EPLAN on machine B.

If you work on a different machine than EPLAN, run the server there and
connect over HTTP:

**On the EPLAN machine** (Python 3.10+, this repo, `pip install -r requirements.txt`):

```powershell
$env:MCP_TRANSPORT = "http"     # default transport is stdio
$env:MCP_HOST      = "127.0.0.1" # bind address (default)
$env:MCP_PORT      = "8321"      # default
python YOUR_PATH\eplan-p8-mcp-server\mcp_server\server.py
```

**On your machine**, tunnel the port (the server has no authentication — do
not expose it beyond localhost/a trusted network) and register it:

```bash
ssh -L 8321:localhost:8321 user@eplan-host   # keep open
claude mcp add --transport http eplan http://localhost:8321/mcp
```

Everything else (remoting setting in EPLAN, `connect to eplan`, all 199
tools) works exactly as in the local setup, because from the server's point
of view EPLAN *is* local.

---

## Schema slimming (always on)

Every tool definition is trimmed before it reaches the client. Pydantic
auto-generates a `"title"` for each schema node (`export_file` becomes
`"Export File"`) and a `"default": null` for each optional argument; neither
tells a model anything the key name does not, and both are sent on every
request. `strip_schema_boilerplate()` removes them after registration.

| | tools | chars | ~tokens |
|---|---:|---:|---:|
| before | 199 | 197,369 | 49,342 |
| after | 199 | 166,101 | 41,525 |

31,268 characters, ~15.8%, with no loss of meaning — informative defaults
(`0`, `false`, `""`) are kept. It is safe because `Tool.parameters` is only
serialised out to the client; validation and dispatch go through
`Tool.fn_metadata`. A test asserts behaviour is byte-identical with and without
the strip.

Set `EPLAN_MCP_KEEP_SCHEMA_TITLES=1` to disable it.

Note that Claude Code already defers tool schemas — it shows the model a name
list and loads a schema on demand — so there the practical cost of this server is
around 1,800 tokens, not 49,000. The strip still helps there, because it shrinks
each schema that gets loaded. See `../TOKEN_COST.md` for the full measurement.

## The action trace (`EPLAN_MCP_LOG_DIR`)

Every executed action appends one JSON line to `logs/actions.jsonl`: timestamp,
action string, duration, success, and whichever of `executor` / `error` /
`errorType` / `eplanMessages` / `message` the result carried. It is the only
durable record of what the server actually did, and it survives the
conversation — which is what makes it possible to audit behaviour after the
fact rather than from memory.

Set `EPLAN_MCP_LOG_DIR` to write it elsewhere. The test suite sets it to a
`tmp_path` via an autouse fixture, and needs to: before that existed, running
`pytest` appended to the real trace. A census on 2026-09-03 found **871 of its
1,463 entries were test fixtures** from 59 separate runs, scattered through the
file rather than sitting at the head — so any statistic drawn from it was
partly measuring the test suite.

Two cautions if you use the trace as evidence:

- **`LOGGED_RESULT_KEYS` in `eplan_connection.py` is a filter.** A diagnostic
  field added to an action result but not to that tuple never reaches the
  trace, and is therefore invisible to any later audit. There is a test that
  fails when the tuple changes, so the omission gets noticed.
- **The trace records real paths**, including project paths on network shares.
  `logs/` is gitignored, and the file should be treated as potentially
  containing customer data — do not attach it to a public issue.

## JSON writer switch (`EPLAN_MCP_JSON_MODE`)

Generated action wrappers and scripted tools need to write their results as JSON.
The default `auto` mode uses a dependency-free C# writer with EPLAN 2.9 and
keeps `Newtonsoft.Json` with newer installations. EPLAN 2.9's script compiler
does not reference Newtonsoft, so scripts using it do not compile even if the DLL
is loaded in EPLAN. Set `EPLAN_MCP_JSON_MODE=portable` to force the portable
writer on another version, or `EPLAN_MCP_JSON_MODE=newtonsoft` to force the
previous behavior. Set the variable before starting the MCP server.

On EPLAN 2.9, use `eplan_connect(version="2.9")` (or first check
`eplan_versions`). If several versions are installed, automatic connection
still selects the newest. The switch only changes result serialization; action
names and API classes must still exist in the connected EPLAN version. The
`discovery` registration mode works on 2.9 because its tool search uses the
MCP's Python registry. `eplan_ribbon_catalog` reports that the pre-ribbon
2.9 UI is unsupported; `eplan_action_catalog` marks its 2027 GUI labels
as version-specific and `eplan_action_describe` can check an action live.

## Escape hatch (`EPLAN_MCP_LEGACY_CLI`)

`EPLAN_MCP_LEGACY_CLI=1` makes the generated C# fall back to the original
`CommandLineInterpreter`-only template — no `ActionManager.FindAction`, no
message-tree capture — as a known-good path in case the enhanced template ever
fails to compile on an EPLAN version it has not been tried on. It has existed
since the capture landed but was documented nowhere; recording it here so it is
discoverable.

Know what it costs before setting it: the legacy template does not emit
`using Eplan.EplApi.Base;`, so with the flag on there is **no `eplanMessages`
at all** and its `catch` records only `ex.Message` with no exception chain and
no `errorType`. Since the message capture is the half that demonstrably
delivers EPLAN's own error text, the flag turns diagnostics off rather than
degrading them. Use it to get unstuck on a new EPLAN version, not as a
steady state.

## Discovery mode (`EPLAN_MCP_MODE`)

Every MCP request carries the **whole tool list**. With one MCP tool per
wrapper that is a fixed, unavoidable tax on every single turn, paid before the
model has done anything:

| Mode | Tools published | Definition characters | Approx. tokens |
|------|-----------------|-----------------------|----------------|
| `full` (default) | 199 | 197,369 | ~49,300 |
| `discovery` | 13 | 14,275 | ~3,600 |

**92.8% smaller.** (Snapshot measurement - the full-mode figure grows with
every new wrapper, the discovery figure does not. Measured by summing
`len(name) + len(description) + len(json.dumps(input_schema))` over the tools
the server actually registers. The same measurement at the 194-tool state that
prompted this feature was 179,881 characters against 14,275, i.e. 92.1%
saved - the ratio is stable because the hidden tier is what grows.)

### What `discovery` publishes

* the connection/session core - `eplan_status`, `eplan_versions`,
  `eplan_servers`, `eplan_connect`, `eplan_disconnect`, `eplan_ping`
* the action-catalog tier, which is already a discovery mechanism for the
  ~1150 raw EPLAN actions - `eplan_action_catalog`, `eplan_action_describe`,
  `eplan_action_run`, `eplan_ribbon_catalog`
* three meta-tools that reach everything else:

| Meta-tool | Purpose |
|-----------|---------|
| `eplan_tools_search(query, limit)` | Find hidden tools by name or by words in their documentation. Returns name + one-line summary + **parameter names only** (never a full schema - that would rebuild the problem). Always reports `total_matches`, the true count, even when the list is truncated. With no `query` it returns a grouped overview by source module. |
| `eplan_tools_describe(names)` | Full signature, full docstring and per-parameter details for one name or a list of them. An unknown name comes back with near-matches instead of a bare error. |
| `eplan_tools_call(name, arguments)` | Invoke a hidden tool. The argument keys are validated against the function's real signature first, so a typo is refused with the list of valid parameter names rather than silently dropped. Returns exactly what the tool would have returned directly. |

Everything else - the ~180 typed action wrappers, the `aas_*` tools,
`eplan_test`, `eplan_list_extensions` and any `EPLAN_MCP_EXTENSIONS` tools -
stays fully usable, it just stops costing tokens on every turn. Extension packs
become *searchable* rather than separately published, so a large private
extension pack is free in discovery mode.

### Enabling it

`full` is the default; set the env var only if you want the smaller surface.

```json
{
  "mcpServers": {
    "eplan": {
      "command": "python",
      "args": ["YOUR_PATH\\eplan-p8-mcp-server\\mcp_server\\server.py"],
      "env": { "EPLAN_MCP_MODE": "discovery" }
    }
  }
}
```

```bash
claude mcp add eplan -e EPLAN_MCP_MODE=discovery -- python YOUR_PATH\eplan-p8-mcp-server\mcp_server\server.py
```

An unrecognised value warns on stderr and falls back to `full`; it never stops
the server from starting.

### The tradeoff

Discovery mode buys the token saving with **one extra round-trip**: the model
must call `eplan_tools_search` (and usually `eplan_tools_describe`) before it
can call a wrapper, instead of seeing the tool up front. That is one or two
cheap calls per new task, against ~49,000 tokens on every request. It also
means a tool the model never looks for is a tool it never learns exists, so
`full` remains the right choice for short sessions, for clients with generous
context, or when you want the model to browse the surface unprompted.

The implementation lives in `mcp_server/tool_registry.py`. The index is built
by introspecting the *same* functions `server.py` registers (`inspect.signature`
+ `inspect.getdoc`, resolved late through `(module, attribute)`), so it is never
hand-maintained and cannot drift from the real tool set. It imports neither
EPLAN nor pythonnet: `eplan_tools_search` works with EPLAN closed.

---

## Tools

### 1. Connection & utility tools

| Tool | Description |
|------|-------------|
| `eplan_versions` | List installed EPLAN versions (from disk, loads no DLLs). |
| `eplan_servers` | Detect active EPLAN instances on the machine. |
| `eplan_connect` | Connect to EPLAN. Optional `host` (remote machines, `"host:port"` accepted), `port` (auto-detected on localhost) and `version` (auto = newest installed). |
| `eplan_status` | Current connection details, including the targeted version. |
| `eplan_ping` | Check the connected instance is responding. |
| `eplan_test` | Show a MessageBox inside EPLAN to verify end-to-end communication. |
| `eplan_disconnect` | Close the active connection. |

### 2. Action tools

Every EPLAN action is exposed as `eplan_<action>`. Each tool's description and
input schema are generated from the underlying Python function's docstring and
type hints, so the connected LLM discovers what is available and how to call it
automatically.

Action categories:

| Category | Examples |
|----------|----------|
| Project | `open_project`, `close_project`, `get_current_project`, `compress_project`, `synchronize_project` |
| Backup / restore | `backup_project`, `backup_masterdata`, `restore_project`, `restore_masterdata` |
| Export | `export_pdf_project`, `export_pdf_pages`, `export_dxf_*`, `export_dwg_*`, `export_graphics_*`, `export_pxf_project`, `export_3d` |
| Import | `import_pxf_project`, `import_dwg_page`, `import_dxf_page`, `import_dxfdwg_files`, `import_pdf_comments`, `import_3d` |
| Print | `print_project`, `print_pages` |
| Check / verify | `check_project`, `check_pages`, `check_parts` |
| Generate | `generate_connections`, `generate_cables` |
| Reports | `update_reports`, `update_model_view_pages`, `create_model_views`, `create_copper_unfolds`, `create_drilling_views` |
| Search | `search_devices`, `search_text`, `search_all_properties`, `search_page_data`, `search_project_data` |
| Navigation / edit | `edit_open_page`, `edit_goto_device`, `edit_open_layout_space`, `close_pages`, `get_selected_pages`, `preview_page`, `navigate_to_eec` |
| Renumber | `renumber_devices`, `renumber_pages`, `renumber_cables`, `renumber_terminals`, `renumber_connections` |
| Translate | `translate_project`, `export_missing_translations`, `remove_language` |
| Device list | `export_device_list`, `import_device_list`, `delete_device_list` |
| Labels / layers | `create_labels`, `change_layer`, `export/import_graphical_layer_table` |
| Macros | `generate_macros`, `prepare_macros`, `update_macros` |
| Scripts | `register_script`, `unregister_script`, `execute_script` |
| Settings | `export_settings`, `import_settings`, `set_setting`, `set_project_setting` |
| Properties | `get/set_project_property`, `get/set_page_property`, `get/set_property`, `export/import_user_properties` |
| Parts | `export/import_parts_list`, `select_part`, `set_parts_data_source`, `partsmanagement_*` |
| PLC | `plc_export`, `plc_import` |
| Workspace | `open_workspace`, `save_workspace`, `clean_workspace` |
| Data exchange | `export_connections/functions/pages`, `dc_import`, `dc_export`, `export_*_definitions`, `export/import_subproject`, `masterdata_operation`, … |
| Cabinet / 3D | `calculate_cabinet_weight`, `update_segments_filling`, `topology_operation`, `import_preplanning_data`, `export/import_segments_template` |
| Production | `export_nc_data`, `export_production_wiring` |
| Ribbon / add-ons | `export/import_ribbon_bar`, `load_api_module`, `register/unregister_addon`, `execute_raw_action` |
| Scripted (advanced APIs via C#) | `parts_db_query/count/get_part/create/update/list_product_groups`, `settings_get/set_string/bool/int/double`, `pathmap_substitute`, `pathmap_get_common_paths`, `execute_custom_script` |
| Discovery (enumerate catalogs) | `settings_list_children`, `list_schemes`, `list_report_templates`, `list_layers`, `list_enums` |

### 3. Asset Administration Shell tools (4)

AAS/AASX digital-twin tools (`aas_*`), built on `basyx-python-sdk` (AAS
metamodel V3). Only `aas_inspect_package` works without an EPLAN connection.

| Tool | Purpose |
|------|---------|
| `aas_export_part` | Export a parts-DB part as an `.aasx` (Digital Nameplate + Technical Data). |
| `aas_export_project` | Export a project as an `.aasx`: properties, part sub-shells, and embedded documents (Handover Documentation). |
| `aas_inspect_package` | List the shells, submodels, and embedded files of any `.aasx` (offline). |
| `aas_import_parts` | Map a supplier `.aasx` onto the parts DB (create + update), always with a `dry_run` preview first. |

---

## Extending: add a new action

The server registers tools **dynamically** from the actions package's `__all__`
list — there is no per-tool `@mcp.tool()` boilerplate to write.

1. Implement the function in `api/actions/<module>.py`:

   ```python
   def my_action(project_name: str = None) -> dict:
       """One-line summary the LLM will see as the tool description.

       Args:
           project_name: Project path (optional).
       """
       manager, error = _get_connected_manager()
       if error:
           return error
       action = _build_action("SomeEplanAction", PROJECTNAME=project_name)
       return manager.execute_action(action)
   ```

2. Export it in `api/actions/__init__.py`: add it to the imports **and** to
   `__all__`.

3. Restart the MCP server. The tool appears as `eplan_my_action`.

Tips:
- Write a meaningful docstring + type hints — they become the tool description
  and input schema the LLM relies on.
- Verify action names/parameters against the official EPLAN P8 docs. The 2027
  wiki (`https://rag2027.covaga.xyz`) serves one page per documented action at
  `API Reference/Actions/<Name>.md` — `GET /file?path=...` returns the whole
  page, `POST /search` (`{"query": ..., "topK": <=20}`) finds it. Then run
  `python ../tools/validate_actions.py` to cross-check the whole wrapper set:
  it confirms every declared `Action:` has a wiki page and that every `/KEY`
  appears on that page **with the same casing**, and writes
  `tools/action_validation_report.md`. Add `--completeness` to also sweep the
  wiki for action pages missing from `tools/data/official_actions_2027.json`
  (several thousand queries, minutes); `--rag-url` points it elsewhere.
- Windows paths need escaping (`\\`) or forward slashes (`/`).

---

## EPLAN version selection (automatic)

Nothing to configure. `eplan_connection.py::detect_installed_versions()` scans
`C:\Program Files\EPLAN\Platform` (override with the `EPLAN_PLATFORM_ROOT`
environment variable) and:

- **Auto (default):** targets the newest installed version and selects the
  matching .NET runtime (coreclr for EPLAN 2027+, .NET Framework for ≤ 2026).
- **Explicit:** `eplan_connect(version="2026")` targets a specific major
  version; `eplan_versions` lists the options without loading any DLLs.

Once one version's DLLs are loaded into the process, switching versions
requires restarting the MCP server (the .NET runtime cannot be swapped at
runtime).

---

## Example session

```
User: Connect to EPLAN and open "C:\Projects\Test.elk".
LLM:  [eplan_connect]         -> Connected on port 49152
      [eplan_open_project]    -> {"success": true, "parameters": {"PROJECT": "C:\\Projects\\Test.elk"}}
      Project opened silently (QuietMode).
```

See [`../../llm.md`](../../llm.md) for an operating guide aimed at the connected LLM.
