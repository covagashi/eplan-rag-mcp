"""Portable C# JSON selection and safe transformation."""
import pytest
from cs_json import json_mode, prepare_script


SCRIPT = """using System;
public class Probe
{
    public void Run()
    {
        var results = new System.Collections.Generic.Dictionary<string, object>();
        string json = Newtonsoft.Json.JsonConvert.SerializeObject(
            results, Newtonsoft.Json.Formatting.Indented);
    }
}
"""


def test_auto_selects_portable_only_for_29(monkeypatch):
    monkeypatch.delenv("EPLAN_MCP_JSON_MODE", raising=False)
    assert json_mode("2.9") == "portable"
    assert json_mode("2022") == "newtonsoft"
    assert json_mode("2027") == "newtonsoft"


def test_explicit_switch_overrides_version(monkeypatch):
    monkeypatch.setenv("EPLAN_MCP_JSON_MODE", "portable")
    assert json_mode("2027") == "portable"
    monkeypatch.setenv("EPLAN_MCP_JSON_MODE", "newtonsoft")
    assert prepare_script(SCRIPT, "2.9") == SCRIPT


def test_invalid_switch_fails_loudly(monkeypatch):
    monkeypatch.setenv("EPLAN_MCP_JSON_MODE", "oops")
    with pytest.raises(ValueError, match="EPLAN_MCP_JSON_MODE"):
        json_mode("2.9")


def test_portable_script_has_no_newtonsoft_reference(monkeypatch):
    monkeypatch.delenv("EPLAN_MCP_JSON_MODE", raising=False)
    out = prepare_script(SCRIPT, "2.9")
    assert "_McpJson(results)" in out
    assert "Newtonsoft.Json" not in out
    assert out.index("_McpJsonEsc") > out.index("public class Probe")
    assert "(char)92" in out  # escaping without an extra assembly


def test_unrecognized_serialize_shape_fails_before_running(monkeypatch):
    monkeypatch.setenv("EPLAN_MCP_JSON_MODE", "portable")
    source = SCRIPT.replace("results, Newtonsoft.Json.Formatting.Indented",
                            "GetResults()")
    with pytest.raises(ValueError, match="Unsupported"):
        prepare_script(source)
