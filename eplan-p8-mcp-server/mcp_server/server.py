"""
EPLAN MCP Server

Complete EPLAN automation server exposing all API actions via MCP protocol.
Every action runs inside a C# script under QuietMode (no blocking dialogs).

Two registration modes, selected with EPLAN_MCP_MODE:
  full       (default) - every wrapper is its own MCP tool. Unchanged behaviour.
  discovery            - publish a small core plus eplan_tools_search /
                         _describe / _call; everything else is reached through
                         them. See tool_registry.py and README.md.

Requirements:
- EPLAN installed
- python -m pip install -r eplan-p8-mcp-server/mcp_server/requirements.txt
  (from the repository root)
"""

import json
import os
import sys
import types
import functools
from mcp.server.fastmcp import FastMCP
from eplan_connection import get_manager, detect_installed_versions

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Add API folders to path for imports
sys.path.insert(0, os.path.join(SCRIPT_DIR, "api"))

# Import the actions package (QuietMode execution)
import api.actions as eplan_actions
from tool_registry import ToolRegistry, make_meta_tools, META_TOOL_NAMES


# ============================================================================
# REGISTRATION MODE
# ============================================================================
# Every MCP request carries all tool definitions, so ~194 tools cost ~180k
# characters (~45k tokens) per request. Discovery mode publishes only the tools
# a session genuinely needs on turn one and hides the rest behind meta-tools.

VALID_MODES = ("full", "discovery")

# Published in discovery mode. The connection/session core is needed before
# anything else can happen; the action-catalog tier is itself a discovery
# mechanism (for the ~1150 raw EPLAN actions) and stays reachable directly.
DISCOVERY_CORE_TOOLS = frozenset({
    "eplan_status",
    "eplan_versions",
    "eplan_servers",
    "eplan_connect",
    "eplan_disconnect",
    "eplan_ping",
    "eplan_action_catalog",
    "eplan_action_describe",
    "eplan_action_run",
    "eplan_ribbon_catalog",
})

DISCOVERY_META_TOOLS = tuple("eplan_" + n for n in META_TOOL_NAMES)


def _resolve_mode(raw=None):
    """
    Normalise EPLAN_MCP_MODE. An unknown value warns on stderr and falls back to
    "full" - a typo in a client config must never stop the server from starting.
    """
    mode = (raw or "full").strip().lower()
    if mode == "":
        return "full"
    if mode in VALID_MODES:
        return mode
    print(
        "Unknown EPLAN_MCP_MODE %r, falling back to 'full'. Valid values: %s."
        % (raw, ", ".join(VALID_MODES)),
        file=sys.stderr,
    )
    return "full"


MODE = _resolve_mode(os.environ.get("EPLAN_MCP_MODE"))


def _is_published(tool_name, mode):
    """Does this tool get its own MCP tool definition in this mode?"""
    if mode != "discovery":
        return True
    return tool_name in DISCOVERY_CORE_TOOLS or tool_name in DISCOVERY_META_TOOLS


def _json_wrapper(f):
    """Wrap an action function so the MCP tool returns formatted JSON."""

    @functools.wraps(f)
    def mcp_tool_wrapper(*args, **kwargs):
        try:
            res = f(*args, **kwargs)
            return json.dumps(res, indent=2, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    mcp_tool_wrapper.__doc__ = f.__doc__ or ""
    return mcp_tool_wrapper


# ============================================================================
# CONNECTION MANAGEMENT (Shared / Version-Agnostic)
# ============================================================================
# These are plain functions; _register_core() attaches them to an app so the
# same definitions can be published on a fresh FastMCP instance (tests, and the
# two modes) instead of being frozen by a decorator at import time.

def eplan_status() -> str:
    """Get the current EPLAN connection status."""
    manager = get_manager()
    return json.dumps(manager.get_status(), indent=2)


def eplan_versions() -> str:
    """List EPLAN versions installed on this machine.

    Use this BEFORE eplan_connect if the user needs a specific version;
    by default the newest installed version is used automatically.
    Does not load any EPLAN DLLs, so it never locks the process to a version.
    """
    installed = detect_installed_versions()
    manager = get_manager()
    return json.dumps({
        "installed": installed,
        "loaded_version": manager.target_version if manager._clr_initialized else None,
        "note": "Pass version to eplan_connect to target one explicitly; omit it to auto-use the newest.",
    }, indent=2)


def eplan_servers() -> str:
    """List active EPLAN servers (running instances).

    Note: this loads the EPLAN DLLs (auto-detected newest version if not
    connected yet).

    This can return an empty list even while EPLAN is fully open with a
    project loaded - auto-detection is not fully reliable, particularly
    right after EPLAN itself was just (re)started. Do not treat an empty
    result as proof EPLAN isn't running. If eplan_connect() then also fails
    with a connection error (e.g. gRPC "failed to connect to all
    addresses"), fall back to asking the user to find the actual listening
    port themselves (e.g. a TCP/port viewer filtered to EPLAN.exe, or
    `netstat -ano | findstr LISTENING` cross-referenced with EPLAN.exe's
    PID) rather than assuming the default port is correct - the default
    49152 is only a guess, real instances have been seen listening on other
    ports (e.g. 49153) especially with multiple EPLAN processes running.
    """
    manager = get_manager()
    servers = manager.get_active_servers()
    return json.dumps({"servers": servers, "count": len(servers)}, indent=2)


def eplan_connect(host: str = None, port: str = None, version: str = None) -> str:
    """Connect to EPLAN.

    Args:
        host: EPLAN machine to connect to. Defaults to "localhost". Set this to
            reach an EPLAN instance on another machine (e.g. "10.10.10.2"). You
            may also pass "host:port" as this argument and it will be split.
        port: Remoting port. Auto-detected from local servers if omitted, but
            auto-detection only works for localhost — when connecting to a
            remote host you must supply the port explicitly (default 49152).
            If the connection fails (e.g. gRPC "failed to connect to all
            addresses") and eplan_servers() found nothing either, the default
            port is likely wrong, not EPLAN being closed - ask the user to
            check the real listening port for EPLAN.exe and retry with that
            port explicitly. Do not silently retry a range of ports yourself
            without telling the user what you're doing.
        version: EPLAN major version to target, e.g. "2026". Omit for
            auto-detection (newest installed version). Use eplan_versions to
            see what is available. Once one version's DLLs are loaded,
            switching requires restarting the MCP server.
    """
    # Allow "10.10.10.2:49152" passed as either host or port.
    if host and ":" in host and port is None:
        host, port = host.rsplit(":", 1)
    elif port and ":" in port:
        maybe_host, maybe_port = port.rsplit(":", 1)
        if maybe_port.isdigit():
            host, port = maybe_host, maybe_port

    manager = get_manager(version)
    result = manager.connect(host=host, port=port)
    result["target_version"] = manager.target_version
    return json.dumps(result, indent=2)


def eplan_disconnect() -> str:
    """Disconnect from EPLAN."""
    manager = get_manager()
    return json.dumps(manager.disconnect(), indent=2)


def eplan_ping() -> str:
    """Check if EPLAN is responding."""
    manager = get_manager()
    return json.dumps(manager.ping(), indent=2)


def eplan_test(show_dialog: bool = False) -> str:
    """
    Verify the connection by compiling and running a real C# script in EPLAN.

    Args:
        show_dialog: Show a MessageBox in the EPLAN GUI instead of the
            default non-interactive round-trip (default False).
            MessageBox.Show is a Windows dialog, not an EPLAN one, so
            QuietMode cannot suppress it: setting this True BLOCKS this
            server - and every other caller waiting on it - until a human
            clicks OK in the EPLAN window. Audit #42 item 10. Leave False
            unless you specifically want the visible confirmation and are
            prepared to go click it yourself.
    """
    manager = get_manager()

    if not manager.connected:
        return json.dumps({
            "success": False,
            "message": "Not connected. Call eplan_connect() first."
        }, indent=2)

    if show_dialog:
        # Create test script
        scripts_dir = os.path.join(SCRIPT_DIR, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        script_path = os.path.join(scripts_dir, "mcp_test.cs")

        script = '''using System.Windows.Forms;
using Eplan.EplApi.Scripting;

public class MCPTest
{
    [Start]
    public void Run()
    {
        MessageBox.Show(
            "MCP Connection OK!",
            "EPLAN MCP Server",
            MessageBoxButtons.OK,
            MessageBoxIcon.Information
        );
    }
}
'''
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(script)

        # Execute only - a [Start]-only script needs no RegisterScript; it just
        # makes EPLAN report "The script does not contain attributes for loading."
        # ExecuteScript compiles and runs [Start] by itself (see scripted.py).
        result = manager.execute_action(f'ExecuteScript /ScriptFile:"{script_path}"')

        return json.dumps({
            "success": result.get("success", False),
            "message": "Check EPLAN for MessageBox" if result.get("success") else result.get("message")
        }, indent=2)

    # Default path: the same liveness proof, without blocking. Round-trips
    # through the ordinary generated-script machinery (scripted._execute_script)
    # instead of hand-building an ExecuteScript call, so this also exercises
    # compile-and-run the same way every other scripted tool does.
    from api.actions.scripted import _execute_script

    script = '''using System;
using System.Collections.Generic;
using System.IO;
using Eplan.EplApi.Scripting;

public class MCPTest
{
    [Start]
    public void Run()
    {
        var results = new Dictionary<string, object>();
        results["success"] = true;
        results["message"] = "MCP Connection OK";
        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results, Newtonsoft.Json.Formatting.Indented);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
'''
    outcome = _execute_script(script)
    if outcome.get("success"):
        return json.dumps({
            "success": True,
            "message": outcome.get("results", {}).get("message", "MCP Connection OK"),
        }, indent=2)
    return json.dumps({
        "success": False,
        "message": outcome.get("error") or outcome.get("message") or "script did not report success",
    }, indent=2)


# Populated by _build_app(); read by eplan_list_extensions below.
_loaded_extensions = []


def eplan_list_extensions() -> str:
    """List the extension modules (and their tools) loaded via EPLAN_MCP_EXTENSIONS."""
    return json.dumps({
        "env": os.environ.get("EPLAN_MCP_EXTENSIONS", ""),
        "extensions": _loaded_extensions,
    }, indent=2)


# Tools defined in this module. The value is the attribute name, which is also
# the tool name (these already carry the eplan_ prefix).
_CORE_TOOLS = (
    "eplan_status",
    "eplan_versions",
    "eplan_servers",
    "eplan_connect",
    "eplan_disconnect",
    "eplan_ping",
    "eplan_test",
    "eplan_list_extensions",
)


def _register_core(app, registry, mode):
    """Register the connection/session tools defined in this module."""
    this_module = sys.modules[__name__]
    for tool_name in _CORE_TOOLS:
        func = getattr(this_module, tool_name)
        published = _is_published(tool_name, mode)
        registry.add(tool_name, this_module, tool_name, prefix="eplan_",
                     published=published)
        if published:
            # Registered raw (not through _json_wrapper): these already return
            # a JSON string, exactly as they did when they were decorated.
            app.tool(name=tool_name)(func)


# ============================================================================
# DYNAMIC ACTIONS REGISTRATION
# ============================================================================

def register_actions(actions_module, prefix="eplan_", app=None, registry=None, mode=None):
    """
    Dynamically registers all actions exported by the actions module.
    Wraps the functions to return formatted JSON.

    Every action is added to the tool registry regardless of mode; in discovery
    mode only the core set additionally gets its own MCP tool definition, and
    the rest are reached through eplan_tools_search / _describe / _call.
    """
    app = app if app is not None else globals().get("mcp")
    registry = registry if registry is not None else globals().get("REGISTRY")
    mode = mode if mode is not None else MODE

    for func_name in actions_module.__all__:
        if func_name.startswith('_'):
            continue

        func = getattr(actions_module, func_name)
        if not callable(func):
            continue

        tool_name = f"{prefix}{func_name}"
        published = _is_published(tool_name, mode)

        if registry is not None:
            # Store (module, attr) rather than the function object so the
            # registry follows reloads and monkeypatching.
            registry.add(tool_name, actions_module, func_name, prefix=prefix,
                         published=published)

        if published and app is not None:
            app.tool(name=tool_name)(_json_wrapper(func))


# ============================================================================
# EXTENSION MODULES (private / site-specific tools)
# ============================================================================
# EPLAN_MCP_EXTENSIONS is an os.pathsep-separated list of directories. Every
# top-level *.py file in each directory (not starting with "_") is imported
# and its __all__ functions are registered as MCP tools, exactly like the
# built-in actions. This lets a private repo ship its own tools (custom
# add-in test harnesses, company-specific workflows) without forking this
# public server.
#
# An extension module can:
#   - set TOOL_PREFIX = "myprefix_" (default "eplan_") to namespace its tools
#   - import shared plumbing: `from actions._base import ...`,
#     `import actions` (the api folder is already on sys.path)
# A broken extension is reported on stderr and skipped - it never prevents
# the server from starting.
#
# In discovery mode extension tools are indexed and searchable rather than
# separately published, so a private extension pack costs no per-request tokens.

def load_extensions(env_value: str = None, app=None, registry=None, mode=None):
    """Import and register extension modules from EPLAN_MCP_EXTENSIONS dirs."""
    import importlib.util

    app = app if app is not None else globals().get("mcp")
    registry = registry if registry is not None else globals().get("REGISTRY")
    mode = mode if mode is not None else MODE

    raw = env_value if env_value is not None else os.environ.get("EPLAN_MCP_EXTENSIONS", "")
    loaded = []
    for ext_dir in [d.strip() for d in raw.split(os.pathsep) if d.strip()]:
        if not os.path.isdir(ext_dir):
            print(f"Extension dir not found, skipping: {ext_dir}", file=sys.stderr)
            continue
        if ext_dir not in sys.path:
            sys.path.insert(0, ext_dir)
        for fname in sorted(os.listdir(ext_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            mod_name = os.path.splitext(fname)[0]
            try:
                spec = importlib.util.spec_from_file_location(
                    f"mcp_extension_{mod_name}", os.path.join(ext_dir, fname))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if not getattr(module, "__all__", None):
                    print(f"Extension {fname} has no __all__, skipping.", file=sys.stderr)
                    continue
                prefix = getattr(module, "TOOL_PREFIX", "eplan_")
                register_actions(module, prefix=prefix, app=app, registry=registry,
                                 mode=mode)
                loaded.append({"module": fname, "dir": ext_dir, "prefix": prefix,
                               "tools": list(module.__all__)})
                print(f"Extension loaded: {fname} ({len(module.__all__)} tools, "
                      f"prefix '{prefix}')", file=sys.stderr)
            except Exception as e:
                print(f"Extension {fname} failed to load, skipping: {e}", file=sys.stderr)
    return loaded


# ============================================================================
# APP CONSTRUCTION
# ============================================================================

def build_app(mode="full", app=None, registry=None):
    """
    Build a fully registered server for `mode`.

    Returns (app, registry, loaded_extensions). Everything the module used to do
    at import time happens here, so tests (and the size measurement) can build a
    second, independent instance without disturbing the live one.
    """
    mode = _resolve_mode(mode)
    app = app if app is not None else FastMCP("EPLAN MCP Server")
    registry = registry if registry is not None else ToolRegistry()

    _register_core(app, registry, mode)

    # All actions (executed inside a C# script under QuietMode)
    register_actions(eplan_actions, app=app, registry=registry, mode=mode)

    # AAS (Asset Administration Shell) tools - optional, needs basyx-python-sdk.
    # Only a missing basyx dependency is treated as "optional"; any other import
    # error inside the package is a real bug and must surface loudly rather than
    # silently dropping the aas_* tools.
    try:
        import api.aas as aas_tools
        register_actions(aas_tools, prefix="aas_", app=app, registry=registry, mode=mode)
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] in ("basyx", "aas"):
            print("AAS tools disabled: basyx-python-sdk not installed "
                  "(pip install basyx-python-sdk).", file=sys.stderr)
        else:
            raise

    loaded = load_extensions(app=app, registry=registry, mode=mode)

    if mode == "discovery":
        # The three meta-tools are the only way to reach everything above, so
        # they exist only in discovery mode - full mode stays byte-identical.
        meta = make_meta_tools(registry)
        # The meta-tools are closures over this registry, so they live in a
        # per-build namespace rather than on this module: two build_app calls in
        # one process (tests, the size measurement) must not clobber each other.
        meta_ns = types.SimpleNamespace(**meta)
        for attr, func in meta.items():
            tool_name = "eplan_" + attr
            registry.add(tool_name, meta_ns, attr, prefix="eplan_", published=True)
            app.tool(name=tool_name)(_json_wrapper(func))

    strip_schema_boilerplate(app)

    return app, registry, loaded


def strip_schema_boilerplate(app):
    """
    Delete zero-information keys from the generated JSON schemas.

    Pydantic (via FastMCP) auto-generates a "title" for every schema node -
    export_file becomes "Export File", and the root gets
    "export_pdf_pagesArguments" - and emits "default": null for every optional
    argument. None of it tells the model anything the key name and the absence
    of a default do not already say, yet all of it is sent on every request
    (and again on every on-demand schema load in clients that defer schemas).

    Measured on this server: 31,268 characters, about 7,800 tokens, roughly
    15% of the whole tool payload, for no loss of meaning. Informative
    (non-null) defaults are preserved.

    Safe because Tool.parameters is only ever serialised out to the client -
    argument validation goes through Tool.fn_metadata.call_fn_with_arg_validation,
    not through this dict. There is no public FastMCP option for this in
    mcp 1.28.x (its StrictJsonSchema generator is wired only into *output*
    schemas), hence the post-registration pass.

    Set EPLAN_MCP_KEEP_SCHEMA_TITLES=1 to skip it if a client ever turns out to
    depend on the titles.

    Returns:
        int: characters removed, for the benefit of tests and measurement.
    """
    if os.environ.get("EPLAN_MCP_KEEP_SCHEMA_TITLES") == "1":
        return 0

    # Keys whose value is a MAP of {name: schema} rather than a schema node
    # itself - "properties" chief among them. Walking into one of these must
    # recurse into its VALUES, never call node.pop() on the map dict itself,
    # because its keys are parameter/definition NAMES. Audit #42 item 11: a
    # parameter literally named `title` had its entire schema entry deleted
    # from `properties` (while `required` still named it), because the old
    # prune() popped "title" from every dict it walked with no way to tell a
    # schema node from the map that holds several of them.
    #
    # This must be decided fresh at each level from "which key did the PARENT
    # use to reach this dict", never by inspecting the dict's own shape or
    # name - a parameter can itself be named `properties` (a real one exists:
    # a generic user-properties dict), and its OWN schema node (which legitimately
    # has an auto-generated "title" to strip) must not be mistaken for the
    # `properties` map merely because of how it happens to be reached.
    _SCHEMA_MAPS = {"properties", "$defs", "definitions", "patternProperties"}

    def prune(node):
        if isinstance(node, dict):
            node.pop("title", None)
            # Only null defaults: "default": 0/""/False is real information.
            if "default" in node and node["default"] is None:
                node.pop("default")
            for key, value in node.items():
                if key in _SCHEMA_MAPS and isinstance(value, dict):
                    for sub in value.values():
                        prune(sub)
                else:
                    prune(value)
        elif isinstance(node, list):
            for value in node:
                prune(value)

    removed = 0
    try:
        tools = app._tool_manager._tools
    except AttributeError:  # FastMCP internals moved; not worth crashing over
        return 0

    for tool in tools.values():
        schema = getattr(tool, "parameters", None)
        if not isinstance(schema, dict):
            continue
        before = len(json.dumps(schema))
        prune(schema)
        removed += before - len(json.dumps(schema))
    return removed


mcp, REGISTRY, _loaded_extensions = build_app(MODE)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    installed = detect_installed_versions()
    print("EPLAN MCP Server")
    if installed:
        versions = ", ".join(i["full_version"] for i in installed)
        print(f"Installed EPLAN versions: {versions} (auto-targets the newest)")
    else:
        print("WARNING: no EPLAN installation detected")
    print("-" * 40)
    print(f"Mode: {MODE} ({len(REGISTRY.published_names())} of {len(REGISTRY)} "
          f"tools published)")
    if MODE == "discovery":
        print("Hidden tools are reachable via eplan_tools_search / "
              "eplan_tools_describe / eplan_tools_call")
    print("All actions run as eplan_* (C# script under QuietMode)")
    print("-" * 40)

    # Transport selection: stdio (default) or streamable-http for running the
    # server on the EPLAN machine and connecting from another machine
    # (e.g. through an SSH tunnel). The server has no authentication of its
    # own - only bind beyond localhost on a trusted network.
    _HTTP_TRANSPORTS = {"http", "streamable-http", "streamable_http"}
    _STDIO_TRANSPORTS = {"stdio", ""}
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()

    if transport in _HTTP_TRANSPORTS:
        raw_port = os.environ.get("MCP_PORT", "8321")
        try:
            port = int(raw_port)
            if not (0 < port < 65536):
                raise ValueError
        except ValueError:
            sys.exit(f"MCP_PORT must be an integer in 1..65535, got {raw_port!r}")
        mcp.settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
        mcp.settings.port = port
        print(f"Transport: streamable-http on {mcp.settings.host}:{mcp.settings.port}")
        mcp.run(transport="streamable-http")
    elif transport in _STDIO_TRANSPORTS:
        mcp.run()
    else:
        sys.exit(
            f"Unknown MCP_TRANSPORT {transport!r}. "
            f"Use 'stdio' (default) or 'http'/'streamable-http'."
        )
