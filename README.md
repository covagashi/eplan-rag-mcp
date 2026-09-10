# EPLAN AI Automation Toolkit

**English** · [中文](README.zh-CN.md) · [Русский](README.ru.md)

[![MCP Badge](https://lobehub.com/badge/mcp/covagashi-eplan_2026_ia_mcp_scripts)](https://lobehub.com/mcp/covagashi-eplan_2026_ia_mcp_scripts)

AI-assisted automation for **EPLAN Electric P8** and **EPLAN EEC Pro 2026**, built on the
Model Context Protocol (MCP).

The repository holds four independent sub-projects: a local MCP server that drives a
running EPLAN instance, and three remote MCP servers on Cloudflare Workers that serve the
indexed EPLAN documentation.

> Working with an LLM here? Read [`llm.md`](llm.md) — it describes, in LLM-facing terms,
> everything the toolkit can do and configure.

## Repository layout

```
.
├── eplan-p8-mcp-server/          # LOCAL:  MCP server that controls EPLAN P8
├── cloudflare-rag-eplan-p8/      # REMOTE: Cloudflare Worker serving the P8 docs RAG over MCP
├── cloudflare-rag-eecpro/        # REMOTE: Cloudflare Worker serving the EEC Pro docs RAG over MCP
├── cloudflare-rag-eplan-2027/    # REMOTE: Cloudflare Worker serving the 2027 API wiki (D1/FTS5 keyword search)
└── claude-skills/                # SKILL:  mirror of covagashi/eplan-development-skill
```

| Folder | Type | Purpose | EPLAN product |
|---|---|---|---|
| `eplan-p8-mcp-server/` | Local Python MCP | Drive a running EPLAN instance from Claude: open/close projects, exports, reports, scripts | EPLAN Electric P8 |
| `cloudflare-rag-eplan-p8/` | Remote Cloudflare Worker | Serve the P8 doc index as a remote MCP + REST API | EPLAN Electric P8 |
| `cloudflare-rag-eecpro/` | Remote Cloudflare Worker | Serve the EEC Pro doc index as a remote MCP + REST API | EPLAN EEC Pro 2026 |
| `cloudflare-rag-eplan-2027/` | Remote Cloudflare Worker | Serve the 2027 API wiki as a remote MCP; D1 + FTS5 keyword search, complementary to the semantic index above | EPLAN Electric P8 2027 |
| `claude-skills/eplan-development/` | Claude Code skill | Teach Claude to write correct EPLAN scripts, API code and Remote Client apps. Mirror of the standalone [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) repository | EPLAN Electric P8 |

Each sub-project carries its own README with installation and usage details.

## What is MCP?

**MCP (Model Context Protocol)** is an open standard that lets AI assistants interact with
external tools and services. Rather than only generating code, Claude can *execute* actions
inside EPLAN in real time and consult the documentation through search.

## Quick start

### Local EPLAN automation (P8)

```bash
pip install pythonnet mcp
claude mcp add eplan -- python YOURPATH/eplan-p8-mcp-server/mcp_server/server.py
claude mcp list   # should list "eplan"
```

Then start EPLAN, open Claude Code, and say `connect to eplan`. The full guide is in
[`eplan-p8-mcp-server/mcp_server/README.md`](eplan-p8-mcp-server/mcp_server/README.md).

![Claude CLI configured](image.png)

#### Prerequisite

Remoting must be enabled in EPLAN before the server can connect: turn on **Allow remote
access via Remote Client** under **File → Settings… → Workstation → Interfaces → Remote
access**.

![Allow remote access via Remote Client](Remoting_Setting_AllowLocalAccess.png)

#### What the server exposes

In its default `full` mode the server publishes **199 tools**:

| Group | Count | What it covers |
|---|---|---|
| Connection / utility | 8 | Connect, version selection, status, extension listing |
| Typed EPLAN actions (`eplan_*`) | 183 | One tool per documented or verified action, each executed silently inside a C# script under QuietMode, so no EPLAN dialog can block an unattended run |
| Action catalog (`eplan_action_catalog` / `_describe` / `_run` / `_ribbon_catalog`) | 4 | Reaches a further ~1,050 actions that exist only as GUI buttons, mined from the install's `MFTools.xml` rather than the official docs |
| Asset Administration Shell (`aas_*`) | 4 | AAS/AASX digital-twin export and import |

The ~1,050 catalog actions are deliberately *not* given one wrapper tool each: that would
have tripled the tool count and degraded tool selection for everything else.

Among the 183 typed tools are four live-DataModel tools —
`eplan_live_query_functions`, `eplan_live_query_pages`, `eplan_live_set_function_text`
and `eplan_live_set_connection_designations` — which read and edit the open project's
object model through runtime reflection, working around the script engine's limitation on
static `using` directives.

Beyond individual actions, the toolset covers the building blocks of a fully unattended
develop → deploy → test loop:

- **Application lifecycle** — `eplan_app_launch` / `eplan_app_shutdown` /
  `eplan_app_restart`: exit EPLAN, swap add-in DLLs, relaunch, reconnect, reopen the project.
- **Disposable fixtures** — `eplan_scratch_project_*`: scratch projects cloned from a template.
- **Diagnostics** — `eplan_get_system_messages`: read EPLAN's message tree and see the same
  errors and warnings the user sees in the GUI.
- **Private extension modules** — see [below](#private-extension-modules-eplan_mcp_extensions).

A tool-by-tool reference lives in [the project wiki](https://github.com/covagashi/eplan-rag-mcp/wiki).

#### Discovery mode

`EPLAN_MCP_MODE=discovery` publishes 13 tools instead of 199, trading a token-heavy tool
list for one extra search round-trip per task. It is worth it for MCP clients that send
every tool's full schema on every request.

**Skip it in Claude Code.** Claude Code already defers tool schemas itself — a name list up
front, a schema fetched on demand — so the `full` 199-tool list already costs it about as
little as `discovery`'s 13 would, and layering discovery's search → describe → call
indirection on top only adds a round-trip. The tradeoff and the measurements are in
[`eplan-p8-mcp-server/mcp_server/README.md`](eplan-p8-mcp-server/mcp_server/README.md#discovery-mode-eplan_mcp_mode).

### Remote documentation RAGs (P8, EEC Pro, 2027)

Already deployed and ready to use — no local data required:

```bash
# EPLAN Electric P8 documentation (2026, semantic search)
claude mcp add eplan-rag -- cmd /c npx mcp-remote https://rag2026.covaga.xyz/mcp

# EPLAN EEC Pro 2026 documentation
claude mcp add eecpro-rag -- cmd /c npx mcp-remote https://rageecpro.covaga.xyz/mcp

# EPLAN Electric P8 documentation (2027, keyword/full-text search)
claude mcp add eplan-wiki-2027 -- cmd /c npx mcp-remote https://rag2027.covaga.xyz/mcp
```

`eplan-wiki-2027` is deliberately a separate server rather than a 2026 → 2027 upgrade of
`eplan-rag`: it indexes a different doc version *and* uses a different search mode (SQLite
FTS5/bm25 keyword matching over [`cloudflare-rag-eplan-2027/`](cloudflare-rag-eplan-2027/)'s
bundled wiki, versus Vectorize + bge semantic search). Measured head-to-head on real
queries they fail differently: FTS5 wins exact-name lookups ("what is the signature of X"),
semantic search wins when the query shares no vocabulary with the docs at all. Install both.

Both also expose a plain REST API, which is convenient for verifying EPLAN action names and
parameters while developing:

```bash
curl -X POST https://rag2026.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "export project pdf", "topK": 3}'

curl -X POST https://rag2027.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "FindAction", "topK": 3}'
```

See [`cloudflare-rag-eplan-p8/README.md`](cloudflare-rag-eplan-p8/README.md) and
[`cloudflare-rag-eecpro/README.md`](cloudflare-rag-eecpro/README.md) for the tools, REST
endpoints and architecture.

### Claude Code skill for EPLAN development

The MCP servers let Claude *act* on EPLAN. The
[**eplan-development**](https://github.com/covagashi/eplan-development-skill) skill teaches
it to *write correct EPLAN code*: scripting entry points, verified action parameters,
parts-database access, Remote Client automation (dynamic ports, headless EPLAN, Cogineer),
and the production pitfalls — pseudo-asynchronous command blocking, the message-loop
monitor thread, dispose discipline, the EPLAN 2025 remoting changes.

The skill lives in its own repository and is deliberately **host-agnostic**: it assumes no
MCP server, no particular script runner and no particular documentation index, so it is
useful on its own whether or not you run anything else from here. That repository is the
canonical copy; `claude-skills/` in this repo mirrors it.

```
/plugin marketplace add covagashi/eplan-development-skill
/plugin install eplan-development@eplan-skills
```

This repository is also a plugin marketplace, so the skill can equally be installed from
here:

```
/plugin marketplace add covagashi/eplan-rag-mcp
/plugin install eplan-development@eplan-tools
```

Manual installation and details:
[`claude-skills/eplan-development/README.md`](claude-skills/eplan-development/README.md).

## Adding new EPLAN actions

The local MCP server registers tools **dynamically** from each actions package's `__all__`
list, so adding an action takes two steps and no per-tool boilerplate.

### 1. Implement the action

In `eplan-p8-mcp-server/mcp_server/api/actions/<your_module>.py`:

```python
def open_project(project_path: str, open_mode: str = None) -> dict:
    """Open a project in EPLAN.

    Args:
        project_path: Full path to the .elk project file.
        open_mode: "Standard", "ReadOnly", or "Exclusive" (optional).
    """
    manager, error = _get_connected_manager()
    if error:
        return error
    action = _build_action("ProjectOpen", Project=project_path, OpenMode=open_mode)
    return manager.execute_action(action)
```

### 2. Export it

Add the function to the imports **and** to `__all__` in
`eplan-p8-mcp-server/mcp_server/api/actions/__init__.py`. It is then auto-registered as
`eplan_open_project`.

### 3. Restart the MCP server

The new tool becomes available once Claude / the server restarts.

### 4. Validate against the official docs (optional)

`eplan-p8-mcp-server/tools/validate_actions.py` cross-checks every action name and
parameter declared in the wrappers against the official EPLAN docs RAG and writes a
markdown report:

```bash
python eplan-p8-mcp-server/tools/validate_actions.py
```

![EPLAN test](image-1.png)

### Tips

1. **Verify against the docs.** Use the remote P8 RAG (`https://rag2026.covaga.xyz`) to
   confirm the exact EPLAN action name and its parameters.
2. **Write meaningful docstrings and type hints.** They become the tool description and
   input schema the LLM sees and relies on.
3. **Handle paths carefully.** Windows paths need escaping (`\\`) or forward slashes (`/`).

## Private extension modules (`EPLAN_MCP_EXTENSIONS`)

The server can load **extra tool modules from outside this repository** — company-specific
or private tooling (custom add-in test harnesses, internal workflows) that must not live in
a public repo.

Point the `EPLAN_MCP_EXTENSIONS` environment variable at one or more directories (separated
by `;` on Windows). Every top-level `*.py` file there that does not start with `_` is
imported at startup, and its `__all__` functions are registered as MCP tools exactly like
the built-in actions:

```python
# my_company_tools.py  (in a private repo, NOT in eplan-rag-mcp)
TOOL_PREFIX = "acme_"          # optional, default "eplan_"
__all__ = ["run_smoke_test"]

import actions                  # the server's api/ folder is on sys.path
from actions._base import _get_connected_manager

def run_smoke_test(project_path: str) -> dict:
    """Docstring becomes the tool description the LLM sees."""
    clone = actions.scratch_project_create(project_path)
    ...
    return {"success": True}
```

Rules and behaviour:

- `TOOL_PREFIX` namespaces the tools (`acme_run_smoke_test` above).
- Extensions may import everything the built-in actions use: `actions`, `actions._base`,
  `actions.scripted._execute_script` (run C# inside EPLAN), `eplan_connection`.
- A broken extension is reported on stderr and skipped; it never prevents the server from
  starting.
- `eplan_list_extensions` shows what was loaded.

Combined with the lifecycle and scratch-fixture tools, this enables a fully unattended loop
for developing private EPLAN add-ins: build the DLL → deploy → `eplan_app_restart` → verify
the add-in's actions registered (for example via a `FindAction` script) → run them against a
disposable scratch project → `eplan_get_system_messages` to catch anything EPLAN complained
about.

## EPLAN version selection (automatic)

There is **nothing to configure**. On startup the server scans
`C:\Program Files\EPLAN\Platform` for installed versions and:

- **Auto mode (default):** `eplan_connect` targets the **newest installed version** and
  picks the matching .NET runtime automatically — coreclr for EPLAN 2027+, .NET Framework
  for 2026 and older.
- **Explicit mode:** the LLM can call `eplan_versions` to list what is installed and then
  connect to a specific one with `eplan_connect(version="2026")` — e.g. "connect to
  eplan 2026".

Notes:

- EPLAN installed somewhere non-standard? Set `EPLAN_PLATFORM_ROOT` to its `Platform` folder.
- Once one version's DLLs are loaded into the process, switching versions requires
  restarting the MCP server — a .NET runtime cannot be swapped at runtime.
- `eplan_connect` also accepts a `host` (and `"host:port"`) to reach an EPLAN instance on
  another machine; port auto-detection only works on localhost.

## Related repositories

| Repository | What it is |
|---|---|
| [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | The Claude Code skill above, standalone and host-agnostic. Install with `/plugin marketplace add covagashi/eplan-development-skill`. |
| [**eplan-ctxmenu-kit**](https://github.com/covagashi/eplan-ctxmenu-kit) | Add your own entries to EPLAN's right-click menus, and read the row the user clicked. A discovery tool plus a worked example. |

## Resources

- [EPLAN API documentation](https://www.eplan.help/)
- [MCP protocol specification](https://modelcontextprotocol.io/)
- [Claude Code documentation](https://docs.anthropic.com/claude-code)

## License

MIT — see [`license`](license).
