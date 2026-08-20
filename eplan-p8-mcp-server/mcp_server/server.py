"""
EPLAN MCP Server

Complete EPLAN automation server exposing all API actions via MCP protocol.
Every action runs inside a C# script under QuietMode (no blocking dialogs).

Requirements:
- EPLAN installed
- pip install pythonnet mcp
"""

import json
import os
import sys
import functools
from mcp.server.fastmcp import FastMCP
from eplan_connection import get_manager, detect_installed_versions

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

mcp = FastMCP("EPLAN MCP Server")

# Add API folders to path for imports
sys.path.insert(0, os.path.join(SCRIPT_DIR, "api"))

# Import the actions package (QuietMode execution)
import api.actions as eplan_actions


# ============================================================================
# CONNECTION MANAGEMENT (Shared / Version-Agnostic)
# ============================================================================

@mcp.tool()
def eplan_status() -> str:
    """Get the current EPLAN connection status."""
    manager = get_manager()
    return json.dumps(manager.get_status(), indent=2)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def eplan_disconnect() -> str:
    """Disconnect from EPLAN."""
    manager = get_manager()
    return json.dumps(manager.disconnect(), indent=2)


@mcp.tool()
def eplan_ping() -> str:
    """Check if EPLAN is responding."""
    manager = get_manager()
    return json.dumps(manager.ping(), indent=2)


@mcp.tool()
def eplan_test() -> str:
    """
    Show a MessageBox in EPLAN to verify the connection is working.
    Creates and executes a temporary C# script.
    """
    manager = get_manager()

    if not manager.connected:
        return json.dumps({
            "success": False,
            "message": "Not connected. Call eplan_connect() first."
        }, indent=2)

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

    # Execute only - a [Start]-only script needs no RegisterScript, which would
    # pop "The script does not contain attributes for loading." (see scripted.py).
    result = manager.execute_action(f'ExecuteScript /ScriptFile:"{script_path}"')

    return json.dumps({
        "success": result.get("success", False),
        "message": "Check EPLAN for MessageBox" if result.get("success") else result.get("message")
    }, indent=2)




# ============================================================================
# DYNAMIC ACTIONS REGISTRATION
# ============================================================================

def register_actions(actions_module, prefix="eplan_"):
    """
    Dynamically registers all actions exported by the actions module.
    Wraps the functions to return formatted JSON.
    """
    for func_name in actions_module.__all__:
        if func_name.startswith('_'):
            continue

        func = getattr(actions_module, func_name)
        if not callable(func):
            continue

        tool_name = f"{prefix}{func_name}"

        def make_wrapper(f):
            @functools.wraps(f)
            def mcp_tool_wrapper(*args, **kwargs):
                try:
                    res = f(*args, **kwargs)
                    return json.dumps(res, indent=2, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({"success": False, "error": str(e)}, indent=2)

            mcp_tool_wrapper.__doc__ = f.__doc__ or ""
            return mcp_tool_wrapper

        wrapped_tool = make_wrapper(func)
        mcp.tool(name=tool_name)(wrapped_tool)


# Register all actions (executed inside a C# script under QuietMode)
register_actions(eplan_actions)

# AAS (Asset Administration Shell) tools - optional, needs basyx-python-sdk.
# Only a missing basyx dependency is treated as "optional"; any other import
# error inside the package is a real bug and must surface loudly rather than
# silently dropping the aas_* tools.
try:
    import api.aas as aas_tools
    register_actions(aas_tools, prefix="aas_")
except ModuleNotFoundError as e:
    if e.name and e.name.split(".")[0] in ("basyx", "aas"):
        print("AAS tools disabled: basyx-python-sdk not installed "
              "(pip install basyx-python-sdk).", file=sys.stderr)
    else:
        raise


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
