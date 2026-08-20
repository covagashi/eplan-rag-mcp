"""
EPLAN Connection Manager
Connection to EPLAN via Remoting API (pythonnet/CLR)

Requirements:
- EPLAN installed
- pip install pythonnet
"""

import sys
import os
import logging
import re
import json
import time
import uuid
from typing import Optional, List

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("EPLAN")


def cs_escape(value) -> str:
    """Escape a value for safe embedding inside a C# regular string literal ("...").

    Handles backslash, double-quote, newlines/tabs and other control
    characters. This is the single defense against C# injection and against
    uncompilable scripts when action parameters, part numbers, setting
    paths, or supplier-supplied values contain quotes/backslashes/newlines.
    Returns the inner content only (no surrounding quotes).
    """
    if value is None:
        return ""
    out = []
    for ch in str(value):
        codepoint = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif codepoint < 0x20 or codepoint in (0x85, 0x2028, 0x2029):
            # Control chars, plus NEL/LS/PS which C# treats as line
            # terminators even inside a regular string literal.
            out.append("\\u%04x" % codepoint)
        else:
            out.append(ch)
    return "".join(out)

# Root folder of EPLAN installations. Override with the EPLAN_PLATFORM_ROOT
# environment variable for non-standard install locations.
PLATFORM_ROOT = os.environ.get("EPLAN_PLATFORM_ROOT", r"C:\Program Files\EPLAN\Platform")


def _version_key(name: str):
    return tuple(int(p) for p in name.split(".") if p.isdigit())


def detect_installed_versions() -> list:
    """Detect EPLAN installations under PLATFORM_ROOT.

    Returns a list of dicts sorted newest-first, one per major version:
    [{"version": "2027", "full_version": "2027.0.3", "bin": "...", "runtime": "coreclr"}]

    runtime: "coreclr" for .NET 8 builds (EPLAN 2027+, detected via the
    .NET 8-only Grpc.Net.Client.dll), "netfx" for 2026 and older.
    """
    installs = {}
    try:
        for name in os.listdir(PLATFORM_ROOT):
            bin_dir = os.path.join(PLATFORM_ROOT, name, "Bin")
            if not os.path.exists(os.path.join(bin_dir, "Eplan.EplApi.RemoteClientu.dll")):
                continue
            major = name.split(".")[0]
            runtime = "coreclr" if os.path.exists(os.path.join(bin_dir, "Grpc.Net.Client.dll")) else "netfx"
            entry = {"version": major, "full_version": name, "bin": bin_dir, "runtime": runtime}
            if major not in installs or _version_key(name) > _version_key(installs[major]["full_version"]):
                installs[major] = entry
    except FileNotFoundError:
        pass
    return sorted(installs.values(), key=lambda e: _version_key(e["full_version"]), reverse=True)


def _select_dotnet_runtime(runtime: str) -> None:
    """Select the pythonnet runtime. Must run before the first `import clr`
    and can only happen once per process — switching afterwards requires
    restarting the MCP server."""
    try:
        from pythonnet import load as _pnet_load
        if runtime == "coreclr":
            _pnet_load("coreclr")
            logger.info("pythonnet: coreclr (.NET 8) runtime selected (EPLAN 2027+)")
        else:
            logger.info("pythonnet: using default runtime (EPLAN 2026 or older)")
    except Exception as _pnet_err:
        logger.warning(f"pythonnet: runtime selection failed ({_pnet_err})")


class EPLANConnectionManager:
    """Manages the connection to EPLAN via Remote Client API."""

    DEFAULT_PORT = "49152"
    DEFAULT_HOST = "localhost"
    TIMEOUT_SECONDS = 10

    def __init__(self, target_version: str = None):
        # target_version: EPLAN major version like "2026".
        # None = auto-detect (newest installed version).
        self.target_version = str(target_version) if target_version else None
        self.client = None
        self.connected = False
        self.port = self.DEFAULT_PORT
        self.last_error = ""
        # Lazy: DLLs load on first use so tools like eplan_versions can run
        # without committing this process to one version's .NET runtime.
        self._clr_initialized = False

    def _setup_api(self) -> bool:
        """Load EPLAN DLLs via pythonnet."""
        try:
            installs = detect_installed_versions()
            if not installs:
                self.last_error = f"No EPLAN installation found under {PLATFORM_ROOT}"
                logger.error(self.last_error)
                return False

            if self.target_version:
                chosen = next((i for i in installs if i["version"] == self.target_version), None)
                if chosen is None:
                    available = ", ".join(i["version"] for i in installs)
                    self.last_error = f"EPLAN {self.target_version} not installed (available: {available})"
                    logger.error(self.last_error)
                    return False
            else:
                chosen = installs[0]
                logger.info(f"Auto-detected EPLAN {chosen['full_version']} (newest installed)")

            self.target_version = chosen["version"]
            _select_dotnet_runtime(chosen["runtime"])

            import clr

            eplan_path = chosen["bin"]

            if eplan_path not in sys.path:
                sys.path.append(eplan_path)

            # Add additional dependency paths
            dep_paths = [
                r"C:\Program Files\EPLAN\Common\IdentityClient",
                os.path.join(os.path.dirname(eplan_path), "Bin"),
            ]
            for dp in dep_paths:
                if os.path.exists(dp) and dp not in sys.path:
                    sys.path.append(dp)

            # Load EPLAN DLLs via LoadFrom so .NET probes the EPLAN Bin directory
            # for dependencies (e.g. Grpc.Core), preventing version conflicts with
            # any system-wide or Python-env assembly of the same name.
            import System.Reflection
            import System

            def _resolve_from_eplan(sender, args):
                asm_name = System.Reflection.AssemblyName(args.Name).Name
                candidate = os.path.join(eplan_path, asm_name + ".dll")
                if os.path.exists(candidate):
                    return System.Reflection.Assembly.LoadFrom(candidate)
                return None

            System.AppDomain.CurrentDomain.AssemblyResolve += _resolve_from_eplan

            for dll in ("Eplan.EplApi.Starteru.dll", "Eplan.EplApi.RemoteClientu.dll", "Eplan.EplApi.Remotingu.dll"):
                dll_path = os.path.join(eplan_path, dll)
                if os.path.exists(dll_path):
                    System.Reflection.Assembly.LoadFrom(dll_path)

            clr.AddReference("Eplan.EplApi.Starteru")
            clr.AddReference("Eplan.EplApi.RemoteClientu")
            clr.AddReference("Eplan.EplApi.Remotingu")

            logger.info(f"EPLAN API loaded from: {eplan_path}")
            return True

        except ImportError:
            self.last_error = "pythonnet not installed. Run: pip install pythonnet"
            logger.error(self.last_error)
            return False
        except Exception as e:
            self.last_error = f"Failed to load EPLAN API: {e}"
            logger.error(self.last_error)
            return False

    def get_active_servers(self) -> list:
        """Get active EPLAN servers on the local machine."""
        if not self._clr_initialized:
            self._clr_initialized = self._setup_api()
        if not self._clr_initialized:
            return []

        try:
            from Eplan.EplApi.RemoteClient import EplanRemoteClient, EplanServerData
            from System.Collections.Generic import List as NetList

            temp = EplanRemoteClient()
            # out parameter in pythonnet
            servers = NetList[EplanServerData]()
            temp.GetActiveEplanServersOnLocalMachine(servers)

            result = []
            for s in servers:
                result.append({
                    "version": str(s.EplanVersion),
                    "variant": str(s.EplanVariant),
                    "port": str(s.ServerPort)
                })
                logger.info(f"Found: EPLAN {s.EplanVersion} on port {s.ServerPort}")

            temp.Dispose()
            return result

        except Exception as e:
            self.last_error = f"Error getting servers: {e}"
            logger.error(self.last_error)
            return []

    def connect(self, host: str = None, port: str = None) -> dict:
        """Connect to an EPLAN instance."""
        if not self._clr_initialized:
            self._clr_initialized = self._setup_api()
        if not self._clr_initialized:
            return {"success": False, "message": self.last_error}

        host = host or self.DEFAULT_HOST

        try:
            from Eplan.EplApi.RemoteClient import EplanRemoteClient
            import System

            # Auto-detect port if not specified
            if not port:
                servers = self.get_active_servers()
                if servers:
                    port = servers[-1]["port"]
                    logger.info(f"Auto-detected port: {port}")
                else:
                    port = self.DEFAULT_PORT

            self.port = port
            logger.info(f"Connecting to {host}:{port}...")

            self.client = EplanRemoteClient()
            timeout = System.TimeSpan.FromSeconds(self.TIMEOUT_SECONDS)
            self.client.Connect(host, port, timeout)

            if self.client.Ping():
                self.connected = True
                logger.info(f"Connected to EPLAN at {host}:{port}")
                return {
                    "success": True,
                    "message": f"Connected to EPLAN at {host}:{port}",
                    "port": port
                }
            else:
                return {"success": False, "message": "Connected but ping failed"}

        except Exception as e:
            self.last_error = f"Connection failed: {e}"
            logger.error(self.last_error)
            self.connected = False
            return {"success": False, "message": self.last_error}

    def ping(self) -> dict:
        """Check if EPLAN is responding."""
        if not self.connected or not self.client:
            return {"alive": False, "message": "Not connected"}

        try:
            alive = self.client.Ping()
            return {
                "alive": alive,
                "message": "EPLAN responding" if alive else "No response"
            }
        except Exception as e:
            self.connected = False
            return {"alive": False, "message": f"Ping failed: {e}"}

    def _log_action(self, action: str, result: dict, started: float) -> None:
        """Append one JSON line per executed action to logs/actions.jsonl.

        Persistent trace of what the LLM did in EPLAN (Audit/TODO.md item 2):
        survives the conversation and lets failures be correlated with what
        the user saw on screen. Never raises.
        """
        try:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(log_dir, exist_ok=True)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "action": action,
                "duration_s": round(time.time() - started, 3),
                "success": result.get("success"),
            }
            for key in ("executor", "error", "errorType", "eplanMessages", "message"):
                if result.get(key):
                    entry[key] = result[key]
            with open(os.path.join(log_dir, "actions.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def execute_action(self, action: str, quiet_mode: bool = False) -> dict:
        """
        Execute an EPLAN action.

        Args:
            action: The action string to execute
            quiet_mode: If True, suppresses all EPLAN dialogs during execution using a C# script.
        """
        if not self.connected or not self.client:
            return {"success": False, "message": "Not connected"}

        started = time.time()
        try:
            # Parse the action name (first word before any space or '/')
            action_name_match = re.match(r'^([^\s/]+)', action)
            action_name = action_name_match.group(1) if action_name_match else action
            action_name_lower = action_name.lower()

            # RegisterScript, ExecuteScript, and UnregisterScript MUST run directly
            # to avoid infinite recursion. Also run directly if quiet_mode is False.
            if action_name_lower in ("registerscript", "executescript", "unregisterscript") or not quiet_mode:
                logger.info(f"Executing directly: {action}")
                self.client.SynchronousMode = True
                self.client.ExecuteAction(action)
                result = {"success": True, "message": f"Executed directly: {action}", "action": action}
                # Script plumbing (register/execute/unregister) is logged only
                # as part of the wrapped action, not as separate entries.
                if action_name_lower not in ("registerscript", "executescript", "unregisterscript"):
                    self._log_action(action, result, started)
                return result

            # Parse parameters
            params = {}
            matches = re.finditer(r'/([a-zA-Z0-9_]+):(?:("([^"]*)"|([^\s]*)))', action)
            for m in matches:
                key = m.group(1)
                val = m.group(3) if m.group(2).startswith('"') else m.group(4)
                params[key] = val

            # Generate directories
            base_dir = os.path.dirname(os.path.abspath(__file__))
            script_dir = os.path.join(base_dir, "scripts", "generated")
            results_dir = os.path.join(base_dir, "scripts", "results")
            os.makedirs(script_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)

            exec_id = str(uuid.uuid4())[:8]
            script_path = os.path.join(script_dir, f"exec_action_{exec_id}.cs")
            result_path = os.path.join(results_dir, f"exec_result_{exec_id}.json")

            # C# parameters generation. Keys are constrained to
            # [a-zA-Z0-9_]+ by the parse regex above; values are cs_escape'd
            # to prevent injection / uncompilable scripts.
            acc_parameters_code = ""
            check_keys = ["PROJECT", "PROJECTS", "PAGES", "LAYOUTSPACES", "PropertyValue", "value", "Value", "Result", "Output", "Success", "Count", "Error", "Message"]
            for key, val in params.items():
                acc_parameters_code += f'\n                acc.AddParameter("{key}", "{cs_escape(val)}");'
                if key not in check_keys:
                    check_keys.append(key)

            check_keys_code = ", ".join([f'"{k}"' for k in check_keys])
            escaped_result_path = result_path.replace("\\", "\\\\")
            escaped_action_name = cs_escape(action_name)

            # Escape hatch: EPLAN_MCP_LEGACY_CLI=1 emits the original
            # CommandLineInterpreter-only template (no FindAction, no message
            # capture) as a known-good fallback in case the enhanced template
            # fails to compile on some EPLAN version.
            if os.environ.get("EPLAN_MCP_LEGACY_CLI") == "1":
                script_content = self._legacy_script_content(
                    exec_id, acc_parameters_code, check_keys_code,
                    escaped_result_path, escaped_action_name,
                )
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(script_content)
                return self._run_generated_script(action, script_path, result_path, started)

            # C# Script Content.
            # Executor strategy (Audit/TODO.md item 1): resolve the action via
            # ActionManager.FindAction and run Action.Execute, which lets real
            # EPLAN exceptions propagate to our catch block — unlike
            # CommandLineInterpreter.Execute, which swallows them and returns
            # only false. CLI remains as fallback for unresolvable actions.
            # A message-tree bookmark taken before execution captures the
            # warnings/errors EPLAN emitted during the call even when no
            # exception is thrown (covers unreliable success:false results).
            script_content = f"""using System;
using System.IO;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.Base;
using Eplan.EplApi.Scripting;

public class QuietExecute_{exec_id}
{{
    private static string ExceptionChain(Exception ex)
    {{
        var parts = new List<string>();
        while (ex != null)
        {{
            parts.Add(ex.Message);
            ex = ex.InnerException;
        }}
        return string.Join(" <- ", parts);
    }}

    [Start]
    public void Run()
    {{
        var results = new Dictionary<string, object>();
        int bookmark = 0;
        try
        {{
            using (var marker = new BaseException("MCP bookmark", MessageLevel.Message))
            {{
                bookmark = marker.GetBookmarkID();
            }}
        }}
        catch {{}}
        try
        {{
            using (var qm = new QuietModeStep(QuietModes.ShowNoDialogs))
            {{
                var acc = new ActionCallingContext();
                {acc_parameters_code}

                bool success;
                Eplan.EplApi.ApplicationFramework.Action eplanAction = null;
                try {{ eplanAction = new ActionManager().FindAction("{escaped_action_name}"); }}
                catch {{}}
                if (eplanAction != null)
                {{
                    results["executor"] = "action";
                    success = eplanAction.Execute(acc);
                }}
                else
                {{
                    results["executor"] = "cli-fallback";
                    var cli = new CommandLineInterpreter();
                    success = cli.Execute("{escaped_action_name}", acc);
                }}
                results["success"] = success;

                var returnParams = new Dictionary<string, string>();
                string[] checkKeys = new string[] {{ {check_keys_code} }};
                foreach (var key in checkKeys)
                {{
                    try
                    {{
                        string val = "";
                        acc.GetParameter(key, ref val);
                        if (!string.IsNullOrEmpty(val))
                        {{
                            returnParams[key] = val;
                        }}
                    }}
                    catch {{}}
                }}
                results["parameters"] = returnParams;
            }}
        }}
        catch (Exception ex)
        {{
            results["success"] = false;
            results["error"] = ExceptionChain(ex);
            results["errorType"] = ex.GetType().FullName;
        }}

        // Collect system messages emitted during this action (bookmark slice
        // only - never the whole historical tree).
        if (bookmark > 0)
        {{
            try
            {{
                var msgs = new List<string>();
                var col = new SysMessagesCollection(bookmark, MessageLevel.Message);
                var it = col.GetSysMsgEnumerator();
                int guard = 0;
                while (it.MoveNext() && guard++ < 20)
                {{
                    // SysMessagesEnumerator.Current is typed object - it must
                    // be cast before .Message is reachable, or the generated
                    // script fails to compile and every action breaks.
                    var m = it.Current as BaseException;
                    if (m != null && !string.IsNullOrEmpty(m.Message) && m.Message != "MCP bookmark")
                    {{
                        msgs.Add(m.Message);
                    }}
                }}
                if (msgs.Count > 0)
                {{
                    results["eplanMessages"] = msgs;
                }}
            }}
            catch {{}}
        }}

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results);
        File.WriteAllText("{escaped_result_path}", json);
    }}
}}
"""
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_content)
            return self._run_generated_script(action, script_path, result_path, started)

        except Exception as e:
            self.last_error = f"Scripted execution failed: {e}"
            logger.error(self.last_error)
            result = {"success": False, "message": self.last_error, "action": action}
            self._log_action(action, result, started)
            return result

    def _legacy_script_content(self, exec_id, acc_parameters_code, check_keys_code,
                               escaped_result_path, escaped_action_name) -> str:
        """Original CommandLineInterpreter-only template (EPLAN_MCP_LEGACY_CLI=1).

        Known-good fallback: no FindAction, no message-tree capture, so it
        cannot be broken by a message-API mismatch on some EPLAN version.
        It swallows EPLAN exceptions (returns only a bool) - the trade-off
        the escape hatch accepts for maximum compatibility.
        """
        return f"""using System;
using System.IO;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.Scripting;

public class QuietExecute_{exec_id}
{{
    [Start]
    public void Run()
    {{
        var results = new Dictionary<string, object>();
        try
        {{
            using (var qm = new QuietModeStep(QuietModes.ShowNoDialogs))
            {{
                var acc = new ActionCallingContext();
                {acc_parameters_code}

                results["executor"] = "cli-legacy";
                var cli = new CommandLineInterpreter();
                bool success = cli.Execute("{escaped_action_name}", acc);
                results["success"] = success;

                var returnParams = new Dictionary<string, string>();
                string[] checkKeys = new string[] {{ {check_keys_code} }};
                foreach (var key in checkKeys)
                {{
                    try
                    {{
                        string val = "";
                        acc.GetParameter(key, ref val);
                        if (!string.IsNullOrEmpty(val))
                        {{
                            returnParams[key] = val;
                        }}
                    }}
                    catch {{}}
                }}
                results["parameters"] = returnParams;
            }}
        }}
        catch (Exception ex)
        {{
            results["success"] = false;
            results["error"] = ex.Message;
        }}

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results);
        File.WriteAllText("{escaped_result_path}", json);
    }}
}}
"""

    def _run_generated_script(self, action, script_path, result_path, started) -> dict:
        """Register, execute, await the result file, and clean up a generated script."""
        try:
            logger.info(f"Wrapping action via script: {action} (script={os.path.basename(script_path)})")
            # Execute only - deliberately NOT RegisterScript first. The wrapper
            # script generated above has only a [Start] method; RegisterScript is
            # for persistent [DeclareAction]/[DeclareEventHandler]/[DeclareMenu]
            # hooks and rejects a [Start]-only script with a modal
            # "The script does not contain attributes for loading." dialog - a
            # popup per call, for nothing. ExecuteScript runs [Start] on its own.
            exec_result = self.execute_action(f'ExecuteScript /ScriptFile:"{script_path}"', quiet_mode=False)
            if not exec_result.get("success"):
                result = {"success": False, "message": f"Failed to execute action via script: {exec_result.get('message')}"}
                self._log_action(action, result, started)
                return result

            # Wait for result file
            timeout = 30.0
            start_time = time.time()
            while not os.path.exists(result_path):
                if time.time() - start_time > timeout:
                    result = {"success": False, "message": "Timeout waiting for scripted action execution result"}
                    self._log_action(action, result, started)
                    return result
                time.sleep(0.1)

            # Read results, tolerating a partially-written file (the C# writer
            # is not atomic vs our existence probe).
            res_data = None
            for _ in range(10):
                time.sleep(0.05)
                try:
                    with open(result_path, "r", encoding="utf-8") as f:
                        res_data = json.load(f)
                    break
                except (json.JSONDecodeError, ValueError):
                    continue
            if res_data is None:
                result = {"success": False, "message": "Could not parse action result file"}
                self._log_action(action, result, started)
                return result

            self._log_action(action, res_data, started)
            return res_data

        except Exception as e:
            self.last_error = f"Scripted execution failed: {e}"
            logger.error(self.last_error)
            result = {"success": False, "message": self.last_error, "action": action}
            self._log_action(action, result, started)
            return result

        finally:
            try:
                if os.path.exists(script_path):
                    # No UnregisterScript - nothing was registered (see above).
                    os.remove(script_path)
            except Exception:
                pass
            try:
                if os.path.exists(result_path):
                    os.remove(result_path)
            except Exception:
                pass

    def disconnect(self) -> dict:
        """Disconnect from EPLAN."""
        try:
            if self.client:
                self.client.Disconnect()
                self.client.Dispose()
                self.client = None
            self.connected = False
            logger.info("Disconnected")
            return {"success": True, "message": "Disconnected"}
        except Exception as e:
            return {"success": False, "message": f"Disconnect failed: {e}"}

    def get_status(self) -> dict:
        """Get current connection status."""
        return {
            "connected": self.connected,
            "api_loaded": self._clr_initialized,
            "target_version": self.target_version,
            "port": self.port if self.connected else None,
            "last_error": self.last_error
        }


# Singleton
_manager: Optional[EPLANConnectionManager] = None


def get_manager(target_version: str = None) -> EPLANConnectionManager:
    """Return the singleton connection manager.

    target_version: EPLAN major version like "2026". None = auto (newest
    installed). Once the DLLs of one version are loaded into this process,
    switching versions requires restarting the MCP server; a mismatching
    request keeps the loaded version and logs a warning.
    """
    global _manager
    if _manager is None:
        _manager = EPLANConnectionManager(target_version)
    elif target_version and str(target_version) != _manager.target_version:
        if _manager._clr_initialized:
            logger.warning(
                f"EPLAN {_manager.target_version} DLLs already loaded; "
                f"restart the MCP server to target {target_version}"
            )
        else:
            _manager = EPLANConnectionManager(target_version)
    return _manager
