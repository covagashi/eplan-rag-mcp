"""
Tool registry + meta-tools for EPLAN_MCP_MODE="discovery".

The problem this solves: every MCP request carries the full tool list. Publishing
all ~194 wrappers as individual MCP tools costs ~180,000 characters (~45,000
tokens) of name + description + JSON schema on EVERY request, before the model
has done anything.

Discovery mode publishes only a small always-on core (connection/session +
the action-catalog tier) plus three meta-tools defined here:

    eplan_tools_search(query, limit)   find hidden tools by name / docstring
    eplan_tools_describe(names)        full docstring + parameters for a few
    eplan_tools_call(name, arguments)  invoke one by name

Everything else stays reachable but stops being paid for on every turn.

Design notes:
- The index is built by introspection of the SAME functions server.py registers
  (module + attribute name), never a hand-maintained list, so it cannot rot.
- Entries store (module, attr) and resolve with getattr at call/describe time.
  Late binding keeps monkeypatching and reloaded extension modules honest.
- Nothing here imports EPLAN, pythonnet or `server` (server imports this).
  Building and searching the index works with EPLAN closed.
- search() returns parameter NAMES only. Returning full schemas would rebuild
  the exact problem discovery mode exists to remove.
"""

import difflib
import inspect
import json
from typing import List, Union

from pydantic import ValidationError

try:
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata
except ImportError:  # pragma: no cover - depends on the installed mcp package
    func_metadata = None

__all__ = ["ToolRegistry", "make_meta_tools", "META_TOOL_NAMES"]


META_TOOL_NAMES = ("tools_search", "tools_describe", "tools_call")


class _ArgValidationError(Exception):
    """Internal signal: ToolRegistry.call's argument coercion failed."""

# Search/describe caps. limit is clamped rather than rejected: an over-eager
# limit=1000 should not error, it should just stop short of re-publishing the
# whole catalogue inline.
_MAX_LIMIT = 200
_MAX_DESCRIBE = 25
_CLOSE_MATCHES = 5


def _first_line(doc):
    """First meaningful line of a docstring (skips a leading blank line)."""
    for line in (doc or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _short_module(func, fallback=""):
    """'api.actions.export_' -> 'export_'; used to group the overview."""
    mod = getattr(func, "__module__", None) or fallback
    return mod.rsplit(".", 1)[-1] if mod else fallback


def _jsonable(value):
    """Defaults land in JSON responses; keep anything exotic printable."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return repr(value)


def _param_records(sig):
    """[{name, required, default, annotation, kind}] for a signature."""
    out = []
    if sig is None:
        return out
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            out.append({"name": "**" + p.name, "required": False, "default": None,
                        "annotation": "", "kind": "var_keyword"})
            continue
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            out.append({"name": "*" + p.name, "required": False, "default": None,
                        "annotation": "", "kind": "var_positional"})
            continue
        required = p.default is inspect.Parameter.empty
        annotation = ""
        if p.annotation is not inspect.Parameter.empty:
            annotation = getattr(p.annotation, "__name__", None) or str(p.annotation)
        out.append({
            "name": p.name,
            "required": required,
            "default": None if required else _jsonable(p.default),
            "annotation": annotation,
            "kind": ("keyword_only" if p.kind is inspect.Parameter.KEYWORD_ONLY
                     else "positional_or_keyword"),
        })
    return out


class ToolRegistry:
    """
    Index of every tool the server knows about, published or hidden.

    server.py adds an entry for each function it would register as an MCP tool
    (in full mode it registers them all; in discovery mode it registers only the
    core and leaves the rest hidden). The registry never decides what is
    published - it only records it, so search results can say so.
    """

    def __init__(self):
        self._entries = {}   # tool_name -> {module, attr, prefix, published}
        self._order = []     # insertion order, for stable overviews
        # Introspection caches, keyed by tool name and holding the function
        # object they were built from. Entries are reused only while
        # resolve() still returns that same object, so a monkeypatched or
        # reloaded module is picked up on the next call exactly as before.
        self._info_cache = {}   # tool_name -> (func, record without "published")
        self._sig_cache = {}    # tool_name -> (func, signature | None)
        self._meta_cache = {}   # tool_name -> (func, FuncMetadata | None)

    # -- population ---------------------------------------------------------

    def add(self, tool_name, module, attr, prefix="eplan_", published=False):
        """Record one tool. Re-adding a name updates it (last registration wins)."""
        if tool_name not in self._entries:
            self._order.append(tool_name)
        self._info_cache.pop(tool_name, None)
        self._sig_cache.pop(tool_name, None)
        self._meta_cache.pop(tool_name, None)
        self._entries[tool_name] = {
            "module": module,
            "attr": attr,
            "prefix": prefix,
            "published": bool(published),
        }

    def mark_published(self, tool_name, published=True):
        entry = self._entries.get(tool_name)
        if entry is not None:
            entry["published"] = bool(published)

    # -- introspection ------------------------------------------------------

    def names(self):
        return list(self._order)

    def hidden_names(self):
        return [n for n in self._order if not self._entries[n]["published"]]

    def published_names(self):
        return [n for n in self._order if self._entries[n]["published"]]

    def __len__(self):
        return len(self._entries)

    def __contains__(self, tool_name):
        return tool_name in self._entries

    def resolve(self, tool_name):
        """
        The live function behind a tool name, resolved through getattr so a
        monkeypatched or reloaded module is picked up. None if the name is
        unknown or the attribute has vanished.
        """
        entry = self._entries.get(tool_name)
        if entry is None:
            return None
        return getattr(entry["module"], entry["attr"], None)

    def _alias(self, name):
        """
        Map a caller-supplied name onto a real one. Models routinely drop the
        "eplan_" prefix ("export_pdf_project"), so try every prefix in use
        before declaring the name unknown.
        """
        if not isinstance(name, str):
            return None
        name = name.strip()
        if name in self._entries:
            return name
        prefixes = dict.fromkeys(e["prefix"] for e in self._entries.values())
        for prefix in prefixes:
            if prefix and prefix + name in self._entries:
                return prefix + name
        return None

    def _near(self, name):
        return difflib.get_close_matches(
            str(name), self._order, n=_CLOSE_MATCHES, cutoff=0.4)

    def _signature(self, tool_name, func):
        """
        inspect.signature(func), or None if it has none, cached until
        resolve() hands back a different object for *tool_name*.
        """
        cached = self._sig_cache.get(tool_name)
        if cached is not None and cached[0] is func:
            return cached[1]
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            sig = None
        self._sig_cache[tool_name] = (func, sig)
        return sig

    def _metadata(self, tool_name, func):
        """
        FuncMetadata for the live function, or None when no argument model
        applies (see _coerce_arguments). Cached like _signature.
        """
        cached = self._meta_cache.get(tool_name)
        if cached is not None and cached[0] is func:
            return cached[1]
        meta = None
        sig = self._signature(tool_name, func)
        if (func_metadata is not None and sig is not None
                and not any(p.kind is inspect.Parameter.VAR_KEYWORD
                            for p in sig.parameters.values())):
            try:
                meta = func_metadata(func)
            except Exception:
                meta = None
        self._meta_cache[tool_name] = (func, meta)
        return meta

    def _info(self, tool_name):
        """Introspected record for one tool. Always reads the live function."""
        entry = self._entries[tool_name]
        func = self.resolve(tool_name)
        if func is None:
            return {"name": tool_name, "module": "", "summary": "", "doc": "",
                    "signature": tool_name + "(...)", "parameters": [],
                    "published": entry["published"],
                    "error": "function is no longer available on its module"}
        cached = self._info_cache.get(tool_name)
        if cached is None or cached[0] is not func:
            doc = inspect.getdoc(func) or ""
            sig = self._signature(tool_name, func)
            record = {
                "name": tool_name,
                "module": _short_module(func, entry["attr"]),
                "summary": _first_line(doc),
                "doc": doc,
                "signature": (tool_name + str(sig)) if sig is not None else tool_name + "(...)",
                "parameters": _param_records(sig),
            }
            cached = (func, record)
            self._info_cache[tool_name] = cached
        info = dict(cached[1])
        info["parameters"] = [dict(p) for p in info["parameters"]]
        info["published"] = entry["published"]
        return info

    # -- meta-tool implementations -----------------------------------------

    def search(self, query=None, limit=30):
        """Implementation of eplan_tools_search."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, _MAX_LIMIT))

        if query is None or not str(query).strip():
            return self._overview()

        terms = [t for t in str(query).lower().split() if t]
        scored = []
        for name in self._order:
            info = self._info(name)
            lname = name.lower()
            lsummary = info["summary"].lower()
            ldoc = info["doc"].lower()
            # every term must appear somewhere in name+doc (AND, not OR)
            if not all(t in lname or t in ldoc for t in terms):
                continue
            score = 0
            for t in terms:
                if t in lname:
                    score += 10
                if lname.startswith(t):
                    score += 5
                if t in lsummary:
                    score += 3
                if t in ldoc:
                    score += 1
            scored.append((-score, name, info))
        scored.sort(key=lambda item: (item[0], item[1]))

        total = len(scored)
        rows = [{
            "name": info["name"],
            "summary": info["summary"],
            "params": [p["name"] for p in info["parameters"]],
            "module": info["module"],
            "published": info["published"],
        } for _, _, info in scored[:limit]]

        result = {
            "success": True,
            "query": query,
            "total_matches": total,
            "returned": len(rows),
            "truncated": total > len(rows),
            "tools": rows,
            "hint": "Call eplan_tools_describe(names=[...]) for full parameter "
                    "docs, then eplan_tools_call(name=..., arguments={...}).",
        }
        if not total:
            result["did_you_mean"] = self._near(query)
        return result

    def _overview(self):
        """No query: a grouped map, not ~190 flat rows."""
        groups = {}
        for name in self._order:
            info = self._info(name)
            groups.setdefault(info["module"] or "other", []).append(name)
        return {
            "success": True,
            "query": None,
            "total_tools": len(self._entries),
            "published_tools": len(self.published_names()),
            "hidden_tools": len(self.hidden_names()),
            "groups": {mod: {"count": len(names), "tools": names}
                       for mod, names in sorted(groups.items())},
            "hint": "Pass a query to eplan_tools_search to narrow this down; "
                    "summaries and parameters come from eplan_tools_describe.",
        }

    def describe(self, names):
        """Implementation of eplan_tools_describe."""
        if isinstance(names, str):
            requested = [names]
        elif isinstance(names, (list, tuple)):
            requested = list(names)
        elif names is None:
            requested = []
        else:
            return {"success": False,
                    "error": "names must be a string or a list of strings, got "
                             + type(names).__name__ + "."}

        if not requested:
            return {"success": False,
                    "error": "names is required: pass one tool name or a list of them."}

        found, missing = [], []
        for raw in requested[:_MAX_DESCRIBE]:
            resolved = self._alias(raw)
            if resolved is None:
                missing.append({"name": raw, "did_you_mean": self._near(raw)})
            else:
                found.append(self._info(resolved))

        result = {"success": bool(found), "tools": found, "not_found": missing}
        if len(requested) > _MAX_DESCRIBE:
            result["truncated"] = True
            result["note"] = ("Only the first %d names were described."
                              % _MAX_DESCRIBE)
        if not found:
            result["error"] = "None of the requested names are known tools."
        return result

    def call(self, name, arguments=None):
        """Implementation of eplan_tools_call."""
        resolved = self._alias(name)
        if resolved is None:
            return {
                "success": False,
                "error": "Unknown tool %r. Use eplan_tools_search to find the "
                         "right name." % (name,),
                "did_you_mean": self._near(name),
            }

        func = self.resolve(resolved)
        if func is None:
            return {"success": False,
                    "error": "Tool %r is indexed but its function is no longer "
                             "available." % (resolved,)}

        if arguments is None:
            arguments = {}
        elif isinstance(arguments, str):
            text = arguments.strip()
            if not text:
                arguments = {}
            else:
                try:
                    arguments = json.loads(text)
                except ValueError as e:
                    return {"success": False,
                            "error": "arguments must be an object; could not parse "
                                     "the string as JSON: %s" % (e,)}
        if not isinstance(arguments, dict):
            return {"success": False,
                    "error": "arguments must be an object mapping parameter names "
                             "to values, got " + type(arguments).__name__ + "."}

        sig = self._signature(resolved, func)

        if sig is not None:
            variadic = (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
            accepts_extra = any(p.kind is inspect.Parameter.VAR_KEYWORD
                                for p in sig.parameters.values())
            valid = [p.name for p in sig.parameters.values() if p.kind not in variadic]
            if not accepts_extra:
                unknown = [k for k in arguments if k not in valid]
                if unknown:
                    # Never silently drop a typo'd key - a dropped parameter
                    # becomes a wrong EPLAN action, not an obvious failure.
                    return {
                        "success": False,
                        "error": "Unknown argument(s) for %s: %s."
                                 % (resolved, ", ".join(sorted(unknown))),
                        "valid_parameters": valid,
                        "did_you_mean": {
                            k: difflib.get_close_matches(k, valid, n=3, cutoff=0.4)
                            for k in sorted(unknown)
                        },
                    }
            required = [p.name for p in sig.parameters.values()
                        if p.default is inspect.Parameter.empty and p.kind not in variadic]
            outstanding = [r for r in required if r not in arguments]
            if outstanding:
                return {
                    "success": False,
                    "error": "Missing required argument(s) for %s: %s."
                             % (resolved, ", ".join(outstanding)),
                    "valid_parameters": valid,
                    "required_parameters": required,
                }

        # Full mode reaches every tool through FastMCP's own
        # fn_metadata.call_fn_with_arg_validation, which validates AND coerces
        # arguments against the function's annotations (str "5" -> int 5, str
        # "true" -> bool True, etc). This call bypassed that and went straight
        # to func(**arguments), so discovery mode accepted argument TYPES full
        # mode would reject - silently producing a wrong EPLAN action instead
        # of a validation error. Audit #42 item 9.
        try:
            arguments = self._coerce_arguments(
                self._metadata(resolved, func), arguments)
        except _ArgValidationError as e:
            return {"success": False, "tool": resolved,
                    "error": "Argument validation failed: %s" % (e,)}

        try:
            result = func(**arguments)
        except Exception as e:  # mirrors server.py's tool wrapper
            return {"success": False, "tool": resolved, "error": str(e)}

        return _normalize_result(result)

    @staticmethod
    def _coerce_arguments(meta, arguments):
        """
        Validate and coerce *arguments* against the function's annotations the
        same way FastMCP's full-mode dispatch does, using the FuncMetadata
        model _metadata() built (None when no model applies).

        Returns the coerced dict. Raises _ArgValidationError on a real
        validation failure. Falls back to the raw arguments, unchanged,
        rather than blocking the call, for anything the fast path cannot
        usefully handle:

        - func_metadata cannot build a model at all (not introspectable), or
        - func takes a bare **kwargs tail (action_args/raw_args-style passthrough
          wrappers, an intentional pattern elsewhere in this codebase).
          func_metadata does not spread **kwargs's keys into flexible fields;
          it treats the parameter's own NAME as one required field, which
          would reject every legitimate call to this genuinely supported
          shape. See test_new_actions_offline.py's "raw_tails" set.
        """
        if meta is None:
            return arguments
        try:
            validated = meta.arg_model.model_validate(arguments)
        except ValidationError as e:
            raise _ArgValidationError(str(e)) from None
        except Exception:
            return arguments
        return validated.model_dump_one_level()


def _normalize_result(result):
    """
    Give back what the tool would have returned directly.

    Action wrappers return dicts; the handful of server-level tools return a
    JSON string (they are registered raw, not through the json.dumps wrapper),
    so decode those instead of handing back a quoted blob.
    """
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            decoded = json.loads(result)
        except ValueError:
            return {"success": True, "result": result}
        return decoded if isinstance(decoded, dict) else {"success": True, "result": decoded}
    return {"success": True, "result": _jsonable(result)}


def make_meta_tools(registry):
    """
    Build the three discovery meta-tools bound to `registry`.

    Returns {attr_name: function}. server.py registers them exactly like any
    other tool, so they pick up the same JSON wrapper and "eplan_" prefix. They
    are the only tools defined here that FastMCP ever sees, which is the whole
    point: three schemas instead of ~180.
    """

    def tools_search(query: str = None, limit: int = 30) -> dict:
        """Search the EPLAN tools that are not published as individual MCP tools.

        This server runs in discovery mode: only the connection core, the action
        catalog and these meta-tools are published, so the full tool list is not
        sent on every request. Everything else - ~180 typed wrappers for export,
        import, reports, parts, settings, macros, renumbering, etc. - is found
        here and invoked with eplan_tools_call.

        Returns compact records only (name, one-line summary, parameter names).
        Use eplan_tools_describe for the full documentation of a specific tool.

        Args:
            query: Words to look for in tool names and their documentation. All
                words must match (AND). Omit it to get a grouped overview of
                every available tool by source module.
            limit: Maximum number of records to return (default 30, max 200).
                The response always reports total_matches, the true number of
                matches, even when the returned list is truncated.
        """
        return registry.search(query=query, limit=limit)

    def tools_describe(names: Union[str, List[str]]) -> dict:
        """Full documentation and parameters for one or more hidden EPLAN tools.

        Use this after eplan_tools_search and before eplan_tools_call: it returns
        the complete docstring (including the EPLAN action name and what every
        argument means) plus the exact signature.

        Args:
            names: One tool name, or a list of them (max 25 per call). The
                "eplan_" prefix is optional. Unknown names come back under
                not_found with near-matches rather than failing the whole call.
        """
        return registry.describe(names)

    def tools_call(name: str, arguments: dict = None) -> dict:
        """Invoke an EPLAN tool that is not published as its own MCP tool.

        Returns exactly what the tool would have returned if it had been called
        directly. Argument names are validated against the tool's real signature
        first: an unrecognised key is refused with the list of valid parameter
        names instead of being silently dropped.

        Args:
            name: The tool to run, e.g. "eplan_export_pdf_project". The "eplan_"
                prefix is optional. Use eplan_tools_search to find it.
            arguments: Object mapping parameter names to values, e.g.
                {"project_path": "C:/P/Demo.elk", "export_path": "C:/out"}.
                Omit for a tool that takes no parameters.
        """
        return registry.call(name, arguments)

    return {
        "tools_search": tools_search,
        "tools_describe": tools_describe,
        "tools_call": tools_call,
    }
