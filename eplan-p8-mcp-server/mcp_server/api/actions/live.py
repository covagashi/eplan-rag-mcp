"""
Live DataModel actions - read and edit the project currently open in EPLAN.

The action-based tools in this package drive EPLAN, but they cannot enumerate
or mutate the object model of the loaded project: `using Eplan.EplApi.DataModel;`
does not compile inside EPLAN's script engine (CS0234 - the engine compiles
against a fixed assembly set). The technique that does work, documented in
claude-skills/eplan-development (references/api-data-access.md and
references/e3d-installation-spaces.md), is runtime reflection over the object
model, with a `LockingStep` held for the duration (without it every project
access throws NoLockingStepException) and `SelectionSet.GetCurrentProject(false)`
to reach the project without ProjectManager.

Types are resolved by scanning AppDomain.CurrentDomain.GetAssemblies() rather
than by Assembly.Load of a hardcoded name, because the assembly that carries
the managed object model was RENAMED between releases:

    EPLAN 2025 (.NET Framework)  Eplan.EplApi.DataModelu / ...HEServicesu
    EPLAN 2027 (.NET 8/coreclr)  Eplan.EplApi.DataModelNetu / ...HEServicesNetu

On 2027 both names are present in the process, but the un-suffixed
`Eplan.EplApi.DataModelu` is the mixed-mode NATIVE twin: Assembly.Load of it
throws BadImageFormatException (0x8007000B, "attempt to load a program with an
incorrect format"), so the documented Assembly.Load("Eplan.EplApi.DataModelu")
recipe fails outright there. EPLAN has the managed assembly loaded already in
either case, so searching the loaded set for the type is both version-proof and
cheaper than a load. Verified on EPLAN 2027 (LockingStep resolved from
Eplan.EplApi.DataModelNetu).

This module applies that technique to the operations the action surface has no
answer for:

    live_query_functions    enumerate the open project's functions (devices)
    live_query_pages        enumerate the open project's pages
    live_set_function_text  set FUNC_TEXT on functions matched by name (write)
    live_set_connection_designations
                            set a function's connection point numbers (write)

Notes on the generated C#, all of them load-bearing (see the pitfalls section
of e3d-installation-spaces.md):
- No `using Eplan.EplApi.DataModel;` / `...HEServices;` - that is the CS0234 trap.
- Index initializers (`new Dictionary<..> { ["k"] = v }`) are written as separate
  `d["k"] = v;` statements here by CONVENTION, not by necessity. This note used
  to say the script engine rejects them with CS1525; that is NOT true on 2027 -
  a direct probe compiled and ran exactly that form. Corrected rather than
  removed because the false version was load-bearing in a debugging session: it
  sent someone hunting for a syntax error when the real fault was elsewhere.
  Separate statements are still preferred, since they keep a long dictionary
  diffable line by line.
- Every value interpolated into the script goes through cs_escape.
- Reflective calls are wrapped and InnerException chains flattened, because
  EPLAN wraps the real failure in TargetInvocationException.
- The LockingStep is constructed before the project is touched and disposed in
  a finally block.
"""

import uuid

from ._base import cs_escape
from .scripted import _execute_script

# EPLAN's dialogs join a function's connection point designations with this
# separator ("Y09¶Y10"); the API stores one designation per index, so it is
# only ever a display convention here - never write it into a single index.
PILCROW = "\u00b6"


_HEADER = '''using System;
using System.IO;
using System.Text;
using System.Reflection;
using System.Collections;
using System.Collections.Generic;
using Eplan.EplApi.Base;
using Eplan.EplApi.Scripting;

public class '''


_HELPERS = '''
{
    static string Flatten(Exception ex)
    {
        StringBuilder sb = new StringBuilder();
        Exception e = ex;
        int depth = 0;
        while (e != null && depth < 6)
        {
            if (sb.Length > 0) sb.Append(" <- ");
            sb.Append(e.GetType().Name);
            sb.Append(": ");
            sb.Append(e.Message);
            e = e.InnerException;
            depth++;
        }
        return sb.ToString();
    }

    static bool Matches(string value, string contains)
    {
        if (string.IsNullOrEmpty(contains)) return true;
        if (value == null) return false;
        return value.IndexOf(contains, StringComparison.OrdinalIgnoreCase) >= 0;
    }

    // Type.GetProperty(name) is NOT safe on EPLAN types - it throws
    // AmbiguousMatchException for two independent reasons:
    //  1. A derived class re-declares a member with a narrower return type
    //     (Function.Properties returns FunctionPropertyList, the base returns
    //     PropertyList), so the name exists at two levels of the hierarchy.
    //  2. A property list declares BOTH a plain and an indexed form of the same
    //     property on the SAME type (FUNC_TEXT and FUNC_TEXT[int]).
    // So: walk from the most-derived type up, DeclaredOnly (fixes 1), and ask
    // for the non-indexed declaration via Type.EmptyTypes (fixes 2) - together
    // that is what C# member lookup would have picked.
    static PropertyInfo GetPropInfo(Type t, string name)
    {
        BindingFlags bf = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
        Type cur = t;
        while (cur != null)
        {
            PropertyInfo pi = null;
            try { pi = cur.GetProperty(name, bf, null, null, Type.EmptyTypes, null); }
            catch { }
            if (pi != null) return pi;
            // No non-indexed form here; accept a unique declaration of any shape.
            try { pi = cur.GetProperty(name, bf); }
            catch { pi = null; }
            if (pi != null) return pi;
            cur = cur.BaseType;
        }
        return null;
    }

    // The indexed sibling of GetPropInfo. Some property-list members are
    // declared BOTH plain and as [int] - FUNC_CONNECTIONDESIGNATION is one -
    // and there the indexed form is the useful one: it addresses a single
    // connection point. GetPropInfo deliberately prefers the non-indexed
    // declaration, so an indexed property needs this explicit lookup.
    static PropertyInfo GetPropInfoIdx(Type t, string name)
    {
        BindingFlags bf = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
        Type cur = t;
        while (cur != null)
        {
            PropertyInfo pi = null;
            try { pi = cur.GetProperty(name, bf, null, null, new Type[] { typeof(int) }, null); }
            catch { }
            if (pi != null) return pi;
            cur = cur.BaseType;
        }
        return null;
    }

    static string PropText(object o, string prop)
    {
        try
        {
            PropertyInfo pi = GetPropInfo(o.GetType(), prop);
            if (pi == null) return null;
            object v = pi.GetValue(o, null);
            if (v == null) return null;
            return v.ToString();
        }
        catch { return null; }
    }

    // Resolve an EPLAN type from the assemblies already loaded in the process.
    // Deliberately NOT Assembly.Load("Eplan.EplApi.DataModelu"): on 2027 that
    // name belongs to the mixed-mode native twin and throws
    // BadImageFormatException, while the managed object model lives in
    // Eplan.EplApi.DataModelNetu. Scanning covers both naming schemes.
    static Assembly[] _asms;
    static Type FindType(string fullName)
    {
        if (_asms == null) _asms = AppDomain.CurrentDomain.GetAssemblies();
        foreach (Assembly a in _asms)
        {
            try
            {
                Type t = a.GetType(fullName);
                if (t != null) return t;
            }
            catch { }
        }
        // Not loaded yet: try the managed assembly names, newest scheme first.
        string[] candidates = new string[] {
            "Eplan.EplApi.DataModelNetu", "Eplan.EplApi.HEServicesNetu",
            "Eplan.EplApi.DataModelu", "Eplan.EplApi.HEServicesu" };
        foreach (string c in candidates)
        {
            try
            {
                Assembly a = Assembly.Load(c);
                if (a == null) continue;
                Type t = a.GetType(fullName);
                if (t != null)
                {
                    _asms = AppDomain.CurrentDomain.GetAssemblies();
                    return t;
                }
            }
            catch { }
        }
        throw new Exception("Could not resolve type " + fullName +
            " in any loaded EPLAN assembly.");
    }

    // ------------------------------------------------------------------
    // Reflection helpers shared by every module that generates a script
    // through _script(). The rule they all enforce: a lookup that finds
    // NOTHING is an error that says what the type really declares - never a
    // silently empty result. A silent miss is how a write reports success
    // having done nothing, and the script engine gives no compiler output to
    // fall back on.
    // ------------------------------------------------------------------

    // Turn a dead end into a one-turn fix by listing what is actually there.
    static string MemberList(Type t, bool methods)
    {
        List<string> names = new List<string>();
        Type cur = t;
        BindingFlags bf = BindingFlags.Public | BindingFlags.Instance |
                          BindingFlags.Static | BindingFlags.DeclaredOnly;
        while (cur != null && names.Count < 250)
        {
            if (cur.FullName == "System.Object") break;
            try
            {
                if (methods)
                {
                    foreach (MethodInfo mi in cur.GetMethods(bf))
                        if (!names.Contains(mi.Name)) names.Add(mi.Name);
                }
                else
                {
                    foreach (PropertyInfo pi in cur.GetProperties(bf))
                        if (!names.Contains(pi.Name)) names.Add(pi.Name);
                }
            }
            catch { }
            cur = cur.BaseType;
        }
        names.Sort();
        return t.FullName + " declares " + (methods ? "methods" : "properties") +
            ": " + string.Join(", ", names.ToArray());
    }

    // GetPropInfo returns the MOST DERIVED declaration, which is right for
    // AmbiguousMatch but wrong when a derived class re-declares a member with
    // only half the accessors: SymbolReference declares
    // `public override PointD Location {set;}` while the READABLE declaration
    // is on Placement. A most-derived lookup then hands back a PropertyInfo
    // whose GetValue throws, the throw gets swallowed, and a pin OFFSET is
    // published as an absolute page coordinate. So walk PAST a declaration
    // that fails the accessor test.
    static PropertyInfo GetPropWalk(Type t, string name, bool needRead, bool needWrite)
    {
        BindingFlags bf = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
        Type cur = t;
        while (cur != null)
        {
            PropertyInfo pi = null;
            try { pi = cur.GetProperty(name, bf, null, null, Type.EmptyTypes, null); }
            catch { pi = null; }
            if (pi == null)
            {
                try { pi = cur.GetProperty(name, bf); } catch { pi = null; }
            }
            if (pi != null && (!needRead || pi.CanRead) && (!needWrite || pi.CanWrite)) return pi;
            cur = cur.BaseType;
        }
        return null;
    }

    static PropertyInfo GetReadable(Type t, string name) { return GetPropWalk(t, name, true, false); }
    static PropertyInfo GetWritable(Type t, string name) { return GetPropWalk(t, name, false, true); }

    static PropertyInfo RequireReadable(Type t, string name)
    {
        PropertyInfo pi = GetReadable(t, name);
        if (pi == null)
            throw new Exception("No readable property '" + name + "'. " + MemberList(t, false));
        return pi;
    }

    static PropertyInfo RequireWritable(Type t, string name)
    {
        PropertyInfo pi = GetWritable(t, name);
        if (pi == null)
            throw new Exception("No writable property '" + name + "'. " + MemberList(t, false));
        return pi;
    }

    // Overload selection by SHAPE, matching parameter-type NAMES rather than
    // typeof(): the script engine may compile against a different
    // Eplan.EplApi.Base than the loaded object model references, and then a
    // typeof()-built Type[] silently matches nothing. "" is a wildcard.
    static MethodInfo MethodByShape(Type t, string name, string[] pTypeNames, bool wantStatic)
    {
        Type cur = t;
        while (cur != null)
        {
            BindingFlags bf = BindingFlags.Public | BindingFlags.DeclaredOnly |
                (wantStatic ? BindingFlags.Static : BindingFlags.Instance);
            MethodInfo[] all = null;
            try { all = cur.GetMethods(bf); } catch { all = null; }
            if (all != null)
            {
                foreach (MethodInfo mi in all)
                {
                    if (mi.Name != name) continue;
                    ParameterInfo[] ps = mi.GetParameters();
                    if (ps.Length != pTypeNames.Length) continue;
                    bool ok = true;
                    for (int i = 0; i < ps.Length; i++)
                    {
                        if (pTypeNames[i].Length > 0 && ps[i].ParameterType.Name != pTypeNames[i])
                        {
                            ok = false;
                            break;
                        }
                    }
                    if (ok) return mi;
                }
            }
            cur = cur.BaseType;
        }
        return null;
    }

    static MethodInfo RequireMethod(Type t, string name, string[] pTypeNames, bool wantStatic)
    {
        MethodInfo mi = MethodByShape(t, name, pTypeNames, wantStatic);
        if (mi == null)
            throw new Exception("No " + (wantStatic ? "static " : "instance ") + name + "(" +
                string.Join(", ", pTypeNames) + ") on " + t.FullName + ". " + MemberList(t, true));
        return mi;
    }

    // Every reflective call goes through here, so no primitive can return the
    // useless "Exception has been thrown by the target of an invocation".
    static object Call(MethodInfo mi, object target, object[] args)
    {
        try { return mi.Invoke(target, args); }
        catch (TargetInvocationException tie)
        {
            throw new Exception(mi.DeclaringType.Name + "." + mi.Name + ": " +
                Flatten(tie.InnerException));
        }
    }

    // For TYPE-DEPENDENT members only (a DynamicConnectionLine has no
    // SymbolVariant). Absence is RECORDED, never silently dropped. Load-bearing
    // lookups use Require*, which throws.
    static object TryRead(object o, string name, List<string> absent)
    {
        PropertyInfo pi = GetReadable(o.GetType(), name);
        if (pi == null)
        {
            if (absent != null && !absent.Contains(name)) absent.Add(name);
            return null;
        }
        try { return pi.GetValue(o, null); }
        catch (TargetInvocationException tie)
        {
            if (absent != null) absent.Add(name + " (threw: " + Flatten(tie.InnerException) + ")");
            return null;
        }
        catch (Exception ex)
        {
            if (absent != null) absent.Add(name + " (threw: " + Flatten(ex) + ")");
            return null;
        }
    }

    // PropertyValue HAS NO PUBLIC CONSTRUCTOR - Activator.CreateInstance(pvType,
    // "P1") throws MissingMethodException. It is built through its STATIC
    // implicit conversion. Match on exact ParameterType AND
    // ReturnType == target, because the REVERSE direction
    // (PropertyValue -> String/Int32/...) shares the name op_Implicit.
    static object MakeValue(Type targetType, object raw)
    {
        if (raw == null)
            throw new Exception("Cannot build a " + targetType.FullName + " from null.");
        Type src = raw.GetType();
        MethodInfo conv = null;
        foreach (MethodInfo mi in targetType.GetMethods(BindingFlags.Public | BindingFlags.Static))
        {
            if (mi.Name != "op_Implicit" && mi.Name != "op_Explicit") continue;
            if (mi.ReturnType != targetType) continue;
            ParameterInfo[] ps = mi.GetParameters();
            if (ps.Length != 1) continue;
            if (ps[0].ParameterType != src) continue;
            conv = mi;
            break;
        }
        if (conv == null)
            throw new Exception("Cannot build a " + targetType.FullName + " from a " +
                src.FullName + ": no public static conversion returning " + targetType.Name +
                ". (PropertyValue has no public constructor, so this is the only route.) " +
                MemberList(targetType, true));
        try { return conv.Invoke(null, new object[] { raw }); }
        catch (TargetInvocationException tie) { throw new Exception(Flatten(tie.InnerException)); }
    }

    // A CONSTRUCTED property list (e.g. a fresh PagePropertyList): its getter
    // may hand back nothing to write through, so build the value and assign.
    static string SetProp(object propList, string name, object raw)
    {
        PropertyInfo pi = GetWritable(propList.GetType(), name);
        if (pi == null)
            throw new Exception("Property list " + propList.GetType().FullName +
                " has no writable '" + name + "'. " + MemberList(propList.GetType(), false));
        object pv = MakeValue(pi.PropertyType, raw);
        try { pi.SetValue(propList, pv, null); }
        catch (TargetInvocationException tie) { throw new Exception(Flatten(tie.InnerException)); }
        return "op_Implicit+SetValue";
    }

    // A LIVE object's property list, as opposed to a freshly constructed one:
    // a PropertyValue fetched from it writes through on Set() - the route
    // live_set_function_text field-proved. Falls back to SetProp and REPORTS
    // which route fired, so the first live run settles it rather than guessing.
    static string SetLiveProp(object propList, string name, string value)
    {
        PropertyInfo pi = GetReadable(propList.GetType(), name);
        if (pi != null)
        {
            object pv = null;
            try { pv = pi.GetValue(propList, null); } catch { pv = null; }
            if (pv != null)
            {
                MethodInfo set = MethodByShape(pv.GetType(), "Set", new string[] { "String" }, false);
                if (set != null)
                {
                    Call(set, pv, new object[] { value });
                    return "getter+Set(string)";
                }
            }
        }
        return SetProp(propList, name, value);
    }

    // PointD identity: ptType ALWAYS comes from the member the point is handed
    // to, never `new PointD(..)` and never FindType("Eplan.EplApi.Base.PointD").
    static object MakePoint(Type ptType, double x, double y)
    {
        try { return Activator.CreateInstance(ptType, new object[] { x, y }); }
        catch (Exception ex)
        {
            throw new Exception("Cannot construct " + ptType.FullName + "(double,double): " +
                Flatten(ex));
        }
    }

    // PointD.ToString() returns the TYPE NAME, not coordinates - and so does
    // Page.Size. Read X/Y explicitly or every read-back says
    // "Eplan.EplApi.Base.PointD".
    static Dictionary<string, object> PtDict(object pt)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        if (pt == null) return d;
        PropertyInfo px = GetReadable(pt.GetType(), "X");
        PropertyInfo py = GetReadable(pt.GetType(), "Y");
        if (px == null || py == null)
            throw new Exception("Cannot read coordinates off " + pt.GetType().FullName +
                " (its ToString() returns the type name, not a coordinate). " +
                MemberList(pt.GetType(), false));
        d["x"] = Math.Round(Convert.ToDouble(px.GetValue(pt, null)), 4);
        d["y"] = Math.Round(Convert.ToDouble(py.GetValue(pt, null)), 4);
        return d;
    }

    // Stringify anything EPLAN hands back, safely.
    //
    // Load-bearing, and NOT obvious: reading an EPLAN property succeeds and
    // returns a PropertyValue, but calling ToString() on one whose property is
    // EMPTY throws EmptyPropertyException - measured on 2027 while reading the
    // connection-designation property of an ungenerated connection. So a
    // try/catch around the READ is not enough; the conversion throws too, and
    // separately from it.
    //
    // Anything that turns an EPLAN value into text must go through here.
    static string SafeText(object value)
    {
        if (value == null) return null;
        try
        {
            string s = value.ToString();
            return s;
        }
        catch { return null; }   // an empty property is absence, not an error
    }

    // Stable within one EPLAN session; NOT a database id that survives a
    // reopen. Callers get it back as "handle" and pass it to the next call.
    static string Handle(object o)
    {
        try
        {
            MethodInfo mi = MethodByShape(o.GetType(), "ToStringIdentifier", new string[] { }, false);
            if (mi == null) return null;
            object v = mi.Invoke(o, null);
            return v == null ? null : v.ToString();
        }
        catch { return null; }
    }

    static double Snap(double v, double grid)
    {
        if (grid <= 0.0001) return v;
        return Math.Round(Math.Floor(v / grid + 0.5) * grid, 4);
    }

    [Start]
    public void Run()
    {
        Dictionary<string, object> results = new Dictionary<string, object>();
        Type lsType = null;
        object lockStep = null;
        try
        {
            // Required before any project access, read or write.
            lsType = FindType("Eplan.EplApi.DataModel.LockingStep");
            lockStep = Activator.CreateInstance(lsType);

            // Current project without ProjectManager.
            Type ssType = FindType("Eplan.EplApi.HEServices.SelectionSet");
            object ss = Activator.CreateInstance(ssType);
            MethodInfo getCur = ssType.GetMethod("GetCurrentProject", new Type[] { typeof(bool) });
            object project = getCur.Invoke(ss, new object[] { false });
            if (project == null)
                throw new Exception("No project is currently open/selected in EPLAN.");

            results["project"] = PropText(project, "ProjectName");

'''


_FOOTER = '''
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = Flatten(ex);
        }
        finally
        {
            if (lockStep != null)
            {
                try { lsType.GetMethod("Dispose").Invoke(lockStep, null); }
                catch { }
            }
        }

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
'''


# Marker inside _HELPERS where the static helper block ends and the [Start]
# method begins. Splitting there is how a caller injects its own static helpers
# without this module having to know about them.
_START_MARKER = "    [Start]\n"


def _script(class_name, body, extra_helpers=""):
    """
    Wrap a reflection body in the standard script scaffold.

    The body may use: project (the open project) and results (the dict
    serialized to {{RESULT_PATH}}). These static helpers are available:

        FindType, Flatten, Matches, PropText, GetPropInfo, GetPropInfoIdx
        MemberList, GetPropWalk, GetReadable, GetWritable,
        RequireReadable, RequireWritable,
        MethodByShape, RequireMethod, Call, TryRead,
        MakeValue, SetProp, SetLiveProp, MakePoint, PtDict, Handle, Snap

    Args:
        class_name: C# class name for the generated script (must be unique per
            execution; callers append a uuid fragment).
        body: the reflection body, inserted inside Run()'s try block.
        extra_helpers: additional static C# members for THIS module only,
            spliced in just before [Start]. Use it for helpers that are
            specific to one feature area rather than useful to every
            reflection script (schematic.py does this).
    """
    helpers = _HELPERS
    if extra_helpers:
        if _START_MARKER not in helpers:  # pragma: no cover - guards a refactor
            raise RuntimeError(
                "live._HELPERS no longer contains the [Start] marker, so "
                "extra_helpers cannot be spliced in. Fix _START_MARKER."
            )
        helpers = helpers.replace(
            _START_MARKER, extra_helpers.rstrip() + "\n\n" + _START_MARKER, 1
        )
    return _HEADER + class_name + helpers + body + _FOOTER


# Shared body fragment: build a DMObjectsFinder over the open project.
_FINDER = '''            Type finderType = FindType("Eplan.EplApi.DataModel.DMObjectsFinder");
            object finder = Activator.CreateInstance(finderType, new object[] { project });
'''


def live_query_functions(contains: str = None, limit: int = 100,
                         timeout_seconds: float = 60.0) -> dict:
    """
    List the functions (devices) of the project currently open in EPLAN.

    Reads the live object model, so it sees the project as it stands in the
    session - including unsaved changes - which the file-based export_functions
    and the action-based search_devices do not.

    Args:
        contains: Optional case-insensitive substring filter on the function's
            identifying name (e.g. "-K" for contactors). Omit for all.
        limit: Max functions to return (default 100). "matched" in the result
            reports how many passed the filter before this cap.
        timeout_seconds: Max seconds to wait for the script (default 60).
            A full walk of a large project can exceed the standard 30s.

    Returns:
        dict with "functions" (list of {"name": ...}), "matched", "returned".
    """
    # limit is interpolated into the C# source outside any string literal, so
    # it must be a real integer - anything else would be code injection.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid limit: {limit!r}. Must be an integer."}

    body = _FINDER + '''            Type filterType = FindType("Eplan.EplApi.DataModel.FunctionsFilter");
            object filter = Activator.CreateInstance(filterType);
            MethodInfo getFunctions = finderType.GetMethod("GetFunctions", new Type[] { filterType });
            IEnumerable found = (IEnumerable)getFunctions.Invoke(finder, new object[] { filter });

            List<Dictionary<string, object>> list = new List<Dictionary<string, object>>();
            int matched = 0;
            foreach (object fn in found)
            {
                if (fn == null) continue;
                string name = PropText(fn, "Name");
                if (!Matches(name, "''' + cs_escape(contains or "") + '''")) continue;
                matched++;
                if (list.Count < ''' + str(int(limit)) + ''')
                {
                    Dictionary<string, object> d = new Dictionary<string, object>();
                    d["name"] = name == null ? "" : name;
                    list.Add(d);
                }
            }

            results["success"] = true;
            results["matched"] = matched;
            results["returned"] = list.Count;
            results["functions"] = list;
'''
    return _execute_script(
        _script("LiveQueryFunctions_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )


def live_query_pages(contains: str = None, limit: int = 100,
                     timeout_seconds: float = 60.0) -> dict:
    """
    List the pages of the project currently open in EPLAN.

    Reads the live object model, so unlike export_pages it reflects unsaved
    session state and needs no export file.

    Args:
        contains: Optional case-insensitive substring filter on the page name.
        limit: Max pages to return (default 100).
        timeout_seconds: Max seconds to wait for the script (default 60).

    Returns:
        dict with "pages" (list of {"name": ..., "pageType": ...}),
        "matched", "returned".
    """
    # limit is interpolated into the C# source outside any string literal, so
    # it must be a real integer - anything else would be code injection.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid limit: {limit!r}. Must be an integer."}

    body = _FINDER + '''            Type filterType = FindType("Eplan.EplApi.DataModel.PagesFilter");
            object filter = Activator.CreateInstance(filterType);
            MethodInfo getPages = finderType.GetMethod("GetPages", new Type[] { filterType });
            IEnumerable found = (IEnumerable)getPages.Invoke(finder, new object[] { filter });

            List<Dictionary<string, object>> list = new List<Dictionary<string, object>>();
            int matched = 0;
            foreach (object pg in found)
            {
                if (pg == null) continue;
                string name = PropText(pg, "Name");
                if (!Matches(name, "''' + cs_escape(contains or "") + '''")) continue;
                matched++;
                if (list.Count < ''' + str(int(limit)) + ''')
                {
                    Dictionary<string, object> d = new Dictionary<string, object>();
                    d["name"] = name == null ? "" : name;
                    string pt = PropText(pg, "PageType");
                    d["pageType"] = pt == null ? "" : pt;
                    list.Add(d);
                }
            }

            results["success"] = true;
            results["matched"] = matched;
            results["returned"] = list.Count;
            results["pages"] = list;
'''
    return _execute_script(
        _script("LiveQueryPages_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )


def live_set_function_text(name: str, text: str, limit: int = 1,
                           timeout_seconds: float = 60.0) -> dict:
    """
    Set the function text (FUNC_TEXT) of functions whose identifying name
    exactly equals `name`, in the project currently open in EPLAN.

    This is a WRITE to the live object model. The change lands on EPLAN's
    normal undo stack and is not persisted until the project is saved. Each
    modified function's previous text is returned so the edit is reversible.

    Args:
        name: Exact identifying name of the target function, e.g. "+TEST-K1".
            Matching is exact, not substring - use live_query_functions first
            to find the name you want.
        text: New function text.
        limit: Max functions to modify (default 1). "matched" reports how many
            functions carry the name; anything beyond `limit` is left alone, so
            a mistaken name cannot mass-edit the project.
        timeout_seconds: Max seconds to wait for the script (default 60).

    Returns:
        dict with "matched", "modified", and "details" (per-function
        {"name", "previous", "new"}).

    Note:
        FUNC_TEXT (property 20011) is backed by MultiLangString. "previous" is
        EPLAN's internal MultiLangString encoding (language markers + text), not
        clean display text - a faithful but opaque snapshot, good for detecting
        "was empty", less so for reading. Beware round-tripping it: writing a
        plain string stores it under the "no language set" key, so a value read
        back can carry a literal "??_??@" prefix. Restoring an empty value is
        clean; restoring a previously-set one may need that prefix stripped.
    """
    # limit is interpolated into the C# source outside any string literal, so
    # it must be a real integer - anything else would be code injection.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid limit: {limit!r}. Must be an integer."}

    if not name:
        return {"success": False, "message": "name is required"}

    body = _FINDER + '''            Type filterType = FindType("Eplan.EplApi.DataModel.FunctionsFilter");
            object filter = Activator.CreateInstance(filterType);
            MethodInfo getFunctions = finderType.GetMethod("GetFunctions", new Type[] { filterType });
            IEnumerable found = (IEnumerable)getFunctions.Invoke(finder, new object[] { filter });

            string targetName = "''' + cs_escape(name) + '''";
            string newText = "''' + cs_escape(text or "") + '''";
            List<Dictionary<string, object>> details = new List<Dictionary<string, object>>();
            int matched = 0;

            foreach (object fn in found)
            {
                if (fn == null) continue;
                string fname = PropText(fn, "Name");
                if (fname != targetName) continue;

                matched++;
                if (details.Count >= ''' + str(int(limit)) + ''') continue;

                // fn.Properties is a FunctionPropertyList, which exposes
                // FUNC_TEXT (function text, property 20011) directly as a
                // PropertyValue. Per the PropertyValue.Set docs, a PropertyValue
                // "accrued from property list or from StorableObject" writes
                // through to the original location on Set() - so no indexer and
                // no PropertyIdentifier plumbing is needed.
                PropertyInfo propsProp = GetPropInfo(fn.GetType(), "Properties");
                if (propsProp == null)
                    throw new Exception("Function has no Properties member.");
                object props = propsProp.GetValue(fn, null);
                PropertyInfo ftProp = GetPropInfo(props.GetType(), "FUNC_TEXT");
                if (ftProp == null)
                    throw new Exception("Property list " + props.GetType().FullName +
                        " has no FUNC_TEXT property.");
                object pv = ftProp.GetValue(props, null);

                string previous = "";
                try
                {
                    PropertyInfo isEmpty = GetPropInfo(pv.GetType(), "IsEmpty");
                    bool empty = isEmpty != null && (bool)isEmpty.GetValue(pv, null);
                    if (!empty) previous = pv.ToString();
                }
                catch { previous = ""; }

                MethodInfo setter = pv.GetType().GetMethod("Set", new Type[] { typeof(string) });
                if (setter == null)
                    throw new Exception("PropertyValue has no Set(string) overload.");
                setter.Invoke(pv, new object[] { newText });

                Dictionary<string, object> d = new Dictionary<string, object>();
                d["name"] = fname;
                d["previous"] = previous;
                d["new"] = newText;
                details.Add(d);
            }

            results["success"] = true;
            results["matched"] = matched;
            results["modified"] = details.Count;
            results["details"] = details;
'''
    return _execute_script(
        _script("LiveSetFunctionText_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )


def live_set_connection_designations(name: str, designations: list, limit: int = 1,
                                     timeout_seconds: float = 60.0) -> dict:
    """
    Set the connection point designations of a function in the project
    currently open in EPLAN.

    These are the connection point numbers shown on the multi-line schematic
    (e.g. "Y09" and "Y10" on a two-point function). EPLAN's dialogs show the
    whole set joined with a pilcrow separator - "Y09<pilcrow>Y10" - but the API
    stores one designation PER INDEX, so pass them as a list and this tool
    writes index 1, 2, ... in order. Do NOT pass a pre-joined string; it would
    land entirely in index 1.

    This is a WRITE to the live object model. The change lands on EPLAN's
    normal undo stack and is not persisted until the project is saved. The
    previous designations are returned so the edit is reversible.

    Note this changes the connection point NUMBERS, not their geometry.
    Connection point position and direction (PinBase.Location / .Direction) are
    read-only in the object model; moving a connection point means authoring
    the symbol itself, or swapping the function's SymbolVariant.

    Args:
        name: Exact identifying name of the target function, e.g. "+-TEST".
            Matching is exact, not substring - use live_query_functions first.
        designations: Designations in connection point order, e.g.
            ["Y11", "Y12"]. Written to indices 1..N. Passing fewer than the
            function has leaves the remaining points untouched.
        limit: Max functions to modify (default 1). "matched" reports how many
            functions carry the name; anything beyond `limit` is left alone.
        timeout_seconds: Max seconds to wait for the script (default 60).

    Returns:
        dict with "matched", "modified", and "details" (per-function
        {"name", "page", "previous", "new"}, previous/new being lists in
        index order).
    """
    if not name:
        return {"success": False, "message": "name is required"}

    if isinstance(designations, str) or not isinstance(designations, (list, tuple)):
        return {"success": False,
                "error": "designations must be a list of strings, one per connection "
                         "point (e.g. [\"Y11\", \"Y12\"]). Do not pass a single joined string."}
    designations = list(designations)
    if not designations:
        return {"success": False, "error": "designations must not be empty."}
    if any(d is None for d in designations):
        return {"success": False, "error": "designations must not contain None."}
    designations = [str(d) for d in designations]
    if any(PILCROW in d for d in designations):
        return {"success": False,
                "error": "a single designation must not contain the pilcrow separator - "
                         "pass one list element per connection point."}

    # limit is interpolated into the C# source outside any string literal, so
    # it must be a real integer - anything else would be code injection.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid limit: {limit!r}. Must be an integer."}

    cs_list = ", ".join('"' + cs_escape(d) + '"' for d in designations)

    body = _FINDER + '''            Type filterType = FindType("Eplan.EplApi.DataModel.FunctionsFilter");
            object filter = Activator.CreateInstance(filterType);
            MethodInfo getFunctions = finderType.GetMethod("GetFunctions", new Type[] { filterType });
            IEnumerable found = (IEnumerable)getFunctions.Invoke(finder, new object[] { filter });

            string targetName = "''' + cs_escape(name) + '''";
            string[] wanted = new string[] { ''' + cs_list + ''' };
            List<Dictionary<string, object>> details = new List<Dictionary<string, object>>();
            int matched = 0;

            foreach (object fn in found)
            {
                if (fn == null) continue;
                string fname = PropText(fn, "Name");
                if (fname != targetName) continue;

                matched++;
                if (details.Count >= ''' + str(int(limit)) + ''') continue;

                PropertyInfo propsProp = GetPropInfo(fn.GetType(), "Properties");
                if (propsProp == null)
                    throw new Exception("Function has no Properties member.");
                object props = propsProp.GetValue(fn, null);

                // Connection point designation is property 20022, declared both
                // plain and indexed; the INDEXED form addresses one connection
                // point, so it is the one to use here.
                PropertyInfo cdIdx = GetPropInfoIdx(props.GetType(), "FUNC_CONNECTIONDESIGNATION");
                if (cdIdx == null)
                    throw new Exception("Property list " + props.GetType().FullName +
                        " has no indexed FUNC_CONNECTIONDESIGNATION property.");

                List<string> before = new List<string>();
                List<string> after = new List<string>();

                for (int i = 0; i < wanted.Length; i++)
                {
                    int slot = i + 1;

                    object pv = cdIdx.GetValue(props, new object[] { slot });
                    PropertyInfo ie = GetPropInfo(pv.GetType(), "IsEmpty");
                    bool empty = ie != null && (bool)ie.GetValue(pv, null);
                    before.Add(empty ? "" : pv.ToString());

                    MethodInfo setter = pv.GetType().GetMethod("Set", new Type[] { typeof(string) });
                    if (setter == null)
                        throw new Exception("PropertyValue has no Set(string) overload.");
                    setter.Invoke(pv, new object[] { wanted[i] });

                    // Read back through a FRESH fetch of the same slot, so the
                    // reported value proves the write reached the object rather
                    // than a local PropertyValue wrapper.
                    object pv2 = cdIdx.GetValue(props, new object[] { slot });
                    PropertyInfo ie2 = GetPropInfo(pv2.GetType(), "IsEmpty");
                    bool empty2 = ie2 != null && (bool)ie2.GetValue(pv2, null);
                    after.Add(empty2 ? "" : pv2.ToString());
                }

                string pageName = "";
                PropertyInfo pgProp = GetPropInfo(fn.GetType(), "Page");
                if (pgProp != null)
                {
                    object pg = pgProp.GetValue(fn, null);
                    if (pg != null)
                    {
                        string pn = PropText(pg, "Name");
                        if (pn != null) pageName = pn;
                    }
                }

                Dictionary<string, object> d = new Dictionary<string, object>();
                d["name"] = fname;
                d["page"] = pageName;
                d["previous"] = before;
                d["new"] = after;
                details.Add(d);
            }

            results["success"] = true;
            results["matched"] = matched;
            results["modified"] = details.Count;
            results["details"] = details;
'''
    return _execute_script(
        _script("LiveSetConnDesignations_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )


def live_read_check_messages(start_index: int = 1, limit: int = 50, contains: str = None,
                              timeout_seconds: float = 60.0) -> dict:
    """
    Read itemized messages from EPLAN's "Message management" dialog (Tools >
    Review > Messages, action XMsgManagementShowAction) - the per-item results
    of the last check run (eplan_check_project / eplan_check_pages / ribbon
    "Check project").

    get_system_messages CANNOT see these: a check run only ever appends two
    summary lines ("N messages appeared...") to the general system message
    tree. The actual N itemized entries live in a separate store,
    Eplan.EplApi.EServices.PrjMessagesCollection - "Collection of project
    messages of the last check run" per the API docs - which is what this
    reads.

    That namespace is Eplan.EplApi.EServices, one of the ones that does NOT
    compile with `using` inside the EPLAN script engine (CS0234 - same trap as
    Eplan.EplApi.DataModel/HEServices, see api-data-access.md). This tool
    reaches it the same way live_query_functions reaches DataModel: by runtime
    reflection over the already-loaded assemblies (FindType + Activator +
    MethodInfo.Invoke), never by referencing the namespace at compile time.

    Indexing is 1-based and matches the order EPLAN's own Message management
    dialog lists items in, so "message number 71 in the dialog" is
    start_index=71, limit=1.

    Args:
        start_index: 1-based position of the first message to return (default
            1). Use this to page through a large check run, or to jump
            straight to one position to compare against what the user sees
            on screen ("is message #1940 the same as what I see?").
        limit: How many consecutive messages to return from start_index
            (default 50, hard-capped at 500 - the collection can hold
            thousands of entries and this is a read tool, not a full dump).
        contains: Optional case-insensitive substring filter on the message
            text. Applied together with the start_index/limit window (i.e.
            this narrows what's returned FROM that window, it does not search
            the whole collection) - pass a wide limit if you need to search
            broadly. Omit to return every message in the window.
        timeout_seconds: Max seconds to wait for the script (default 60).
            Raise it for very large projects - the whole collection is
            enumerated once regardless of the window requested.

    Returns:
        dict with "total" (Count of the full collection, read directly - not
        by walking), "enumerated" (how many entries the enumerator actually
        walked before stopping at end_index; less than total whenever the
        window ends before the collection does), "matched" (how many
        in-window messages passed the contains filter), and "messages": a
        list of {"index" (1-based), "id" (message number), "text" (full
        substituted message text), "object1Type" (.NET type name of the
        first object the message is attached to, e.g. "Terminal", "Function",
        or "" if none)}.
    """
    try:
        start_index = int(start_index)
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False,
                "error": f"start_index/limit must be integers (got start_index={start_index!r}, limit={limit!r})."}
    if start_index < 1:
        return {"success": False, "error": "start_index is 1-based; must be >= 1."}
    if limit < 1:
        return {"success": False, "error": "limit must be >= 1."}
    limit = min(limit, 500)
    end_index = start_index + limit - 1

    body = '''            Type collType = FindType("Eplan.EplApi.EServices.PrjMessagesCollection");
            if (collType == null)
                throw new Exception("PrjMessagesCollection type not found - the EServices assembly may not be loaded in this process.");
            object coll = Activator.CreateInstance(collType, new object[] { project });

            PropertyInfo countProp = collType.GetProperty("Count");
            int total = countProp != null ? (int)countProp.GetValue(coll, null) : -1;

            MethodInfo getEnumMethod = collType.GetMethod("GetPrjMsgEnumerator");
            object it = getEnumMethod.Invoke(coll, null);
            Type itType = it.GetType();
            MethodInfo moveNext = itType.GetMethod("MoveNext");
            PropertyInfo currentProp = itType.GetProperty("CurrentProjectMessage");

            int startIdx = ''' + str(start_index) + ''';
            int endIdx = ''' + str(end_index) + ''';
            string filterText = "''' + cs_escape(contains or "") + '''";

            List<Dictionary<string, object>> messages = new List<Dictionary<string, object>>();
            int idx = 0;
            int matched = 0;
            bool has = (bool)moveNext.Invoke(it, null);
            while (has)
            {
                idx++;
                if (idx > endIdx) break;   // past the window - no need to keep walking further, but Count/total is already known
                object msg = currentProp.GetValue(it, null);
                if (msg != null && idx >= startIdx)
                {
                    Type msgType = msg.GetType();
                    string text = "";
                    int id = -1;
                    string objType = "";
                    try { text = (string)msgType.GetMethod("GetText").Invoke(msg, null); }
                    catch (Exception iex) { text = "<GetText failed: " + Flatten(iex) + ">"; }
                    try { id = (int)msgType.GetMethod("GetId").Invoke(msg, null); } catch { }
                    try
                    {
                        object o1 = msgType.GetMethod("GetObject1").Invoke(msg, null);
                        if (o1 != null) objType = o1.GetType().Name;
                    }
                    catch { }

                    if (Matches(text, filterText))
                    {
                        matched++;
                        Dictionary<string, object> m = new Dictionary<string, object>();
                        m["index"] = idx;
                        m["id"] = id;
                        m["text"] = text;
                        m["object1Type"] = objType;
                        messages.Add(m);
                    }
                }
                has = (bool)moveNext.Invoke(it, null);
            }

            results["success"] = true;
            results["total"] = total;
            results["enumerated"] = idx;
            results["matched"] = matched;
            results["messages"] = messages;
'''
    return _execute_script(
        _script("LiveReadCheckMessages_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )
