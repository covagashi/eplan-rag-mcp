---
name: eplan-development
description: Develop EPLAN Electric P8 scripts, API extensions, and remote-control applications. Use when writing C# scripts for EPLAN (actions, event handlers, ribbon), accessing the EPLAN API (parts database, projects, pages), building external apps that drive EPLAN via Remote Client, or debugging EPLAN automation issues (blocking, threading, dispose). Covers EPLAN 2022–2027.
---

# EPLAN Electric P8 Development

Comprehensive guide for developing with EPLAN Electric P8: scripting (C#), the EPLAN API, and remote automation. Distilled from working production code and curated examples.

## The three development models

| Model | Runs | License | Use for |
|---|---|---|---|
| **Scripting** | Inside EPLAN (compiled on load, C# subset) | None extra | Automating actions, UI additions (ribbon), event hooks, file exports |
| **API extension** | Inside EPLAN (compiled DLL) or offline app | API license | Deep data access: parts DB, project object model, pages, properties |
| **Remote Client** | External process (WPF/console) driving a running EPLAN | RemoteClient DLLs | Orchestration apps, headless build pipelines, Cogineer generation |

Note: scripts CAN use some API namespaces (e.g. `Eplan.EplApi.MasterData` for the parts database) directly from a `[Start]` method — see `references/api-data-access.md`.

## Reference files — read the one that matches the task

- **`references/script-basics.md`** — Script structure, entry-point attributes (`[Start]`, `[DeclareAction]`, `[DeclareEventHandler]`, `[DeclareRegister]`), deployment, scripting limitations.
- **`references/actions-reference.md`** — Executing actions with `CommandLineInterpreter` + `ActionCallingContext`; catalog of common actions (backup, export PDF, reports, labels, edit, selectionset…) with their parameters.
- **`references/core-classes.md`** — `Progress`, `Decider`, `PathMap` variables, `Settings`, `MultiLangString`, ribbon/context menus, `QuietModeStep`, system messages (`BaseException`, `SysMessagesCollection`).
- **`references/api-data-access.md`** — Parts database (`MDPartsManagement`), reading *and writing* part properties (`AddPart`/`RemovePart`, the `MDPropertyValue` constructor that doesn't exist, the `AmbiguousMatchException` every name-based property lookup hits), user-defined properties, multilanguage string parsing, resolving `$(MD_DOCUMENTS)`-style paths. Also: why `using Eplan.EplApi.DataModel;`/`...HEServices;` don't compile in scripts (CS0234) and how to reach that object model anyway. Also: `SymbolLibrary`/`Symbol` enumeration — why walking `Symbol(lib, int)` by index must `continue`, never `break`, on a construction miss (SymbolIds are sparse, not contiguous), and why a symbol's real identity comes from `Symbol.Properties.SYMB_DESC`/`FUNC_CATEGORY`, never from its short name or IEC-letter prefix.
- **`references/e3d-installation-spaces.md`** — The version-proof runtime-reflection recipe for reaching `Eplan.EplApi.DataModel`/`HEServices` from a script (`LockingStep`, `FindType()` assembly scanning, EPLAN 2027's `...Netu`-suffixed assemblies), applied to creating 3D installation spaces and inserting window macros headlessly.
- **`references/remoting.md`** — `EplanRemoteClient`: server discovery, dynamic ports, headless launch, version gotchas (2023 vs 2025), executing actions and scripts remotely, Cogineer generation from Excel.
- **`references/pitfalls.md`** — CRITICAL: the command-blocking issue (message loop / monitor thread), `using`/`Dispose` discipline, sequential execution model, error-handling rules, and the compile errors that masquerade as timeouts, including which C# each version's script engine actually accepts (#9).
- **`references/integration-patterns.md`** — Connecting EPLAN to the outside: HTTP servers, SignalR real-time messaging, forwarding EPLAN system errors to external services.

## When you don't know something: query the RAG

Two remote search services, indexing different doc releases with different
search modes — **always query one before guessing** action names, parameters,
or API signatures:

```bash
# rag2027: EPLAN 2027 docs, keyword/full-text search (SQLite FTS5 + bm25).
# Try this FIRST for anything with a real or guessable exact name -- an
# action name, a class/method/property, an error code. Measured head-to-head
# against rag2026 on real queries: FTS5 wins that case because it doesn't
# fragment a page into disconnected chunks the way the embedding index does.
curl -X POST https://rag2027.covaga.xyz/search \
  -H "Content-Type: application/json" \
  -d '{"query": "FindAction", "topK": 5}'

# rag2026: EPLAN 2026 docs, semantic search (Vectorize + bge-base, ~57k
# vectors). Use when the query shares no real vocabulary with the docs at
# all -- e.g. describing a UI behavior without knowing what it's called.
curl -X POST https://rag2026.covaga.xyz/search \
  -H "Content-Type: application/json" \
  -d '{"query": "export project to PDF parameters", "topK": 5}'
```

- `POST /search` — body `{"query": "...", "topK": N}` (public, no auth) — same shape on both
- `GET /stats` — index statistics; `GET /health` — health check — same shape on both

Use natural-language queries in English ("action to renumber devices", "PagePropertyList page type values"). Prefer several narrow queries over one broad one.

## Golden rules (violating these causes real production failures)

1. **EPLAN actions are pseudo-asynchronous.** After `oCLI.Execute(...)` in an external/long-running context, code can hang forever without an active message loop. See `references/pitfalls.md` before writing any multi-step automation.
2. **Dispose everything.** `ActionCallingContext`, `EplanRemoteClient`, temp clients — wrap in `using` or dispose in `finally`.
3. **Operations are sequential.** Each EPLAN action must fully finish before the next; never fire actions in parallel against one instance.
4. **Never use empty `catch {}`.** Log with `BaseException(msg, MessageLevel.X).FixMessage()` (inside EPLAN) or a logger (outside).
5. **EPLAN 2025 remoting requires "Remote Client Access"** enabled in EPLAN options; transport is gRPC (default port 49152, dynamic). In 2023 it was on by default.
6. **Target .NET Framework 4.8.1** for EPLAN 2025 API/RemoteClient work; reference DLLs from `C:\Program Files\EPLAN\Platform\<version>\Bin\`.
7. **Verify action names/parameters against the RAG** — many are undocumented and case-sensitive.
8. **Never `using Eplan.EplApi.DataModel;`/`...HEServices;` in a script** — that statement doesn't compile in EPLAN's script engine (CS0234, a fixed assembly set). Reach that object model via runtime reflection instead, and never hardcode the assembly name: it's `Eplan.EplApi.DataModelu`/`HEServicesu` through EPLAN ~2023, `...DataModelNetu`/`HEServicesNetu` on 2025/2027 — a hardcoded old name throws `BadImageFormatException` on 2027. See `references/e3d-installation-spaces.md`.
9. **Never `RegisterScript` a one-shot `[Start]` script.** That's for installing persistent hooks (`[DeclareAction]`/`[DeclareEventHandler]`/`[DeclareRegister]`); a `[Start]`-only script has none, so registering it first just produces a spurious EPLAN warning and wastes two remote-API round-trips. Call `ExecuteScript` alone. See `references/pitfalls.md` #10.
10. **Write generated scripts to the oldest C# your fleet needs — on 2026 that is C# 5: no `?.`, no `$"..."`, no `{ ["k"] = v }`, no `nameof`.** A compile error is *silent*: `ExecuteScript` returns success, the script never runs, and a caller waiting on its output just sees a timeout — so it gets misread as a hung or blocked EPLAN. **On any script timeout, read EPLAN's system-message tree for the `CS####` line before suspecting anything else.** (2027's engine accepts more than 2026's, so probe rather than assume either way.) See `references/pitfalls.md` #9.
