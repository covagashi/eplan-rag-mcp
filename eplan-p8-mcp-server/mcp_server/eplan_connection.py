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


EPLAN_EXE_NAME = "EPLAN.exe"

# Severity ranking for the message slice, most important first.
#
# Deliberately a hand-written list rather than MessageLevel's numeric order,
# which is NOT a severity order: Trace=0, Message=1, Warning=2, Assert=3,
# Error=4, FatalError=5, so a naive sort puts Assert - documented as "the
# lowest level of an error, which will not appear in GUI" - above Warning.
#
# Trace and Assert are omitted entirely: the SysMessagesCollection docs state
# neither is ever added to the collection, so listing them would imply a
# guarantee that does not exist. Anything unranked sorts last.
MESSAGE_LEVEL_RANK = ("FatalError", "Error", "Warning", "Message")

# Entries kept in eplanMessages after ranking. Half of all captures were
# already hitting the old cap of 20 (measured over 1,463 logged actions on
# 2026-09-03: 104 of the 206 entries carrying messages had exactly 20, and
# nothing exceeded it), so the cap stays where it is - the fix is that the most
# severe entries survive it, and that truncation is now reported, not that more
# text comes back.
MESSAGE_CAP = 20

# Upper bound on what the generated C# collects before Python ranks it. Guards
# against a pathological action pushing a huge payload through the result file,
# while leaving ranking enough to choose from.
MESSAGE_SCAN_CAP = 500

# How long to wait for a generated script's result file before declaring that
# the action did not run. EPLAN's ExecuteScript is synchronous on this path, so
# a slow action still writes its result before returning - meaning this
# expiring points at the script never having run (usually a compile error),
# not at a slow action. A module constant so tests can shorten it without
# patching the clock: eplan_connection.time is the shared time module, and
# monkeypatching its sleep/time affects pytest itself.
SCRIPT_RESULT_TIMEOUT_S = 30.0

# The action-result contract, declared rather than implied.
#
# Written because not having it cost real money three times in one session: a
# field was added and silently never reached the trace; a docstring pointed the
# model at a field that did not exist; and a guard meant to catch the second
# could not be written because there was no vocabulary to check names against.
#
# Each entry is (name, when_present, meaning). "when_present" is part of the
# contract, not a note: a field that appears unconditionally is a tax on every
# happy-path response, so anything diagnostic must be able to say when it stays
# quiet. Keep this in sync when adding a field - the tests below fail if a
# producer emits something undeclared.
RESULT_FIELDS = (
    # --- always present on a completed action
    ("success", "always", "Whether EPLAN reported the action as succeeding. NOT proof of effect: several actions return true having done nothing, and at least one returned false after a completed overwrite."),
    ("action", "on failures raised by this server", "The action string as sent."),
    ("parameters", "when the action returned any", "Values read back out of the ActionCallingContext."),
    ("executor", "always on the scripted path", 'Which executor ran it: "action" (ActionManager.FindAction + Action.Execute), "cli-fallback" (the action name did not resolve), "cli-legacy" (EPLAN_MCP_LEGACY_CLI=1), or "none" (it never ran).'),

    # --- diagnostics
    ("error", "when a cause is known", "Human-readable cause. Sourced either from the thrown exception chain or from ActionCallingContext.GetException()."),
    ("errorType", "with error, when the cause was an exception", "The .NET type name, e.g. Eplan.EplApi.Base.BaseException."),
    ("errorFrom", "with error, on the scripted path", '"context" if read from GetException() after execution, "throw" if the exception propagated. Provenance matters: errorType appeared in 0 of 1,463 logged actions before the context read landed, so the two channels have to stay distinguishable in the trace.'),
    ("message", "on failures raised by this server", "Server-side explanation, kept verbatim for callers matching on the older strings."),
    ("failedScriptPath", "when a generated script failed", "Where the generated C# was preserved, since the cleanup deletes the original and it is the only evidence of a compile error."),

    # --- EPLAN's own messages
    ("eplanMessages", "when EPLAN emitted any", "EPLAN's own text, severity-ranked then capped at MESSAGE_CAP. A list of strings."),
    ("eplanMessagesTotal", "with eplanMessages", "How many EPLAN produced, from the collection's own Count plus anything only the per-call context supplied."),
    ("eplanMessagesTruncated", "only when entries were dropped", "True when the cap discarded lower-severity entries."),
    ("eplanMessagesLevels", "only when something outranks Message", "Per-entry severity, parallel to eplanMessages."),
    ("eplanMessagesUnbounded", "only when the slice could not be bounded", "True when no end bookmark could be taken, so entries from outside this action may be included and the total is an upper bound."),
    ("eplanMessagesFromContextOnly", "only when the context knew more", "How many messages came from ActionCallingContext.SysMessages but not the bookmark slice. Its presence is the evidence that would justify switching channels."),
)

# Fields the generated C# emits for the Python side to consume. They are
# internal plumbing and must never survive into a response - a leak of exactly
# this kind reached every no-message action before it was caught by running the
# template against live EPLAN.
INTERNAL_RESULT_FIELDS = (
    "eplanMessagesRaw",
    "eplanContextMessagesRaw",
    "eplanMessagesScanned",
    "eplanMessagesTrueTotal",
    "eplanMessagesBounded",
)

RESULT_FIELD_NAMES = tuple(name for name, _, _ in RESULT_FIELDS)

# Result keys copied into each actions.jsonl entry, on top of the always-present
# ts / action / duration_s / success.
#
# Derived from RESULT_FIELDS rather than hand-maintained, which is the whole
# point: this tuple is a FILTER, and a field missing from it never reaches the
# trace and so cannot be measured in a later audit. Deriving it means adding a
# field to the contract cannot forget the trace. "action" and "success" are
# excluded because _log_action writes them itself; "parameters" because the
# trace records intent, not the full echo.
_NOT_LOGGED = ("success", "action", "parameters")
LOGGED_RESULT_KEYS = tuple(n for n in RESULT_FIELD_NAMES if n not in _NOT_LOGGED)


def eplan_pids() -> list:
    """PIDs of running EPLAN.exe processes (empty list on any error)."""
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {EPLAN_EXE_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        pids = []
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.strip().split('","')]
            if len(parts) >= 2 and parts[0].lower() == EPLAN_EXE_NAME.lower():
                pids.append(int(parts[1]))
        return pids
    except Exception:
        return []


def eplan_listening_ports(only_pids=None, pids=None) -> list:
    """TCP ports EPLAN.exe processes are LISTENING on (via netstat).

    Fallback discovery: GetActiveEplanServersOnLocalMachine is unreliable
    right after EPLAN (re)starts - it can return empty while the remoting
    port is already up and accepting connections (observed live 2026-08-20:
    fresh EPLAN 2027 listening on 49153, server enumeration empty). The
    remoting port is dynamic (49152 is only the usual first choice), so
    never assume the default - discover.

    Args:
        only_pids: If given, restrict results to ports owned by one of these
            PIDs (any iterable, coerced to a set). Used by app_launch to tell
            the instance it just started apart from one that was already
            running - see Audit #42 item 12.
        pids: Already-computed result of eplan_pids(). If given, skips the
            internal tasklist spawn.
    """
    import subprocess
    pids = set(eplan_pids() if pids is None else pids)
    if not pids:
        return []
    if only_pids is not None:
        pids &= set(only_pids)
        if not pids:
            return []
    ports = []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            parts = line.split()
            # TCP  0.0.0.0:49153  0.0.0.0:0  LISTENING  45472
            if (len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING"
                    and parts[4].isdigit() and int(parts[4]) in pids):
                port = parts[1].rsplit(":", 1)[-1]
                if port.isdigit() and port not in ports:
                    ports.append(port)
    except Exception:
        pass
    return ports


class EPLANConnectionManager:
    """Manages the connection to EPLAN via Remote Client API."""

    DEFAULT_PORT = "49152"
    DEFAULT_HOST = "localhost"
    TIMEOUT_SECONDS = 10

    # Hosts treated as "this workstation". EPLAN remoting has no authentication,
    # so connecting anywhere else is a trust decision, not a configuration
    # detail: whoever runs that EPLAN sees every project this session touches
    # and every action it runs. Kept as a set so get_status() and the action
    # trace can both label the target.
    LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

    def __init__(self, target_version: str = None):
        # target_version: EPLAN major version like "2026".
        # None = auto-detect (newest installed version).
        self.target_version = str(target_version) if target_version else None
        self.client = None
        self.connected = False
        self.host = self.DEFAULT_HOST
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

            # Auto-detect port(s) if not specified. Server enumeration first;
            # if it comes back empty (it is unreliable right after EPLAN
            # starts), fall back to EPLAN.exe's actual listening ports from
            # netstat, then the historical default as a last resort. Each
            # candidate is tried until one answers a Ping.
            if port:
                candidates = [str(port)]
            else:
                servers = self.get_active_servers()
                if servers:
                    candidates = [servers[-1]["port"]]
                    logger.info(f"Auto-detected port: {candidates[0]}")
                else:
                    candidates = (eplan_listening_ports() if host in ("localhost", "127.0.0.1")
                                  else []) or [self.DEFAULT_PORT]
                    logger.info(f"Server enumeration empty; trying ports: {candidates}")

            timeout = System.TimeSpan.FromSeconds(self.TIMEOUT_SECONDS)
            last_exc = None
            for candidate in candidates:
                # `client` is disposed on EVERY path except the one where it is
                # handed to self.client. Previously Dispose() ran only when
                # Connect succeeded AND Ping returned False, so the ordinary
                # wrong-port case - Connect throwing - dropped a CLR remoting
                # client (and its gRPC channel) undisposed. Across the reconnect
                # retries the docstrings encourage and repeated app_launch
                # cycles, those accumulate for the life of the MCP process.
                client = None
                keep = False
                try:
                    logger.info(f"Connecting to {host}:{candidate}...")
                    client = EplanRemoteClient()
                    client.Connect(host, candidate, timeout)
                    if client.Ping():
                        self.client = client
                        self.host = host
                        self.port = candidate
                        self.connected = True
                        keep = True
                        logger.info(f"Connected to EPLAN at {host}:{candidate}")
                        return {
                            "success": True,
                            "message": f"Connected to EPLAN at {host}:{candidate}",
                            "host": host,
                            "port": candidate
                        }
                except Exception as exc:
                    last_exc = exc
                finally:
                    if client is not None and not keep:
                        try:
                            client.Dispose()
                        except Exception:
                            pass
            if last_exc is not None:
                raise last_exc
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

    @staticmethod
    def _shape_messages(result: dict) -> dict:
        """Turn eplanMessagesRaw into a ranked, capped, honest eplanMessages.

        Replaces a plain "first 20 in tree order" slice. In tree order the 20
        kept were whichever EPLAN happened to log first, which for a chatty
        action means twenty "Started opening database" lines while the actual
        per-project error falls off the end - measured on a real
        upgrade_projects call.

        eplanMessages keeps its existing shape, a list of strings, so this is
        additive rather than a contract break. Added alongside it:
          eplanMessagesTruncated  only when entries were dropped
          eplanMessagesTotal      how many EPLAN actually produced
          eplanMessagesLevels     only when something above Message is present

        The "only when" matters: on the healthy path this adds nothing to the
        payload at all.
        """
        # Drain every internal hint first. The C# emits eplanMessagesBounded
        # and eplanMessagesTrueTotal whenever it took a bookmark, which is on
        # every action - so returning early before popping them leaked two
        # internal fields into the response of every action that produced no
        # messages at all, i.e. most of them. Caught by running the real
        # template against live EPLAN; the offline tests all supplied messages.
        drained = {name: result.pop(name, None)
                   for name in INTERNAL_RESULT_FIELDS}
        raw = drained["eplanMessagesRaw"]
        ctx_raw = drained["eplanContextMessagesRaw"]
        scanned = drained["eplanMessagesScanned"]
        true_total = drained["eplanMessagesTrueTotal"]
        bounded = drained["eplanMessagesBounded"]

        if not isinstance(raw, list):
            raw = []
        if not isinstance(ctx_raw, list):
            ctx_raw = []
        if not raw and not ctx_raw:
            return result

        # Merge the per-call context collection with the bookmark slice,
        # de-duplicating on text. The bookmark slice goes first so its
        # chronology survives; anything only the context knew about is
        # appended and counted, which is how we find out whether that channel
        # is worth switching to. On the two actions measured so far the two
        # sources agreed exactly, so this is expected to add nothing most of
        # the time - eplanMessagesFromContextOnly appearing at all is the
        # signal that the bookmark slice has a gap.
        seen = set()
        merged = []
        for record in raw:
            if not isinstance(record, dict):
                continue
            text = record.get("text")
            if text and text not in seen:
                seen.add(text)
                merged.append(record)
        context_only = 0
        for record in ctx_raw:
            if not isinstance(record, dict):
                continue
            text = record.get("text")
            if text and text not in seen:
                seen.add(text)
                merged.append(record)
                context_only += 1
        raw = merged

        # Prefer the collection's own Count over how many entries we walked.
        # eplanMessagesScanned is bounded by MESSAGE_SCAN_CAP, so using it as
        # the total under-reports whenever the scan cap is what stopped us -
        # which is precisely the case where an accurate total matters.
        total = true_total
        if not isinstance(total, int) or total < 1:
            total = scanned
        if not isinstance(total, int):
            total = len(raw)
        # Count is the bookmark collection's own total and knows nothing about
        # the context collection, so anything only the context supplied has to
        # be added or the total under-reports what we are holding.
        total += context_only

        def rank(record):
            level = (record or {}).get("level") or ""
            try:
                return MESSAGE_LEVEL_RANK.index(level)
            except ValueError:
                return len(MESSAGE_LEVEL_RANK)

        # sorted() is stable, so chronological order is preserved inside each
        # severity bucket - a reader still sees the sequence EPLAN produced.
        ordered = sorted((r for r in raw if isinstance(r, dict)), key=rank)
        kept = ordered[:MESSAGE_CAP]


        texts = [r.get("text", "") for r in kept if r.get("text")]
        if not texts:
            return result

        result["eplanMessages"] = texts
        result["eplanMessagesTotal"] = total
        if total > len(texts):
            result["eplanMessagesTruncated"] = True

        levels = [r.get("level") for r in kept if r.get("level")]
        if any(lvl != "Message" for lvl in levels):
            result["eplanMessagesLevels"] = levels

        # Only surfaced when the slice could NOT be bounded, since that is the
        # case where entries from outside this action may have leaked in and
        # the total is therefore an upper bound rather than a fact.
        if bounded is False:
            result["eplanMessagesUnbounded"] = True
        # Only when the context collection knew something the bookmark slice
        # did not. Absent on the healthy path, and its presence is the
        # evidence that would justify switching channels.
        if context_only:
            result["eplanMessagesFromContextOnly"] = context_only
        return result

    def _log_dir(self) -> str:
        """Directory for actions.jsonl.

        EPLAN_MCP_LOG_DIR overrides the default, matching the EPLAN_MCP_MODE /
        EPLAN_MCP_EXTENSIONS convention. Read per call rather than cached at
        import, so a test fixture can point it at a tmp_path.
        """
        override = os.environ.get("EPLAN_MCP_LOG_DIR")
        if override:
            return override
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

    def _log_action(self, action: str, result: dict, started: float) -> None:
        """Append one JSON line per executed action to actions.jsonl.

        Persistent trace of what the LLM did in EPLAN (Audit/TODO.md item 2):
        survives the conversation and lets failures be correlated with what
        the user saw on screen. Never raises.

        Note that LOGGED_RESULT_KEYS is a filter, not documentation: a
        diagnostic field added to the result but not to that tuple is absent
        from the trace, and so cannot be measured in a later audit.
        """
        try:
            log_dir = self._log_dir()
            os.makedirs(log_dir, exist_ok=True)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                # WHERE the action ran. Without this the trace for an action
                # against a remote, unauthenticated EPLAN is byte-identical to
                # one against localhost, so a reviewer cannot tell the session
                # ever left this workstation. Only recorded, never used to
                # decide anything.
                "host": self.host,
                "port": self.port,
                "action": action,
                "duration_s": round(time.time() - started, 3),
                "success": result.get("success"),
            }
            for key in LOGGED_RESULT_KEYS:
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
            raw_scan_cap = MESSAGE_SCAN_CAP

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

                // The exception behind a silent success:false is already in
                // the context we are holding. The Action docs say exceptions
                // occurring during execution can be retrieved from the
                // ActionCallingContext via GetException(). Measured
                // 2026-09-03 on EPLAN 2027.0.1: after Action.Execute of
                // projectmanagement /TYPE:READPROJECTINFO with PROJECTNAME
                // omitted, this returns EPLAN's own no-file-found text
                // naming the FILENAME parameter, as a BaseException.
                //
                // Read AFTER the single execution and never by re-running the
                // action: success:false does not mean nothing happened
                // (restore_masterdata overwrote a folder and took sibling
                // files with it while reporting false), so harvesting a
                // message by retrying could repeat a side effect.
                try
                {{
                    var ctxEx = acc.GetException();
                    if (ctxEx != null)
                    {{
                        results["error"] = ctxEx.Message;
                        results["errorType"] = ctxEx.GetType().FullName;
                        results["errorFrom"] = "context";
                    }}
                }}
                catch {{}}

                // ActionCallingContext.SysMessages is a per-call collection,
                // scoped by construction rather than by bookmarking the global
                // tree. Measured populated on the Action.Execute path WITHOUT
                // CommandLineInterpreter's bCollectSysMessages flag, which is
                // undocumented. Collected as a second source and merged with
                // the bookmark slice below rather than replacing it: it has
                // been compared on two actions only, so this widens coverage
                // without betting the existing channel on a small sample.
                try
                {{
                    var ctxMsgs = acc.SysMessages;
                    if (ctxMsgs != null)
                    {{
                        var craw = new List<Dictionary<string, string>>();
                        var cit = ctxMsgs.GetSysMsgEnumerator();
                        int cscanned = 0;
                        while (cit.MoveNext() && cscanned < {raw_scan_cap})
                        {{
                            var cm = cit.Current as BaseException;
                            if (cm != null && !string.IsNullOrEmpty(cm.Message))
                            {{
                                var crec = new Dictionary<string, string>();
                                crec["text"] = cm.Message;
                                try {{ crec["level"] = cm.MessageLevel.ToString(); }}
                                catch {{ crec["level"] = ""; }}
                                craw.Add(crec);
                                cscanned++;
                            }}
                        }}
                        if (craw.Count > 0)
                        {{
                            results["eplanContextMessagesRaw"] = craw;
                        }}
                    }}
                }}
                catch {{}}

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
            // Provenance matters for auditing: errorType was absent from all
            // 1,463 logged actions before this change, so when it starts
            // appearing we need to know which channel produced it.
            results["errorFrom"] = "throw";
        }}

        // Close the slice at the top. With only a start bookmark the
        // collection is open-ended, so anything EPLAN emits after this action
        // - including from a later action in the same session - lands in it.
        int bookmarkEnd = 0;
        try
        {{
            using (var endMarker = new BaseException("MCP bookmark end", MessageLevel.Message))
            {{
                bookmarkEnd = endMarker.GetBookmarkID();
            }}
        }}
        catch {{}}

        // Collect system messages emitted during this action (bounded slice
        // only - never the whole historical tree).
        if (bookmark > 0)
        {{
            try
            {{
                // Collect text AND severity, and count everything seen. The
                // ranking and the cap are applied on the Python side, so the
                // policy is unit-testable without string-matching generated
                // C# - in the one file where a typo mutes all ~180 tools.
                var raw = new List<Dictionary<string, string>>();
                // Three-arg ctor (start, end, level) bounds the slice at both
                // ends; fall back to the open-ended two-arg one only if the
                // end bookmark could not be taken.
                SysMessagesCollection col;
                if (bookmarkEnd > bookmark)
                {{
                    col = new SysMessagesCollection(bookmark, bookmarkEnd, MessageLevel.Message);
                    results["eplanMessagesBounded"] = true;
                }}
                else
                {{
                    col = new SysMessagesCollection(bookmark, MessageLevel.Message);
                    results["eplanMessagesBounded"] = false;
                }}
                // Count is the collection's OWN total, so truncation is
                // reported exactly rather than inferred from how many entries
                // we managed to walk.
                try {{ results["eplanMessagesTrueTotal"] = col.Count; }} catch {{}}
                var it = col.GetSysMsgEnumerator();
                int scanned = 0;
                while (it.MoveNext() && scanned < {raw_scan_cap})
                {{
                    // SysMessagesEnumerator.Current is typed object - it must
                    // be cast before .Message is reachable, or the generated
                    // script fails to compile and every action breaks.
                    var m = it.Current as BaseException;
                    if (m != null && !string.IsNullOrEmpty(m.Message)
                        && m.Message != "MCP bookmark"
                        && m.Message != "MCP bookmark end")
                    {{
                        var rec = new Dictionary<string, string>();
                        rec["text"] = m.Message;
                        // Use MessageLevel. The shorter "Level" property does
                        // not exist and raises CS1061, which breaks every
                        // action - verified live 2026-09-01. ToString() rather
                        // than an enum member, so a renamed member cannot
                        // become a CS0117 at generation time. (Written without
                        // the forbidden spelling on purpose: a test greps the
                        // generated source for it.)
                        try {{ rec["level"] = m.MessageLevel.ToString(); }}
                        catch {{ rec["level"] = ""; }}
                        raw.Add(rec);
                        scanned++;
                    }}
                }}
                if (raw.Count > 0)
                {{
                    results["eplanMessagesRaw"] = raw;
                    results["eplanMessagesScanned"] = raw.Count;
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

    def _preserve_failed_script(self, script_path: str) -> Optional[str]:
        """Copy a generated script aside before the cleanup deletes it.

        When no result file appears the generated C# is the only evidence of
        why, and the `finally` below removes it - which is what made commit
        21d10d4d6 (a CS1061 that broke every wrapped action) expensive to
        diagnose. Returns the preserved path, or None if it could not be kept;
        never raises, because this runs on an error path.
        """
        try:
            dest_dir = os.path.join(self._log_dir(), "failed_scripts")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(script_path))
            with open(script_path, "r", encoding="utf-8") as src:
                content = src.read()
            with open(dest, "w", encoding="utf-8") as out:
                out.write(content)
            return dest
        except Exception:
            return None

    def _script_failure(self, action, script_path, started, error_type, error, message):
        """Build, log and return a 'the action did not run' result.

        The three no-result paths below all used to return a bare `message`,
        which reads to a caller exactly like a slow action that eventually
        worked. Each now carries errorType plus executor="none", so the model
        can tell "EPLAN ran this and it failed" from "this never reached
        EPLAN", and stop retrying in the second case.
        """
        result = {
            "success": False,
            "executor": "none",
            "errorType": error_type,
            "error": error,
            "message": message,
            "action": action,
        }
        preserved = self._preserve_failed_script(script_path)
        if preserved:
            result["failedScriptPath"] = preserved
        self._log_action(action, result, started)
        return result

    def _run_generated_script(self, action, script_path, result_path, started) -> dict:
        """Execute a generated [Start] script, await the result file, clean up."""
        try:
            logger.info(f"Wrapping action via script: {action} (script={os.path.basename(script_path)})")
            # Execute only - deliberately NOT RegisterScript first. The wrapper
            # script generated above has only a [Start] method; RegisterScript is
            # for persistent [DeclareAction]/[DeclareEventHandler]/[DeclareMenu]
            # hooks, and ExecuteScript compiles and runs [Start] on its own.
            # Registering it only makes EPLAN report "The script does not
            # contain attributes for loading." (in its UI, not in the API
            # result) and costs two extra round-trips per action. Under
            # QuietMode - the whole point of this path - that is an error the
            # caller cannot even see. See scripted.py.
            exec_result = self.execute_action(f'ExecuteScript /ScriptFile:"{script_path}"', quiet_mode=False)
            if not exec_result.get("success"):
                return self._script_failure(
                    action, script_path, started,
                    "McpScriptExecuteFailed",
                    "EPLAN refused to run the generated wrapper script, so the "
                    "action did NOT execute. This is a fault in this MCP "
                    "server's script plumbing, not in the action or its "
                    "parameters - do not retry with different parameters. "
                    "Underlying ExecuteScript result: %s"
                    % exec_result.get("message"),
                    "Failed to execute action via script: %s"
                    % exec_result.get("message"),
                )

            # Wait for result file
            timeout = SCRIPT_RESULT_TIMEOUT_S
            start_time = time.time()
            while not os.path.exists(result_path):
                if time.time() - start_time > timeout:
                    return self._script_failure(
                        action, script_path, started,
                        "McpScriptNoResult",
                        "The action did NOT run: the generated wrapper script "
                        "produced no result file within %gs. This is NOT a "
                        "timeout on a slow action - EPLAN's ExecuteScript is "
                        "synchronous here, so a slow action would still have "
                        "written its result before returning. The usual cause "
                        "is that the generated C# failed to compile, which "
                        "affects every wrapped action equally rather than just "
                        "this one. The script has been preserved at "
                        "failedScriptPath; check eplan_get_system_messages for "
                        "a compiler error." % timeout,
                        "Timeout waiting for scripted action execution result",
                    )
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
                return self._script_failure(
                    action, script_path, started,
                    "McpScriptBadResult",
                    "The action may or may not have run: a result file appeared "
                    "but never became valid JSON. Unlike McpScriptNoResult the "
                    "script did reach its write, so treat any side effect of "
                    "this action as possibly applied and verify before "
                    "retrying. The script has been preserved at "
                    "failedScriptPath.",
                    "Could not parse action result file",
                )

            res_data = self._shape_messages(res_data)
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
        """
        Get current connection status.

        "host" is reported because EPLAN remoting is UNAUTHENTICATED and this
        manager is a process-wide singleton: once something retargets it at
        another machine, every later action runs there. Without the host in the
        status (and in the action trace) neither the user nor a post-incident
        reviewer could tell a session had left the workstation.
        """
        return {
            "connected": self.connected,
            "api_loaded": self._clr_initialized,
            "target_version": self.target_version,
            "host": self.host if self.connected else None,
            "is_local": (self.host in self.LOCAL_HOSTS) if self.connected else None,
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
