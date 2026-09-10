# Live expectations: execute_script / register_script and EPLAN's diagnostics

What EPLAN actually reports when a script handed to `ExecuteScript` fails to
compile, or one handed to `RegisterScript` has nothing to register. The offline
suite pins our parsing of those reports; this pins the reports themselves, which
are EPLAN's format and not ours — so a release that changes them, or a
differently localised install, breaks the fix while every offline test stays
green.

**Measured: 2026-09-09, EPLAN Electric P8 2025.0.3, remoting port 49152/49153,
Spanish UI.** All eight tests in `tests/test_execute_script_live.py` pass.

The run changed the implementation. `register_script` originally attributed
complaints by filename, the way `execute_script` does; the live test failed
because the registration refusal carries **no path at all**. That is the single
most valuable thing this file records, and no offline test could have found it —
the offline suite could only assert that we classify a block we ourselves
invented.

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

Eight tests; all skip with `no EPLAN answering the remote-control channel` when
none does. They write nothing into any project — both scripts are generated into
`tmp_path`, and the good one only writes a marker file back there — so no
scratch project is needed and whatever is open is safe.

Import `api.actions.scripts` directly, as the other live procedures do: a
connected MCP server is running the *installed* code, not your working tree.

## What was observed

| # | Call | Observed |
|---|---|---|
| 1 | `execute_script(good.cs)` | `success=true`, no `compile_errors`, marker file written ✅ |
| 2 | `execute_script(broken.cs)` | `success=false`, `errorType="McpScriptCompileError"` ✅ |
| 3 | same, `compile_errors` | 6-line block, header + 2×CS0105 + 2×CS0246 + footer ✅ |
| 4 | same, `message` | `Script did not compile: CS0246 … | CS0246 …` — CS0105 suppressed ✅ |
| 5 | fix `broken.cs`, run again | `success=true` — the previous run's errors are NOT re-attributed ✅ |
| 6 | `register_script(start_only.cs)` | `success=false`, `errorType="McpScriptRegisterFailed"`, **one line, no CS number, no path** ✅ |
| 7 | `register_script(broken.cs)` | `success=false`, `errorType="McpScriptCompileError"`, CS0246 present ✅ |
| 8 | `execute_raw_action("<name never declared>")` | `success=false`, action-not-found ✅ — but see the executor split below |
| 9 | `register_script(declare.cs)`, then call the declared action | `success=true`, the action now resolves, **and the marker file is written** ✅ |
| 10 | `unregister_script`, then call it again | action stops resolving, marker unchanged ✅ |
| 11 | `unregister_script(<never registered>)` and `(<no such file>)` | `success=true` both — a silent no-op ✅ |

### The exact strings (2025.0.3, Spanish UI)

Compile block — the file is named in **both** header and footer, which is what
makes filename attribution work for `execute_script`:

```
Error de compilador o advertencias de compilador en script <path> :
CS0105 (Fila:1, Columna:7): La directiva using para 'System' aparece previamente en este espacio de nombres
CS0105 (Fila:2, Columna:7): La directiva using para 'Eplan.EplApi.Scripting' aparece previamente en este espacio de nombres
CS0246 (Fila:9, Columna:9): No se puede encontrar el tipo o el nombre de espacio de nombres 'NoSuchTypeAnywhere' …
CS0246 (Fila:9, Columna:36): …
No se ha podido compilar el script <path>.
```

Registration refusal — **one line, level `Error`, and it names nothing**:

```
En el script no hay atributos disponibles para cargar.
```

Two consequences, both load-bearing:

1. **`register_script` cannot use filename attribution.** It diffs the tree
   across the call instead. This is exactly the bug the live run caught.
2. **The CS0105 pair confirms the suppression is needed, not theoretical.** Every
   script that declares `using System;` gets two of them, because EPLAN
   pre-imports `System`, `Eplan.EplApi.Base` and `Eplan.EplApi.Scripting`. Left
   unsuppressed they would lead every summary and bury the real CS0246.

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

- **Severity: both complaints are logged at `Error`.** Measured, not assumed —
  an earlier version read at `Warning` for `register_script` on the guess that
  the refusal might be a warning. It is not, so both tools now read at `Error`
  and nothing unrelated gets admitted into the diff.
  - If EPLAN ever logs either diagnostic as a warning, the Error-only read
    returns nothing and broken scripts silently report success again. Worth
    re-checking per release.
  - The before-snapshot and the after-read must always use the SAME level, or
    they describe different sets and the delta is fiction.

- **Both tools diff the tree; neither matches the filename.** That is the second
  thing the live run corrected. Filename matching is more precise in principle
  and was the original design for `execute_script`, but the tree read is
  **windowed** — `get_system_messages` keeps only the newest N — and a
  caller-supplied basename repeats across runs. Once enough entries accumulate,
  the window slides by exactly the number of entries a call adds, the
  count-based skip discards the real block, and the tool reports success for a
  broken script again.
  - Seen exactly that way: four live tests passed alone and failed in the full
    suite, after earlier runs had left their own `mcp_live_*.cs` diagnostics in
    the tree. A failure that appears only in a full run is the worst shape a
    test failure can take, and it was the tool being wrong, not the test.
  - The window is now 1000 (`scripted._DIAGNOSTIC_WINDOW`, was 200) and
    `new_messages_since` **aligns** a slid window on its overlap rather than
    giving up on it. It returns nothing only when no shift lines the two reads
    up at all, which means the tree was cleared or rewritten.
  - This all rests on EPLAN's actions being synchronous here: nothing else logs
    between the two reads, so whatever appeared belongs to the call in between.

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
file at Error level or above". Rows 6 and 7 prove the failures are caught. That
it is also proof the hooks are **live** is now measured rather than assumed —
row 9 registers a `[DeclareAction]`, calls the action, and reads the file the
action writes; row 10 shows the hook going away again with `unregister_script`.
Two things had to be true for that round trip to mean anything, and both were
measured rather than reasoned about:

- **A registration persists for the rest of the EPLAN session.** So the test's
  action name carries a uuid. With a fixed name, a run that died before its
  teardown would leave a live hook behind, and the next run's baseline call
  would fire the *previous* run's script — pointed at a `tmp_path` that no
  longer exists.
- **`UnregisterScript` on a path that was never registered is a silent no-op**
  (row 11), for a real-but-unregistered script and for a path with no file at
  all alike. That was an open question before; it is what lets teardown
  unregister unconditionally.

### The executor split, which row 8 only half tells

An action name EPLAN has never heard of is reported — `success=false`,
`errorType` `Eplan.EplApi.Base.BaseException`, `"No se ha podido encontrar la
acción 'X'. No está incluida en el conjunto de funciones."` — but only through
the tool surface. Every wrapper in `api/actions/` gets its manager from
`_base._get_connected_manager`, whose `QuietManagerWrapper` forces
`quiet_mode=True`; that routes through the generated script, which resolves via
`ActionManager.FindAction` and lets the exception propagate.

A bare `manager.execute_action(name)` takes the DIRECT path instead and returns

```json
{"success": true, "message": "Executed directly: ThisActionCertainlyDoesNotExist12345"}
```

for an action that does not exist. Measured both ways on 2026-09-09, same
session, same name.

This is the same silent-success hole as the one at the top of this file, and
`RegisterScript` / `ExecuteScript` / `UnregisterScript` are pinned to that
direct path *on purpose* — `quiet_mode` would have them recurse through a
generated script that itself needs ExecuteScript. Hence the message-tree diff:
it is not belt-and-braces over an executor that would have reported the failure
anyway, it is the only reporting those three can have.
