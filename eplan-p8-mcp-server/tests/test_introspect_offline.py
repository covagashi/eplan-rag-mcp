"""Offline tests for api/actions/introspect.py: the generated reflection C# must
stay well-formed, injection-free, and free of the traps that make it fail inside
EPLAN's script engine. _execute_script is replaced by a capture stub, so no
EPLAN is needed.

The distinguishing property of this module versus live.py is what it must NOT
do: introspection reads metadata, so it must never open a project or take a
LockingStep. A regression there would make api_describe fail with "No project
is currently open" for a question that has nothing to do with a project."""

import pytest

from api.actions import introspect


@pytest.fixture
def capture(monkeypatch):
    """Stub _execute_script; captures the generated C# instead of running it."""
    captured = {}

    def fake_execute(script, timeout=30.0):
        captured["script"] = script
        captured["timeout"] = timeout
        return {"success": True, "results": {"stubbed": True}}

    monkeypatch.setattr(introspect, "_execute_script", fake_execute)
    return captured


INJECTION = '"; System.Environment.Exit(0); string y = "'


def _string_literals_balanced(cs: str) -> bool:
    """After stripping escape sequences, every line must contain an even
    number of quotes - i.e. no value broke out of its string literal."""
    stripped = cs.replace("\\\\", "").replace('\\"', "")
    return all(line.count('"') % 2 == 0 for line in stripped.splitlines())


ALL_TOOLS = [
    (introspect.api_types, {}),
    (introspect.api_describe, {"type_name": "Eplan.EplApi.DataModel.Page"}),
]


# ---------------------------------------------------------------------------
# The CS0234 trap: DataModel/HEServices must never appear as `using` directives
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_no_datamodel_using_directive(capture, fn, kwargs):
    fn(**kwargs)
    for line in capture["script"].splitlines():
        if line.strip().startswith("using "):
            assert "Eplan.EplApi.DataModel" not in line
            assert "Eplan.EplApi.HEServices" not in line
            assert "Eplan.EplApi.EServices" not in line


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_scaffold_basics(capture, fn, kwargs):
    fn(**kwargs)
    cs = capture["script"]
    assert "{{RESULT_PATH}}" in cs
    assert "AppDomain.CurrentDomain.GetAssemblies()" in cs
    assert "[Start]" in cs
    # `new Dictionary<..> { ["k"] = v }` is CS1525 on older script engines.
    assert '{ ["' not in cs
    assert "{[" not in cs.replace(" ", "")


# ---------------------------------------------------------------------------
# Metadata only: no project, no LockingStep. This is the point of the module.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_never_opens_a_project(capture, fn, kwargs):
    fn(**kwargs)
    cs = capture["script"]
    assert "GetCurrentProject" not in cs, \
        "introspection must not require an open project"
    assert "No project is currently open" not in cs


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_never_takes_a_locking_step(capture, fn, kwargs):
    fn(**kwargs)
    cs = capture["script"]
    assert "LockingStep" not in cs, \
        "metadata reads touch no project data, so they need no LockingStep"


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_reuses_live_static_helpers(capture, fn, kwargs):
    # The helper block is spliced out of live.py at _START_MARKER; if that seam
    # ever moves, these helpers silently vanish and the script stops compiling.
    fn(**kwargs)
    cs = capture["script"]
    assert "static Type FindType(string fullName)" in cs
    assert "static string Flatten(Exception ex)" in cs
    assert "static bool Matches(string value, string contains)" in cs
    # ...and the module's own additions
    assert "static string Sig(ParameterInfo[] ps)" in cs


def test_start_marker_seam_is_guarded():
    # The import-time guard in introspect.py is what turns a live.py refactor
    # into a loud failure instead of an uncompilable script.
    from api.actions import live
    assert live._START_MARKER in live._HELPERS
    assert "[Start]" not in introspect._STATIC_HELPERS


# ---------------------------------------------------------------------------
# Injection: every caller-supplied value must stay inside its string literal
# ---------------------------------------------------------------------------

def test_api_types_contains_is_escaped(capture):
    introspect.api_types(contains=INJECTION)
    cs = capture["script"]
    # The payload survives as DATA inside the literal - that is correct. What
    # matters is that its quotes were escaped so it cannot close the literal and
    # become code: no bare `"` remains around the injected text.
    assert '\\"' in cs
    assert _string_literals_balanced(cs)
    assert '"' + INJECTION not in cs


def test_api_types_namespace_is_escaped(capture):
    introspect.api_types(namespace=INJECTION)
    assert _string_literals_balanced(capture["script"])


def test_api_describe_type_name_is_escaped(capture):
    introspect.api_describe(type_name=INJECTION)
    assert _string_literals_balanced(capture["script"])


def test_api_describe_contains_is_escaped(capture):
    introspect.api_describe(type_name="Eplan.EplApi.DataModel.Page",
                            contains=INJECTION)
    assert _string_literals_balanced(capture["script"])


# ---------------------------------------------------------------------------
# limit is interpolated OUTSIDE a string literal -> must be a real integer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_rejects_non_integer_limit(capture, fn, kwargs):
    result = fn(limit=INJECTION, **kwargs)
    assert result["success"] is False
    assert "limit" in result["error"].lower()
    assert "script" not in capture, "malicious limit must never reach the script"


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_rejects_zero_limit(capture, fn, kwargs):
    result = fn(limit=0, **kwargs)
    assert result["success"] is False
    assert "script" not in capture


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_limit_is_capped(capture, fn, kwargs):
    fn(limit=999999, **kwargs)
    assert "int limit = 2000;" in capture["script"]


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_coerces_numeric_string_limit(capture, fn, kwargs):
    fn(limit="25", **kwargs)
    assert "int limit = 25;" in capture["script"]


# ---------------------------------------------------------------------------
# api_types: response shape switches on whether a filter was given
# ---------------------------------------------------------------------------

def test_bare_call_returns_namespace_summary_only(capture):
    introspect.api_types()
    cs = capture["script"]
    assert "bool wantTypes = false;" in cs
    assert 'string prefix = "Eplan.EplApi";' in cs


def test_contains_switches_on_type_listing(capture):
    introspect.api_types(contains="Macro")
    assert "bool wantTypes = true;" in capture["script"]


def test_namespace_switches_on_type_listing_and_overrides_prefix(capture):
    introspect.api_types(namespace="Eplan.EplApi.DataModel.MasterData")
    cs = capture["script"]
    assert "bool wantTypes = true;" in cs
    assert 'string prefix = "Eplan.EplApi.DataModel.MasterData";' in cs


def test_load_all_defaults_off(capture):
    # Loading assemblies mutates the EPLAN process; a read tool must not do it
    # unless asked.
    introspect.api_types()
    assert "if (false)\n                results[\"lazyAssembliesLoaded\"]" in capture["script"]


def test_load_all_can_be_enabled(capture):
    introspect.api_types(load_all=True)
    cs = capture["script"]
    assert "if (true)" in cs
    assert "LoadLazyAssemblies()" in cs


# ---------------------------------------------------------------------------
# api_describe: argument validation and the AmbiguousMatchException-safe walk
# ---------------------------------------------------------------------------

def test_describe_requires_a_type_name(capture):
    for bad in (None, "", "   "):
        result = introspect.api_describe(type_name=bad)
        assert result["success"] is False
        assert "type_name" in result["error"]
    assert "script" not in capture


def test_describe_rejects_unknown_members_category(capture):
    result = introspect.api_describe(type_name="X", members="everything")
    assert result["success"] is False
    # The error must name the valid values, so a wrong guess is a one-turn fix.
    for valid in ("all", "methods", "properties", "constructors", "fields"):
        assert valid in result["error"]
    assert "script" not in capture


@pytest.mark.parametrize("members", ["all", "methods", "properties",
                                     "constructors", "fields"])
def test_describe_accepts_each_category(capture, members):
    introspect.api_describe(type_name="Eplan.EplApi.DataModel.Page",
                            members=members)
    assert 'string want = "%s";' % members in capture["script"]


def test_describe_members_is_case_insensitive(capture):
    introspect.api_describe(type_name="Eplan.EplApi.DataModel.Page",
                            members="METHODS")
    assert 'string want = "methods";' in capture["script"]


def test_describe_walks_declared_only_most_derived_first(capture):
    # Type.GetProperty(name) throws AmbiguousMatchException on EPLAN types; the
    # DeclaredOnly walk up the chain is what avoids it, and matches how C#
    # member lookup actually binds.
    introspect.api_describe(type_name="Eplan.EplApi.DataModel.Page")
    cs = capture["script"]
    assert "BindingFlags.DeclaredOnly" in cs
    assert "cur = cur.BaseType;" in cs
    assert 'cur.FullName != "System.Object"' in cs
    assert '"declaredIn"' in cs


def test_describe_inherited_flag(capture):
    introspect.api_describe(type_name="Eplan.EplApi.DataModel.Page",
                            inherited=False)
    assert "bool walkBase = false;" in capture["script"]
    introspect.api_describe(type_name="Eplan.EplApi.DataModel.Page")
    assert "bool walkBase = true;" in capture["script"]


def test_describe_reports_enum_numeric_values(capture):
    # Enum.ToObject(t, n) in generated scripts needs the NUMBER, not the name -
    # this is how MoveKind.Relative=2 and NumerationMode.None=1 were settled.
    introspect.api_describe(
        type_name="Eplan.EplApi.HEServices.Insert+MoveKind")
    cs = capture["script"]
    assert "Enum.GetValues(target)" in cs
    assert "Convert.ToInt64(v)" in cs


def test_describe_does_not_walk_an_enums_base_chain(capture):
    # Measured live: describing MoveKind returned its 2 members plus 31
    # inherited System.Enum/System.ValueType methods (Parse, ToObject x8,
    # HasFlag...), none of which says anything about the enum.
    introspect.api_describe(
        type_name="Eplan.EplApi.HEServices.Insert+MoveKind")
    assert "if (target.IsEnum) walkBase = false;" in capture["script"]


def test_describe_nested_type_name_survives_escaping(capture):
    nested = "Eplan.EplApi.DataModel.MasterData.PageMacro+Enums+NumerationMode"
    introspect.api_describe(type_name=nested)
    assert '"%s"' % nested in capture["script"]


def test_describe_offers_candidates_on_a_miss(capture):
    # A wrong namespace is the single most common mistake; answering with a
    # bare "not found" makes it a dead end instead of a one-turn fix.
    introspect.api_describe(type_name="Eplan.EplApi.Nope.Page")
    cs = capture["script"]
    assert '"candidates"' in cs
    assert "not found in any loaded Eplan assembly" in cs


def test_describe_skips_property_accessors(capture):
    # get_/set_ pairs would double every property list for no information.
    introspect.api_describe(type_name="Eplan.EplApi.DataModel.Page")
    assert "mi.IsSpecialName" in capture["script"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_tools_are_exported():
    from api import actions
    assert "api_types" in actions.__all__
    assert "api_describe" in actions.__all__
    assert callable(actions.api_types)
    assert callable(actions.api_describe)
