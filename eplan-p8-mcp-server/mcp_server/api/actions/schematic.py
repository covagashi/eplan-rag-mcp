"""
Schematic authoring - create pages, place devices, wire them, read it back.

This is the write side of the live object model. The read-only tools in live.py
can enumerate a project; these can BUILD one, which is what a model needs before
it can draw a schematic rather than describe one.

Six primitives, in the order a caller uses them:

    live_symbol_catalog     what symbols exist, and where their pins are
    live_create_page        a new schematic page
    live_place_symbol       a device on that page at a coordinate
    live_connect_pins       a connection line between two devices' pins
    live_read_page          the canonical page state
    live_remove_placement   undo one placement, or the whole page

DESIGN RULES, each of which exists because its absence produced a real failure
during development:

1. EVERY WRITE RETURNS THE READ-BACK. `page_after` on a write is the SAME
   structure `live_read_page` returns, produced by the same C# serializer, so a
   write and a later verification cannot disagree. EPLAN's auto-connect
   frequently does something other than what was asked; the only way a model
   learns that is by being shown what actually landed.

2. A REFLECTIVE MISS IS FATAL, AND SAYS WHAT THE TYPE DECLARES. During the
   spike, reading `Page.Placements` - a property that does not exist - returned
   null, the code treated null as "no placements", and it reported an empty page
   after successfully creating three objects. A silent miss is exactly how a
   write appears to succeed having done nothing, so the helpers in live.py throw
   with a MemberList instead. The correct accessor is `AllPlacements`.

3. WRITES ARE SCRATCH-GUARDED BY DEFAULT. `allow_real_project=False` is the
   signature default and the guard runs twice: once in Python before the script
   is even built, and again inside the LockingStep against the project EPLAN
   actually has focused - because between those two moments the user could have
   switched projects. Master data on a real workstation resolves to production
   paths; a schematic writer loose in a live project is not an acceptable
   default.

4. NEVER `new PointD(...)`. The point type is always taken from the member the
   point is handed to (`create.GetParameters()[2].ParameterType`), because the
   script engine may compile against a different Eplan.EplApi.Base than the
   loaded object model references, and a typeof()-built signature then silently
   matches nothing.

MEASURED ON EPLAN 2027.0.1 - the call forms below are the ones that actually
work, not the ones the docs suggest:

    Page.Create(Project, DocumentType, PagePropertyList)      (only 3-arg form)
    new SymbolLibrary(Project, name) / new Symbol(lib, name)
    new SymbolVariant(Symbol, int)                            (NOT (int) alone)
    Function.Create(Page, SymbolVariant, PointD, PointD)      INSTANCE method
    DynamicConnectionLine.Create(Page) + SetGraphics(PointD, PointD)
    Page.AllPlacements / Page.Functions                       (NOT .Placements)

Traps that cost real debugging time, all encoded below:

  - PropertyValue has NO public constructor. `Activator.CreateInstance(pvType,
    "P1")` throws MissingMethodException; it is built through its static
    op_Implicit. See live.MakeValue.
  - A SymbolVariant instance CANNOT be reused: the second Create throws
    ObjectAlreadyCreatedException. A fresh SymbolLibrary+Symbol+SymbolVariant is
    built per placement, so a batch loop must NOT hoist them.
  - PointD.ToString() returns the type name, not coordinates. Read X/Y.
  - A placed Function's Name is "+" until a device tag is assigned - placing and
    tagging are separate operations.
  - Page naming follows project structure settings, so the created page's Name
    is READ BACK and reported rather than predicted.
  - Not every symbol name exists in a library, which is why symbol discovery is
    primitive #1 rather than an afterthought.
"""

import uuid

from ._base import cs_escape
from .live import _script
from .scripted import _execute_script
from .fixtures import SCRATCH_ROOT
from .schematic_model import (
    SchematicValueError,
    cs_bool,
    cs_double,
    cs_int,
    cs_text,
    DEFAULT_GRID_MM,
    absolute_pins,
    axis_aligned,
    axis_alignment_message,
    pins_coincide,
)

__all__ = [
    "live_symbol_catalog",
    "live_create_page",
    "live_place_symbol",
    "live_connect_pins",
    "live_read_page",
    "live_remove_placement",
]


# Page types worth offering by name. DocumentTypeManager.DocumentType has 78
# values; these are the ones a schematic author actually creates. An unlisted
# value is still accepted - it is validated against the live enum inside the
# script, which reports the real names on a miss.
COMMON_PAGE_TYPES = (
    "Circuit",              # the ordinary multi-line schematic page
    "CircuitSingleLine",
    "Overview",
    "Graphics",
    "PanelLayout",
    "TerminalDiagram",
    "InterconnectDiagram",
    "CableLayout",
)


# ---------------------------------------------------------------------------
# Module-specific C# helpers, spliced in ahead of [Start] by live._script.
# ---------------------------------------------------------------------------

_HELPERS_SCHEMATIC = r'''
    // ---- the scratch guard -------------------------------------------------
    // Re-checked HERE, inside the LockingStep, against the project EPLAN really
    // has focused. The Python pre-flight cannot be trusted on its own: the user
    // may have switched projects between that check and this script running.
    static void GuardScratch(object project, bool allowReal, string scratchRoot)
    {
        string path = null;
        string[] candidates = new string[] {
            "ProjectDirectoryPath", "ProjectLinkFilePath", "ProjectFullName" };
        List<string> tried = new List<string>();
        foreach (string name in candidates)
        {
            PropertyInfo pi = GetReadable(project.GetType(), name);
            tried.Add(name + (pi == null ? "(absent)" : "(present)"));
            if (pi == null) continue;
            try
            {
                object v = pi.GetValue(project, null);
                if (v != null && v.ToString().Length > 0) { path = v.ToString(); break; }
            }
            catch { }
        }
        if (path == null)
            throw new Exception("Cannot determine the open project's path, so the " +
                "scratch guard cannot run - refusing to write. Tried: " +
                string.Join(", ", tried.ToArray()) + ". " + MemberList(project.GetType(), false));

        string norm = path.Replace("/", "\\").TrimEnd('\\').ToUpperInvariant();
        string root = scratchRoot.Replace("/", "\\").TrimEnd('\\').ToUpperInvariant();
        bool inside = (norm == root) || norm.StartsWith(root + "\\", StringComparison.Ordinal);

        if (!inside && !allowReal)
            throw new Exception("REFUSING TO WRITE: the open project is '" + path +
                "', which is outside the scratch root '" + scratchRoot + "'. " +
                "Schematic writes default to scratch-only so a real project cannot " +
                "be modified by accident. Clone a disposable copy with " +
                "eplan_scratch_project_create, or pass allow_real_project=true if " +
                "you genuinely intend to write to this project.");
    }

    // ---- page lookup -------------------------------------------------------
    // Every page-taking primitive starts here, so a wrong page name fails
    // BEFORE anything is created.
    static object FindPage(object project, string wanted)
    {
        Type finderType = FindType("Eplan.EplApi.DataModel.DMObjectsFinder");
        object finder = Activator.CreateInstance(finderType, new object[] { project });
        Type filterType = FindType("Eplan.EplApi.DataModel.PagesFilter");
        object filter = Activator.CreateInstance(filterType);
        MethodInfo getPages = RequireMethod(finderType, "GetPages",
            new string[] { filterType.Name }, false);
        IEnumerable pages = (IEnumerable)Call(getPages, finder, new object[] { filter });

        List<string> names = new List<string>();
        foreach (object p in pages)
        {
            if (p == null) continue;
            string n = PropText(p, "Name");
            if (n == wanted) return p;
            if (names.Count < 30 && n != null) names.Add(n);
        }
        throw new Exception("No page named '" + wanted + "' in this project. " +
            "Pages present (up to 30): " + string.Join(" | ", names.ToArray()));
    }

    // ---- placements on a page ---------------------------------------------
    static IEnumerable PagePlacements(object page)
    {
        PropertyInfo pi = GetReadable(page.GetType(), "AllPlacements");
        if (pi == null)
            throw new Exception("Page has no readable 'AllPlacements'. Note that " +
                "'Placements' does NOT exist on Page - reading it returns null and " +
                "makes a populated page look empty. " + MemberList(page.GetType(), false));
        object v = pi.GetValue(page, null);
        if (v == null)
            throw new Exception("Page.AllPlacements returned null; refusing to report " +
                "an empty page, because that is indistinguishable from a write that " +
                "did nothing.");
        return (IEnumerable)v;
    }

    // ---- THE serializer ---------------------------------------------------
    // Shared by place / connect / read / remove, so a write's page_after and a
    // later live_read_page cannot disagree about the same page.
    // Deliberately never calls GetLogicalArea(): it throws
    // NotImplementedException for anything but macro boxes, location boxes,
    // shieldings and cable definition lines.
    static Dictionary<string, object> DumpPlacement(object pl, bool withPins)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        List<string> absent = new List<string>();

        d["clrType"] = pl.GetType().Name;
        d["handle"] = Handle(pl);

        object nameVal = TryRead(pl, "Name", absent);
        if (nameVal != null) d["name"] = nameVal.ToString();

        object loc = TryRead(pl, "Location", absent);
        if (loc != null) d["location"] = PtDict(loc);

        MethodInfo bbox = MethodByShape(pl.GetType(), "GetBoundingBox", new string[] { }, false);
        if (bbox != null)
        {
            try
            {
                object bb = bbox.Invoke(pl, null);
                if (bb is IEnumerable)
                {
                    List<object> pts = new List<object>();
                    foreach (object p in (IEnumerable)bb) if (p != null) pts.Add(PtDict(p));
                    if (pts.Count > 0) d["boundingBox"] = pts;
                }
            }
            catch (Exception ex) { absent.Add("GetBoundingBox (threw: " + Flatten(ex) + ")"); }
        }
        else absent.Add("GetBoundingBox");

        // Which symbol this is - the triple a caller needs to place another one.
        object variant = TryRead(pl, "SymbolVariant", absent);
        if (variant != null)
        {
            Dictionary<string, object> sym = new Dictionary<string, object>();
            object lib = TryRead(variant, "SymbolLibraryName", null);
            object sname = TryRead(variant, "SymbolName", null);
            object vnr = TryRead(variant, "VariantNr", null);
            if (lib != null) sym["library"] = lib.ToString();
            if (sname != null) sym["name"] = sname.ToString();
            if (vnr != null) sym["variantNr"] = Convert.ToInt32(vnr);
            if (sym.Count > 0) d["symbol"] = sym;
        }

        if (withPins)
        {
            object cps = TryRead(pl, "GraphicalConnectionPoints", absent);
            if (cps is IEnumerable)
            {
                List<object> pins = new List<object>();
                int idx = 0;
                foreach (object pin in (IEnumerable)cps)
                {
                    if (pin == null) { idx++; continue; }
                    Dictionary<string, object> pd = new Dictionary<string, object>();
                    object pidx = TryRead(pin, "Index", null);
                    pd["index"] = pidx == null ? idx : Convert.ToInt32(pidx);
                    object des = TryRead(pin, "Designation", null);
                    if (des != null) pd["designation"] = des.ToString();
                    object ploc = TryRead(pin, "Location", null);
                    // RAW on purpose: whether this is absolute or an offset is
                    // decided on the Python side against the bounding box.
                    // Emitting a guess here is how offsets get published as
                    // page coordinates.
                    if (ploc != null) pd["raw"] = PtDict(ploc);
                    pins.Add(pd);
                    idx++;
                }
                d["pins"] = pins;
            }
        }

        if (absent.Count > 0) d["absentMembers"] = absent;
        return d;
    }

    static Dictionary<string, object> ReadPage(object page, int limit, bool withPins,
                                               string[] onlyTypes)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        d["page"] = PropText(page, "Name");
        d["pageType"] = PropText(page, "PageType");

        object grid = TryRead(page, "GridSize", null);
        if (grid != null) d["gridSize"] = Convert.ToDouble(grid);
        object size = TryRead(page, "Size", null);
        if (size != null) d["size"] = PtDict(size);

        // onlyTypes matters more than it looks. A real schematic page is mostly
        // GRAPHICS: measured on a production go-by, one Circuit page held 1887
        // placements of which the first 40 were all PolyLine. An unfiltered read
        // therefore truncates before it reaches a single device, so a caller
        // looking for devices sees none and concludes the page is empty.
        List<object> items = new List<object>();
        int total = 0, matched = 0;
        foreach (object pl in PagePlacements(page))
        {
            if (pl == null) continue;
            total++;
            if (onlyTypes != null && onlyTypes.Length > 0)
            {
                bool keep = false;
                string tn = pl.GetType().Name;
                foreach (string want in onlyTypes)
                    if (string.Equals(tn, want, StringComparison.OrdinalIgnoreCase)) { keep = true; break; }
                if (!keep) continue;
            }
            matched++;
            if (items.Count < limit) items.Add(DumpPlacement(pl, withPins));
        }
        // placementCount stays the TRUE total on the page, so a filtered read is
        // never mistaken for an empty page; "matched" is what the filter kept.
        d["placementCount"] = total;
        d["matched"] = matched;
        d["returned"] = items.Count;
        d["truncated"] = matched > items.Count;
        d["placements"] = items;
        if (onlyTypes != null && onlyTypes.Length > 0) d["filteredTo"] = onlyTypes;
        return d;
    }

    // ---- handle -> object, scoped to one page -----------------------------
    // Deliberately NOT StorableObject.FromStringIdentifier: scanning the named
    // page means an object on a DIFFERENT page can never be resolved, which
    // makes the page argument a structural guard rather than a comment.
    static object ResolveOnPage(object page, string handle)
    {
        List<string> present = new List<string>();
        foreach (object pl in PagePlacements(page))
        {
            if (pl == null) continue;
            string h = Handle(pl);
            if (h == handle) return pl;
            if (present.Count < 20) present.Add(pl.GetType().Name + "=" + h);
        }
        throw new Exception("No placement with handle '" + handle + "' on page '" +
            PropText(page, "Name") + "'. Handles are stable only within one EPLAN " +
            "session - if EPLAN restarted or the project was reopened, re-read the " +
            "page to get current handles. Present (up to 20): " +
            string.Join(" | ", present.ToArray()));
    }

    static object FindPinAt(object pl, int index)
    {
        object cps = TryRead(pl, "GraphicalConnectionPoints", null);
        if (!(cps is IEnumerable))
            throw new Exception(pl.GetType().Name + " has no readable " +
                "GraphicalConnectionPoints, so it has no pins to connect. " +
                MemberList(pl.GetType(), false));
        List<string> have = new List<string>();
        int i = 0;
        foreach (object pin in (IEnumerable)cps)
        {
            if (pin == null) { i++; continue; }
            object pidx = TryRead(pin, "Index", null);
            int real = pidx == null ? i : Convert.ToInt32(pidx);
            if (real == index) return pin;
            have.Add(real.ToString());
            i++;
        }
        throw new Exception("No pin with index " + index + " on " + pl.GetType().Name +
            ". Indices present: " + string.Join(", ", have.ToArray()) +
            ". Call live_read_page(include_pins=true) to see them.");
    }
'''


# ---------------------------------------------------------------------------
# Python-side shared plumbing
# ---------------------------------------------------------------------------

def _cls(prefix):
    """Unique C# class name - two scripts in one session must not collide."""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])


def _guard_prelude(allow_real):
    """The C# line that runs the scratch guard, or a note that it was waived."""
    return (
        '            GuardScratch(project, %s, "%s");\n'
        % (cs_bool(allow_real), cs_escape(SCRATCH_ROOT))
    )


def _shape(raw, timeout_hint=None):
    """
    Flatten _execute_script's envelope into one dict, and never report success
    for a script that reported failure.

    _execute_script returns {"success": True, "results": {...}} whenever the
    result FILE was written - including when the script's own body caught an
    exception and wrote success:false into it. Returning that outer True is how
    a reflective failure comes back looking like a success with the real error
    nested a level down.
    """
    if not isinstance(raw, dict):
        return {"success": False, "error": "Unexpected result: %r" % (raw,)}
    if not raw.get("success"):
        msg = raw.get("message") or raw.get("error") or "script did not run"
        out = {"success": False, "error": msg}
        if "Timeout" in str(msg):
            out["hint"] = (
                "A timeout here usually means the generated C# failed to COMPILE, "
                "not that it ran slowly: a compile error writes no result file, and "
                "EPLAN reports it only in its own system-message log. Call "
                "eplan_get_system_messages(min_level='Error') to see the real "
                "reason."
            )
            if timeout_hint:
                out["hint"] += " " + timeout_hint
        return out
    inner = raw.get("results")
    if not isinstance(inner, dict):
        return {"success": False, "error": "Script wrote no usable result: %r" % (inner,)}
    if inner.get("success") is False:
        return {"success": False, "error": inner.get("error") or "unknown script error",
                "project": inner.get("project")}
    return dict(inner, success=True)


def _annotate_pins(payload):
    """
    Add absolute pin coordinates to every placement, computed on this side.

    The script emits each pin's RAW value; whether that is absolute or an offset
    is decided here against the placement's bounding box, and a pin whose frame
    cannot be established is reported as unknown rather than as a coordinate.
    """
    for page_key in ("page_after", "page_before"):
        if isinstance(payload.get(page_key), dict):
            _annotate_pins(payload[page_key])
    unknown = 0
    for pl in payload.get("placements") or []:
        if not pl.get("pins"):
            continue
        pl["pins"] = absolute_pins(pl)
        unknown += sum(1 for p in pl["pins"] if p.get("frame") == "unknown")
    if unknown:
        payload["pinFrameWarning"] = (
            "%d pin(s) could not be resolved to an absolute page coordinate "
            "(frame='unknown'), so their 'point' is null. Do not treat that as "
            "(0,0) - read the page again, or connect by coordinate instead of "
            "by pin." % unknown
        )
    return payload


def _err(exc):
    return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 1. Symbol discovery
# ---------------------------------------------------------------------------

def live_symbol_catalog(library: str = None, symbol: str = None,
                        contains: str = None, limit: int = 100,
                        timeout_seconds: float = 120.0) -> dict:
    """
    Discover the symbols available in the open project, and their pins.

    Start here. A model cannot place a symbol it cannot name, and symbol names
    are not guessable: on the reference installation "SL", "S", "O", "Q1" exist
    in NFPA_symbol_en_US while "K1" and "M1" do not. There is no naming
    convention to fall back on.

    Three depths, selected by which arguments you pass:

        ()                        -> the project's symbol libraries
        (library="NFPA_symbol_en_US")
                                  -> that library's symbols, with pin counts
        (library=..., symbol="SL")
                                  -> that symbol's variants and each variant's
                                     connection points

    Symbols come from the project's OWN libraries, so this reflects what this
    project can actually place - a scratch clone carries project-local copies
    inside its .edb, which is why writing to a clone cannot touch production
    master data.

    Args:
        library: Symbol library name. Omit to list libraries.
        symbol: Symbol name within `library`. Requires `library`.
        contains: Case-insensitive substring filter on symbol names, applied at
            depth 2. A symbol with >= 2 connection points is one you can wire.
        limit: Max entries returned at the chosen depth (default 100).
        timeout_seconds: Walking a large library is slow; default 120s.

    Returns:
        depth 1: {"libraries": [name, ...]}
        depth 2: {"library", "symbols": [{"index", "name", "connectionPoints",
                  "variantCount"}, ...], "matched", "returned", "truncated"}
        depth 3: {"library", "symbol", "variants": [{"variantNr", "pins":
                  [{"index", "designation", "raw"}]}]}

        A symbol's pin geometry is RELATIVE to its insertion point until it is
        placed, so at depth 3 "raw" is exactly that and no absolute coordinate
        is reported.
    """
    try:
        limit = cs_int(limit, "limit", minimum=1, maximum=2000)
        lib_cs = cs_escape(cs_text(library, "library")) if library else None
        sym_cs = cs_escape(cs_text(symbol, "symbol")) if symbol else None
        contains_cs = cs_escape(cs_text(contains, "contains", allow_empty=True) or "")
    except SchematicValueError as exc:
        return _err(exc)

    if symbol and not library:
        return {"success": False,
                "error": "symbol requires library - a symbol name is only unique "
                         "within one library. Call this with no arguments to list "
                         "libraries first."}

    if not library:
        body = '''            PropertyInfo slProp = RequireReadable(project.GetType(), "SymbolLibraries");
            object sl = slProp.GetValue(project, null);
            List<string> libs = new List<string>();
            if (sl is IEnumerable)
            {
                foreach (object one in (IEnumerable)sl)
                {
                    if (one == null) continue;
                    string n = PropText(one, "Name");
                    libs.Add(n == null ? one.ToString() : n);
                }
            }
            results["libraries"] = libs;
            results["depth"] = 1;
            results["next"] = "Call again with library=<one of these> to list its symbols.";
'''
    elif not symbol:
        body = '''            Type libType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolLibrary");
            Type symType = FindType("Eplan.EplApi.DataModel.MasterData.Symbol");
            ConstructorInfo libCtor = libType.GetConstructor(new Type[] { project.GetType(), typeof(string) });
            if (libCtor == null)
                throw new Exception("SymbolLibrary has no (Project, string) constructor. " +
                    MemberList(libType, true));
            object lib = null;
            try { lib = libCtor.Invoke(new object[] { project, LIB }); }
            catch (TargetInvocationException tie)
            { throw new Exception("Cannot open symbol library '" + LIB + "': " + Flatten(tie.InnerException)); }

            ConstructorInfo symCtorInt = symType.GetConstructor(new Type[] { libType, typeof(int) });
            if (symCtorInt == null)
                throw new Exception("Symbol has no (SymbolLibrary, int) constructor. " +
                    MemberList(symType, true));

            List<object> syms = new List<object>();
            int matched = 0;
            // Walk by INDEX - proven to enumerate a library exhaustively, and it
            // stops at the first index that does not resolve.
            for (int i = 0; i < 5000; i++)
            {
                object sym = null;
                try { sym = symCtorInt.Invoke(new object[] { lib, i }); }
                catch { break; }
                if (sym == null) break;
                if (PropText(sym, "IsValid") != "True") continue;
                string sname = PropText(sym, "Name");
                if (sname == null) continue;
                if (!Matches(sname, CONTAINS)) continue;
                matched++;
                if (syms.Count >= LIMIT) continue;

                Dictionary<string, object> d = new Dictionary<string, object>();
                d["index"] = i;
                d["name"] = sname;
                object variants = TryRead(sym, "Variants", null);
                int vcount = 0;
                int pinCount = -1;
                if (variants is IEnumerable)
                {
                    foreach (object v in (IEnumerable)variants)
                    {
                        if (v == null) continue;
                        vcount++;
                        if (pinCount < 0)
                        {
                            object cps = TryRead(v, "ConnectionPoints", null);
                            int n = 0;
                            if (cps is IEnumerable)
                                foreach (object c in (IEnumerable)cps) if (c != null) n++;
                            pinCount = n;
                        }
                    }
                }
                d["variantCount"] = vcount;
                d["connectionPoints"] = pinCount < 0 ? 0 : pinCount;
                syms.Add(d);
            }
            results["library"] = LIB;
            results["symbols"] = syms;
            results["matched"] = matched;
            results["returned"] = syms.Count;
            results["truncated"] = matched > syms.Count;
            results["depth"] = 2;
            results["next"] = "A symbol with connectionPoints >= 2 can be wired. " +
                "Call again with symbol=<name> for its variants and pin geometry.";
'''.replace("LIB", '"%s"' % lib_cs).replace("CONTAINS", '"%s"' % contains_cs) \
   .replace("LIMIT", str(limit))
    else:
        body = '''            Type libType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolLibrary");
            Type symType = FindType("Eplan.EplApi.DataModel.MasterData.Symbol");
            ConstructorInfo libCtor = libType.GetConstructor(new Type[] { project.GetType(), typeof(string) });
            object lib = libCtor.Invoke(new object[] { project, LIB });
            ConstructorInfo symCtorStr = symType.GetConstructor(new Type[] { libType, typeof(string) });
            if (symCtorStr == null)
                throw new Exception("Symbol has no (SymbolLibrary, string) constructor. " +
                    MemberList(symType, true));
            object sym = null;
            try { sym = symCtorStr.Invoke(new object[] { lib, SYM }); }
            catch (TargetInvocationException tie)
            { throw new Exception("Cannot open symbol '" + SYM + "' in library '" + LIB +
                "': " + Flatten(tie.InnerException) +
                ". Not every name exists in every library - list the library first."); }
            if (sym == null || PropText(sym, "IsValid") != "True")
                throw new Exception("Symbol '" + SYM + "' does not resolve in library '" +
                    LIB + "'. Call live_symbol_catalog(library=...) to see real names.");

            results["library"] = LIB;
            results["symbol"] = PropText(sym, "Name");
            results["symbolType"] = PropText(sym, "Type");

            List<object> variants = new List<object>();
            object vs = TryRead(sym, "Variants", null);
            if (vs is IEnumerable)
            {
                foreach (object v in (IEnumerable)vs)
                {
                    if (v == null) continue;
                    Dictionary<string, object> vd = new Dictionary<string, object>();
                    object vnr = TryRead(v, "VariantNr", null);
                    vd["variantNr"] = vnr == null ? variants.Count : Convert.ToInt32(vnr);
                    List<object> pins = new List<object>();
                    object cps = TryRead(v, "ConnectionPoints", null);
                    int i = 0;
                    if (cps is IEnumerable)
                    {
                        foreach (object pin in (IEnumerable)cps)
                        {
                            if (pin == null) { i++; continue; }
                            Dictionary<string, object> pd = new Dictionary<string, object>();
                            object pidx = TryRead(pin, "Index", null);
                            pd["index"] = pidx == null ? i : Convert.ToInt32(pidx);
                            object des = TryRead(pin, "Designation", null);
                            if (des != null) pd["designation"] = des.ToString();
                            object ploc = TryRead(pin, "Location", null);
                            if (ploc != null) pd["raw"] = PtDict(ploc);
                            pins.Add(pd);
                            i++;
                        }
                    }
                    vd["pins"] = pins;
                    vd["connectionPoints"] = pins.Count;
                    variants.Add(vd);
                }
            }
            results["variants"] = variants;
            results["depth"] = 3;
            results["note"] = "Pin 'raw' values here are relative to the symbol's " +
                "insertion point - an unplaced symbol has no page coordinate. " +
                "live_place_symbol reports absolute pins once it is placed.";
'''.replace("LIB", '"%s"' % lib_cs).replace("SYM", '"%s"' % sym_cs)

    raw = _execute_script(
        _script(_cls("SymCat"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    )
    return _shape(raw)


# ---------------------------------------------------------------------------
# 2. Create a page
# ---------------------------------------------------------------------------

def live_create_page(plant: str = None, location: str = None, counter: int = 1,
                     page_type: str = "Circuit",
                     allow_real_project: bool = False,
                     timeout_seconds: float = 90.0) -> dict:
    """
    Create a schematic page in the open project. WRITES - scratch-only by default.

    The page's final NAME is read back and returned rather than predicted,
    because page naming follows the project's structure settings: on the
    reference installation, setting plant + location + counter produced
    "+SPIKE5/950" - the plant designation did not appear in the name at all. Use
    the returned "page" value for every later call.

    Only name-forming properties can be set at creation time; that is EPLAN's
    own documented restriction on the property list Page.Create accepts.

    There is deliberately NO `description` argument. PagePropertyList has no
    page-description member - checked by reflection on 2027.0.1, which lists
    only DESIGNATION_*_DESCR (descriptions OF a structure identifier) and
    PAGE_CUSTOM_SUPPLEMENTARYFIELD01..100. Setting a description needs the
    generic Property[AnyPropertyId] indexer and a decision about which property
    id a "page description" is, which is a separate change rather than a guess
    made here. Until then, a caller asking for one gets no argument to pass
    rather than an argument that silently does nothing.

    Args:
        plant: Plant designation (the "=" part). Optional.
        location: Location designation (the "+" part). Optional.
        counter: Page counter - the numeric part of the name. Default 1.
        page_type: A DocumentTypeManager.DocumentType name. Default "Circuit",
            which is the ordinary multi-line schematic page. Common values:
            Circuit, CircuitSingleLine, Overview, Graphics, PanelLayout,
            TerminalDiagram, InterconnectDiagram, CableLayout. An unknown value
            is refused with the real enum names.
        allow_real_project: Must be True to write to a project outside the
            scratch root. Default False - see the module docstring.
        timeout_seconds: Default 90s.

    Returns:
        {"success", "page" (the actual name), "pageType", "gridSize", "size",
         "handle", "undo": {"tool", "page"}, "page_after"}

        "gridSize" is the coordinate quantum for later placements - 3.175mm
        (one eighth inch) on the reference installation. Placing off-grid tends
        to produce devices that look right and refuse to auto-connect.
    """
    try:
        counter = cs_int(counter, "counter", minimum=0)
        page_type = cs_text(page_type, "page_type")
        plant_cs = cs_escape(cs_text(plant, "plant")) if plant else None
        loc_cs = cs_escape(cs_text(location, "location")) if location else None
    except SchematicValueError as exc:
        return _err(exc)

    if not plant and not location:
        return {
            "success": False,
            "error": "Give at least one of plant or location: with neither, the "
                     "created page's name is only its counter, which collides with "
                     "existing pages and is not addressable afterwards.",
        }

    sets = []
    if plant_cs:
        sets.append('            routes["DESIGNATION_PLANT"] = SetProp(ppl, "DESIGNATION_PLANT", "%s");\n' % plant_cs)
    if loc_cs:
        sets.append('            routes["DESIGNATION_LOCATION"] = SetProp(ppl, "DESIGNATION_LOCATION", "%s");\n' % loc_cs)
    sets.append('            routes["PAGE_COUNTER"] = SetProp(ppl, "PAGE_COUNTER", (int)%d);\n' % counter)

    body = _guard_prelude(allow_real_project) + '''
            Type pageType = FindType("Eplan.EplApi.DataModel.Page");
            Type pplType = FindType("Eplan.EplApi.DataModel.PagePropertyList");
            Type dtmType = FindType("Eplan.EplApi.DataModel.DocumentTypeManager");
            Type docEnum = dtmType.GetNestedType("DocumentType");
            if (docEnum == null)
                throw new Exception("DocumentTypeManager has no nested DocumentType enum. " +
                    MemberList(dtmType, false));

            object docType = null;
            try { docType = Enum.Parse(docEnum, PAGETYPE, false); }
            catch
            {
                string[] names = Enum.GetNames(docEnum);
                Array.Sort(names);
                throw new Exception("Unknown page_type " + PAGETYPE + ". Valid values: " +
                    string.Join(", ", names));
            }

            object ppl = Activator.CreateInstance(pplType);
            Dictionary<string, object> routes = new Dictionary<string, object>();
''' + "".join(sets) + '''            results["propertyRoutes"] = routes;

            object page = Activator.CreateInstance(pageType);
            MethodInfo create = RequireMethod(pageType, "Create",
                new string[] { project.GetType().Name, docEnum.Name, pplType.Name }, false);
            results["boundSignature"] = create.ToString();
            Call(create, page, new object[] { project, docType, ppl });

            // Read the name BACK. Page naming follows project structure settings,
            // so predicting it is unreliable - measured live, a plant designation
            // set here did not surface in the name.
            string realName = PropText(page, "Name");
            if (realName == null || realName.Length == 0)
                throw new Exception("Page was created but has no readable Name, so it " +
                    "cannot be addressed by later calls.");
            results["page"] = realName;
            results["pageType"] = PropText(page, "PageType");
            results["handle"] = Handle(page);
            object grid = TryRead(page, "GridSize", null);
            if (grid != null) results["gridSize"] = Convert.ToDouble(grid);
            object size = TryRead(page, "Size", null);
            if (size != null) results["size"] = PtDict(size);
            results["page_after"] = ReadPage(page, 50, false, null);
'''
    body = body.replace("PAGETYPE", '"%s"' % cs_escape(page_type))

    raw = _execute_script(
        _script(_cls("MkPage"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    )
    out = _shape(raw)
    if out.get("success") and out.get("page"):
        out["undo"] = {"tool": "eplan_live_remove_placement",
                       "page": out["page"], "remove_page": True}
        if page_type not in COMMON_PAGE_TYPES:
            out["note"] = ("page_type %r is outside the common set; it was accepted "
                           "by the live enum." % page_type)
    return out


# ---------------------------------------------------------------------------
# 3. Place a symbol
# ---------------------------------------------------------------------------

def live_place_symbol(page: str, library: str, symbol: str, x: float, y: float,
                      variant_nr: int = 0, x2: float = None, y2: float = None,
                      snap_to_grid: bool = True,
                      allow_real_project: bool = False,
                      timeout_seconds: float = 90.0) -> dict:
    """
    Place a device (Function) on a page at a coordinate. WRITES - scratch-only by default.

    Uses Function.Create(Page, SymbolVariant, PointD, PointD), which places
    directly at the target coordinate. There is deliberately no create-then-move
    step: a device that exists momentarily at (0,0) can auto-connect to whatever
    is near the origin of a populated page.

    The placed function's NAME will be "+" - a function is unnamed until a device
    tag is assigned, which is a separate operation. That is expected, not a
    failure.

    Args:
        page: Page name, exactly as live_create_page or live_read_page reports it.
        library: Symbol library name (see live_symbol_catalog).
        symbol: Symbol name within that library.
        x, y: Insertion point in page millimetres.
        variant_nr: Symbol variant index, default 0 (variant "A").
        x2, y2: Opposite corner of the function's logical area. Both default to
            the insertion point, giving a degenerate rectangle there. Pass them
            only if a placement needs an explicit extent.
        snap_to_grid: Round coordinates to the page's own GridSize before
            placing (default True). Off-grid devices commonly look correct and
            then refuse to auto-connect.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 90s.

    Returns:
        {"success", "page", "handle", "placed" (the DumpPlacement record,
         including absolute pin coordinates), "requested"/"snapped" coordinates,
         "undo": {...}, "page_after"}

        Every pin carries "frame": "absolute", "relative" or "unknown". An
        "unknown" pin has "point": null and MUST NOT be treated as (0,0) - the
        result carries pinFrameWarning when any pin is in that state.
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        lib_cs = cs_escape(cs_text(library, "library"))
        sym_cs = cs_escape(cs_text(symbol, "symbol"))
        x_cs = cs_double(x, "x")
        y_cs = cs_double(y, "y")
        variant_nr = cs_int(variant_nr, "variant_nr", minimum=0)
        x2_cs = cs_double(x2, "x2") if x2 is not None else None
        y2_cs = cs_double(y2, "y2") if y2 is not None else None
    except SchematicValueError as exc:
        return _err(exc)

    body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, PAGENAME);
            double grid = 0.0;
            object gridVal = TryRead(page, "GridSize", null);
            if (gridVal != null) grid = Convert.ToDouble(gridVal);
            bool doSnap = SNAP;

            double ax = XVAL, ay = YVAL;
            double bx = X2VAL, by = Y2VAL;
            Dictionary<string, object> requested = new Dictionary<string, object>();
            requested["x"] = ax; requested["y"] = ay;
            results["requested"] = requested;
            if (doSnap && grid > 0.0001)
            {
                ax = Snap(ax, grid); ay = Snap(ay, grid);
                bx = Snap(bx, grid); by = Snap(by, grid);
            }
            Dictionary<string, object> used = new Dictionary<string, object>();
            used["x"] = ax; used["y"] = ay;
            results["snapped"] = used;
            results["gridSize"] = grid;

            // A FRESH SymbolLibrary + Symbol + SymbolVariant per placement.
            // A SymbolVariant instance cannot be reused: the second Create on it
            // throws ObjectAlreadyCreatedException. A batch loop must build these
            // inside the loop, not hoist them.
            Type libType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolLibrary");
            Type symType = FindType("Eplan.EplApi.DataModel.MasterData.Symbol");
            Type varType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolVariant");
            Type funcType = FindType("Eplan.EplApi.DataModel.Function");

            ConstructorInfo libCtor = libType.GetConstructor(new Type[] { project.GetType(), typeof(string) });
            if (libCtor == null)
                throw new Exception("SymbolLibrary has no (Project, string) ctor. " + MemberList(libType, true));
            object lib = null;
            try { lib = libCtor.Invoke(new object[] { project, LIBNAME }); }
            catch (TargetInvocationException tie)
            { throw new Exception("Cannot open symbol library " + LIBNAME + ": " + Flatten(tie.InnerException)); }

            ConstructorInfo symCtor = symType.GetConstructor(new Type[] { libType, typeof(string) });
            if (symCtor == null)
                throw new Exception("Symbol has no (SymbolLibrary, string) ctor. " + MemberList(symType, true));
            object sym = null;
            try { sym = symCtor.Invoke(new object[] { lib, SYMNAME }); }
            catch (TargetInvocationException tie)
            { throw new Exception("Cannot open symbol " + SYMNAME + " in " + LIBNAME + ": " +
                Flatten(tie.InnerException) + ". Use live_symbol_catalog to list real names."); }
            if (sym == null || PropText(sym, "IsValid") != "True")
                throw new Exception("Symbol " + SYMNAME + " does not resolve in " + LIBNAME +
                    ". Use live_symbol_catalog(library=...) for the real names.");

            // NOT Activator.CreateInstance(varType, new object[]{ index }) - there is
            // no single-int constructor and that throws MissingMethodException.
            ConstructorInfo varCtor = varType.GetConstructor(new Type[] { symType, typeof(int) });
            if (varCtor == null)
                throw new Exception("SymbolVariant has no (Symbol, int) ctor. " + MemberList(varType, true));
            object variant = null;
            try { variant = varCtor.Invoke(new object[] { sym, VARNR }); }
            catch (TargetInvocationException tie)
            { throw new Exception("Symbol " + SYMNAME + " has no variant " + VARNR + ": " +
                Flatten(tie.InnerException)); }

            // Bind the 4-arg INSTANCE overload by parameter-type NAMES, and take the
            // point type from the member itself rather than resolving PointD.
            MethodInfo create = RequireMethod(funcType, "Create",
                new string[] { "Page", "SymbolVariant", "PointD", "PointD" }, false);
            results["boundSignature"] = create.ToString();
            Type ptType = create.GetParameters()[2].ParameterType;

            object fn = Activator.CreateInstance(funcType);
            Call(create, fn, new object[] {
                page, variant, MakePoint(ptType, ax, ay), MakePoint(ptType, bx, by) });

            results["page"] = PropText(page, "Name");
            results["handle"] = Handle(fn);
            results["placed"] = DumpPlacement(fn, true);
            results["nameNote"] = "A newly placed function has no device tag yet, so " +
                "its name is '+' until one is assigned. That is expected.";
            results["page_after"] = ReadPage(page, 200, true, null);
'''
    body = (body
            .replace("PAGENAME", '"%s"' % page_cs)
            .replace("LIBNAME", '"%s"' % lib_cs)
            .replace("SYMNAME", '"%s"' % sym_cs)
            .replace("X2VAL", x2_cs if x2_cs is not None else x_cs)
            .replace("Y2VAL", y2_cs if y2_cs is not None else y_cs)
            .replace("XVAL", x_cs)
            .replace("YVAL", y_cs)
            .replace("VARNR", str(variant_nr))
            .replace("SNAP", cs_bool(snap_to_grid)))

    raw = _execute_script(
        _script(_cls("Place"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    )
    out = _shape(raw)
    if out.get("success"):
        _annotate_pins(out)
        if out.get("placed"):
            out["placed"]["pins"] = absolute_pins(out["placed"])
        if out.get("handle"):
            out["undo"] = {"tool": "eplan_live_remove_placement",
                           "page": out.get("page"), "handle": out["handle"]}
    return out


# ---------------------------------------------------------------------------
# 4. Connect two pins
# ---------------------------------------------------------------------------

def live_connect_pins(page: str, from_handle: str, from_pin: int,
                      to_handle: str, to_pin: int,
                      allow_real_project: bool = False,
                      timeout_seconds: float = 90.0) -> dict:
    """
    Draw a connection line between two placed devices' pins. WRITES - scratch-only.

    Addresses the endpoints by HANDLE and PIN INDEX rather than by coordinate,
    so the caller never computes a millimetre: the server resolves each pin's
    absolute position and draws between them.

    Only straight segments. A DynamicConnectionLine's SetGraphics(p1, p2) is one
    segment, so if the two pins share neither X nor Y this refuses rather than
    drawing a diagonal EPLAN will not treat as a wire. Move one device onto the
    other's axis, or place an intermediate device.

    IMPORTANT about what "connected" means here: this draws the graphical
    connection line and reports the resulting page state. Whether EPLAN's
    connection logic has produced a logical Connection object between the two
    functions is a separate question that needs a report generation to settle -
    so the result says "lineDrawn", not "devices are wired".

    Args:
        page: Page name both placements are on.
        from_handle, to_handle: Handles from live_place_symbol or live_read_page.
            Handles are valid only within one EPLAN session.
        from_pin, to_pin: Pin indices, as reported by live_read_page's "pins".
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 90s.

    Returns:
        {"success", "page", "lineDrawn", "handle" (the connection line),
         "from"/"to" (resolved pin geometry with its frame), "undo", "page_after"}
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        from_cs = cs_escape(cs_text(from_handle, "from_handle"))
        to_cs = cs_escape(cs_text(to_handle, "to_handle"))
        from_pin = cs_int(from_pin, "from_pin", minimum=0)
        to_pin = cs_int(to_pin, "to_pin", minimum=0)
    except SchematicValueError as exc:
        return _err(exc)

    if from_handle == to_handle and from_pin == to_pin:
        return {"success": False,
                "error": "from and to are the same pin of the same placement; a "
                         "connection needs two distinct endpoints."}

    # Step 1: resolve both pins WITHOUT writing, so the axis check happens on
    # this side where it can be tested with EPLAN closed.
    probe_body = '''            object page = FindPage(project, PAGENAME);
            object a = ResolveOnPage(page, FROMH);
            object b = ResolveOnPage(page, TOH);
            object pinA = FindPinAt(a, FROMP);
            object pinB = FindPinAt(b, TOP);

            Dictionary<string, object> ra = new Dictionary<string, object>();
            ra["placement"] = DumpPlacement(a, true);
            object la = TryRead(pinA, "Location", null);
            if (la != null) ra["pinRaw"] = PtDict(la);
            results["from"] = ra;

            Dictionary<string, object> rb = new Dictionary<string, object>();
            rb["placement"] = DumpPlacement(b, true);
            object lb = TryRead(pinB, "Location", null);
            if (lb != null) rb["pinRaw"] = PtDict(lb);
            results["to"] = rb;
            results["page"] = PropText(page, "Name");
'''
    probe_body = (probe_body
                  .replace("PAGENAME", '"%s"' % page_cs)
                  .replace("FROMH", '"%s"' % from_cs)
                  .replace("TOH", '"%s"' % to_cs)
                  .replace("FROMP", str(from_pin))
                  .replace("TOP", str(to_pin)))
    probe = _shape(_execute_script(
        _script(_cls("PinProbe"), probe_body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    ))
    if not probe.get("success"):
        return probe

    pa = _pin_point(probe.get("from"), from_pin)
    pb = _pin_point(probe.get("to"), to_pin)
    if pa is None or pb is None:
        return {
            "success": False,
            "error": "Could not establish an absolute page coordinate for %s. A pin "
                     "whose frame is unknown cannot be connected by index - connect "
                     "by coordinate, or re-read the page."
                     % ("the 'from' pin" if pa is None else "the 'to' pin"),
            "from": probe.get("from"),
            "to": probe.get("to"),
        }
    if not axis_aligned(pa, pb):
        return {"success": False, "error": axis_alignment_message(pa, pb),
                "from_point": pa, "to_point": pb}
    if pins_coincide(pa, pb):
        return {"success": False,
                "error": "Both pins are at the same point (%.4f, %.4f); there is no "
                         "segment to draw. They may already be touching, in which "
                         "case EPLAN auto-connects them without a line."
                         % (pa["x"], pa["y"]),
                "from_point": pa, "to_point": pb}

    # Step 2: draw it.
    body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, PAGENAME);
            Type dclType = FindType("Eplan.EplApi.DataModel.DynamicConnectionLine");
            object dcl = Activator.CreateInstance(dclType);
            MethodInfo create = RequireMethod(dclType, "Create", new string[] { "Page" }, false);
            Call(create, dcl, new object[] { page });

            MethodInfo setG = RequireMethod(dclType, "SetGraphics",
                new string[] { "PointD", "PointD" }, false);
            results["boundSignature"] = setG.ToString();
            Type ptType = setG.GetParameters()[0].ParameterType;
            Call(setG, dcl, new object[] {
                MakePoint(ptType, AX, AY), MakePoint(ptType, BX, BY) });

            results["page"] = PropText(page, "Name");
            results["lineDrawn"] = true;
            results["handle"] = Handle(dcl);
            results["line"] = DumpPlacement(dcl, false);
            results["page_after"] = ReadPage(page, 200, true, null);
'''
    body = (body
            .replace("PAGENAME", '"%s"' % page_cs)
            .replace("AX", cs_double(pa["x"], "from x"))
            .replace("AY", cs_double(pa["y"], "from y"))
            .replace("BX", cs_double(pb["x"], "to x"))
            .replace("BY", cs_double(pb["y"], "to y")))

    out = _shape(_execute_script(
        _script(_cls("Connect"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    ))
    if out.get("success"):
        _annotate_pins(out)
        out["from_point"] = pa
        out["to_point"] = pb
        if out.get("handle"):
            out["undo"] = {"tool": "eplan_live_remove_placement",
                           "page": out.get("page"), "handle": out["handle"]}
        out["scopeNote"] = (
            "A graphical connection line was drawn between the two pin "
            "coordinates. Whether EPLAN has also created a LOGICAL connection "
            "between the two functions is not asserted here - that needs a "
            "connection report (eplan_generate_connections + "
            "eplan_export_connections)."
        )
    return out


def _pin_point(side, index):
    """Absolute coordinate of one pin from the probe result, or None."""
    if not isinstance(side, dict):
        return None
    placement = side.get("placement")
    if not isinstance(placement, dict):
        return None
    for pin in absolute_pins(placement):
        if pin.get("index") == index and pin.get("frame") != "unknown":
            return pin.get("point")
    return None


# ---------------------------------------------------------------------------
# 5. Read a page
# ---------------------------------------------------------------------------

def live_read_page(page: str, include_pins: bool = True, limit: int = 200,
                   types: list = None, timeout_seconds: float = 90.0) -> dict:
    """
    Read one page's full state: every placement, its geometry and its pins.

    This is the canonical page reader, and every write above returns exactly
    this structure as "page_after" - produced by the same C# serializer - so a
    write's own report and a later verification cannot disagree.

    It is also the discovery route that matters most in practice: run it against
    a page a human drew and every Function answers with its
    symbol{library, name, variantNr} triple, which tells you which symbols this
    company actually uses rather than what master data merely contains.

    Args:
        page: Page name, exactly as reported elsewhere.
        include_pins: Include each placement's connection points (default True).
        limit: Max placements returned; the true count is always reported so a
            truncated read is never mistaken for a complete one.
        types: Only return placements of these CLR types, e.g. ["Function"] for
            devices or ["DynamicConnectionLine"] for wires. Omit for everything.

            USE THIS when you are looking for devices. A real schematic page is
            mostly graphics: measured on a production go-by, one Circuit page
            held 1887 placements whose first 40 were all PolyLine. Unfiltered,
            `limit` is exhausted on graphics before a single device is reached,
            and the page looks empty.
        timeout_seconds: Default 90s.

    Returns:
        {"success", "page", "pageType", "gridSize", "size", "placementCount",
         "returned", "truncated", "placements": [...]}

        Each placement: {"clrType", "handle", "name", "location",
        "boundingBox", "symbol", "pins", "absentMembers"}.

        "absentMembers" lists members this placement TYPE does not have (a
        connection line has no SymbolVariant, for instance). It is reported
        rather than silently omitted, because a silently missing field is
        indistinguishable from a write that did nothing.

        Every pin carries "frame" ("absolute"/"relative"/"unknown") and a
        "point" that is null when the frame is unknown. Never read a null point
        as (0,0).
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        limit = cs_int(limit, "limit", minimum=1, maximum=5000)
        if types is not None:
            if isinstance(types, str):
                types = [types]
            types = [cs_escape(cs_text(t, "types entry")) for t in types]
            if not types:
                types = None
    except SchematicValueError as exc:
        return _err(exc)

    only = ("null" if not types
            else "new string[] { %s }" % ", ".join('"%s"' % t for t in types))

    body = '''            object page = FindPage(project, PAGENAME);
            Dictionary<string, object> state = ReadPage(page, LIMIT, WITHPINS, ONLYTYPES);
            foreach (KeyValuePair<string, object> kv in state) results[kv.Key] = kv.Value;
            results["handle"] = Handle(page);
'''
    body = (body
            .replace("PAGENAME", '"%s"' % page_cs)
            .replace("LIMIT", str(limit))
            .replace("ONLYTYPES", only)
            .replace("WITHPINS", cs_bool(include_pins)))

    out = _shape(_execute_script(
        _script(_cls("ReadPage"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    ))
    if out.get("success"):
        _annotate_pins(out)
    return out


# ---------------------------------------------------------------------------
# 6. Remove
# ---------------------------------------------------------------------------

def live_remove_placement(page: str, handle: str = None,
                          expect_type: str = None, remove_page: bool = False,
                          allow_real_project: bool = False,
                          timeout_seconds: float = 90.0) -> dict:
    """
    Remove one placement, or a whole page. WRITES - scratch-only by default.

    This is the undo for the writers above: each of them returns an "undo" dict
    naming this tool and the handle to pass. Reversibility is what makes the
    write side safe to iterate on.

    The handle is resolved by scanning the NAMED page only, so an object on a
    different page cannot be removed through this call - the page argument is a
    structural guard, not a hint.

    Args:
        page: Page name.
        handle: Handle of the placement to remove. Required unless
            remove_page=True.
        expect_type: Optional CLR type name ("Function",
            "DynamicConnectionLine", ...) to assert before removing. Given a
            stale handle that now resolves to something else, this refuses
            instead of deleting the wrong object.
        remove_page: Remove the whole page and everything on it. Requires
            handle to be omitted, so it cannot happen by accident.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 90s.

    Returns:
        {"success", "page", "removed", "removedType", "page_before",
         "page_after"} - or, for remove_page, {"pageRemoved": true} with
        "page_before" as the record of what was destroyed.
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        handle_cs = cs_escape(cs_text(handle, "handle")) if handle else None
        expect_cs = cs_escape(cs_text(expect_type, "expect_type")) if expect_type else None
    except SchematicValueError as exc:
        return _err(exc)

    if remove_page and handle:
        return {"success": False,
                "error": "Pass either handle (remove one placement) or "
                         "remove_page=True (remove the whole page), not both."}
    if not remove_page and not handle:
        return {"success": False,
                "error": "handle is required. To remove the entire page and "
                         "everything on it, pass remove_page=True and no handle."}

    if remove_page:
        body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, PAGENAME);
            results["page"] = PropText(page, "Name");
            // Record what is about to be destroyed BEFORE destroying it.
            results["page_before"] = ReadPage(page, 500, false, null);
            MethodInfo rm = RequireMethod(page.GetType(), "Remove", new string[] { }, false);
            Call(rm, page, null);
            results["pageRemoved"] = true;
'''
    else:
        expect_block = ""
        if expect_cs:
            expect_block = '''            if (target.GetType().Name != EXPECTTYPE)
                throw new Exception("Refusing to remove: handle resolves to a " +
                    target.GetType().Name + ", but expect_type was " + EXPECTTYPE +
                    ". Handles are session-scoped; re-read the page.");
'''.replace("EXPECTTYPE", '"%s"' % expect_cs)
        body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, PAGENAME);
            object target = ResolveOnPage(page, HANDLE);
''' + expect_block + '''            results["page"] = PropText(page, "Name");
            results["removed"] = DumpPlacement(target, false);
            results["removedType"] = target.GetType().Name;
            MethodInfo rm = RequireMethod(target.GetType(), "Remove", new string[] { }, false);
            Call(rm, target, null);
            results["page_after"] = ReadPage(page, 200, false, null);
'''
        body = body.replace("HANDLE", '"%s"' % handle_cs)
    body = body.replace("PAGENAME", '"%s"' % page_cs)

    return _shape(_execute_script(
        _script(_cls("Remove"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    ))
