"""
Action catalog - reach EVERY EPLAN action without one MCP tool per action.

The typed wrappers in this package cover ~101 of the ~1150 actions EPLAN 2027
knows about. Publishing a tool for each of the rest would bury the useful ones,
so this module is the generic tier instead:

    action_catalog()   search the offline registry (no EPLAN needed)
    action_describe()  full registry entry + a live "is this action actually
                       registered in THIS session?" probe
    action_run()       validated dispatch for any action name + parameters
    ribbon_catalog()   walk the live ribbon bar (tab -> group -> command)

The registry (data/action_registry.json) is built from two sources and says so
per parameter: the official EPLAN 2027 API documentation ("docs") and the
command lines observed in the live install's GUI action map, MFTools.xml
("observed"). It is therefore authoritative about which actions EXIST, and only
indicative about which parameters an action accepts - hence the
allow_unknown_params escape hatch on action_run.

action_run supersedes execute_raw_action (addons.py): same reach, but it
validates the action name and parameter keys against the registry first and can
show the exact command line without running it (dry_run).

API surfaces used by the live parts (both assemblies are in the script engine's
fixed assembly set, so plain `using` directives compile here - unlike
Eplan.EplApi.DataModel, see live.py):
- Eplan.EplApi.ApplicationFramework.ActionManager.FindAction(string) ->
  Action or null; Action.Name / Action.ModuleName / Action.ActionProperties
- Eplan.EplApi.Gui.RibbonBar.Tabs -> RibbonTab.CommandGroups ->
  RibbonCommandGroup.Commands (Dictionary<uint, RibbonCommand>)

Note on the generated C#: `using System;` and
`using Eplan.EplApi.ApplicationFramework;` together make the bare type name
`Action` ambiguous (CS0104), so the probe script never spells it - it uses
`var`. ActionProperties/ActionParameterProperties members are read by
reflection so a member that is absent in some release degrades to a missing
field instead of a compile error (which the script engine reports only as a
silent timeout here).
"""

import difflib
import json
import os
import uuid

from ._base import _get_connected_manager, _build_action, cs_escape, get_manager
from .scripted import _execute_script


_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "action_registry.json"
)

# Cached after the first successful read. Path is resolved from __file__, never
# from the cwd: the MCP server is started from arbitrary working directories.
_REGISTRY = None
_REGISTRY_META = None
_REGISTRY_LOWER = None
_REGISTRY_CMD_INDEX = None
# name -> lowercased search text (name, description, param names, ribbon
# labels and "tab > group" paths). Kept outside the entries so
# action_describe's full-entry output does not grow a synthetic field.
_REGISTRY_SEARCH = None


def _load_registry():
    """
    Load and cache the action registry.

    Returns:
        (actions, meta, error) - error is None on success, otherwise a
        ready-to-return dict.
    """
    global _REGISTRY, _REGISTRY_META, _REGISTRY_LOWER, _REGISTRY_CMD_INDEX
    global _REGISTRY_SEARCH

    if _REGISTRY is not None:
        return _REGISTRY, _REGISTRY_META, None

    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        return None, None, {
            "success": False,
            "error": f"Could not read action registry at {_REGISTRY_PATH}: {e}",
        }

    meta = raw.get("_meta", {})
    # Every underscore-prefixed top-level key is registry metadata (_meta,
    # _command_index, ...), never an action. Filtering only "_meta" would let
    # a future metadata block masquerade as an action entry.
    actions = {k: v for k, v in raw.items() if not k.startswith("_")}

    _REGISTRY = actions
    _REGISTRY_META = meta
    _REGISTRY_CMD_INDEX = raw.get("_command_index", {})
    _REGISTRY_LOWER = {k.lower(): k for k in actions}
    _REGISTRY_SEARCH = {k: _search_text(k, v) for k, v in actions.items()}
    return _REGISTRY, _REGISTRY_META, None


def _search_text(name, entry):
    """Lowercased haystack action_catalog(search=...) matches against."""
    gui = entry.get("gui") or {}
    return " ".join(
        [name, entry.get("description") or ""]
        + _param_names(entry)
        + list(gui.get("labels") or [])
        + list(gui.get("ribbon_paths") or [])
    ).lower()


def _param_names(entry):
    """Parameter names of a registry entry, in registry order."""
    names = []
    for p in entry.get("params") or []:
        name = p.get("name")
        if name and name not in names:
            names.append(name)
    return names


def _compact(entry):
    """Small record for list results - never a reference into the cache."""
    gui = entry.get("gui") or {}
    examples = gui.get("examples") or []
    return {
        "name": entry.get("name"),
        "description": entry.get("description", ""),
        "documented": bool(entry.get("documented")),
        "wrapped_by": list(entry.get("wrapped_by") or []),
        "params": _param_names(entry),
        "example": examples[0] if examples else None,
        "categories": list(gui.get("categories") or []),
        "live_resolved": entry.get("live_resolved"),
        "module_name": entry.get("module_name"),
        # What the button is called in the GUI, when this action is behind one.
        "labels": list(gui.get("labels") or []),
        "ribbon_paths": list(gui.get("ribbon_paths") or []),
    }


def action_catalog(
    search: str = None,
    category: str = None,
    documented_only: bool = False,
    wrapped: bool = None,
    available_only: bool = False,
    limit: int = 50,
) -> dict:
    """
    Search the offline catalog of every EPLAN action this install knows about.

    Answers "what action does X?" without guessing. The catalog is a JSON
    registry shipped with the server (built from the official EPLAN 2027 API
    docs plus the live install's GUI action map, MFTools.xml), so this tool
    needs NO EPLAN connection and never blocks. Use action_describe() for the
    full entry plus a live check, and action_run() to execute one.

    Args:
        search: Case-insensitive substring matched against the action name, its
            description, its parameter names, AND the ribbon button text and
            "tab > group" path where the action lives in the GUI. So the words
            you would look for on screen work as search terms - "Coordinate
            input", "Page macro", "Increment" - not only internal names like
            "pdf" or "SCHEME". Omit to browse everything.
        category: EPLAN rights-management category id to filter by, as a
            string. These are numeric ids from MFTools.xml (e.g. "5", "16"),
            not human names - read them off the "categories" field of results
            rather than guessing.
        documented_only: True to return only actions that appear in the
            official API documentation (~100 of ~1150); the rest are real and
            callable but documented only by their observed GUI command lines.
        wrapped: True for actions that already have a typed MCP tool, False for
            those that do not, None (default) for both.
        available_only: True to return only actions that the RECORDED probe
            found registered - and the recording was made on the reference
            installation this repo shipped, NOT on the machine you are talking
            to. So this filter is advisory: an action it hides may well run
            here, and an action it keeps can still refuse with a licence error,
            because module licensing is enforced at execution time and not at
            lookup time. The names that fail to resolve are mostly dialog ids
            and GED interaction names (values for XGedStartInteractionAction
            /Name:, not actions in their own right) rather than missing
            features. Re-probe this machine with tools/probe_live_actions.py to
            make the filter authoritative. Each record also carries
            "live_resolved" (True/False, or None if never probed) and
            "module_name"; the shipped totals are in the registry's
            _meta.counts rather than quoted here, so they cannot go stale.
        limit: Maximum records to return. The full match count is always
            reported, so a truncated result is never mistaken for the total.

    Returns:
        dict with "count" (total matches), "returned", "truncated", and
        "actions" (compact records: name, description, documented, wrapped_by,
        param names, one example command line, category ids).
    """
    try:
        actions, meta, error = _load_registry()
        if error:
            return error

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return {"success": False, "error": f"Invalid limit: {limit!r}. Must be an integer."}
        if limit < 0:
            limit = 0

        needle = (search or "").strip().lower()
        cat = str(category).strip() if category not in (None, "") else None

        matches = []
        for name, entry in actions.items():
            if documented_only and not entry.get("documented"):
                continue
            if available_only and entry.get("live_resolved") is not True:
                continue
            if wrapped is not None:
                is_wrapped = bool(entry.get("wrapped_by"))
                if is_wrapped != bool(wrapped):
                    continue
            if cat is not None:
                cats = (entry.get("gui") or {}).get("categories") or []
                if cat not in [str(c) for c in cats]:
                    continue
            # Ribbon button text and its tab > group path are searched too,
            # because people look for the words on the button ("Coordinate
            # input") rather than the internal action name
            # (GedEditGuiPosDialogShow).
            if needle and needle not in _REGISTRY_SEARCH[name]:
                continue
            matches.append(entry)

        matches.sort(key=lambda e: (not e.get("documented"), (e.get("name") or "").lower()))
        shown = matches[:limit]

        return {
            "success": True,
            "count": len(matches),
            "returned": len(shown),
            "truncated": len(matches) - len(shown),
            "limit": limit,
            "filters": {
                "search": search,
                "category": category,
                "documented_only": bool(documented_only),
                "wrapped": wrapped,
            },
            "registry": {
                "total_actions": len(actions),
                "eplan_version": meta.get("eplan_version"),
            },
            "actions": [_compact(e) for e in shown],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


_PROBE_SCRIPT = '''using System;
using System.IO;
using System.Reflection;
using System.Collections;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.Scripting;

public class __CLASS__
{
    static string PropText(object o, string name)
    {
        try
        {
            if (o == null) return null;
            PropertyInfo pi = o.GetType().GetProperty(name);
            if (pi == null) return null;
            object v = pi.GetValue(o, null);
            return v == null ? null : v.ToString();
        }
        catch { return null; }
    }

    [Start]
    public void Run()
    {
        var results = new Dictionary<string, object>();

        try
        {
            // Never spell the type: `using System;` + ApplicationFramework make
            // the bare name `Action` ambiguous (CS0104).
            var manager = new ActionManager();
            var found = manager.FindAction("__NAME__");

            if (found == null)
            {
                results["found"] = false;
            }
            else
            {
                results["found"] = true;
                string nm = PropText(found, "Name");
                results["name"] = nm == null ? "" : nm;
                string mod = PropText(found, "ModuleName");
                results["module_name"] = mod == null ? "" : mod;

                // ActionProperties / ActionParameterProperties are read
                // reflectively: a member missing in some release must degrade,
                // not turn into a compile error (which surfaces only as a
                // silent script timeout).
                try
                {
                    PropertyInfo apProp = found.GetType().GetProperty("ActionProperties");
                    object props = apProp == null ? null : apProp.GetValue(found, null);
                    if (props != null)
                    {
                        string desc = PropText(props, "Description");
                        if (desc != null) results["action_description"] = desc;

                        MethodInfo mi = props.GetType().GetMethod("GetParameterProperties");
                        if (mi != null)
                        {
                            object list = mi.Invoke(props, null);
                            IEnumerable items = list as IEnumerable;
                            if (items != null)
                            {
                                var pnames = new List<string>();
                                foreach (object p in items)
                                {
                                    string pn = PropText(p, "Name");
                                    if (pn != null && pn.Length > 0) pnames.Add(pn);
                                }
                                // Only when non-empty: most actions never fill
                                // ActionProperties, and an empty list reads as
                                // "takes no parameters", which is wrong.
                                if (pnames.Count > 0) results["declared_params"] = pnames;
                            }
                        }
                    }
                }
                catch { }
            }

            results["success"] = true;
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = ex.Message;
        }

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results, Newtonsoft.Json.Formatting.Indented);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
'''


def action_describe(name: str) -> dict:
    """
    Describe one EPLAN action: full registry entry plus a live registration probe.

    The registry half (documentation text, every known parameter with its
    source, the GUI command lines observed for it, which typed MCP tool wraps
    it) works offline. When EPLAN is connected this also runs
    ActionManager.FindAction(name) in-process, which is the only way to know
    whether the action is really available in THIS session: FindAction returns
    null when the owning module is not loaded or is not covered by the license.
    Not being connected degrades to registry-only data, never an error.

    Args:
        name: Action name, e.g. "label", "XEGActionInsertSymRef". Matched
            case-insensitively against the registry; the live probe uses the
            registry's canonical casing when the name resolves, otherwise the
            name as given.

    Returns:
        dict with "registry" (the full entry, or null when unknown), "live"
        (probed / found / name / module_name) and "licensed_hint" explaining
        what a null FindAction result means. "live.declared_params" appears
        ONLY when the action self-describes its parameters through
        ActionProperties; most EPLAN actions do not, so its absence means
        "not declared", never "takes no parameters" - the registry entry's
        params list stays the authority.
    """
    try:
        actions, meta, error = _load_registry()
        if error:
            return error

        if not name or not str(name).strip():
            return {"success": False, "error": "name is required."}
        name = str(name).strip()

        resolved = name if name in actions else _REGISTRY_LOWER.get(name.lower())
        entry = actions.get(resolved) if resolved else None
        probe_name = resolved or name

        result = {
            "success": True,
            "name": probe_name,
            "in_registry": entry is not None,
            "registry": json.loads(json.dumps(entry)) if entry is not None else None,
        }
        if entry is None:
            result["near_matches"] = difflib.get_close_matches(name, list(actions), n=8, cutoff=0.5)
            result["note"] = (
                "Not in the offline registry. The live probe still runs - an action "
                "shipped by an add-on installed after the registry was built can be "
                "registered without appearing here."
            )

        # Check connectivity directly instead of round-tripping through the
        # script engine just to discover we are offline.
        try:
            connected = bool(get_manager().connected)
        except Exception:
            connected = False

        if not connected:
            result["live"] = {
                "probed": False,
                "reason": "Not connected to EPLAN. Call eplan_connect() first for the live probe.",
            }
            result["licensed_hint"] = (
                "Not probed: registry presence alone does NOT prove the action is "
                "available in a given session."
            )
            return result

        script = _PROBE_SCRIPT.replace(
            "__CLASS__", "ActionDescribe_" + uuid.uuid4().hex[:6]
        ).replace("__NAME__", cs_escape(probe_name))

        probe = _execute_script(script, timeout=60.0)
        if not probe.get("success"):
            result["live"] = {
                "probed": False,
                "reason": probe.get("message") or probe.get("error") or "Probe failed.",
            }
            result["licensed_hint"] = "Live probe did not complete; registry data only."
            return result

        inner = probe.get("results") or {}
        live = {"probed": True}
        live.update(inner)
        result["live"] = live

        if inner.get("found"):
            result["licensed_hint"] = (
                "Registered in this EPLAN session: the owning module is loaded and "
                "the action can be executed with action_run()."
            )
        else:
            result["licensed_hint"] = (
                "ActionManager.FindAction returned null: the action is NOT registered "
                "in this EPLAN session. Typically the owning module is not loaded or "
                "is not covered by the license; it can also be a misspelled name, or "
                "an action that only exists in another EPLAN variant/version. "
                "Executing it now would fail."
            )
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


def action_run(
    name: str,
    params: dict = None,
    dry_run: bool = False,
    allow_unknown_params: bool = False,
) -> dict:
    """
    Run ANY EPLAN action by name, validated against the action registry first.

    The generic escape from "there is no tool for that": every one of the
    ~1150 known actions is reachable here. Both the action name and the
    parameter keys are checked against the registry before anything executes,
    and dry_run shows the exact command line that would be sent. Prefer a typed
    wrapper (see action_catalog(wrapped=True)) when one exists - it documents
    the parameters properly.

    Parameter keys are passed through VERBATIM as /KEY:value and EPLAN's casing
    is not uniform (PROJECTNAME, but OpenMode, PartNr, ConfigScheme). Wrong
    casing is silently ignored by EPLAN, so validation is case-SENSITIVE and
    never auto-corrects: a key that differs only in case is reported as such.
    Values follow the usual rules - True/False become 1/0, None and "" are
    dropped, values containing spaces are quoted.

    Args:
        name: Action name (case-insensitively resolved to the registry's
            canonical casing, which is what gets executed).
        params: dict of EPLAN parameter name -> value, e.g.
            {"PROJECTNAME": "C:/Projects/x.elk", "CONFIGSCHEME": "Default"}.
        dry_run: True to return the command string that WOULD run, without
            executing it. Validation is identical either way.
        allow_unknown_params: True to send parameter keys the registry does not
            list for this action. Off by default because an unrecognised key is
            usually a typo or wrong casing, and EPLAN drops it silently instead
            of complaining. The registry's parameter lists are incomplete for
            undocumented actions (many are inferred from observed GUI command
            lines), so a caller with a better source - the EPLAN docs, or the
            rarely-present live declared_params from action_describe() - can
            set this to proceed.

    Returns:
        dict with the built "command" plus EPLAN's execution result; on
        dry_run, the command and the validation outcome only.
    """
    try:
        actions, meta, error = _load_registry()
        if error:
            return error

        if not name or not str(name).strip():
            return {"success": False, "error": "name is required."}
        name = str(name).strip()

        # --- action name -----------------------------------------------------
        resolved = name if name in actions else _REGISTRY_LOWER.get(name.lower())
        if resolved is None:
            near = difflib.get_close_matches(name, list(actions), n=8, cutoff=0.4)
            return {
                "success": False,
                "error": f"Unknown action '{name}' - it is not in the action registry.",
                "near_matches": near,
                "hint": (
                    "Search with action_catalog(search=...) or confirm the action "
                    "exists in this session with action_describe(name). There is "
                    "deliberately no override for an unknown NAME: use "
                    "execute_raw_action() if you are certain the action exists and "
                    "the registry is stale."
                ),
            }

        entry = actions[resolved]
        known = _param_names(entry)
        known_lower = {k.lower(): k for k in known}

        # --- parameters ------------------------------------------------------
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return {
                "success": False,
                "error": f"params must be a dict of EPLAN parameter name -> value, got {type(params).__name__}.",
            }

        clean = {}
        for key, value in params.items():
            key = str(key)
            if not key:
                return {"success": False, "error": "Empty parameter name in params."}
            clean[key] = value

        # _build_action's own first argument is positional-or-keyword; a param
        # literally called action_name would bind to it instead of becoming a
        # /KEY. No EPLAN action uses that name - refuse rather than misfire.
        if "action_name" in clean:
            return {
                "success": False,
                "error": "The parameter key 'action_name' collides with the command "
                         "builder's own argument. Use execute_raw_action() for that case.",
            }

        unknown = [k for k in clean if k not in known]
        if unknown and not allow_unknown_params:
            suggestions = {}
            for k in unknown:
                if k.lower() in known_lower:
                    suggestions[k] = f"{known_lower[k.lower()]} (casing matters)"
                else:
                    close = difflib.get_close_matches(k, known, n=3, cutoff=0.5)
                    if close:
                        suggestions[k] = ", ".join(close)
            if known:
                hint = (
                    "Parameter keys are case-sensitive and are sent verbatim as /KEY. "
                    "Fix the key, or pass allow_unknown_params=True if you know the "
                    "registry's list is incomplete for this action."
                )
            else:
                hint = (
                    "The registry has NO parameter information for this action (it is "
                    "documented only by its name). Pass allow_unknown_params=True to "
                    "send these keys anyway, after confirming them with "
                    "action_describe() or the EPLAN docs."
                )
            return {
                "success": False,
                "error": f"Unknown parameter(s) for '{resolved}': {', '.join(sorted(unknown))}.",
                "action": resolved,
                "known_params": known,
                "suggestions": suggestions,
                "hint": hint,
            }

        command = _build_action(resolved, **clean)

        validation = {
            "action": resolved,
            "resolved_name": resolved if resolved != name else None,
            "known_params": known,
            "unknown_params_sent": sorted(unknown) if unknown else [],
            "allow_unknown_params": bool(allow_unknown_params),
        }

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "command": command,
                "validation": validation,
                "note": "Nothing was executed. Call again with dry_run=False to run it.",
            }

        manager, error = _get_connected_manager()
        if error:
            error["command"] = command
            return error

        result = manager.execute_action(command)
        out = dict(result) if isinstance(result, dict) else {"result": result}
        out.setdefault("success", True)
        out["command"] = command
        out["validation"] = validation
        return out
    except Exception as e:
        return {"success": False, "error": str(e)}


_RIBBON_SCRIPT = '''using System;
using System.IO;
using System.Collections.Generic;
using Eplan.EplApi.Gui;
using Eplan.EplApi.Scripting;

public class __CLASS__
{
    [Start]
    public void Run()
    {
        var results = new Dictionary<string, object>();
        var tabs = new List<Dictionary<string, object>>();
        var errors = new List<string>();
        int tabCount = 0;
        int groupCount = 0;
        int commandCount = 0;
        int withCommandLine = 0;

        try
        {
            var ribbon = new RibbonBar();
            var allTabs = ribbon.Tabs;
            if (allTabs != null)
            {
                foreach (var tab in allTabs)
                {
                    // One bad tab must never abort the walk.
                    try
                    {
                        if (tab == null) continue;
                        var tabDict = new Dictionary<string, object>();

                        string tabName = null;
                        try { tabName = tab.DisplayName; } catch { }
                        tabDict["tab"] = tabName == null ? "" : tabName;
                        try { tabDict["identifier"] = Convert.ToString(tab.Identifier); } catch { }
                        try { tabDict["is_custom"] = tab.IsCustom; } catch { }

                        var groups = new List<Dictionary<string, object>>();
                        var cmdGroups = tab.CommandGroups;
                        if (cmdGroups != null)
                        {
                            foreach (var grp in cmdGroups)
                            {
                                try
                                {
                                    if (grp == null) continue;
                                    var groupDict = new Dictionary<string, object>();
                                    string groupName = null;
                                    try { groupName = grp.Name; } catch { }
                                    groupDict["group"] = groupName == null ? "" : groupName;
                                    try { groupDict["is_custom"] = grp.IsCustom; } catch { }

                                    var commands = new List<Dictionary<string, object>>();
                                    var cmdMap = grp.Commands;
                                    if (cmdMap != null)
                                    {
                                        foreach (var pair in cmdMap)
                                        {
                                            try
                                            {
                                                var cmdDict = new Dictionary<string, object>();
                                                cmdDict["id"] = Convert.ToString(pair.Key);
                                                var cmd = pair.Value;

                                                string text = null;
                                                try { text = cmd.Text; } catch { }
                                                cmdDict["text"] = text == null ? "" : text;

                                                // Documented limitation: the value is
                                                // available only from a CUSTOM command,
                                                // so built-ins usually yield "".
                                                string line = null;
                                                try { line = cmd.ActionCommandLine; } catch { }
                                                cmdDict["action_command_line"] = line == null ? "" : line;
                                                if (line != null && line.Length > 0) withCommandLine++;

                                                try { cmdDict["is_custom"] = cmd.IsCustom; } catch { }
                                                string descr = null;
                                                try { descr = cmd.Description; } catch { }
                                                if (descr != null && descr.Length > 0) cmdDict["description"] = descr;

                                                commands.Add(cmdDict);
                                                commandCount++;
                                            }
                                            catch (Exception exCmd)
                                            {
                                                errors.Add("command: " + exCmd.Message);
                                            }
                                        }
                                    }

                                    groupDict["commands"] = commands;
                                    groups.Add(groupDict);
                                    groupCount++;
                                }
                                catch (Exception exGroup)
                                {
                                    errors.Add("group: " + exGroup.Message);
                                }
                            }
                        }

                        tabDict["groups"] = groups;
                        tabs.Add(tabDict);
                        tabCount++;
                    }
                    catch (Exception exTab)
                    {
                        errors.Add("tab: " + exTab.Message);
                    }
                }
            }

            results["success"] = true;
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = ex.Message;
        }

        results["tabs"] = tabs;
        results["tab_count"] = tabCount;
        results["group_count"] = groupCount;
        results["command_count"] = commandCount;
        results["commands_with_action_command_line"] = withCommandLine;
        results["errors"] = errors;

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results, Newtonsoft.Json.Formatting.Indented);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
'''


def ribbon_catalog(tab: str = None, search: str = None) -> dict:
    """
    Walk the live EPLAN ribbon bar: tab -> command group -> commands.

    CALL SHAPE (the full tree is ~147,000 characters and would be truncated,
    so this drills down instead of dumping):
      - ribbon_catalog()                 -> the tab index: every tab with its
                                            group and command counts. Start here.
      - ribbon_catalog(tab="Insert")     -> the full group/command tree for one
                                            tab (case-insensitive, also matches
                                            the tab identifier).
      - ribbon_catalog(search="macro")   -> commands whose button text matches,
                                            across all tabs, each with its tab
                                            and group so you can find it again.

    Maps what a user sees in the GUI onto something automatable. Each command
    reports its display text, its numeric command ID and, when EPLAN exposes
    it, the action command line behind the button.

    IMPORTANT: RibbonCommand.ActionCommandLine is documented as "available only
    from a custom command", so BUILT-IN buttons typically return an empty
    action_command_line and only text/ID are usable. To find the action name
    behind a built-in button, search the display text with
    action_catalog(search=...) - the registry carries the command IDs observed
    for each action, which is the same numbering as the "id" field here.

    The walk is defensive: a tab, group or command that throws is recorded in
    "errors" and skipped, never aborting the rest of the tree.

    Returns:
        dict with "tabs" (each: tab, identifier, is_custom, groups -> group,
        is_custom, commands -> text, id, action_command_line, is_custom),
        counts, any per-node "errors", and a "note" restating the
        built-in-command limitation.
    """
    try:
        script = _RIBBON_SCRIPT.replace("__CLASS__", "RibbonCatalog_" + uuid.uuid4().hex[:6])
        result = _execute_script(script, timeout=90.0)
        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("message") or result.get("error") or "Ribbon walk failed.",
            }

        inner = result.get("results") or {}
        out = {"success": bool(inner.get("success", True))}
        out.update(inner)

        # EPLAN leaves ActionCommandLine empty for built-in buttons, so join
        # each command's live id against the registry's command index (built
        # from the install's MFTools.xml UsedActions) to recover the action
        # the button actually runs. Verified live: ribbon id 35037 "Paste"
        # resolves to 'GfDlgMgrActionIGfWind /function:Paste'.
        _load_registry()
        index = _REGISTRY_CMD_INDEX or {}
        resolved = unresolved = 0
        for _tab in out.get("tabs") or []:
            for group in _tab.get("groups") or []:
                for cmd in group.get("commands") or []:
                    if cmd.get("action_command_line"):
                        continue
                    hit = index.get(str(cmd.get("id")))
                    if hit:
                        cmd["resolved_action"] = hit["action"]
                        cmd["resolved_action_command_line"] = hit["command_line"]
                        resolved += 1
                    else:
                        unresolved += 1
        # Counted over the whole ribbon walk, before any tab/search filtering,
        # so the numbers mean the same thing in every view.
        out["resolved_from_command_index"] = resolved
        out["unresolved_commands"] = unresolved

        # Shape the response so it never exceeds the tool-result size cap.
        all_tabs = out.get("tabs") or []

        def _tab_matches(t):
            needle = tab.strip().lower()
            return (str(t.get("tab", "")).lower() == needle
                    or str(t.get("identifier", "")).lower() == needle)

        if tab:
            picked = [t for t in all_tabs if _tab_matches(t)]
            if not picked:
                return {
                    "success": False,
                    "error": f"No ribbon tab matches {tab!r}.",
                    "available_tabs": [t.get("tab") for t in all_tabs],
                }
            out["tabs"] = picked
            out["view"] = "tab"
        elif search:
            needle = search.strip().lower()
            hits = []
            for t in all_tabs:
                for g in t.get("groups") or []:
                    for c in g.get("commands") or []:
                        text = str(c.get("text") or "")
                        if needle in text.lower():
                            hit = dict(c)
                            hit["tab"] = t.get("tab")
                            hit["group"] = g.get("group")
                            hits.append(hit)
            out.pop("tabs", None)
            out["view"] = "search"
            out["count"] = len(hits)
            out["commands"] = hits[:200]
            out["truncated"] = max(0, len(hits) - 200)
        else:
            out["tabs"] = [{
                "tab": t.get("tab"),
                "identifier": t.get("identifier"),
                "is_custom": t.get("is_custom"),
                "groups": len(t.get("groups") or []),
                "commands": sum(len(g.get("commands") or [])
                                for g in t.get("groups") or []),
            } for t in all_tabs]
            out["view"] = "index"
            out["hint"] = ("Tab index only. Pass tab=\"<name>\" for that tab's "
                           "buttons, or search=\"<text>\" to find a button by "
                           "its label across all tabs.")
        out["note"] = (
            "action_command_line is populated only for CUSTOM ribbon commands "
            "(EPLAN documents RibbonCommand.ActionCommandLine as available only "
            "from a custom command). For built-in buttons this tool fills in "
            "'resolved_action' / 'resolved_action_command_line' by joining the "
            "button's command id against the registry command index built from "
            "the installation's MFTools.xml. Run one with action_run(), or look "
            "it up with action_catalog(search=<button text>). A command with "
            "neither field is a button whose id is absent from MFTools.xml."
        )
        return out
    except Exception as e:
        return {"success": False, "error": str(e)}
