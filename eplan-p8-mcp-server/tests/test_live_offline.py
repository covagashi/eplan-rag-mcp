"""Offline tests for api/actions/live.py: the generated reflection C# must stay
well-formed, injection-free, and free of the traps that make it fail inside
EPLAN's script engine. _execute_script is replaced by a capture stub, so no
EPLAN is needed."""

import pytest

from api.actions import live


@pytest.fixture
def capture(monkeypatch):
    """Stub _execute_script; captures the generated C# instead of running it."""
    captured = {}

    def fake_execute(script, timeout=30.0):
        captured["script"] = script
        captured["timeout"] = timeout
        return {"success": True, "results": {"stubbed": True}}

    monkeypatch.setattr(live, "_execute_script", fake_execute)
    return captured


INJECTION = '"; System.Environment.Exit(0); string y = "'


def _string_literals_balanced(cs: str) -> bool:
    """After stripping escape sequences, every line must contain an even
    number of quotes - i.e. no value broke out of its string literal."""
    stripped = cs.replace("\\\\", "").replace('\\"', "")
    return all(line.count('"') % 2 == 0 for line in stripped.splitlines())


ALL_TOOLS = [
    (live.live_query_functions, {}),
    (live.live_query_pages, {}),
    (live.live_set_function_text, {"name": "+X-K1", "text": "hi"}),
    (live.live_set_connection_designations,
     {"name": "+X-K1", "designations": ["Y11", "Y12"]}),
    (live.live_read_check_messages, {}),
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


# ---------------------------------------------------------------------------
# Assembly resolution: never Assembly.Load the native twin as the primary route
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_resolves_types_from_loaded_assemblies(capture, fn, kwargs):
    fn(**kwargs)
    cs = capture["script"]
    # Scanning the loaded set is the primary route.
    assert "AppDomain.CurrentDomain.GetAssemblies()" in cs
    # In the Assembly.Load FALLBACK list, the *Netu names must come first: on
    # 2027 the un-suffixed name is the mixed-mode native twin and loading it
    # throws BadImageFormatException. Scope the check to the candidates array,
    # since the surrounding comment mentions the un-suffixed name too.
    start = cs.index("string[] candidates")
    candidates = cs[start:cs.index("};", start)]
    assert candidates.index('"Eplan.EplApi.DataModelNetu"') < candidates.index('"Eplan.EplApi.DataModelu"')
    assert candidates.index('"Eplan.EplApi.HEServicesNetu"') < candidates.index('"Eplan.EplApi.HEServicesu"')


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_locking_step_taken_and_disposed(capture, fn, kwargs):
    fn(**kwargs)
    cs = capture["script"]
    assert 'FindType("Eplan.EplApi.DataModel.LockingStep")' in cs
    assert "finally" in cs
    assert 'lsType.GetMethod("Dispose")' in cs


# ---------------------------------------------------------------------------
# Script-engine syntax limits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_no_dictionary_index_initializers(capture, fn, kwargs):
    # `new Dictionary<..> { ["k"] = v }` is CS1525 in EPLAN's script engine.
    fn(**kwargs)
    assert '{ ["' not in capture["script"]
    assert "{[" not in capture["script"].replace(" ", "")


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_result_path_placeholder_present(capture, fn, kwargs):
    fn(**kwargs)
    assert "{{RESULT_PATH}}" in capture["script"]


# ---------------------------------------------------------------------------
# limit is interpolated outside a string literal -> must be a real integer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_rejects_non_integer_limit(capture, fn, kwargs):
    result = fn(limit=INJECTION, **kwargs)
    assert result["success"] is False
    assert "limit" in result["error"].lower()
    assert "script" not in capture, "malicious limit must never reach the script"


def test_query_coerces_numeric_string_limit(capture):
    result = live.live_query_pages(limit="25")
    assert result["success"] is True
    assert "list.Count < 25" in capture["script"]


def test_query_default_limit_in_script(capture):
    live.live_query_functions()
    assert "list.Count < 100" in capture["script"]


def test_set_function_text_default_limit_is_one(capture):
    # A mistaken name must not mass-edit the project.
    live.live_set_function_text(name="+X-K1", text="hi")
    assert "details.Count >= 1" in capture["script"]


# ---------------------------------------------------------------------------
# String parameters must stay inside their literals
# ---------------------------------------------------------------------------

def test_contains_injection_stays_in_literal(capture):
    live.live_query_functions(contains=INJECTION)
    cs = capture["script"]
    # The payload survives as DATA inside the literal - that is correct. What
    # matters is that its quotes were escaped so it cannot close the literal and
    # become code: no bare `"` remains around the injected text.
    assert '\\"' in cs
    assert _string_literals_balanced(cs)
    # Every quote in the payload is escaped, so the only occurrences of a bare
    # quote are the literal's own delimiters.
    assert '"' + INJECTION not in cs


def test_name_and_text_injection_stay_in_literal(capture):
    live.live_set_function_text(name=INJECTION, text=INJECTION)
    cs = capture["script"]
    assert _string_literals_balanced(cs)


def test_backslash_path_is_escaped(capture):
    live.live_set_function_text(name=r"+X-K1", text=r"C:\Temp\A1")
    cs = capture["script"]
    # A raw C:\Temp\A1 would be CS1009 (unrecognized escape \T, \A).
    assert r"C:\\Temp\\A1" in cs
    assert _string_literals_balanced(cs)


def test_unicode_line_terminator_stays_in_literal(capture):
    ls = chr(0x2028)
    live.live_query_pages(contains=f"a{ls}b")
    assert ls not in capture["script"]


# ---------------------------------------------------------------------------
# Contract details
# ---------------------------------------------------------------------------

def test_set_function_text_requires_name(capture):
    result = live.live_set_function_text(name="", text="hi")
    assert result["success"] is False
    assert "script" not in capture


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_timeout_is_forwarded(capture, fn, kwargs):
    fn(timeout_seconds=123.0, **kwargs)
    assert capture["timeout"] == 123.0


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_class_names_are_unique_per_call(capture, fn, kwargs):
    fn(**kwargs)
    first = capture["script"]
    fn(**kwargs)
    second = capture["script"]
    # Same script twice would collide in EPLAN's script engine cache.
    assert first != second


def test_ambiguous_match_guard_present(capture):
    # Function.Properties and FUNC_TEXT[int] both make a plain GetProperty throw
    # AmbiguousMatchException; the generated code must use the guarded lookup.
    live.live_set_function_text(name="+X-K1", text="hi")
    cs = capture["script"]
    assert "DeclaredOnly" in cs
    assert "Type.EmptyTypes" in cs
    assert 'GetPropInfo(props.GetType(), "FUNC_TEXT")' in cs


# ---------------------------------------------------------------------------
# discovery.list_layers reaches the DataModel through the same reflection
# scaffold. Emitting `using Eplan.EplApi.DataModel;` is CS0234, which means the
# script never compiles, never writes its result file, and the tool can only
# ever time out - so guard the using-directive absence here too.
# ---------------------------------------------------------------------------

def test_list_layers_uses_reflection_not_using_directive(monkeypatch):
    from api.actions import discovery

    captured = {}

    def fake_execute(script, timeout=30.0):
        captured["script"] = script
        captured["timeout"] = timeout
        return {"success": True, "results": {"stubbed": True}}

    monkeypatch.setattr(discovery, "_execute_script", fake_execute)
    discovery.list_layers()

    cs = captured["script"]
    for line in cs.splitlines():
        if line.strip().startswith("using "):
            assert "Eplan.EplApi.DataModel" not in line
            assert "Eplan.EplApi.HEServices" not in line
    # goes through the shared reflection scaffold
    assert "AppDomain.CurrentDomain.GetAssemblies()" in cs
    assert 'FindType("Eplan.EplApi.DataModel.LockingStep")' in cs
    assert 'GetPropInfo(project.GetType(), "LayerTable")' in cs
    # a 457-layer table needs more headroom than the 30s default
    assert captured["timeout"] > 30.0


# ---------------------------------------------------------------------------
# live_set_connection_designations - connection point numbers (property 20022)
# ---------------------------------------------------------------------------

def test_conn_designations_writes_each_index_in_order(capture):
    live.live_set_connection_designations("+-TEST", ["Y11", "Y12"])
    cs = capture["script"]
    # one designation per index, in the order given
    assert 'string[] wanted = new string[] { "Y11", "Y12" };' in cs
    # and they are written by slot, not as one joined blob
    assert "int slot = i + 1;" in cs


def test_conn_designations_uses_indexed_property_lookup(capture):
    # FUNC_CONNECTIONDESIGNATION is declared both plain and [int]; the indexed
    # form is the one that addresses a single connection point. Using the
    # non-indexed lookup here would fetch the wrong declaration.
    live.live_set_connection_designations("+-TEST", ["Y11"])
    cs = capture["script"]
    assert 'GetPropInfoIdx(props.GetType(), "FUNC_CONNECTIONDESIGNATION")' in cs
    assert "new Type[] { typeof(int) }" in cs


def test_conn_designations_reads_back_each_slot(capture):
    # The reported value must come from a fresh fetch, not the local wrapper.
    live.live_set_connection_designations("+-TEST", ["Y11", "Y12"])
    cs = capture["script"]
    assert cs.count("cdIdx.GetValue(props, new object[] { slot })") >= 2


def test_conn_designations_rejects_joined_string(capture):
    # "Y11¶Y12" as one value would land entirely in index 1.
    result = live.live_set_connection_designations("+-TEST", "Y11" + live.PILCROW + "Y12")
    assert result["success"] is False
    assert "list of strings" in result["error"]
    assert "script" not in capture


def test_conn_designations_rejects_pilcrow_inside_an_element(capture):
    result = live.live_set_connection_designations("+-TEST", ["Y11" + live.PILCROW + "Y12"])
    assert result["success"] is False
    assert "pilcrow" in result["error"]
    assert "script" not in capture


@pytest.mark.parametrize("bad", [[], None, 42, {}])
def test_conn_designations_rejects_bad_container(capture, bad):
    result = live.live_set_connection_designations("+-TEST", bad)
    assert result["success"] is False
    assert "script" not in capture


def test_conn_designations_rejects_none_element(capture):
    result = live.live_set_connection_designations("+-TEST", ["Y11", None])
    assert result["success"] is False
    assert "None" in result["error"]
    assert "script" not in capture


def test_conn_designations_requires_name(capture):
    result = live.live_set_connection_designations("", ["Y11"])
    assert result["success"] is False
    assert "script" not in capture


def test_conn_designations_default_limit_is_one(capture):
    live.live_set_connection_designations("+-TEST", ["Y11"])
    assert "details.Count >= 1" in capture["script"]


def test_conn_designations_coerces_non_string_elements(capture):
    live.live_set_connection_designations("+-TEST", [11, 12])
    assert 'new string[] { "11", "12" };' in capture["script"]


def test_conn_designations_escapes_injection(capture):
    live.live_set_connection_designations("+-TEST", [INJECTION, "Y12"])
    cs = capture["script"]
    assert _string_literals_balanced(cs)
    assert '"' + INJECTION not in cs


# ---------------------------------------------------------------------------
# live_read_check_messages - PrjMessagesCollection (Eplan.EplApi.EServices),
# the itemized store behind the "Message management" dialog. Reached the same
# way DataModel/HEServices are: never `using`, always FindType + reflection.
# ---------------------------------------------------------------------------

def test_read_check_messages_no_eservices_using_directive(capture):
    # EServices is a third namespace in the same CS0234 family as
    # DataModel/HEServices - must never appear as a `using` directive either.
    live.live_read_check_messages()
    for line in capture["script"].splitlines():
        if line.strip().startswith("using "):
            assert "Eplan.EplApi.EServices" not in line


def test_read_check_messages_resolves_collection_by_reflection(capture):
    live.live_read_check_messages()
    cs = capture["script"]
    assert 'FindType("Eplan.EplApi.EServices.PrjMessagesCollection")' in cs
    assert 'collType.GetMethod("GetPrjMsgEnumerator")' in cs
    assert 'itType.GetProperty("CurrentProjectMessage")' in cs


def test_read_check_messages_default_window(capture):
    live.live_read_check_messages()
    cs = capture["script"]
    assert "int startIdx = 1;" in cs
    assert "int endIdx = 50;" in cs


def test_read_check_messages_start_index_and_limit_set_the_window(capture):
    live.live_read_check_messages(start_index=1940, limit=1)
    cs = capture["script"]
    assert "int startIdx = 1940;" in cs
    assert "int endIdx = 1940;" in cs


def test_read_check_messages_limit_is_capped_at_500(capture):
    live.live_read_check_messages(start_index=1, limit=999999)
    cs = capture["script"]
    assert "int endIdx = 500;" in cs


def test_read_check_messages_rejects_start_index_below_one(capture):
    result = live.live_read_check_messages(start_index=0)
    assert result["success"] is False
    assert "script" not in capture


def test_read_check_messages_contains_stays_in_literal(capture):
    live.live_read_check_messages(contains=INJECTION)
    cs = capture["script"]
    assert _string_literals_balanced(cs)
    assert '"' + INJECTION not in cs


def test_read_check_messages_default_contains_is_empty_string(capture):
    # Matches() treats an empty filter as "match everything" - confirm that is
    # what an omitted `contains` actually compiles to, not the string "None".
    live.live_read_check_messages()
    assert 'string filterText = "";' in capture["script"]
