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
  EPLAN.exe ourselves and discover listening ports owned by that process.
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
from process_control import connected_process
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

    Only a unique listening port owned by the launched process is accepted.
    If startup hands off to another process, identity is not inferred: this
    returns an error instead of connecting to an unrelated instance.

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

    # Only the process we launched may supply the remoting endpoint. A PID
    # that merely appeared since our initial snapshot could belong to a human
    # launching another instance concurrently.
    deadline = time.monotonic() + max(10, wait_seconds)
    servers = []
    fallback_ports = []
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        fallback_ports = _eplan_listening_ports(only_pids={proc.pid})
        if len(fallback_ports) == 1:
            break
        time.sleep(3)

    port = fallback_ports[0] if len(fallback_ports) == 1 else None
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
        result["error"] = (
            f"EPLAN process (pid {proc.pid}) started, but no unambiguous remoting "
            f"port owned by that process was found within {wait_seconds}s "
            "(or the process exited). Refusing to connect to another instance. "
            "Check that 'Allow remote access via Remote Client' is enabled."
        )
        return result

    # Recheck ownership and liveness immediately before connecting.
    if proc.poll() is not None or port not in _eplan_listening_ports(only_pids={proc.pid}):
        result.update(success=False, error="Launched process exited or its port changed before connection.")
        return result

    if connect_after:
        result["connect"] = manager.connect(port=port)
        result["success"] = result["connect"].get("success", False)
        if result["success"] and (
            proc.poll() is not None or port not in _eplan_listening_ports(only_pids={proc.pid})
        ):
            manager.disconnect()
            result.update(success=False, error="Launched process exited or port ownership changed during connection.")
    return result


def app_shutdown(force: bool = False, wait_seconds: int = 60) -> dict:
    """
    Stop the connected EPLAN instance via Remote Client StopEplan().

    IMPORTANT: This exits the whole EPLAN application. Unsaved project data
    is EPLAN's to handle (projects save continuously, but open dialogs or
    edits-in-progress can be lost). Never call this on a machine where a
    human is actively working in EPLAN without their explicit go-ahead.

    Success requires verified process exit. If local process identity cannot
    be established (including remote connections), StopEplan is still requested,
    but exit_verified and success are False and force cannot terminate anything.

    Args:
        force: If True and StopEplan() fails or the process lingers past
            wait_seconds, terminate only the identified connected local process as a last resort.
            Default False - never kills.
        wait_seconds: How long to wait for the process to exit (default 60).
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    # Acquire a Windows process handle before StopEplan. Waiting/termination
    # use this handle, so PID reuse can never redirect them to another process.
    target = None
    identity_error = None
    try:
        target = connected_process(manager.host, manager.port, allow_terminate=force)
    except Exception as exc:
        identity_error = str(exc)

    pids_before = _eplan_pids()  # informational only; never evidence of exit
    result = {
        "success": False,
        "target_pid": target.pid if target else None,
        "exit_verified": False,
        "stop_eplan_returned": False,
        "pids_before": pids_before,
        "force_killed": [],
    }
    try:
        try:
            result["stop_eplan_returned"] = bool(manager.client.StopEplan())
        except Exception as exc:
            result["stop_eplan_error"] = str(exc)
        finally:
            try:
                manager.disconnect()
            except Exception:
                pass

        if target is None:
            result["error"] = (
                "Shutdown requested but process exit could not be verified; "
                "forced termination is disabled. " + (identity_error or "Unknown target.")
            )
        else:
            try:
                exited = target.wait(max(0, wait_seconds))
                if not exited and force:
                    target.terminate()
                    exited = target.wait(15)
                    if exited:
                        result["force_killed"] = [target.pid]
                result["exit_verified"] = exited
                result["success"] = exited
                if not exited:
                    result["error"] = f"Target EPLAN process {target.pid} has not exited."
            except Exception as exc:
                result["error"] = f"Could not verify or terminate target process: {exc}"
    finally:
        if target is not None:
            target.close()

    remaining = _eplan_pids()
    result["pids_still_running"] = remaining
    result["other_instances"] = [pid for pid in remaining if pid != result["target_pid"]] if target else None
    result["note"] = "Other EPLAN instances are left untouched. Process lists are best-effort diagnostics."
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
                "error": "Target process exit was not verified; not relaunching."}

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
