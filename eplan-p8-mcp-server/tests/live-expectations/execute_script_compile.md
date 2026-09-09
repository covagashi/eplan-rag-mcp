# Live expectations: execute_script and EPLAN's compile diagnostics

What EPLAN actually reports when a script handed to `ExecuteScript` fails to
compile. The offline suite pins our parsing of that report; this pins the report
itself, which is EPLAN's format and not ours — so a release that changes it, or
a differently localised install, breaks the fix while every offline test stays
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

- **Severity.** The tree is read at `min_level="Error"`. If EPLAN ever logs
  compile diagnostics as warnings, the read returns nothing and every broken
  script silently reports success again. Worth re-checking per release.

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

Also uncovered: `register_script` has the same structural blind spot
(EPLAN complains "The script does not contain attributes for loading" in its own
UI while the remote call returns success in ~0.45s — see the comment in
`scripted._execute_script`). Not fixed here; noted so it is not mistaken for
covered ground.
