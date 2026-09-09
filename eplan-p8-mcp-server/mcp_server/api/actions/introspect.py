"""
API introspection - ask the running EPLAN what its own object model looks like.

WHY THIS EXISTS

`using Eplan.EplApi.DataModel;` does not compile in EPLAN's script engine
(CS0234): the engine compiles against a fixed assembly reference set. That is a
*compiler* restriction, not a capability boundary - every `Eplan.EplApi.*`
namespace is reachable at runtime through reflection. Measured on 2025.0.3:
26 namespaces, 606 public types, 0 assemblies that threw on GetTypes(), and all
6 non-preloaded `Eplan.EplApi.*.dll` loaded on demand with zero refusals.

The practical consequence is that writing against this API means looking up
member names and signatures constantly, and until now the only way to do that
from here was to hand-write a throwaway reflection script per question. Those
scripts fail for boring reasons - a guessed type name that does not exist, a
missing `using System.Reflection` - and each failure costs a round trip and a
compile. These two tools freeze that lookup so it stops being bespoke code:

    api_types      what namespaces/types exist and where they live
    api_describe   a type's constructors, methods, properties, enum values

Deliberately NOT one tool per API area. The repo already made this call once,
for the ~1,050 GUI-only actions that became 4 catalog tools rather than 1,050
wrappers (see llm.md). Wrapping a class whose only difficulty is "what is the
member called" captures nothing and costs a tool slot; the difficulty worth
freezing is the *recipe* (live_read_check_messages, live_connect_pins_routed),
not the signature.

NOTES ON THE GENERATED C#

- These two tools need NO open project and NO LockingStep - they never touch
  the object model, only its metadata. So they reuse live.py's static helper
  block but not its Run() prologue, which requires a current project and would
  make `api_describe` fail with "No project is currently open" when the answer
  has nothing to do with a project.
- `Type.GetProperty(name)` throws AmbiguousMatchException on EPLAN types for
  two independent reasons (a derived class narrowing a return type; a property
  list declaring both a plain and an indexed form of one name). Everything here
  therefore walks the hierarchy DeclaredOnly, most-derived first, and reports
  `declaredIn` per member so the caller can see which level they will bind to.
- Every value interpolated into the script goes through cs_escape.
"""

import uuid

from ._base import cs_escape
from .live import _HEADER, _HELPERS, _START_MARKER
from .scripted import _execute_script

# live.py splices caller-supplied helpers in at this marker; we split at the
# same seam to take its static helpers (FindType, Flatten, Matches,
# MemberList, ...) WITHOUT its [Start] Run(), which opens a project.
if _START_MARKER not in _HELPERS:  # pragma: no cover - guards a refactor of live.py
    raise RuntimeError(
        "live._HELPERS no longer contains the [Start] marker, so introspect.py "
        "cannot reuse its static helper block. Fix _START_MARKER."
    )
_STATIC_HELPERS = _HELPERS.split(_START_MARKER)[0]


# Extra helpers, specific to describing metadata rather than reading data.
_META_HELPERS = '''
    // Readable name for a type in a signature. Nested types come back as
    // "Enums+RepresentationType" from Name, which is what you actually need to
    // pass to FindType, so keep it rather than prettifying it away.
    static string TypeName(Type t)
    {
        if (t == null) return "void";
        return t.Name;
    }

    static string Sig(ParameterInfo[] ps)
    {
        StringBuilder sb = new StringBuilder();
        sb.Append("(");
        for (int i = 0; i < ps.Length; i++)
        {
            if (i > 0) sb.Append(", ");
            sb.Append(TypeName(ps[i].ParameterType));
            sb.Append(" ");
            sb.Append(ps[i].Name);
        }
        sb.Append(")");
        return sb.ToString();
    }

    // Load the Eplan.EplApi.*.dll that EPLAN has not touched yet. Off by
    // default: it mutates the process (loads assemblies into EPLAN) for a
    // read-only question, and the six lazy ones are rarely what you want.
    static int LoadLazyAssemblies()
    {
        int n = 0;
        try
        {
            string binDir = Path.GetDirectoryName(
                typeof(Eplan.EplApi.Base.BaseException).Assembly.Location);
            foreach (string f in Directory.GetFiles(binDir, "Eplan.EplApi.*.dll"))
            {
                try { Assembly.Load(Path.GetFileNameWithoutExtension(f)); n++; }
                catch { }
            }
        }
        catch { }
        return n;
    }
'''


def _script(class_name, body):
    """Wrap a body in a project-free variant of live.py's scaffold."""
    return (
        _HEADER + class_name + _STATIC_HELPERS + _META_HELPERS + '''
    [Start]
    public void Run()
    {
        Dictionary<string, object> results = new Dictionary<string, object>();
        try
        {
''' + body + '''
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = Flatten(ex);
        }

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
'''
    )


def api_types(contains: str = None, namespace: str = None, limit: int = 200,
              load_all: bool = False, timeout_seconds: float = 60.0) -> dict:
    """
    List the API types the running EPLAN actually has loaded. Read-only.

    Answers "does this type exist, what is its full name, and which assembly is
    it in" without writing a throwaway reflection script. Needs no open
    project.

    Two response shapes, because a bare call should be a map and not a dump:

      - No `contains` and no `namespace`: returns the NAMESPACE SUMMARY only
        (~26 rows on 2025.0.3) and an empty "types". Start here, then drill in.
      - Either filter given: returns matching types too, capped at `limit`.

    Args:
        contains: Case-insensitive substring matched against the type's FULL
            name (namespace included), e.g. "Macro", "PropertyList",
            "DataModel.Page". Omit to filter by namespace alone.
        namespace: Namespace PREFIX to restrict to. Default "Eplan.EplApi".
            Pass "Eplan" to widen to every Eplan assembly (SDK/WPF/IdentityClient
            included), or e.g. "Eplan.EplApi.DataModel.MasterData" to narrow.
        limit: Max types returned (default 200, hard cap 2000). The namespace
            summary is never capped - it is small and is the useful map.
        load_all: Also Assembly.Load the `Eplan.EplApi.*.dll` present in the
            install's Bin but not yet loaded (on 2025.0.3: MasterDatau,
            RecorderToolsu, RemoteClientu, Starteru, WebServicesu,
            WebServiceu). Default False, because this mutates the EPLAN process
            for a read-only question. Set True only when a type you expect is
            genuinely missing.
        timeout_seconds: Default 60.

    Returns:
        dict with "namespaces" (list of {"namespace", "types", "assembly"},
        sorted by name), "namespaceCount", "typeCount" (total matching the
        namespace prefix, before `contains`), "types" (list of {"name" (full
        name), "namespace", "assembly", "kind": class|interface|enum|struct|
        delegate}), "returned", "truncated", and "assembliesScanned".

        A type's "name" is exactly what api_describe and the FindType helper in
        generated scripts expect. Nested types use the CLR form with '+'
        (e.g. "Eplan.EplApi.DataModel.MasterData.PageMacro+Enums+NumerationMode").
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False,
                "error": f"limit must be an integer (got {limit!r})."}
    if limit < 1:
        return {"success": False, "error": "limit must be >= 1."}
    limit = min(limit, 2000)

    prefix = namespace if namespace else "Eplan.EplApi"
    want_types = bool(contains) or bool(namespace)

    body = '''            string prefix = "''' + cs_escape(prefix) + '''";
            string filter = "''' + cs_escape(contains or "") + '''";
            int limit = ''' + str(limit) + ''';
            bool wantTypes = ''' + ("true" if want_types else "false") + ''';

            if (''' + ("true" if load_all else "false") + ''')
                results["lazyAssembliesLoaded"] = LoadLazyAssemblies();

            Dictionary<string, int> nsCount = new Dictionary<string, int>();
            Dictionary<string, string> nsAsm = new Dictionary<string, string>();
            List<Dictionary<string, object>> types = new List<Dictionary<string, object>>();
            int scanned = 0, totalInPrefix = 0, returned = 0;
            bool truncated = false;

            foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
            {
                string an = a.GetName().Name;
                if (an.IndexOf("Eplan", StringComparison.OrdinalIgnoreCase) < 0) continue;
                scanned++;
                Type[] found = null;
                // A partially loadable assembly still yields the types it CAN
                // resolve; a silent `continue` here would under-report and look
                // like the type does not exist.
                try { found = a.GetTypes(); }
                catch (ReflectionTypeLoadException rtle) { found = rtle.Types; }
                catch { continue; }
                if (found == null) continue;

                foreach (Type t in found)
                {
                    if (t == null || !t.IsPublic) continue;
                    string ns = t.Namespace;
                    if (ns == null || !ns.StartsWith(prefix, StringComparison.Ordinal)) continue;

                    totalInPrefix++;
                    if (!nsCount.ContainsKey(ns)) { nsCount[ns] = 0; nsAsm[ns] = an; }
                    nsCount[ns] = nsCount[ns] + 1;

                    if (!wantTypes) continue;
                    string full = t.FullName;
                    if (!Matches(full, filter)) continue;
                    if (returned >= limit) { truncated = true; continue; }

                    string kind = "class";
                    if (t.IsEnum) kind = "enum";
                    else if (t.IsInterface) kind = "interface";
                    else if (t.IsValueType) kind = "struct";
                    else if (typeof(Delegate).IsAssignableFrom(t)) kind = "delegate";

                    Dictionary<string, object> d = new Dictionary<string, object>();
                    d["name"] = full;
                    d["namespace"] = ns;
                    d["assembly"] = an;
                    d["kind"] = kind;
                    types.Add(d);
                    returned++;
                }
            }

            List<string> nsNames = new List<string>(nsCount.Keys);
            nsNames.Sort(StringComparer.Ordinal);
            List<Dictionary<string, object>> nsList = new List<Dictionary<string, object>>();
            foreach (string ns in nsNames)
            {
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["namespace"] = ns;
                d["types"] = nsCount[ns];
                d["assembly"] = nsAsm[ns];
                nsList.Add(d);
            }

            results["success"] = true;
            results["assembliesScanned"] = scanned;
            results["namespaces"] = nsList;
            results["namespaceCount"] = nsList.Count;
            results["typeCount"] = totalInPrefix;
            results["types"] = types;
            results["returned"] = returned;
            results["truncated"] = truncated;
            if (!wantTypes)
                results["hint"] = "Namespace summary only. Pass contains= or namespace= to list types.";
'''
    return _execute_script(
        _script("ApiTypes_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )


def api_describe(type_name: str, members: str = "all", contains: str = None,
                 limit: int = 200, inherited: bool = True,
                 timeout_seconds: float = 60.0) -> dict:
    """
    Describe one API type: constructors, methods, properties, enum values.
    Read-only, needs no open project.

    This is the lookup that reflection code needs constantly and that guessing
    gets wrong. Real examples from this repo's own history, each of which cost
    a failed script before being settled by exactly this kind of probe:
    `MDPart.GenericProductGroup` (not `ProductTopGroup`);
    `BaseException.MessageLevel` (not `.Level`); `Page.SetName(PagePropertyList)`
    and that `PAGE_COUNTER` is writable; that `Insert.WindowMacro` has eight
    overloads and only one takes a Page + PointD.

    Members are reported with `declaredIn`, the type in the hierarchy that
    actually declares them. That matters because `Type.GetProperty(name)`
    throws AmbiguousMatchException on EPLAN types - a derived class narrows a
    base property's return type (Function.Properties), or one property list
    declares both a plain and an `[int]` form of the same name. Binding rules
    follow most-derived-first, which is the order used here.

    For an enum, "fields" carries name -> numeric value. Read it before writing
    `Enum.ToObject(t, n)` in a generated script: that is how MoveKind.Relative=2
    and NumerationMode.None=1 were established.

    Args:
        type_name: Full CLR type name, e.g. "Eplan.EplApi.DataModel.Page",
            "Eplan.EplApi.HEServices.Insert". Nested types use '+':
            "Eplan.EplApi.DataModel.MasterData.PageMacro+Enums+NumerationMode".
            If it is not found, the error lists types whose SIMPLE name matches,
            so a wrong namespace is a one-turn fix rather than a dead end.
        members: Which categories to return - "all" (default), "methods",
            "properties", "constructors" or "fields". Anything else is refused
            with the valid values.
        contains: Case-insensitive substring filter on the MEMBER name (not the
            type name). Use it on big types: DataModel property lists run to
            hundreds of members.
        limit: Max members per category (default 200, hard cap 2000).
        inherited: Walk the base-type chain (default True). False reports only
            what this exact type declares. System.Object is never walked.
        timeout_seconds: Default 60.

    Returns:
        dict with "type" (full name), "assembly", "kind", "baseTypes" (the
        chain, most-derived first), "isEnum", and per category:

          "constructors": [{"signature", "declaredIn"}]
          "methods":      [{"name", "signature", "returns", "declaredIn",
                            "static"}]  - property accessors (get_/set_/add_/
                            remove_) are excluded; properties are listed
                            separately
          "properties":   [{"name", "type", "access": "RW"|"R"|"W",
                            "indexParams", "declaredIn", "static"}]
          "fields":       [{"name", "type", "value", "declaredIn"}] - for an
                            enum, "value" is the number to pass to
                            Enum.ToObject

        Each category also gets a "<category>Truncated" bool.
    """
    if not type_name or not str(type_name).strip():
        return {"success": False, "error": "type_name is required."}
    valid_members = ("all", "methods", "properties", "constructors", "fields")
    members = str(members).lower().strip()
    if members not in valid_members:
        return {"success": False,
                "error": f"members must be one of {', '.join(valid_members)} (got {members!r})."}
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False,
                "error": f"limit must be an integer (got {limit!r})."}
    if limit < 1:
        return {"success": False, "error": "limit must be >= 1."}
    limit = min(limit, 2000)

    body = '''            string typeName = "''' + cs_escape(str(type_name).strip()) + '''";
            string want = "''' + cs_escape(members) + '''";
            string filter = "''' + cs_escape(contains or "") + '''";
            int limit = ''' + str(limit) + ''';
            bool walkBase = ''' + ("true" if inherited else "false") + ''';

            // Resolve without FindType's throw, so a miss can answer with
            // candidates instead of a bare "not found".
            Type target = null;
            foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
            {
                try { Type t = a.GetType(typeName); if (t != null) { target = t; break; } }
                catch { }
            }
            if (target == null)
            {
                string simple = typeName;
                int dot = simple.LastIndexOf('.');
                if (dot >= 0 && dot < simple.Length - 1) simple = simple.Substring(dot + 1);
                List<string> candidates = new List<string>();
                foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
                {
                    string an = a.GetName().Name;
                    if (an.IndexOf("Eplan", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    Type[] found = null;
                    try { found = a.GetTypes(); }
                    catch (ReflectionTypeLoadException rtle) { found = rtle.Types; }
                    catch { continue; }
                    if (found == null) continue;
                    foreach (Type t in found)
                    {
                        if (t == null || !t.IsPublic || t.FullName == null) continue;
                        if (string.Equals(t.Name, simple, StringComparison.OrdinalIgnoreCase)
                            || t.FullName.IndexOf(simple, StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            if (!candidates.Contains(t.FullName)) candidates.Add(t.FullName);
                            if (candidates.Count >= 25) break;
                        }
                    }
                    if (candidates.Count >= 25) break;
                }
                candidates.Sort(StringComparer.Ordinal);
                results["success"] = false;
                results["error"] = "Type '" + typeName + "' not found in any loaded Eplan assembly."
                    + (candidates.Count > 0
                        ? " Types with a similar name (use one of these verbatim):"
                        : " No similarly named type either - check the spelling, or call api_types(load_all=true) if it lives in an assembly EPLAN has not loaded yet.");
                results["candidates"] = candidates;
                string j0 = Newtonsoft.Json.JsonConvert.SerializeObject(results);
                File.WriteAllText(@"{{RESULT_PATH}}", j0);
                return;
            }

            results["type"] = target.FullName;
            results["assembly"] = target.Assembly.GetName().Name;
            results["isEnum"] = target.IsEnum;
            string kind = "class";
            if (target.IsEnum) kind = "enum";
            else if (target.IsInterface) kind = "interface";
            else if (target.IsValueType) kind = "struct";
            results["kind"] = kind;

            List<string> chain = new List<string>();
            Type cur = target;
            while (cur != null && cur.FullName != "System.Object")
            {
                chain.Add(cur.FullName);
                if (!walkBase) break;
                cur = cur.BaseType;
            }
            results["baseTypes"] = chain;

            BindingFlags bf = BindingFlags.Public | BindingFlags.Instance
                            | BindingFlags.Static | BindingFlags.DeclaredOnly;

            List<Dictionary<string, object>> ctors = new List<Dictionary<string, object>>();
            List<Dictionary<string, object>> methods = new List<Dictionary<string, object>>();
            List<Dictionary<string, object>> props = new List<Dictionary<string, object>>();
            List<Dictionary<string, object>> fields = new List<Dictionary<string, object>>();
            bool ctorsTrunc = false, methodsTrunc = false, propsTrunc = false, fieldsTrunc = false;

            // Enum members live on the type itself, not the walked chain.
            if (target.IsEnum && (want == "all" || want == "fields"))
            {
                foreach (object v in Enum.GetValues(target))
                {
                    string nm = v.ToString();
                    if (!Matches(nm, filter)) continue;
                    if (fields.Count >= limit) { fieldsTrunc = true; break; }
                    Dictionary<string, object> d = new Dictionary<string, object>();
                    d["name"] = nm;
                    d["type"] = TypeName(Enum.GetUnderlyingType(target));
                    d["value"] = Convert.ToInt64(v);
                    d["declaredIn"] = target.FullName;
                    fields.Add(d);
                }
            }

            if (want == "all" || want == "constructors")
            {
                foreach (ConstructorInfo ci in target.GetConstructors())
                {
                    if (ctors.Count >= limit) { ctorsTrunc = true; break; }
                    Dictionary<string, object> d = new Dictionary<string, object>();
                    d["signature"] = target.Name + Sig(ci.GetParameters());
                    d["declaredIn"] = target.FullName;
                    ctors.Add(d);
                }
            }

            cur = target;
            while (cur != null && cur.FullName != "System.Object")
            {
                if (want == "all" || want == "methods")
                {
                    MethodInfo[] mis = null;
                    try { mis = cur.GetMethods(bf); } catch { mis = null; }
                    if (mis != null)
                        foreach (MethodInfo mi in mis)
                        {
                            // Accessors are noise: the property they belong to
                            // is reported in "properties" with its access mode.
                            if (mi.IsSpecialName) continue;
                            if (!Matches(mi.Name, filter)) continue;
                            if (methods.Count >= limit) { methodsTrunc = true; break; }
                            Dictionary<string, object> d = new Dictionary<string, object>();
                            d["name"] = mi.Name;
                            d["signature"] = mi.Name + Sig(mi.GetParameters());
                            d["returns"] = TypeName(mi.ReturnType);
                            d["declaredIn"] = cur.FullName;
                            d["static"] = mi.IsStatic;
                            methods.Add(d);
                        }
                }

                if (want == "all" || want == "properties")
                {
                    PropertyInfo[] pis = null;
                    try { pis = cur.GetProperties(bf); } catch { pis = null; }
                    if (pis != null)
                        foreach (PropertyInfo pi in pis)
                        {
                            if (!Matches(pi.Name, filter)) continue;
                            if (props.Count >= limit) { propsTrunc = true; break; }
                            string access = pi.CanRead
                                ? (pi.CanWrite ? "RW" : "R")
                                : (pi.CanWrite ? "W" : "");
                            Dictionary<string, object> d = new Dictionary<string, object>();
                            d["name"] = pi.Name;
                            d["type"] = TypeName(pi.PropertyType);
                            d["access"] = access;
                            d["indexParams"] = Sig(pi.GetIndexParameters());
                            d["declaredIn"] = cur.FullName;
                            MethodInfo acc = pi.GetGetMethod();
                            if (acc == null) acc = pi.GetSetMethod();
                            d["static"] = acc != null && acc.IsStatic;
                            props.Add(d);
                        }
                }

                if (!target.IsEnum && (want == "all" || want == "fields"))
                {
                    FieldInfo[] fis = null;
                    try { fis = cur.GetFields(bf); } catch { fis = null; }
                    if (fis != null)
                        foreach (FieldInfo fi in fis)
                        {
                            if (!Matches(fi.Name, filter)) continue;
                            if (fields.Count >= limit) { fieldsTrunc = true; break; }
                            Dictionary<string, object> d = new Dictionary<string, object>();
                            d["name"] = fi.Name;
                            d["type"] = TypeName(fi.FieldType);
                            d["declaredIn"] = cur.FullName;
                            object fv = null;
                            if (fi.IsLiteral) { try { fv = fi.GetRawConstantValue(); } catch { } }
                            d["value"] = fv == null ? null : fv.ToString();
                            fields.Add(d);
                        }
                }

                if (!walkBase) break;
                cur = cur.BaseType;
            }

            results["success"] = true;
            results["constructors"] = ctors;
            results["methods"] = methods;
            results["properties"] = props;
            results["fields"] = fields;
            results["constructorsTruncated"] = ctorsTrunc;
            results["methodsTruncated"] = methodsTrunc;
            results["propertiesTruncated"] = propsTrunc;
            results["fieldsTruncated"] = fieldsTrunc;
'''
    return _execute_script(
        _script("ApiDescribe_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )
