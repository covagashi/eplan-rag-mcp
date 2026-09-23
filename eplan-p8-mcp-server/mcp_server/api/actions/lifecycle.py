"""
EPLAN process lifecycle - launch, shutdown, and restart the EPLAN application.

These tools exist for unattended develop-deploy-test loops: an add-in DLL
loaded by EPLAN cannot be replaced on disk until EPLAN exits, so redeploying
a rebuilt add-in requires a full application restart. With these tools an
agent can do the whole cycle (close project, exit EPLAN, swap the DLL,
relaunch, reconnect, reopen the project) without a human at the keyboard.

Implementation notes (verified against the P8 docs RAG and the
eplan-development skill's remoting reference):
- EplanRemoteClient.StartEplan() relies on the removed EplanServer action on
  EPLAN 2025+ and does NOT work there. The reliable way is to launch
  EPLAN.exe ourselves and poll GetActiveEplanServersOnLocalMachine until the
  remoting server appears.
- EplanRemoteClient.StopEplan() stops the connected instance; a process kill
  is available only as an explicit opt-in fallback (force=True).
- "Allow remote access via Remote Client" must be enabled in the EPLAN
  workstation settings or the relaunched instance never opens a remoting
  port and app_launch times out.
"""

import os
import subprocess
import time

from ._base import _get_connected_manager, _quote_param
from eplan_connection import (
    get_manager,
    detect_installed_versions,
    eplan_pids as _eplan_pids,
    eplan_listening_ports as _eplan_listening_ports,
    EPLAN_EXE_NAME,
)

DEFAULT_VARIANT = "Electric P8"


def _find_eplan_exe(version: str = None) -> tuple:
    """Resolve the EPLAN.exe path for a version ("2026") or the newest install.

    Returns (exe_path, resolved_version) or (None, error_dict).
    """
    installs = detect_installed_versions()
    if not installs:
        return None, {"success": False, "error": "No EPLAN installation detected."}
    if version:
        chosen = next((i for i in installs if i["version"] == str(version)), None)
        if chosen is None:
            available = ", ".join(i["version"] for i in installs)
            return None, {"success": False,
                          "error": f"EPLAN {version} not installed (available: {available})."}
    else:
        chosen = installs[0]
    exe = os.path.join(chosen["bin"], EPLAN_EXE_NAME)
    if not os.path.exists(exe):
        return None, {"success": False, "error": f"Executable not found: {exe}"}
    return (exe, chosen["version"]), None




def app_launch(version: str = None, variant: str = None, headless: bool = False,
                 wait_seconds: int = 600, extra_args: str = None,
                 connect_after: bool = True) -> dict:
    """
    Launch EPLAN and wait until its remoting server accepts connections.

    Use this (optionally after app_shutdown) to bring EPLAN up unattended,
    e.g. in a build-deploy-test loop. EPLAN startup routinely takes 1-3
    minutes; the call blocks while polling for the remoting port.

    Requires "Allow remote access via Remote Client" to be enabled in the
    EPLAN workstation settings, otherwise no remoting port ever opens and
    this times out even though EPLAN itself started fine.

    Args:
        version: EPLAN major version, e.g. "2026". Omit for the newest
            installed. Must match the version whose DLLs this server has
            loaded (if any) or reconnection will fail.
        variant: EPLAN variant to start, default "Electric P8".
        headless: If True, starts with /Frame:0 (no visible main window).
            Useful for CI-style runs; leave False when a human also wants to
            watch the GUI.
        wait_seconds: How long to poll for the remoting server (default
            600). A cold GUI start that restores the workspace and reopens a
            network project can take 3-4 minutes before the port opens
            (measured live 2026-08-20: ~3.5 min).
        extra_args: Additional raw command-line arguments appended verbatim.
        connect_after: Connect this MCP server to the new instance once the
            remoting port appears (default True).
    """
    found, error = _find_eplan_exe(version)
    if error:
        return error
    exe, resolved_version = found

    manager = get_manager()
    if manager.connected and manager.ping().get("alive"):
        return {"success": False,
                "error": "Already connected to a running EPLAN. Use app_restart "
                         "to recycle it, or app_shutdown first."}

    args = [f'"{exe}"', _quote_param("Variant", variant or DEFAULT_VARIANT), "/NoSplash"]
    if headless:
        # /Quiet (batch mode) only for headless runs: on a GUI launch it
        # blocks the workspace panels from restoring and fills the system
        # message tree with "attempt to open dialog ... in batch mode"
        # errors the user then sees (observed live 2026-08-20).
        args.extend(["/Quiet", "/Frame:0"])
    if extra_args:
        args.append(extra_args)
    cmdline = " ".join(args)

    already_running = _eplan_pids()
    try:
        # Detach so EPLAN outlives this MCP server process. Keep the handle -
        # its pid is how a pre-existing instance is told apart below.
        proc = subprocess.Popen(
            cmdline,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as e:
        return {"success": False, "error": f"Failed to start {exe}: {e}"}

    # Poll for the remoting server. Server enumeration is unreliable right
    # after a restart, so netstat-discovered listening ports of EPLAN.exe
    # serve as a fallback signal (verified live: enumeration stayed empty
    # while the port already accepted connections).
    #
    # GetActiveEplanServersOnLocalMachine (manager.get_active_servers()) does
    # not report a PID, so it cannot tell a pre-existing instance's server
    # apart from the one just launched. When nothing else was running before
    # this call, that ambiguity does not exist and the fast path (servers,
    # then unfiltered ports) is used as before. When one or more EPLAN
    # instances already existed, only a listening port OWNED BY A NEW PID
    # (this process's, or any that appeared after launch) is accepted -
    # never the enumerated servers list, and never an old instance's port.
    # Audit #42 item 12: this call used to accept whichever port turned up
    # first, which could be the pre-existing instance's.
    deadline = time.time() + max(10, wait_seconds)
    servers = []
    fallback_ports = []
    new_pids = set()
    while time.time() < deadline:
        time.sleep(3)
        if not already_running:
            servers = manager.get_active_servers()
            if servers:
                break
            fallback_ports = _eplan_listening_ports()
            if fallback_ports:
                break
        else:
            current_pids = _eplan_pids()
            new_pids = set(current_pids) - set(already_running)
            new_pids.add(proc.pid)
            fallback_ports = _eplan_listening_ports(only_pids=new_pids, pids=current_pids)
            if fallback_ports:
                break

    port = servers[-1]["port"] if servers else (fallback_ports[-1] if fallback_ports else None)
    result = {
        "success": port is not None,
        "exe": exe,
        "version": resolved_version,
        "command_line": cmdline,
        "servers": servers,
        "fallback_ports": fallback_ports,
        "eplan_was_already_running": bool(already_running),
        "new_pid": proc.pid,
    }
    if port is None:
        if already_running:
            result["error"] = (
                f"EPLAN process (pid {proc.pid}) started, but no remoting port "
                f"owned by that process (or any new EPLAN process) was found "
                f"within {wait_seconds}s, and {len(already_running)} EPLAN "
                f"instance(s) were already running before this call - refusing "
                f"to connect to one of those instead of the one just launched. "
                f"Check that 'Allow remote access via Remote Client' is enabled "
                f"(File > Settings > Workstation > Interfaces > Remote access)."
            )
        else:
            result["error"] = (
                f"EPLAN process started but no remoting server appeared within "
                f"{wait_seconds}s. Check that 'Allow remote access via Remote Client' "
                f"is enabled (File > Settings > Workstation > Interfaces > Remote access)."
            )
        return result

    if connect_after:
        result["connect"] = manager.connect(port=port)
        result["success"] = result["connect"].get("success", False)
    return result


def app_shutdown(force: bool = False, wait_seconds: int = 60) -> dict:
    """
    Stop the connected EPLAN instance via Remote Client StopEplan().

    IMPORTANT: This exits the whole EPLAN application. Unsaved project data
    is EPLAN's to handle (projects save continuously, but open dialogs or
    edits-in-progress can be lost). Never call this on a machine where a
    human is actively working in EPLAN without their explicit go-ahead.

    Args:
        force: If True and StopEplan() fails or the process lingers past
            wait_seconds, kill EPLAN.exe with taskkill as a last resort.
            Default False - never kills.
        wait_seconds: How long to wait for the process to exit (default 60).
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    pids_before = _eplan_pids()
    stop_ok = False
    stop_error = None
    try:
        stop_ok = bool(manager.client.StopEplan())
    except Exception as e:
        stop_error = str(e)

    # The remote client is stale either way - drop the connection state.
    try:
        manager.disconnect()
    except Exception:
        pass

    # Wait for the process(es) to actually exit so the caller can safely
    # overwrite DLLs afterwards.
    deadline = time.time() + max(5, wait_seconds)
    remaining = _eplan_pids()
    while remaining and time.time() < deadline:
        time.sleep(2)
        remaining = _eplan_pids()

    killed = []
    if remaining and force:
        for pid in remaining:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=15)
                killed.append(pid)
            except Exception:
                pass
        time.sleep(2)
        remaining = _eplan_pids()

    result = {
        "success": not remaining,
        "stop_eplan_returned": stop_ok,
        "pids_before": pids_before,
        "pids_still_running": remaining,
        "force_killed": killed,
    }
    if stop_error:
        result["stop_eplan_error"] = stop_error
    if remaining:
        result["error"] = (f"EPLAN process(es) still running after {wait_seconds}s: "
                           f"{remaining}. Retry with force=True to kill them.")
    return result


def app_restart(reopen_project: bool = True, headless: bool = False,
                  version: str = None, variant: str = None,
                  wait_seconds: int = 600, force: bool = False) -> dict:
    """
    Full EPLAN recycle: remember the focused project, close it, exit EPLAN,
    relaunch, reconnect, and reopen the project.

    This is the workhorse of the add-in develop-deploy-test loop: EPLAN locks
    loaded add-in DLLs, so a rebuilt DLL can only be deployed while EPLAN is
    down. Typical sequence: build -> app_restart(reopen_project=False) with
    the deploy happening between shutdown and launch via your own tooling, or
    simply app_shutdown / deploy / app_launch yourself for finer control.
    app_restart alone (no deploy step) is still useful to pick up
    already-deployed DLLs or recover a wedged instance.

    Only the currently focused project can be detected and reopened - if
    multiple projects are open, the others are closed by the exit and NOT
    reopened.

    Args:
        reopen_project: Reopen the previously focused project after the
            restart (default True).
        headless: Relaunch with no visible main window (default False).
        version: EPLAN major version to relaunch. Omit to keep the current one.
        variant: EPLAN variant, default "Electric P8".
        wait_seconds: Poll budget for the remoting server after relaunch.
        force: Passed to app_shutdown - kill the process if StopEplan
            does not bring it down.
    """
    from .project import get_current_project, open_project

    manager, error = _get_connected_manager()
    if error:
        return error

    steps = {}

    previous_project = None
    if reopen_project:
        cur = get_current_project()
        previous_project = (cur.get("parameters") or {}).get("PROJECT") if cur.get("success") else None
        steps["previous_project"] = previous_project

    steps["shutdown"] = app_shutdown(force=force)
    if not steps["shutdown"].get("success"):
        return {"success": False, "steps": steps,
                "error": "Shutdown failed - EPLAN still running, not relaunching."}

    steps["launch"] = app_launch(version=version, variant=variant, headless=headless,
                                   wait_seconds=wait_seconds, connect_after=True)
    if not steps["launch"].get("success"):
        return {"success": False, "steps": steps,
                "error": "Relaunch failed - see steps.launch for details."}

    if reopen_project and previous_project:
        # EPLAN often reopens the last project by itself at startup ("reopen
        # projects on start" workstation setting) - a second ProjectOpen then
        # fails with "Project is already open" (observed live 2026-08-20).
        cur = get_current_project()
        focused = (cur.get("parameters") or {}).get("PROJECT", "")
        if focused and os.path.normcase(focused) == os.path.normcase(previous_project):
            steps["reopen"] = {"success": True,
                               "message": "Project already reopened by EPLAN on startup."}
        else:
            steps["reopen"] = open_project(previous_project)
            msgs = steps["reopen"].get("eplanMessages") or []
            if any("already open" in str(m).lower() for m in msgs):
                steps["reopen"] = {"success": True,
                                   "message": "Project already open.",
                                   "eplanMessages": msgs}

    ok = all(s.get("success", True) for s in steps.values() if isinstance(s, dict))
    return {"success": ok, "steps": steps}
