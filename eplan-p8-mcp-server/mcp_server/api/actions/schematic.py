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

import math
import re
import uuid

from ._base import cs_escape
from .live import _script
from .scripted import _execute_script
from .fixtures import SCRATCH_ROOT
from .schematic_model import (
    SchematicValueError,
    diff_page,
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
    "live_verify_page",
    "live_set_device_tag",
    "live_read_connections",
    "live_connect_pins_routed",
    "live_routing_catalog",
    "live_place_connection_symbol",
    "live_place_corner",
    "live_place_tnode",
    "live_place_connected",
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
    // The Symbol.Type values that are CONNECTION symbols rather than devices.
    // Kept beside the placement code because Function.Create's refusal of one is
    // otherwise unattributable.
    static readonly string[] ROUTING_TYPES = new string[] {
        "Routing", "DynamicRouting", "RoutingCross", "RoutingBridge",
        "TNodeUp", "TNodeDown", "TNodeLeft", "TNodeRight",
        "InterruptionPoint", "ConnectionDefinition",
        "PotentialDefinition", "PotentialTerminal",
        "Shielding", "CableDefinitionLine", "NetDefinition" };

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
        if (nameVal != null) d["name"] = SafeText(nameVal);

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
            if (lib != null) sym["library"] = SafeText(lib);
            if (sname != null) sym["name"] = SafeText(sname);
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
                    if (des != null) pd["designation"] = SafeText(des);
                    // WHICH WAY THE PIN FACES. Load-bearing for placement: two
                    // pins autoconnect only when they face each other on a
                    // shared axis, so a caller solving for where a device must
                    // sit needs this and cannot infer it from geometry - an
                    // offset of (0,+6) tells you the pin is at the top, not
                    // that it points Up.
                    object dir = TryRead(pin, "Direction", null);
                    if (dir != null) pd["direction"] = SafeText(dir);
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


def _fill(template, **values):
    """
    Substitute placeholder tokens into a C# template in ONE pass.

    Chained `.replace()` calls are unsafe here, and not theoretically: a page
    named "+TAGTEST/610" was substituted for PAGENAME, and the later TAG
    substitution then rewrote "TAGTEST" INSIDE the value just inserted, emitting
    `+"-K1"TEST/610` and a CS0103 that surfaced only as a timeout. Any caller
    value containing a token name does the same - a library called "SNAP", a
    page called "TOP".

    Two properties make that impossible:

      - ONE PASS: text inserted by this substitution is never re-scanned, so a
        value cannot be corrupted by a later token.
      - WORD BOUNDARIES: LIB cannot match inside LIBNAME, and AX cannot match
        inside a longer identifier.

    A token left unsubstituted raises, rather than shipping a script with a bare
    identifier in it - which is the same CS0103 by a slower route.
    """
    if not values:
        return template
    alternation = "|".join(sorted((re.escape(k) for k in values),
                                  key=len, reverse=True))
    pattern = re.compile(r"\b(" + alternation + r")\b")

    # Check the TEMPLATE, before substituting. Scanning the OUTPUT cannot work:
    # a caller value may legitimately contain a token name - a library actually
    # called "LIB", a page called "TAGTEST" - and that is indistinguishable
    # afterwards from a token that was never filled. Which is precisely the
    # confusion this function exists to remove, so re-introducing it here would
    # be self-defeating.
    missing = sorted(k for k in values if not re.search(r"\b%s\b" % re.escape(k), template))
    if missing:  # pragma: no cover - a template/caller mismatch is a bug
        raise RuntimeError(
            "Placeholder(s) %s were supplied but do not appear in the template; "
            "the template and its caller have drifted apart." % missing
        )
    return pattern.sub(lambda m: values[m.group(1)], template)


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
            // Walk by INDEX across the full 0..5000 range. SymbolIds are
            // SPARSE within a library, so every index is tried and a
            // construction failure is skipped (continue), never taken as
            // "the library ends here" (break) - see the comment at the try/
            // catch below for the finding that corrected this.
            for (int i = 0; i < 5000; i++)
            {
                object sym = null;
                // SymbolIds are SPARSE - a library can hold ids well past the
                // first one that fails to construct (SPECIAL/DCP2JICM is id
                // 402, "SPECIAL" itself runs dry at 72 and picks up again
                // higher up). continue, not break, or every id above the
                // first gap goes permanently invisible to this listing while
                // "truncated" still reports false - see
                // Testing/09-symbol-catalog-enumeration-gap.md.
                try { sym = symCtorInt.Invoke(new object[] { lib, i }); }
                catch { continue; }
                if (sym == null) continue;
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
'''
        body = _fill(body, LIB='"%s"' % lib_cs,
                     CONTAINS='"%s"' % contains_cs, LIMIT=str(limit))
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
                            if (des != null) pd["designation"] = SafeText(des);
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
'''
        body = _fill(body, LIB='"%s"' % lib_cs, SYM='"%s"' % sym_cs)

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
    body = _fill(body, PAGETYPE='"%s"' % cs_escape(page_type))

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

            // A routing symbol is NOT a Function, and Function.Create refuses one
            // with "S511085Cannot create function" - which says nothing about
            // why. Measured on 2027 placing SPECIAL_en_US/CO. Name the real
            // problem here instead.
            string symKind = PropText(sym, "Type");
            if (symKind != null && Array.IndexOf(ROUTING_TYPES, symKind) >= 0)
                throw new Exception("Symbol " + SYMNAME + " is a " + symKind +
                    " - a connection symbol, not a device. Function.Create cannot " +
                    "place one. Use live_place_connection_symbol (or " +
                    "live_place_corner / live_place_tnode) instead.");

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
    body = _fill(
        body,
        PAGENAME='"%s"' % page_cs,
        LIBNAME='"%s"' % lib_cs,
        SYMNAME='"%s"' % sym_cs,
        X2VAL=x2_cs if x2_cs is not None else x_cs,
        Y2VAL=y2_cs if y2_cs is not None else y_cs,
        XVAL=x_cs,
        YVAL=y_cs,
        VARNR=str(variant_nr),
        SNAP=cs_bool(snap_to_grid),
    )

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

# The pin-resolution script, shared by the straight and routed writers.
# Two readers would drift, and a routed wire that disagreed with a straight
# one about where a pin is would be very hard to explain.
_PROBE_BODY = '''            object page = FindPage(project, PAGENAME);
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


def _probe_pins(page_cs, from_cs, to_cs, from_pin, to_pin, timeout_seconds):
    """Resolve two pins to absolute page coordinates. Writes nothing."""
    body = _fill(
        _PROBE_BODY,
        PAGENAME='"%s"' % page_cs,
        FROMH='"%s"' % from_cs,
        TOH='"%s"' % to_cs,
        FROMP=str(from_pin),
        TOP=str(to_pin),
    )
    return _shape(_execute_script(
        _script(_cls("PinProbe"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    ))


def live_connect_pins(page: str, from_handle: str, from_pin: int,
                      to_handle: str, to_pin: int,
                      allow_real_project: bool = False,
                      timeout_seconds: float = 90.0) -> dict:
    """
    Draw an EXPLICIT connection line between two pins. WRITES - scratch-only.

    Usually the wrong tool. Two pins that FACE each other on a shared axis are
    already wired: EPLAN draws an autoconnecting line between them and no object
    exists on the page. Measured on a real page - four Functions, zero line
    objects, two wires rendered. Drawing a line over that run adds a redundant
    object on top of a connection EPLAN had already made.

    Reach for this only when autoconnect cannot apply: a run that has to be
    drawn explicitly across a page. For an ordinary turn place a corner
    (`live_place_corner`); for a branch a T-node (`live_place_tnode`); for a
    straight run between facing pins, place the two devices in line and draw
    nothing.

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

    # Step 1: resolve both pins WITHOUT writing, so the geometry decision
    # happens on this side where it can be tested with EPLAN closed.
    probe = _probe_pins(page_cs, from_cs, to_cs, from_pin, to_pin,
                        timeout_seconds)
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

            // SetGraphics takes coordinates RELATIVE to the line's Location,
            // not absolute page coordinates. Measured on real, human-drawn lines
            // in a production project: Location is the absolute anchor
            // (326.39, 346.71) and GetGraphics() returns a Line whose points are
            // (0,0) -> (-1.27, 2.54), with the connection points relative too.
            //
            // Passing absolute coordinates with Location left at its default put
            // one end of the wire at the PAGE ORIGIN - a line that visibly
            // exists, reports success, and connects nothing. So: anchor at the
            // first pin, then draw the segment relative to it.
            PropertyInfo locProp = GetWritable(dclType, "Location");
            if (locProp == null)
                throw new Exception("DynamicConnectionLine has no writable Location, " +
                    "so the line cannot be anchored and SetGraphics would place it " +
                    "relative to the page origin. " + MemberList(dclType, false));
            locProp.SetValue(dcl, MakePoint(ptType, AX, AY), null);

            Call(setG, dcl, new object[] {
                MakePoint(ptType, 0.0, 0.0),
                MakePoint(ptType, BX - AX, BY - AY) });

            results["page"] = PropText(page, "Name");
            results["lineDrawn"] = true;
            results["anchor"] = PtDict(MakePoint(ptType, AX, AY));
            results["handle"] = Handle(dcl);
            results["line"] = DumpPlacement(dcl, false);
            results["page_after"] = ReadPage(page, 200, true, null);
'''
    body = _fill(
        body,
        PAGENAME='"%s"' % page_cs,
        AX=cs_double(pa["x"], "from x"),
        AY=cs_double(pa["y"], "from y"),
        BX=cs_double(pb["x"], "to x"),
        BY=cs_double(pb["y"], "to y"),
    )

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
    body = _fill(
        body,
        PAGENAME='"%s"' % page_cs,
        LIMIT=str(limit),
        ONLYTYPES=only,
        WITHPINS=cs_bool(include_pins),
    )

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
'''
            expect_block = _fill(expect_block, EXPECTTYPE='"%s"' % expect_cs)
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
        body = _fill(body, HANDLE='"%s"' % handle_cs)
    body = _fill(body, PAGENAME='"%s"' % page_cs)

    return _shape(_execute_script(
        _script(_cls("Remove"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    ))


# ---------------------------------------------------------------------------
# 7. Verify a page against an expectation
# ---------------------------------------------------------------------------

def live_verify_page(page: str, expected: dict, tolerance: float = 0.05,
                     timeout_seconds: float = 90.0) -> dict:
    """
    Check a page against a description of what it SHOULD contain.

    This is what turns the primitives from append-only into something that can
    CONVERGE. Without it a caller can add to a page but has no way to state a
    target and be told exactly what does not match, so "did what I intended
    land?" stays an eyeball job over a large JSON dump.

    `expected` is written in live_read_page's OWN schema, as a SUBSET. That is
    the point: the read format doubles as the specification format, so there is
    no second vocabulary to learn and a verification is just "re-read, then
    compare". Copy a read result, cut it down to what you care about, and it is
    a valid expectation.

    Only keys present in `expected` are compared. Everything else is ignored, so
    you can assert on one device's position without describing the whole page.

    Reads only - nothing is modified.

    Args:
        page: Page name.
        expected: A subset of a live_read_page result. Recognised keys:
            "placementCount" - exact number of placements on the page.
            "placements"     - a list of partial placement records, each matched
                               by "handle" if given, otherwise by "clrType" +
                               "location" + "name", whichever are present.
            A placement record may carry "clrType", "name", "handle" and
            "location" ({"x", "y"}).
        tolerance: Millimetres within which two coordinates count as equal
            (default 0.05 - deliberately under half a grid step, so devices on
            ADJACENT grid points are never treated as the same place).
        timeout_seconds: Default 90s.

    Returns:
        {"success", "match": bool, "differences": [str, ...], "page_state"}

        "differences" names each mismatch in the caller's own terms - what was
        expected and what was found - so a failed verification says what to fix
        rather than only that something is wrong. "page_state" is the full read
        the comparison ran against, so no second call is needed for context.

    Example:
        live_verify_page("+MCP/1", {
            "placementCount": 3,
            "placements": [{"clrType": "Function",
                            "location": {"x": 60.325, "y": 200.025}}],
        })
    """
    try:
        cs_text(page, "page")
        tolerance = float(tolerance)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)
    if not isinstance(expected, dict):
        return {"success": False,
                "error": "expected must be a dict written in live_read_page's "
                         "schema - e.g. {'placementCount': 3}. Got %s."
                         % type(expected).__name__}
    if not expected:
        return {"success": False,
                "error": "expected is empty, so this would trivially pass and "
                         "tell you nothing. State at least one thing to check, "
                         "e.g. {'placementCount': N}."}

    state = live_read_page(page, include_pins=False, limit=1000,
                           timeout_seconds=timeout_seconds)
    if not state.get("success"):
        return state

    result = diff_page(expected, state, tolerance=tolerance)
    out = {
        "success": True,
        "match": result["match"],
        "differences": result["differences"],
        "page": state.get("page"),
        "page_state": state,
    }
    if state.get("truncated"):
        # A truncated read could turn a present placement into a false "missing".
        out["caution"] = (
            "The page read was truncated at %s of %s placements, so a reported "
            "difference may be an artefact of the cut-off rather than a real "
            "mismatch." % (state.get("returned"), state.get("placementCount"))
        )
    return out


# ---------------------------------------------------------------------------
# 8. Give a placed device its tag
# ---------------------------------------------------------------------------

def live_set_device_tag(page: str, handle: str, tag: str,
                        allow_merge: bool = False,
                        allow_real_project: bool = False,
                        timeout_seconds: float = 90.0) -> dict:
    """
    Give a placed function its device tag. WRITES - scratch-only by default.

    A function placed by live_place_symbol is ANONYMOUS: its name is "+" until a
    tag is assigned (measured on 2027). An anonymous device looks placed in the
    GED but is invisible to every name-addressed tool - live_query_functions,
    live_set_function_text, search_devices - so placing and tagging are two
    steps, and only the first existed before this.

    DUPLICATE TAGS ARE REFUSED BY DEFAULT, and that default is the point.
    Assigning a tag that already exists does not error in EPLAN: it MERGES this
    function into that device as a further sub-function. For a contactor's coil
    and its contacts that is exactly right; for a model reusing a tag by
    accident it silently rewires the schematic. So the safe reading is the
    default and the useful one is opt-in.

    Args:
        page: Page the function is on.
        handle: Handle from live_place_symbol or live_read_page. Session-scoped.
        tag: The device tag, e.g. "-K1", "+1162-MA1", "=AP+ST1-Q2". Written to
            Function.Name, which reflection on 2027 reports as directly
            writable. The result reports the name EPLAN actually stored, since
            project structure settings can reformat it.
        allow_merge: Permit a tag already present on this page, merging this
            function into that device. Default False.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 90s.

    Returns:
        {"success", "page", "handle", "requestedTag", "name" (as EPLAN stored
         it), "merged", "route", "placement", "page_after"}

        "route" names which write path succeeded, so the first live run on a new
        EPLAN version documents itself instead of leaving you guessing.
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        handle_cs = cs_escape(cs_text(handle, "handle"))
        tag_cs = cs_escape(cs_text(tag, "tag"))
    except SchematicValueError as exc:
        return _err(exc)

    body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, PAGENAME);
            object target = ResolveOnPage(page, HANDLE);
            if (target.GetType().Name != "Function")
                throw new Exception("Only a Function can carry a device tag; " +
                    "handle resolves to a " + target.GetType().Name + ".");

            // A tag already in use MERGES this function into that device as a
            // further sub-function. Correct for a coil + its contacts, wrong for
            // a model that reused a tag by accident - so it is opt-in.
            bool allowMerge = ALLOWMERGE;
            List<string> clash = new List<string>();
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null || pl.GetType().Name != "Function") continue;
                if (Handle(pl) == HANDLE) continue;
                string n = PropText(pl, "Name");
                if (n != null && n == TAG) clash.Add(Handle(pl));
            }
            results["merged"] = clash.Count > 0;
            if (clash.Count > 0 && !allowMerge)
                throw new Exception("Device tag " + TAG + " is already used on this " +
                    "page by " + clash.Count + " other function(s) (" +
                    string.Join(", ", clash.ToArray()) + "). Assigning it would MERGE " +
                    "this function into that device as a sub-function - correct for a " +
                    "contactor coil and its contacts, but a silent rewire when a tag " +
                    "is reused by accident. Pass allow_merge=true if the merge is " +
                    "intended, or choose a different tag.");

            // Function.Name is directly writable (reflection on 2027 reports
            // r=True w=True). Fall back to VisibleName and REPORT which route
            // fired, so a future EPLAN version documents itself.
            string route = null;
            PropertyInfo nameProp = GetWritable(target.GetType(), "Name");
            if (nameProp != null)
            {
                try { nameProp.SetValue(target, TAG, null); route = "Function.Name"; }
                catch (Exception exName) { results["nameError"] = Flatten(exName); }
            }
            if (route == null)
            {
                PropertyInfo vis = GetWritable(target.GetType(), "VisibleName");
                if (vis == null)
                    throw new Exception("Function exposes neither a writable Name nor " +
                        "VisibleName. " + MemberList(target.GetType(), false));
                vis.SetValue(target, TAG, null);
                route = "Function.VisibleName";
            }
            results["route"] = route;

            // Read the stored name BACK: structure settings can reformat what was
            // asked for, exactly as they do for page names.
            results["page"] = PropText(page, "Name");
            results["handle"] = Handle(target);
            results["name"] = PropText(target, "Name");
            results["visibleName"] = PropText(target, "VisibleName");
            results["placement"] = DumpPlacement(target, false);
            results["page_after"] = ReadPage(page, 200, false, new string[] { "Function" });
'''
    body = _fill(
        body,
        PAGENAME='"%s"' % page_cs,
        HANDLE='"%s"' % handle_cs,
        ALLOWMERGE=cs_bool(allow_merge),
        TAG='"%s"' % tag_cs,
    )

    out = _shape(_execute_script(
        _script(_cls("SetTag"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    ))
    if out.get("success"):
        out["requestedTag"] = tag
        stored = out.get("name")
        if stored and stored != tag:
            out["note"] = (
                "EPLAN stored the tag as %r rather than %r - project structure "
                "settings reformat device tags. Use the stored name for later "
                "name-addressed calls." % (stored, tag)
            )
    return out


# ---------------------------------------------------------------------------
# 9. Read the LOGICAL connections
# ---------------------------------------------------------------------------

_HELPERS_CONNECTIONS = r'''
    // One end of a connection: which device, at which connection point.
    //
    // Read from the SymbolReference rather than the Pin, because the pin knows
    // its index and designation but not what it belongs to. Both are reported:
    // "device" is what an engineer names, "designation" is what the wire lands
    // on, and a connection is only meaningful with both.
    static Dictionary<string, object> ConnEnd(object conn, bool start)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        List<string> absent = new List<string>();

        object sr = TryRead(conn, start ? "StartSymbolReference" : "EndSymbolReference", absent);
        if (sr != null)
        {
            d["clrType"] = sr.GetType().Name;
            d["handle"] = Handle(sr);
            object nm = TryRead(sr, "Name", null);
            if (nm != null) d["device"] = SafeText(nm);
            object loc = TryRead(sr, "Location", null);
            if (loc != null) d["location"] = PtDict(loc);
        }

        object pin = TryRead(conn, start ? "StartPin" : "EndPin", absent);
        if (pin != null)
        {
            object des = TryRead(pin, "Designation", null);
            if (des != null) d["designation"] = SafeText(des);
            object idx = TryRead(pin, "Index", null);
            if (idx != null) d["pinIndex"] = Convert.ToInt32(idx);
        }

        object idxProp = TryRead(conn, start ? "StartIndex" : "EndIndex", null);
        if (idxProp != null) d["connIndex"] = Convert.ToInt32(idxProp);

        if (absent.Count > 0) d["absentMembers"] = absent;
        return d;
    }

    static Dictionary<string, object> DumpConnection(object conn)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        List<string> absent = new List<string>();

        d["handle"] = Handle(conn);
        d["clrType"] = conn.GetType().Name;

        object pg = TryRead(conn, "Page", absent);
        if (pg != null) d["page"] = PropText(pg, "Name");

        object kind = TryRead(conn, "KindOfWire", null);
        if (kind != null) d["kindOfWire"] = SafeText(kind);

        object placed = TryRead(conn, "IsPlaced", null);
        if (placed != null) d["isPlaced"] = Convert.ToBoolean(placed);

        d["from"] = ConnEnd(conn, true);
        d["to"] = ConnEnd(conn, false);

        // The connection's own designation - the wire number an engineer reads
        // off the drawing. Type-dependent, so recorded as absent rather than
        // silently omitted when a connection has no property list.
        object props = TryRead(conn, "Properties", absent);
        if (props != null)
        {
            foreach (string p in new string[] {
                "CONNECTION_DESIGNATION", "CONNECTION_CABLENAME",
                "CONNECTION_COLORNAME", "CONNECTION_CROSSSECTION" })
            {
                object v = TryRead(props, p, null);
                // SafeText, not ToString: an EMPTY property throws
                // EmptyPropertyException on conversion even though the read
                // succeeded - measured on an ungenerated connection.
                string s = SafeText(v);
                if (s != null && s.Length > 0) d[p] = s;
            }
        }

        if (absent.Count > 0) d["absentMembers"] = absent;
        return d;
    }
'''


def live_read_connections(page: str = None, limit: int = 200,
                          timeout_seconds: float = 120.0) -> dict:
    """
    Read the project's LOGICAL connections - what is actually wired to what.

    This is the difference between "a line was drawn" and "these two devices are
    connected". live_connect_pins proves GRAPHICAL adjacency: a line touches two
    pins. The logical `Connection` objects are what carry connection
    designations, wire numbers, cable assignment and every report - and they do
    not exist until EPLAN generates them.

    Without this, a caller told "connected: true" could reasonably conclude the
    schematic is electrically correct when it is not. That is a source of false
    confidence in the one layer built to prevent it.

    READ-ONLY. This deliberately does NOT run eplan_generate_connections for
    you: generating connections MUTATES the project, and a tool named "read"
    that quietly writes is exactly the kind of surprise this layer exists to
    avoid. If nothing comes back, the result says so and names the tool to run.

    Args:
        page: Only connections on this page. Omit for the whole project.
        limit: Max connections returned (default 200). The true total is always
            reported, so a truncated read is never mistaken for the whole set.
        timeout_seconds: Default 120s - a project-wide walk is not fast.

    Returns:
        {"success", "connections": [...], "total", "returned", "truncated",
         "page"}

        Each connection: {"handle", "page", "kindOfWire", "isPlaced",
        "from": {...}, "to": {...}} where each end carries "device" (the device
        tag), "designation" (the connection point), "pinIndex" and "location".

        When the project has NO connections at all, "stale" is true and
        "nextStep" names eplan_generate_connections - because zero connections
        almost always means they have not been generated yet, not that nothing
        is wired.
    """
    try:
        limit = cs_int(limit, "limit", minimum=1, maximum=5000)
        page_cs = cs_escape(cs_text(page, "page")) if page else None
    except SchematicValueError as exc:
        return _err(exc)

    page_filter = ""
    if page_cs:
        page_filter = '''
                object cpg = TryRead(conn, "Page", null);
                string cpgName = cpg == null ? null : PropText(cpg, "Name");
                if (cpgName != PAGENAME) continue;'''

    body = '''            Type finderType = FindType("Eplan.EplApi.DataModel.DMObjectsFinder");
            object finder = Activator.CreateInstance(finderType, new object[] { project });
            Type filterType = FindType("Eplan.EplApi.DataModel.ConnectionsFilter");
            object filter = Activator.CreateInstance(filterType);
            MethodInfo getConns = RequireMethod(finderType, "GetConnections",
                new string[] { filterType.Name }, false);
            results["boundSignature"] = getConns.ToString();

            IEnumerable found = (IEnumerable)Call(getConns, finder, new object[] { filter });
            if (found == null)
                throw new Exception("GetConnections returned null; refusing to report " +
                    "an unwired project, because that is indistinguishable from " +
                    "connections simply not having been generated.");

            List<object> items = new List<object>();
            int total = 0, matched = 0;
            foreach (object conn in found)
            {
                if (conn == null) continue;
                total++;''' + page_filter + '''
                matched++;
                if (items.Count < LIMIT) items.Add(DumpConnection(conn));
            }
            results["total"] = total;
            results["matched"] = matched;
            results["returned"] = items.Count;
            results["truncated"] = matched > items.Count;
            results["connections"] = items;
'''
    subs = {"LIMIT": str(limit)}
    if page_cs:
        subs["PAGENAME"] = '"%s"' % page_cs
    body = _fill(body, **subs)

    out = _shape(_execute_script(
        _script(_cls("ReadConn"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_CONNECTIONS),
        timeout=timeout_seconds,
    ))
    if out.get("success"):
        if page:
            out["page"] = page
        if not out.get("total"):
            # Zero connections almost always means they have not been generated,
            # not that nothing is wired. Say which, rather than letting the
            # caller read an empty list as "nothing is connected".
            out["stale"] = True
            out["nextStep"] = (
                "This project reports NO logical connections at all, which "
                "usually means they have not been generated yet rather than "
                "that nothing is wired. Run eplan_generate_connections (it "
                "MODIFIES the project) and read again. Graphical lines drawn by "
                "live_connect_pins do not become Connection objects until then."
            )
        elif page and not out.get("matched"):
            out["stale"] = False
            out["note"] = (
                "The project has %d connection(s) but none on page %r. The "
                "page may genuinely have no wiring, or connections may predate "
                "the lines drawn on it - regenerate if in doubt."
                % (out.get("total", 0), page)
            )
    return out


# ---------------------------------------------------------------------------
# 10. Route a connection through a corner
# ---------------------------------------------------------------------------

def live_connect_pins_routed(page: str, from_handle: str, from_pin: int,
                             to_handle: str, to_pin: int,
                             corner: str = "x",
                             allow_real_project: bool = False,
                             timeout_seconds: float = 120.0) -> dict:
    """
    DRAW two segments through an elbow. WRITES - scratch-only.

    Not the normal way to turn a wire. The normal way is to place a corner
    SYMBOL with `live_place_corner` and let EPLAN autoconnect into it - verified
    live, that produces one clean logical connection between the two devices,
    with the corner at neither end. This tool instead draws the line objects
    itself, which leaves geometry on the page that EPLAN did not derive.

    It stays because a drawn elbow is still the answer when no autoconnecting
    path exists - the free-routing case the `DynamicRouting` symbols cover, rare
    in practice: 18 placements out of roughly 5000 on one measured production
    project, all on a single page.

    If both pins can be brought onto a shared axis, or joined by a placed
    corner, prefer that.

    Args:
        page: Page both placements are on.
        from_handle, to_handle: Handles from live_place_symbol or
            live_read_page. Session-scoped.
        from_pin, to_pin: Pin indices, from live_read_page's "pins".
        corner: Which way the elbow turns.
            "x" - leave the FROM pin horizontally, arrive at the TO pin
                  vertically. Corner sits at (to.x, from.y).
            "y" - leave vertically, arrive horizontally. Corner at
                  (from.x, to.y).
            Pick the one whose corner does not land on top of another device;
            the result reports the corner so you can check.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 120s - this is two writes plus a read-back.

    Returns:
        {"success", "page", "corner", "segments": [{"handle", "from", "to"}, ...],
         "undo": {...}, "page_after"}

        BOTH segment handles come back, so the undo is complete - removing only
        one would leave half a wire behind, which is worse than leaving the whole
        thing.

        As with live_connect_pins this reports what was DRAWN. Whether EPLAN has
        created a logical Connection is a separate question - use
        live_read_connections after eplan_generate_connections.
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        from_cs = cs_escape(cs_text(from_handle, "from_handle"))
        to_cs = cs_escape(cs_text(to_handle, "to_handle"))
        from_pin = cs_int(from_pin, "from_pin", minimum=0)
        to_pin = cs_int(to_pin, "to_pin", minimum=0)
        corner = cs_text(corner, "corner").strip().lower()
    except SchematicValueError as exc:
        return _err(exc)

    if corner not in ("x", "y"):
        return {"success": False,
                "error": "corner must be 'x' (leave horizontally, arrive "
                         "vertically) or 'y' (leave vertically, arrive "
                         "horizontally); got %r." % corner}
    if from_handle == to_handle and from_pin == to_pin:
        return {"success": False,
                "error": "from and to are the same pin; a connection needs two "
                         "distinct endpoints."}

    # Resolve both pins first, exactly as live_connect_pins does, so the
    # geometry decision happens on this side where it is testable offline.
    probe = _probe_pins(page_cs, from_cs, to_cs, from_pin, to_pin,
                        timeout_seconds)
    if not probe.get("success"):
        return probe
    pa = _pin_point(probe.get("from"), from_pin)
    pb = _pin_point(probe.get("to"), to_pin)
    if pa is None or pb is None:
        return {
            "success": False,
            "error": "Could not establish an absolute page coordinate for %s. A "
                     "pin whose frame is unknown cannot be routed by index."
                     % ("the 'from' pin" if pa is None else "the 'to' pin"),
            "from": probe.get("from"), "to": probe.get("to"),
        }

    if pins_coincide(pa, pb):
        return {"success": False,
                "error": "Both pins are at the same point (%.4f, %.4f); there is "
                         "nothing to route." % (pa["x"], pa["y"]),
                "from_point": pa, "to_point": pb}

    if axis_aligned(pa, pb):
        return {
            "success": False,
            "error": (
                "These pins already share an axis, so a corner would draw a "
                "redundant elbow where a single segment does the job. Use "
                "live_connect_pins instead."
            ),
            "from_point": pa, "to_point": pb,
        }

    elbow = ({"x": pb["x"], "y": pa["y"]} if corner == "x"
             else {"x": pa["x"], "y": pb["y"]})

    body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, PAGENAME);
            Type dclType = FindType("Eplan.EplApi.DataModel.DynamicConnectionLine");
            MethodInfo create = RequireMethod(dclType, "Create", new string[] { "Page" }, false);
            MethodInfo setG = RequireMethod(dclType, "SetGraphics",
                new string[] { "PointD", "PointD" }, false);
            Type ptType = setG.GetParameters()[0].ParameterType;
            results["boundSignature"] = setG.ToString();

            // Two segments through the elbow. Each is anchored at its own start
            // and drawn RELATIVE to that anchor - the same rule as the straight
            // case, where passing absolute coordinates put one end at the page
            // origin.
            double[][] segs = new double[][] {
                new double[] { AX, AY, CX, CY },
                new double[] { CX, CY, BX, BY }
            };

            List<object> drawn = new List<object>();
            foreach (double[] s in segs)
            {
                object dcl = Activator.CreateInstance(dclType);
                Call(create, dcl, new object[] { page });

                PropertyInfo locProp = GetWritable(dclType, "Location");
                if (locProp == null)
                    throw new Exception("DynamicConnectionLine has no writable " +
                        "Location, so a segment cannot be anchored and would be " +
                        "drawn from the page origin. " + MemberList(dclType, false));
                locProp.SetValue(dcl, MakePoint(ptType, s[0], s[1]), null);
                Call(setG, dcl, new object[] {
                    MakePoint(ptType, 0.0, 0.0),
                    MakePoint(ptType, s[2] - s[0], s[3] - s[1]) });

                Dictionary<string, object> d = new Dictionary<string, object>();
                d["handle"] = Handle(dcl);
                d["from"] = PtDict(MakePoint(ptType, s[0], s[1]));
                d["to"] = PtDict(MakePoint(ptType, s[2], s[3]));
                drawn.Add(d);
            }

            results["page"] = PropText(page, "Name");
            results["segments"] = drawn;
            results["segmentCount"] = drawn.Count;
            results["page_after"] = ReadPage(page, 200, true, null);
'''
    body = _fill(
        body,
        PAGENAME='"%s"' % page_cs,
        AX=cs_double(pa["x"], "from x"),
        AY=cs_double(pa["y"], "from y"),
        BX=cs_double(pb["x"], "to x"),
        BY=cs_double(pb["y"], "to y"),
        CX=cs_double(elbow["x"], "corner x"),
        CY=cs_double(elbow["y"], "corner y"),
    )

    out = _shape(_execute_script(
        _script(_cls("Routed"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    ))
    if out.get("success"):
        _annotate_pins(out)
        out["from_point"] = pa
        out["to_point"] = pb
        out["corner"] = elbow
        out["cornerMode"] = corner
        handles = [s.get("handle") for s in (out.get("segments") or [])
                   if s.get("handle")]
        if handles:
            # BOTH handles: removing one would leave half a wire, which is
            # worse than leaving the whole thing.
            out["undo"] = {"tool": "eplan_live_remove_placement",
                           "page": out.get("page"), "handles": handles,
                           "note": "Remove BOTH segments; one alone leaves half "
                                   "a wire on the page."}
        out["scopeNote"] = (
            "Two graphical segments were drawn through the corner. Whether "
            "EPLAN has created a LOGICAL connection is a separate question - "
            "run eplan_generate_connections then live_read_connections."
        )
    return out


# ---------------------------------------------------------------------------
# 11. Discover the connection symbols (corners, T-nodes, breaks)
# ---------------------------------------------------------------------------

# Symbol.Type values that are CONNECTION symbols rather than devices. From the
# 54-value Symbol.Type enum; these are the ones that participate in wiring.
ROUTING_SYMBOL_TYPES = (
    "Routing",             # a corner
    "DynamicRouting",      # free/diagonal routing
    "RoutingCross",        # four-way cross
    "RoutingBridge",       # hop over
    "TNodeUp", "TNodeDown", "TNodeLeft", "TNodeRight",
    "InterruptionPoint",   # cross-page jump
    "ConnectionDefinition",  # carries wire number / colour / cross-section
    "PotentialDefinition", "PotentialTerminal",
    "Shielding", "CableDefinitionLine", "NetDefinition",
)

_HELPERS_ROUTING = r'''
    // The connection points of one symbol variant: which way each faces AND
    // where it sits relative to the placement location.
    //
    // Both halves are load-bearing, and ORDER is preserved rather than sorted.
    // Measured on SPECIAL_en_US: TLRO and TLRO_1 are both TNodeUp and face the
    // same three directions, differing only in the order of their pins; while
    // the five variants of TLRU face the same three directions but put the pins
    // in DIFFERENT PLACES - v8 has all three at the vertex, v0 pushes its Right
    // pin one grid step out. Sorting, or reporting directions alone, would make
    // any of those look interchangeable.
    static List<object> VariantPins(object variant)
    {
        List<object> pins = new List<object>();
        object cps = TryRead(variant, "ConnectionPoints", null);
        if (!(cps is IEnumerable)) return pins;
        foreach (object p in (IEnumerable)cps)
        {
            if (p == null) continue;
            Dictionary<string, object> pd = new Dictionary<string, object>();
            object d = TryRead(p, "Direction", null);
            pd["direction"] = d == null ? "Undefined" : SafeText(d);
            object loc = TryRead(p, "Location", null);
            if (loc != null) pd["offset"] = PtDict(loc);
            pins.Add(pd);
        }
        return pins;
    }

    static Dictionary<string, object> DumpSymbol(object sym, string libName, Type symT, Type varT)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        d["library"] = libName;
        d["symbol"] = PropText(sym, "Name");
        d["type"] = PropText(sym, "Type");
        List<object> vs = new List<object>();
        object variants = TryRead(sym, "Variants", null);
        if (variants is IEnumerable)
        {
            foreach (object v in (IEnumerable)variants)
            {
                if (v == null) continue;
                List<object> pins = VariantPins(v);
                if (pins.Count == 0) continue;
                List<object> dirs = new List<object>();
                foreach (object one in pins)
                    dirs.Add(((Dictionary<string, object>)one)["direction"]);
                Dictionary<string, object> vd = new Dictionary<string, object>();
                object vn = TryRead(v, "VariantNr", null);
                vd["variantNr"] = vn == null ? -1 : Convert.ToInt32(vn);
                vd["directions"] = dirs;
                vd["pins"] = pins;
                vs.Add(vd);
            }
        }
        d["variants"] = vs;
        return d;
    }
'''


def live_routing_catalog(symbol_type: str = None, directions: list = None,
                         library: str = None,
                         timeout_seconds: float = 180.0) -> dict:
    """
    Discover the CONNECTION symbols this project can use - corners, T-nodes,
    crosses, interruption points - and which directions each variant faces.

    You need this before placing any of them, and it must be DISCOVERED rather
    than assumed: a project carries several symbols for the same job. Measured
    on one production installation, `SPECIAL_en_US` alone holds 16, including
    TWO different `TNodeUp` symbols (`TLRO` and `TLRO_1`) that differ only in
    pin ORDER, and six interruption-point symbols - some two-pin, some one-pin
    and directional.

    So "the corner symbol" is not a constant. Ask what this project has.

    A connection symbol is identified by `Symbol.Type`, not by its name. The
    types that matter:

        Routing            a corner
        TNodeUp/Down/Left/Right    a branch - a SEPARATE TYPE per direction,
                           not a variant, unlike corners
        RoutingCross       four-way crossing
        RoutingBridge      hop over without connecting
        DynamicRouting     free/diagonal routing
        InterruptionPoint  cross-page jump
        ConnectionDefinition  carries wire number, colour, cross-section
        PotentialDefinition, PotentialTerminal, Shielding,
        CableDefinitionLine, NetDefinition

    Reads only.

    Args:
        symbol_type: Filter to one Symbol.Type, e.g. "Routing" for corners or
            "TNodeUp" for upward branches. Omit for all connection symbols.
        directions: Filter to variants whose pins face exactly this SET of
            directions, e.g. ["Right", "Down"] for a corner turning east and
            south. Order is ignored for matching but REPORTED in the result,
            because two symbols of the same type can differ only in pin order.
        library: Restrict to one symbol library. Omit to search all the
            project's libraries - which is usually right, since on the measured
            installation every routing symbol lived in ONE library and the
            device libraries had none.
        timeout_seconds: Default 180s; this walks every library.

    Returns:
        {"success", "libraries" (all searched), "symbols": [...], "matched",
         "byType"}

        Each symbol: {"library", "symbol", "type", "variants": [{"variantNr",
        "directions": ["Right","Down"]}]}.

        When `directions` is given, each symbol also carries "matchingVariants"
        - the variant numbers whose direction SET matches. If more than one
        symbol matches, they are ALL returned rather than one being chosen:
        which is correct is a house convention, not something this can decide.
    """
    try:
        stype = cs_text(symbol_type, "symbol_type") if symbol_type else None
        lib_cs = cs_escape(cs_text(library, "library")) if library else None
        want = None
        if directions:
            if isinstance(directions, str):
                directions = [directions]
            valid = {"Up", "Down", "Left", "Right", "Undefined"}
            want = []
            for d in directions:
                d = cs_text(d, "directions entry").strip().title()
                if d not in valid:
                    return {"success": False,
                            "error": "Unknown direction %r. PinBase.Directions is "
                                     "Up, Down, Left, Right (or Undefined)." % d}
                want.append(d)
    except SchematicValueError as exc:
        return _err(exc)

    if stype and stype not in ROUTING_SYMBOL_TYPES:
        return {
            "success": False,
            "error": "symbol_type %r is not a connection-symbol type. Known: %s"
                     % (stype, ", ".join(ROUTING_SYMBOL_TYPES)),
        }

    types_cs = ", ".join('"%s"' % t for t in ([stype] if stype else ROUTING_SYMBOL_TYPES))
    lib_filter = ""
    if lib_cs:
        lib_filter = '                if (ln != LIBNAME) continue;\n'

    body = '''            Type symT = FindType("Eplan.EplApi.DataModel.MasterData.Symbol");
            Type varT = FindType("Eplan.EplApi.DataModel.MasterData.SymbolVariant");
            Type libT = FindType("Eplan.EplApi.DataModel.MasterData.SymbolLibrary");
            ConstructorInfo libCtor = libT.GetConstructor(new Type[] { project.GetType(), typeof(string) });
            if (libCtor == null)
                throw new Exception("SymbolLibrary has no (Project, string) ctor. " +
                    MemberList(libT, true));
            ConstructorInfo symByIdx = symT.GetConstructor(new Type[] { libT, typeof(int) });
            if (symByIdx == null)
                throw new Exception("Symbol has no (SymbolLibrary, int) ctor. " +
                    MemberList(symT, true));

            List<string> wantTypes = new List<string>(new string[] { TYPES });

            PropertyInfo slProp = RequireReadable(project.GetType(), "SymbolLibraries");
            object sl = slProp.GetValue(project, null);
            List<string> libNames = new List<string>();
            if (sl is IEnumerable)
                foreach (object one in (IEnumerable)sl)
                {
                    if (one == null) continue;
                    string n = PropText(one, "Name");
                    if (n != null) libNames.Add(n);
                }
            results["libraries"] = libNames;

            List<object> found = new List<object>();
            foreach (string ln in libNames)
            {
''' + lib_filter + '''                object lib = null;
                try { lib = libCtor.Invoke(new object[] { project, ln }); }
                catch { continue; }   // a library the project lists but cannot open
                for (int i = 0; i < 5000; i++)
                {
                    object s = null;
                    // Same sparse-SymbolId gap as live_symbol_catalog's depth-2
                    // walk (Testing/09-symbol-catalog-enumeration-gap.md) -
                    // continue past a gap instead of stopping at the first one.
                    try { s = symByIdx.Invoke(new object[] { lib, i }); }
                    catch { continue; }
                    if (s == null) continue;
                    if (PropText(s, "IsValid") != "True") continue;
                    string tn = PropText(s, "Type");
                    if (tn == null || !wantTypes.Contains(tn)) continue;
                    found.Add(DumpSymbol(s, ln, symT, varT));
                }
            }
            results["symbols"] = found;
'''
    body = _fill(body, TYPES=types_cs, **({"LIBNAME": '"%s"' % lib_cs} if lib_cs else {}))

    out = _shape(_execute_script(
        _script(_cls("RoutCat"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_ROUTING),
        timeout=timeout_seconds,
    ))
    if not out.get("success"):
        return out

    syms = out.get("symbols") or []

    # Direction matching happens HERE, not in C#, so it is testable offline.
    if want:
        target = sorted(want)
        for s in syms:
            s["matchingVariants"] = [
                v["variantNr"] for v in (s.get("variants") or [])
                if sorted(v.get("directions") or []) == target
            ]
        syms = [s for s in syms if s["matchingVariants"]]
        out["symbols"] = syms
        out["requestedDirections"] = want

    out["matched"] = len(syms)
    by_type = {}
    for s in syms:
        by_type.setdefault(s.get("type"), []).append(s.get("symbol"))
    out["byType"] = by_type

    if want and len(syms) > 1:
        out["ambiguous"] = True
        out["note"] = (
            "%d symbols match those directions (%s). Which one is right is a "
            "house convention, not something this can decide - pick from the "
            "project's own usage, or ask. Note that two symbols of the same "
            "type can differ only in pin ORDER, which is reported per variant."
            % (len(syms), ", ".join("%s/%s" % (s["library"], s["symbol"])
                                    for s in syms))
        )
    elif want and not syms:
        out["note"] = (
            "No symbol in this project has a variant facing exactly %s. Call "
            "again without `directions` to see what the project does have."
            % (want,)
        )
    return out


# ---------------------------------------------------------------------------
# 12. Place a connection symbol - corner, T-node, cross, interruption point
# ---------------------------------------------------------------------------

# Which absolute direction each Symbol.Type branches TOWARD. A T-node is not a
# variant of one symbol the way a corner is: `TNodeUp` and `TNodeDown` are
# separate types. That asymmetry is EPLAN's, not ours.
TNODE_TYPE_BY_DIRECTION = {
    "Up": "TNodeUp",
    "Down": "TNodeDown",
    "Left": "TNodeLeft",
    "Right": "TNodeRight",
}


def live_place_connection_symbol(page: str, library: str, symbol: str,
                                 x: float, y: float, variant_nr: int = 0,
                                 snap_to_grid: bool = True,
                                 allow_real_project: bool = False,
                                 timeout_seconds: float = 90.0) -> dict:
    """
    Place a CONNECTION symbol - a corner, T-node, cross, interruption point.
    WRITES - scratch-only by default.

    This is a separate tool from `live_place_symbol` because EPLAN places the
    two through different APIs. A routing symbol is not a `Function`, and
    `Function.Create` refuses one with `S511085Cannot create function`, which
    names no cause. The path that works is `SymbolVariant.Create(Page)`,
    returning a `SymbolReference`. Measured on 2027 with `SPECIAL_en_US/CO`.

    Prefer `live_place_corner` or `live_place_tnode`, which pick the symbol out
    of the project instead of making you name one. Reach for this directly when
    you already know exactly which symbol and variant you want, or for a type
    those two do not cover (`RoutingCross`, `InterruptionPoint`, `Shielding`).

    A caveat you should know about: `SymbolVariant.Create` takes no coordinate.
    The object is born at the page ORIGIN and moved. This script moves it inside
    the same locking step, before anything can observe it there, and connections
    are only ever computed on demand by `generate_connections` - so the transit
    is not visible to EPLAN's connection logic. It is still the reason this tool
    reports `bornAtOrigin: true`: if a page ever does acquire a stray connection
    near (0,0), that is where to look.

    Args:
        page: Page name, exactly as `live_create_page` or `live_read_page` reports it.
        library: Symbol library name (see `live_routing_catalog`).
        symbol: Symbol name within that library.
        x, y: Where the symbol's connection vertex goes, in page millimetres.
            For a corner both pins sit exactly here - a corner's two connection
            points coincide at the turn, which is why one coordinate places it.
        variant_nr: Variant index. For a corner this IS the rotation - measured
            on `CO`: v0 Right+Down, v1 Right+Up, v2 Left+Up, v3 Left+Down.
        snap_to_grid: Round to the page's own GridSize first (default True).
            An off-grid connection symbol looks right and refuses to autoconnect.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 90s.

    Returns:
        {"success", "page", "handle", "placed", "requested"/"snapped",
         "symbolType", "undo", "page_after"}

    Refuses a device symbol, pointing at `live_place_symbol` - the mirror of the
    check that tool makes.
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        lib_cs = cs_escape(cs_text(library, "library"))
        sym_cs = cs_escape(cs_text(symbol, "symbol"))
        x_cs = cs_double(x, "x")
        y_cs = cs_double(y, "y")
        variant_nr = cs_int(variant_nr, "variant_nr", minimum=0)
    except SchematicValueError as exc:
        return _err(exc)

    body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, PAGENAME);
            double grid = 0.0;
            object gridVal = TryRead(page, "GridSize", null);
            if (gridVal != null) grid = Convert.ToDouble(gridVal);

            double ax = XVAL, ay = YVAL;
            Dictionary<string, object> requested = new Dictionary<string, object>();
            requested["x"] = ax; requested["y"] = ay;
            results["requested"] = requested;
            if (SNAP && grid > 0.0001) { ax = Snap(ax, grid); ay = Snap(ay, grid); }
            Dictionary<string, object> used = new Dictionary<string, object>();
            used["x"] = ax; used["y"] = ay;
            results["snapped"] = used;
            results["gridSize"] = grid;

            Type libType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolLibrary");
            Type symType = FindType("Eplan.EplApi.DataModel.MasterData.Symbol");
            Type varType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolVariant");
            Type ptType = FindType("Eplan.EplApi.Base.PointD");

            ConstructorInfo libCtor = libType.GetConstructor(new Type[] { project.GetType(), typeof(string) });
            if (libCtor == null)
                throw new Exception("SymbolLibrary has no (Project, string) ctor. " + MemberList(libType, true));
            object lib = null;
            try { lib = libCtor.Invoke(new object[] { project, LIBNAME }); }
            catch (TargetInvocationException tie)
            { throw new Exception("Cannot open symbol library " + LIBNAME + ": " + Flatten(tie.InnerException)); }

            ConstructorInfo symCtor = symType.GetConstructor(new Type[] { libType, typeof(string) });
            object sym = null;
            try { sym = symCtor.Invoke(new object[] { lib, SYMNAME }); }
            catch (TargetInvocationException tie)
            { throw new Exception("Cannot open symbol " + SYMNAME + " in " + LIBNAME + ": " +
                Flatten(tie.InnerException) + ". Use live_routing_catalog to list real names."); }
            if (sym == null || PropText(sym, "IsValid") != "True")
                throw new Exception("Symbol " + SYMNAME + " does not resolve in " + LIBNAME + ".");

            // The mirror of live_place_symbol's guard: a device placed through
            // SymbolVariant.Create would come back as a bare SymbolReference
            // with no device tag, no article, and no place in the parts list -
            // a silently degraded object rather than an error.
            string symKind = PropText(sym, "Type");
            results["symbolType"] = symKind;
            if (symKind == null || Array.IndexOf(ROUTING_TYPES, symKind) < 0)
                throw new Exception("Symbol " + SYMNAME + " is of type " +
                    (symKind == null ? "(unknown)" : symKind) + " - a device, not a " +
                    "connection symbol. Use live_place_symbol, which creates a real " +
                    "Function that can carry a device tag and an article.");

            ConstructorInfo varCtor = varType.GetConstructor(new Type[] { symType, typeof(int) });
            object variant = null;
            try { variant = varCtor.Invoke(new object[] { sym, VARNR }); }
            catch (TargetInvocationException tie)
            { throw new Exception("Symbol " + SYMNAME + " has no variant " + VARNR + ": " +
                Flatten(tie.InnerException) + ". live_routing_catalog reports the real ones."); }

            // Create(Page) takes no coordinate: the object is born at (0,0).
            MethodInfo mk = MethodByShape(varType, "Create", new string[] { "Page" }, false);
            if (mk == null)
                throw new Exception("SymbolVariant has no Create(Page). " + MemberList(varType, false));
            object sref = Call(mk, variant, new object[] { page });
            if (sref == null)
                throw new Exception("SymbolVariant.Create(" + PAGENAME + ") returned null for " + SYMNAME + ".");
            results["bornAtOrigin"] = true;

            // ...so move it before returning. Leaving it at the origin is not an
            // option: an unmoved connection symbol sits on top of the page frame
            // and will autoconnect to whatever else is there.
            PropertyInfo locProp = GetWritable(sref.GetType(), "Location");
            if (locProp == null)
                throw new Exception(sref.GetType().Name + " has no writable Location, so a " +
                    "symbol created by SymbolVariant.Create cannot be moved off the page " +
                    "origin. Refusing to leave it there. " + MemberList(sref.GetType(), false));
            locProp.SetValue(sref, MakePoint(ptType, ax, ay), null);

            object check = TryRead(sref, "Location", null);
            if (check == null) throw new Exception("Location unreadable after the move.");
            Dictionary<string, object> got = PtDict(check);
            double gx = Convert.ToDouble(got["x"]), gy = Convert.ToDouble(got["y"]);
            if (Math.Abs(gx - ax) > 0.001 || Math.Abs(gy - ay) > 0.001)
                throw new Exception("The symbol did not move: asked for (" + ax + ", " + ay +
                    "), it is at (" + gx + ", " + gy + "). It is still near the page origin.");

            results["page"] = PropText(page, "Name");
            results["handle"] = Handle(sref);
            results["placed"] = DumpPlacement(sref, true);
            results["page_after"] = ReadPage(page, 200, true, null);
'''
    body = _fill(
        body,
        PAGENAME='"%s"' % page_cs,
        LIBNAME='"%s"' % lib_cs,
        SYMNAME='"%s"' % sym_cs,
        XVAL=x_cs,
        YVAL=y_cs,
        VARNR=str(variant_nr),
        SNAP=cs_bool(snap_to_grid),
    )

    out = _shape(_execute_script(
        _script(_cls("PlaceConn"), body, extra_helpers=_HELPERS_SCHEMATIC),
        timeout=timeout_seconds,
    ))
    if out.get("success"):
        _annotate_pins(out)
        if out.get("placed"):
            out["placed"]["pins"] = absolute_pins(out["placed"])
        if out.get("handle"):
            out["undo"] = {"tool": "eplan_live_remove_placement",
                           "page": out.get("page"), "handle": out["handle"]}
    return out


def _pick(catalog, directions, symbol, kind, variant_nr=None):
    """
    Choose one symbol+variant out of a catalog read, or explain why not.

    Never picks between equally-matching symbols. Two symbols of the same type
    can differ only in pin order (`TLRO` vs `TLRO_1`), and which one a drawing
    should use is a house convention this cannot derive. Refusing and listing
    the candidates is the honest answer; picking the first would be a coin flip
    presented as a decision.
    """
    if not catalog.get("success"):
        return None, catalog

    syms = catalog.get("symbols") or []
    rivals = len(syms)
    if symbol:
        syms = [s for s in syms if s.get("symbol") == symbol]
        if not syms:
            return None, {
                "success": False,
                "error": "No symbol named %r in this project faces %s. "
                         "Call live_routing_catalog to see what does."
                         % (symbol, directions),
            }

    if not syms:
        return None, {
            "success": False,
            "error": "This project has no %s facing %s. live_routing_catalog "
                     "lists what it does have." % (kind, directions),
            "requestedDirections": directions,
        }

    if len(syms) > 1:
        return None, {
            "success": False,
            "error": "%d symbols face %s: %s. Which one this drawing should use "
                     "is a house convention, not something this can derive - "
                     "note that two symbols of the same type can differ only in "
                     "pin ORDER. Pass symbol=... to choose."
                     % (len(syms), directions,
                        ", ".join("%s/%s" % (s["library"], s["symbol"]) for s in syms)),
            "candidates": syms,
            "ambiguous": True,
        }

    s = syms[0]
    chosen_from = ("named by the caller out of %d symbols facing %s"
                   % (rivals, directions)) if (symbol and rivals > 1) else None
    variants = s.get("matchingVariants") or []
    if not variants:
        return None, {
            "success": False,
            "error": "%s/%s is the right symbol but no variant of it faces %s."
                     % (s["library"], s["symbol"], directions),
        }
    if variant_nr is not None:
        if variant_nr not in variants:
            return None, {
                "success": False,
                "error": "%s/%s variant %s does not face %s. The variants that "
                         "do: %s." % (s["library"], s["symbol"], variant_nr,
                                      directions,
                                      ", ".join(str(v) for v in variants)),
                "candidates": [s],
            }
        why = ("variant %s named by the caller out of %d that face %s"
               % (variant_nr, len(variants), directions))
        if chosen_from:
            why = "%s/%s %s; %s" % (s["library"], s["symbol"], chosen_from, why)
        return (s, variant_nr, why), None

    if len(variants) > 1:
        by_nr = {v["variantNr"]: v for v in (s.get("variants") or [])}
        detail = []
        for nr in variants:
            v = by_nr.get(nr) or {}
            detail.append("v%s: %s" % (nr, ", ".join(
                "%s at (%+.2f, %+.2f)" % (pin.get("direction"),
                                          (pin.get("offset") or {}).get("x", 0.0),
                                          (pin.get("offset") or {}).get("y", 0.0))
                for pin in (v.get("pins") or []))))
        return None, {
            "success": False,
            "error": "%s/%s has %d variants facing %s. They are NOT "
                     "interchangeable - the pins sit in different places. "
                     "Pass variant_nr to choose:\n  %s"
                     % (s["library"], s["symbol"], len(variants), directions,
                        "\n  ".join(detail)),
            "candidates": [s],
            "variantOffsets": detail,
            "ambiguous": True,
        }
    if chosen_from:
        return (s, variants[0], "%s/%s %s" % (s["library"], s["symbol"],
                                              chosen_from)), None
    return (s, variants[0], "the only %s in this project with a variant facing "
                            "%s" % (kind, directions)), None


def live_place_corner(page: str, x: float, y: float, directions: list,
                      symbol: str = None, variant_nr: int = None,
                      snap_to_grid: bool = True,
                      allow_real_project: bool = False,
                      timeout_seconds: float = 180.0) -> dict:
    """
    Place a corner where a wire turns. WRITES - scratch-only by default.

    A straight run between two facing pins needs NO object: EPLAN draws an
    autoconnecting line between them. A turn does need one, and this places it.

    The symbol is looked up in the project rather than assumed. There is no
    universal corner symbol - `live_routing_catalog` exists because a project
    can carry several, and a name that is right in one installation is wrong in
    the next.

    Args:
        page: Page name.
        x, y: The turn itself, in page millimetres. A corner's two connection
            points coincide exactly here - measured on `CO`, both pins report
            an offset of (0, 0) from the placement location - so one coordinate
            fully places it.
        directions: The two directions the corner faces, e.g. ["Right", "Down"]
            for a wire arriving from the east and leaving to the south. Order is
            irrelevant. Directions are absolute page directions: +y is Up,
            measured on a placed symbol whose Up pin sits at offset (0, +6.3).
        symbol: Name a symbol explicitly. Needed only when the project offers
            more than one for these directions, in which case this refuses and
            lists them rather than choosing.
        variant_nr: Force a variant. Normally derived from `directions`.
        snap_to_grid: Default True.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 180s - this reads the symbol libraries first.

    Returns:
        The `live_place_connection_symbol` result, plus "chosen" recording which
        symbol and variant were selected and why.

        On ambiguity: {"success": False, "ambiguous": True, "candidates": [...]}
        with no write attempted.
    """
    if isinstance(directions, str):
        directions = [directions]
    directions = list(directions or [])
    if len(directions) != 2:
        return {"success": False,
                "error": "A corner faces exactly two directions, got %d (%s). For a "
                         "three-way branch use live_place_tnode."
                         % (len(directions), directions)}
    if len(set(d.strip().title() for d in directions)) != 2:
        return {"success": False,
                "error": "A corner's two directions must differ, got %s. Two pins "
                         "facing the same way is a straight run, which needs no "
                         "symbol at all - EPLAN autoconnects it." % (directions,)}

    cat = live_routing_catalog(symbol_type="Routing", directions=directions,
                               timeout_seconds=timeout_seconds)
    picked, problem = _pick(cat, directions, symbol, "corner (Symbol.Type Routing)",
                            variant_nr=variant_nr)
    if problem:
        return problem
    sym, vnr, why = picked

    out = live_place_connection_symbol(
        page, sym["library"], sym["symbol"], x, y, variant_nr=vnr,
        snap_to_grid=snap_to_grid, allow_real_project=allow_real_project,
        timeout_seconds=timeout_seconds)
    if out.get("success"):
        out["chosen"] = {"library": sym["library"], "symbol": sym["symbol"],
                         "variantNr": vnr, "type": sym.get("type"),
                         "directions": directions,
                         "why": why}
    return out


def live_place_tnode(page: str, x: float, y: float, branch_direction: str,
                     symbol: str = None, variant_nr: int = None,
                     snap_to_grid: bool = True,
                     allow_real_project: bool = False,
                     timeout_seconds: float = 180.0) -> dict:
    """
    Place a T-node where a wire branches three ways. WRITES - scratch-only by
    default.

    Note the asymmetry with corners, which is EPLAN's and not ours: a corner is
    ONE symbol whose four rotations are variants, but a T-node is a separate
    `Symbol.Type` per direction - `TNodeUp` is a different type from
    `TNodeDown`. So a corner is chosen by variant and a T-node by type. That is
    why this takes a single `branch_direction` rather than a direction list.

    Args:
        page: Page name.
        x, y: The junction, in page millimetres.
        branch_direction: Which way the third leg points - "Up", "Down", "Left"
            or "Right". The other two legs run along the perpendicular axis, so
            "Up" means a horizontal run with a branch rising out of it.
        symbol: Name a symbol explicitly. A real project can hold two T-nodes of
            the SAME type differing only in pin order - measured, `TLRO` and
            `TLRO_1` are both `TNodeUp`. When that happens this refuses and
            lists them rather than flipping a coin.
        variant_nr: Force a variant.
        snap_to_grid: Default True.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 180s.

    Returns:
        The `live_place_connection_symbol` result plus "chosen", or an
        ambiguity refusal carrying "candidates".
    """
    try:
        d = cs_text(branch_direction, "branch_direction").strip().title()
    except SchematicValueError as exc:
        return _err(exc)
    if d not in TNODE_TYPE_BY_DIRECTION:
        return {"success": False,
                "error": "branch_direction must be one of %s, got %r."
                         % (", ".join(sorted(TNODE_TYPE_BY_DIRECTION)), branch_direction)}

    stype = TNODE_TYPE_BY_DIRECTION[d]
    # The three legs: the branch, plus the two along the perpendicular axis.
    legs = ([d, "Left", "Right"] if d in ("Up", "Down") else [d, "Up", "Down"])

    cat = live_routing_catalog(symbol_type=stype, directions=legs,
                               timeout_seconds=timeout_seconds)
    picked, problem = _pick(cat, legs, symbol, "T-node of type %s" % stype,
                            variant_nr=variant_nr)
    if problem:
        problem["branchDirection"] = d
        problem["symbolType"] = stype
        return problem
    sym, vnr, why = picked

    out = live_place_connection_symbol(
        page, sym["library"], sym["symbol"], x, y, variant_nr=vnr,
        snap_to_grid=snap_to_grid, allow_real_project=allow_real_project,
        timeout_seconds=timeout_seconds)
    if out.get("success"):
        out["chosen"] = {"library": sym["library"], "symbol": sym["symbol"],
                         "variantNr": vnr, "type": stype,
                         "branchDirection": d, "directions": legs,
                         "why": why}
    return out


# ---------------------------------------------------------------------------
# 13. Place a device already lined up with something on the page
# ---------------------------------------------------------------------------

OPPOSITE_DIRECTION = {"Up": "Down", "Down": "Up", "Left": "Right", "Right": "Left"}

_HELPERS_ONESYM = r'''
    // The connection points of ONE named symbol variant, straight out of the
    // library. live_routing_catalog reads the same thing, but only walks
    // CONNECTION symbols; this has to answer for a device.
    static Dictionary<string, object> ReadVariantPins(object project, string libName,
                                                      string symName, int variantNr)
    {
        Type libType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolLibrary");
        Type symType = FindType("Eplan.EplApi.DataModel.MasterData.Symbol");
        Type varType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolVariant");

        object lib = libType.GetConstructor(new Type[] { project.GetType(), typeof(string) })
            .Invoke(new object[] { project, libName });
        object sym = null;
        try
        {
            sym = symType.GetConstructor(new Type[] { libType, typeof(string) })
                .Invoke(new object[] { lib, symName });
        }
        catch (TargetInvocationException tie)
        { throw new Exception("Cannot open symbol " + symName + " in " + libName + ": " +
            Flatten(tie.InnerException)); }
        if (sym == null || PropText(sym, "IsValid") != "True")
            throw new Exception("Symbol " + symName + " does not resolve in " + libName + ".");

        object variant = null;
        try
        {
            variant = varType.GetConstructor(new Type[] { symType, typeof(int) })
                .Invoke(new object[] { sym, variantNr });
        }
        catch (TargetInvocationException tie)
        { throw new Exception("Symbol " + symName + " has no variant " + variantNr + ": " +
            Flatten(tie.InnerException)); }

        Dictionary<string, object> d = new Dictionary<string, object>();
        d["library"] = libName;
        d["symbol"] = symName;
        d["variantNr"] = variantNr;
        d["type"] = PropText(sym, "Type");
        d["pins"] = VariantPins(variant);
        return d;
    }
'''


def live_place_connected(page: str, to_handle: str, to_pin: int,
                         library: str, symbol: str, distance: float,
                         variant_nr: int = 0, new_pin: int = None,
                         allow_real_project: bool = False,
                         timeout_seconds: float = 120.0) -> dict:
    """
    Place a device already lined up to autoconnect with a pin on the page.
    WRITES - scratch-only by default.

    This is the tool that removes coordinate arithmetic from schematic
    authoring. You say "put a contact 40mm below -K1's pin 2"; it works out
    where the symbol has to sit so its own pin faces that one across a shared
    axis, which is the condition EPLAN needs to draw an autoconnecting line.

    No line is drawn, and none should be. Two facing pins on a shared axis ARE
    the connection - `generate_connections` then materialises a logical
    Connection between them. Verified live: -K1 -> corner -> -K2 laid out this
    way produced one connection, `+-K1[2] -> +-K2[1]`.

    How the new symbol is oriented is NOT decided here. `variant_nr` is the
    rotation - measured on `NFPA_symbol_en_US/SL`, v0 faces Up/Down and v1 faces
    Left/Right - and the variant you pass must actually have a pin facing back
    toward the anchor, or this refuses. Picking a rotation for you would be
    guessing at the drawing's intent.

    Args:
        page: Page the anchor is on. The new symbol goes on the same page.
        to_handle: Handle of the placement to line up with, from
            `live_place_symbol` or `live_read_page`. Session-scoped.
        to_pin: Which of its pins, by index, as `live_read_page` reports them.
            That pin's `direction` sets the axis and which way the run goes.
        library, symbol: The symbol to place.
        distance: Millimetres between the two PINS, measured along the run.
            Must be positive and a whole multiple of the page grid - an
            off-grid pin looks correct and refuses to autoconnect. The
            placement location is derived from this, not passed.
        variant_nr: Rotation of the new symbol, default 0.
        new_pin: Which pin of the new symbol faces the anchor. Needed only when
            the chosen variant has more than one pin facing that way; with
            exactly one, it is inferred.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 120s - this reads the symbol library first.

    Returns:
        The `live_place_symbol` result, plus "alignment": the anchor pin, the
        new pin, the shared axis, and the location that was solved for.

    Refuses rather than placing something that will not connect: a pin with no
    direction, a variant with no pin facing back, an ambiguous choice of pin, or
    a distance that is not a grid multiple.
    """
    try:
        page_cs = cs_text(page, "page")
        handle_cs = cs_text(to_handle, "to_handle")
        lib_cs = cs_text(library, "library")
        sym_cs = cs_text(symbol, "symbol")
        to_pin = cs_int(to_pin, "to_pin", minimum=0)
        variant_nr = cs_int(variant_nr, "variant_nr", minimum=0)
        if new_pin is not None:
            new_pin = cs_int(new_pin, "new_pin", minimum=0)
        distance = cs_double(distance, "distance")
    except SchematicValueError as exc:
        return _err(exc)

    try:
        dist = float(distance)
    except (TypeError, ValueError):
        return {"success": False, "error": "distance must be a number."}
    if dist <= 0:
        return {"success": False,
                "error": "distance must be positive - it is the gap between the two "
                         "pins along the run. The DIRECTION comes from the anchor "
                         "pin, not from the sign."}

    # Read the anchor pin and the candidate symbol's pins in one round trip.
    probe_body = '''            object page = FindPage(project, PAGENAME);
            object anchor = ResolveOnPage(page, ANCHORH);
            results["page"] = PropText(page, "Name");
            object grid = TryRead(page, "GridSize", null);
            results["gridSize"] = grid == null ? 0.0 : Convert.ToDouble(grid);
            results["anchor"] = DumpPlacement(anchor, true);
            results["candidate"] = ReadVariantPins(project, LIBNAME, SYMNAME, VARNR);
'''
    probe_body = _fill(
        probe_body,
        PAGENAME='"%s"' % cs_escape(page_cs),
        ANCHORH='"%s"' % cs_escape(handle_cs),
        LIBNAME='"%s"' % cs_escape(lib_cs),
        SYMNAME='"%s"' % cs_escape(sym_cs),
        VARNR=str(variant_nr),
    )
    probe = _shape(_execute_script(
        _script(_cls("AlignProbe"), probe_body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_ROUTING + _HELPERS_ONESYM),
        timeout=timeout_seconds,
    ))
    if not probe.get("success"):
        return probe

    anchor = probe.get("anchor") or {}
    anchor["pins"] = absolute_pins(anchor)
    apins = anchor.get("pins") or []
    match = [p for p in apins if p.get("index") == to_pin]
    if not match:
        return {"success": False,
                "error": "Placement %s has no pin %d. Its pins: %s."
                         % (to_handle, to_pin,
                            ", ".join(str(p.get("index")) for p in apins) or "none")}
    apin = match[0]

    direction = apin.get("direction")
    if direction not in OPPOSITE_DIRECTION:
        return {"success": False,
                "error": "Pin %d of %s reports direction %r, so there is no axis to "
                         "line up along. A pin with no direction cannot autoconnect."
                         % (to_pin, to_handle, direction)}
    if apin.get("point") is None:
        return {"success": False,
                "error": "Pin %d of %s has no resolved position (frame %r), so where "
                         "to put the new symbol cannot be computed. Treating it as "
                         "(0,0) would place the device on the page origin."
                         % (to_pin, to_handle, apin.get("frame"))}

    grid = probe.get("gridSize") or 0.0
    if grid > 0.0001:
        steps = dist / grid
        if abs(steps - round(steps)) > 1e-6:
            return {"success": False,
                    "error": "distance %g is not a multiple of the page grid %g "
                             "(%.3f steps). An off-grid pin looks correct on screen "
                             "and refuses to autoconnect. Nearest multiples: %g or %g."
                             % (dist, grid, steps,
                                math.floor(steps) * grid,
                                math.ceil(steps) * grid)}

    # The new symbol must have a pin facing BACK along the run.
    want_back = OPPOSITE_DIRECTION[direction]
    cand = probe.get("candidate") or {}
    cpins = cand.get("pins") or []
    facing = [i for i, p in enumerate(cpins) if p.get("direction") == want_back]
    if not facing:
        return {"success": False,
                "error": "%s/%s variant %d has no pin facing %s, so nothing of it "
                         "would face pin %d of %s (which faces %s). Its pins face: "
                         "%s. variant_nr is the rotation - try another."
                         % (library, symbol, variant_nr, want_back, to_pin,
                            to_handle, direction,
                            ", ".join(str(p.get("direction")) for p in cpins) or "nothing"),
                "candidatePins": cpins}
    if new_pin is not None:
        if new_pin not in facing:
            return {"success": False,
                    "error": "Pin %d of %s/%s variant %d does not face %s. The pins "
                             "that do: %s."
                             % (new_pin, library, symbol, variant_nr, want_back,
                                ", ".join(str(i) for i in facing))}
        chosen_pin = new_pin
    elif len(facing) > 1:
        return {"success": False,
                "error": "%s/%s variant %d has %d pins facing %s (indices %s). Which "
                         "one should meet pin %d of %s is not something this can "
                         "derive - pass new_pin."
                         % (library, symbol, variant_nr, len(facing), want_back,
                            ", ".join(str(i) for i in facing), to_pin, to_handle),
                "candidatePins": cpins,
                "ambiguous": True}
    else:
        chosen_pin = facing[0]

    # Solve for the LOCATION. The pin offset is relative to it, so the location
    # is the target pin position minus that offset.
    ax, ay = apin["point"]["x"], apin["point"]["y"]
    step = {"Up": (0.0, 1.0), "Down": (0.0, -1.0),
            "Left": (-1.0, 0.0), "Right": (1.0, 0.0)}[direction]
    tx, ty = ax + step[0] * dist, ay + step[1] * dist
    off = cpins[chosen_pin].get("offset") or {}
    loc_x, loc_y = tx - float(off.get("x", 0.0)), ty - float(off.get("y", 0.0))

    out = live_place_symbol(page, library, symbol, loc_x, loc_y,
                            variant_nr=variant_nr, snap_to_grid=False,
                            allow_real_project=allow_real_project,
                            timeout_seconds=timeout_seconds)
    if out.get("success"):
        out["alignment"] = {
            "anchor": {"handle": to_handle, "pin": to_pin,
                       "direction": direction, "point": {"x": ax, "y": ay}},
            "placed": {"pin": chosen_pin, "direction": want_back,
                       "point": {"x": tx, "y": ty}},
            "axis": "y" if direction in ("Up", "Down") else "x",
            "distance": dist,
            "location": {"x": loc_x, "y": loc_y},
            "note": "The two pins face each other on a shared axis, which is the "
                    "whole connection - no line is drawn. Run generate_connections "
                    "and read it back with live_read_connections.",
        }
    return out

