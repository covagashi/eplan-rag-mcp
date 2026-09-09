# Live expectations: execute_script / register_script and EPLAN's diagnostics

What EPLAN actually reports when a script handed to `ExecuteScript` fails to
compile, or one handed to `RegisterScript` has nothing to register. The offline
suite pins our parsing of those reports; this pins the reports themselves, which
are EPLAN's format and not ours — so a release that changes them, or a
differently localised install, breaks the fix while every offline test stays
green.

**Status: NOT YET MEASURED.** The automated half
(`tests/test_execute_script_live.py`) exists and skips itself until an EPLAN
answers. The observations below are what the run must fill in. Do not treat the
empty cells as passing.

## Why this needed fixing

`ExecuteScript` returns success in well under a second whether or not the C#
compiled. A compile failure reaches only EPLAN's own system-message tree; the
remote-control call never learns of it. Before the fix, `execute_script`
returned the underlying action result unchanged, so a script that never ran came
back as:

```json
{"success": true, "message": "Executed directly: ExecuteScript /ScriptFile:...\\probe_describe.cs"}
```

Observed 2026-09-09 on EPLAN 2025.0.3: a generated introspection script was run
twice, reported success both times, and wrote no result file at all. The first
run's real fault was a doubled backslash in a path inside a C# `@"..."` verbatim
literal; the second was invisible because EPLAN had stopped answering. Neither
was distinguishable from success at the call site.

`execute_custom_script` never had this hole, but only because it owns a
`{{RESULT_PATH}}` contract and can notice the file never appearing. An arbitrary
file has no such contract, so the fix reads the message tree directly.

## Reproduce

With EPLAN running and the remote channel up:

```bash
cd eplan-p8-mcp-server
python -m pytest tests/test_execute_script_live.py -v -rs
```

Four tests; all skip with `no EPLAN answering the remote-control channel` when
none does. They write nothing into any project — both scripts are generated into
`tmp_path`, and the good one only writes a marker file back there — so no
scratch project is needed and whatever is open is safe.

Import `api.actions.scripts` directly, as the other live procedures do: a
connected MCP server is running the *installed* code, not your working tree.

## What to record

**Measured:** _(date, EPLAN version, remoting port, UI language)_

| # | Call | Expected | Observed |
|---|---|---|---|
| 1 | `execute_script(good.cs)` | `success=true`, no `compile_errors`, marker file written | |
| 2 | `execute_script(broken.cs)` | `success=false`, `errorType="McpScriptCompileError"` | |
| 3 | same, inspect `compile_errors` | the block EPLAN logged, oldest first | |
| 4 | same, inspect `message` | starts `Script did not compile:`, carries the CS number | |
| 5 | fix `broken.cs`, run again | `success=true` — the previous run's errors are NOT re-attributed | |
| 6 | `register_script(start_only.cs)` | `success=false`, `errorType="McpScriptRegisterFailed"`, block names the file, **no CS number** | |
| 7 | `register_script(broken.cs)` | `success=false`, `errorType="McpScriptCompileError"`, CS0246 present | |

**Record the exact no-attributes text from row 6.** It is the one string in this
whole area nobody has captured verbatim, and two things depend on it: that EPLAN
names the script file in it (otherwise the complaint cannot be attributed and
`register_script` is silently blind again), and its severity — see below.

## Numbers and shapes a future change must not silently alter

- **The compile block's shape.** `_compile_errors_for` slices EPLAN's tree from
  the first entry mentioning the script's basename to the last. That works
  because EPLAN brackets the diagnostics with a header and a footer that both
  name the file, and the `CS####` lines sit between them. Record the exact
  observed block: if a release stops naming the file in the header or footer,
  the slice silently narrows or vanishes.
  - Header seen on 2025.0.3 (Spanish UI): `Error de compilador o advertencias de
    compilador en script <path> :`
  - Footer: `No se ha podido compilar el script <path>.`
  - **The header and footer are localised; the `CS####` codes are not.** Match
    on the filename and the CS number, never on the prose.

- **Severity, and why the two tools read the tree differently.**
  `execute_script` reads at `min_level="Error"`; `register_script` reads at
  `"Warning"`. That is not a style difference — the severity EPLAN assigns to
  the no-attributes complaint has **never been confirmed**, and reading only
  Errors would miss it entirely if it is logged as a Warning. The cost of the
  wider read is that an unrelated warning naming the same file would be
  reported too.
  - **When row 6 is measured, record the level** and narrow `register_script`
    to `"Error"` if that is what EPLAN uses. Until then the wide read is the
    safe direction: a false positive is visible and annoying, a false negative
    is the bug being fixed.
  - Likewise, if EPLAN ever logs *compile* diagnostics as warnings,
    `execute_script`'s Error-only read returns nothing and every broken script
    silently reports success again. Worth re-checking per release.
  - Whatever level a caller uses, `count_script_mentions` must use the same
    one: the snapshot and the read would otherwise count different sets and the
    skip would be meaningless.

- **Classification is structural, not textual.** `McpScriptCompileError` vs
  `McpScriptRegisterFailed` is decided by whether the block contains a line
  starting `CS`. Nothing matches EPLAN's prose, in any language. If a future
  release stops prefixing diagnostics with the CS code, both collapse into one
  and the distinction is lost silently — that is the thing to watch.

- **Attribution across runs.** A caller-supplied basename is reused; a generated
  `script_<uuid>.cs` is not. `execute_script` therefore snapshots
  `count_script_mentions()` before the run and passes it as `skip_matches`, so
  only entries this run added are reported. Test 5 is the one that proves it —
  without it, a script the user just fixed keeps being called broken forever,
  because EPLAN never clears its tree.
  - The snapshot reads at most 200 messages. On an EPLAN whose tree has grown
    past that between the two reads, the window slides and the count is wrong.
    Not seen in practice; record the tree size when measuring.

## What this does NOT cover

`success=true` from `execute_script` means "EPLAN compiled it and logged no
error against it". It does **not** mean the script did what you wanted: an
arbitrary file has no result contract, so one that compiles and then silently
does nothing still returns success. Test 1 asserts a marker file to catch that
case for its own script, which is as far as a generic tool can go — if you need
the outcome of your script, have it write a file and read that yourself.

For `register_script`, `success=True` means "EPLAN logged nothing against this
file at Warning level or above". It is **not** proof the hooks are live —
nothing here calls a `[DeclareAction]` to confirm one actually fires. Rows 6 and
7 prove the failures are caught; proving a *successful* registration really
registered something would need a script that declares an action, plus a call to
it, and is not attempted.

Still uncovered anywhere: whether an `UnregisterScript` of a path that was never
registered is a no-op or an error. `unregister_script` reports whatever the
action says and adds no diagnosis of its own.
