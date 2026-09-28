"""JSON output for C# scripts compiled by EPLAN.

EPLAN 2.9 does not reference Newtonsoft.Json in its script compiler, even when
the assembly is loaded in the process. Keep modern installations unchanged by
default and use a self-contained writer for 2.9.
"""

import os
import re


CS_JSON_HELPER = r'''
    private static string _McpJsonEsc(string value)
    {
        var sb = new System.Text.StringBuilder();
        sb.Append((char)34);
        foreach (char ch in value)
        {
            switch (ch)
            {
                case (char)34: sb.Append((char)92).Append((char)34); break;
                case (char)92: sb.Append((char)92).Append((char)92); break;
                case (char)8: sb.Append((char)92).Append('b'); break;
                case (char)9: sb.Append((char)92).Append('t'); break;
                case (char)10: sb.Append((char)92).Append('n'); break;
                case (char)12: sb.Append((char)92).Append('f'); break;
                case (char)13: sb.Append((char)92).Append('r'); break;
                default:
                    if (ch < 32 || ch == 0x2028 || ch == 0x2029)
                    {
                        sb.Append((char)92).Append('u');
                        sb.Append(((int)ch).ToString("x4", System.Globalization.CultureInfo.InvariantCulture));
                    }
                    else sb.Append(ch);
                    break;
            }
        }
        sb.Append((char)34);
        return sb.ToString();
    }

    private static string _McpJson(object value)
    {
        if (value == null) return "null";
        if (value is string || value is char) return _McpJsonEsc(value.ToString());
        if (value is bool) return (bool)value ? "true" : "false";
        var dict = value as System.Collections.IDictionary;
        if (dict != null)
        {
            var items = new System.Collections.Generic.List<string>();
            foreach (System.Collections.DictionaryEntry entry in dict)
                items.Add(_McpJsonEsc(System.Convert.ToString(entry.Key)) + ":" + _McpJson(entry.Value));
            return "{" + string.Join(",", items.ToArray()) + "}";
        }
        var sequence = value as System.Collections.IEnumerable;
        if (sequence != null)
        {
            var items = new System.Collections.Generic.List<string>();
            foreach (object item in sequence) items.Add(_McpJson(item));
            return "[" + string.Join(",", items.ToArray()) + "]";
        }
        if (value is double || value is float)
        {
            double number = System.Convert.ToDouble(value);
            if (double.IsNaN(number) || double.IsInfinity(number)) return "null";
        }
        if (value is System.IFormattable && !(value is System.DateTime))
        {
            var code = System.Type.GetTypeCode(value.GetType());
            if (code >= System.TypeCode.SByte && code <= System.TypeCode.Decimal)
                return ((System.IFormattable)value).ToString(null, System.Globalization.CultureInfo.InvariantCulture);
        }
        return _McpJsonEsc(System.Convert.ToString(value, System.Globalization.CultureInfo.InvariantCulture));
    }
'''

_SERIALIZE = re.compile(
    r"Newtonsoft\.Json\.JsonConvert\.SerializeObject\(\s*([A-Za-z_]\w*)"
    r"\s*(?:,\s*Newtonsoft\.Json\.Formatting\.Indented\s*)?\)"
)
_CLASS = re.compile(r"^[ \t]*(?:(?:public|internal|private|sealed|static|partial|abstract)\s+)*class\s+[A-Za-z_]\w*[^\{]*\{", re.MULTILINE)


def json_mode(target_version=None):
    """Choose 'portable' or 'newtonsoft'; allow an explicit deployment switch."""
    configured = os.environ.get("EPLAN_MCP_JSON_MODE", "auto").strip().lower()
    if configured not in ("auto", "portable", "newtonsoft"):
        raise ValueError("EPLAN_MCP_JSON_MODE must be auto, portable, or newtonsoft")
    if configured != "auto":
        return configured
    return "portable" if str(target_version or "").startswith("2.9") else "newtonsoft"


def prepare_script(source, target_version=None):
    """Replace generated Newtonsoft result writes when portable mode is on."""
    if json_mode(target_version) == "newtonsoft" or "Newtonsoft.Json.JsonConvert.SerializeObject" not in source:
        return source
    converted, count = _SERIALIZE.subn(r"_McpJson(\1)", source)
    if not count or "Newtonsoft.Json.JsonConvert.SerializeObject" in converted:
        raise ValueError("Unsupported Newtonsoft.Json.SerializeObject call in generated script")
    match = _CLASS.search(converted)
    if not match:
        raise ValueError("Cannot insert portable JSON writer: no C# class found")
    return converted[:match.end()] + CS_JSON_HELPER + converted[match.end():]
