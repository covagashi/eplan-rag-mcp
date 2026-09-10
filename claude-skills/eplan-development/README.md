# eplan-development — Claude Code skill

*Part of the [EPLAN AI Automation Toolkit](../../README.md).*

> **This directory is a mirror.** The canonical copy lives in its own repository:
> **[covagashi/eplan-development-skill](https://github.com/covagashi/eplan-development-skill)**.
> Edit it there; changes are copied here, not the other way round.

A Claude Code **skill** for developing with EPLAN Electric P8: C# scripting, the EPLAN API
(parts database, properties, 3D installation spaces), and Remote Client automation
(headless EPLAN, Cogineer). Distilled from working production code and from live testing,
with the pitfalls that actually bite — command blocking, the message loop, dispose
discipline, the EPLAN 2025 remoting changes, and compile errors that masquerade as hangs.

The skill is deliberately **host-agnostic**: it assumes no MCP server, no particular script
runner and no particular documentation index. It is useful on its own, whether or not you
run anything else from this repository.

Once installed, Claude loads it automatically whenever you ask it to write EPLAN scripts,
call EPLAN actions, access the parts database, or build apps that drive EPLAN remotely.

## Contents

```
eplan-development/                    # plugin root
├── .claude-plugin/plugin.json
└── skills/eplan-development/
    ├── SKILL.md                      # entry point: dev models, lookup order, golden rules
    └── references/
        ├── script-basics.md          # script structure, entry-point attributes, deployment
        ├── actions-reference.md      # CommandLineInterpreter + verified action catalog
        ├── core-classes.md           # Progress, PathMap, Settings, MultiLangString, ribbon, context menus
        ├── api-data-access.md        # parts DB (MDPartsManagement), properties, symbol enumeration
        ├── e3d-installation-spaces.md# reaching DataModel/HEServices by reflection; 3D spaces
        ├── eec-typicals.md           # generating a project from an EEC One typical workbook
        ├── remoting.md               # EplanRemoteClient, dynamic ports, headless, Cogineer
        ├── pitfalls.md               # blocking, threading, dispose, silent compile failures
        └── integration-patterns.md   # HTTP, SignalR, error forwarding
```

## Install

### As a plugin (recommended)

From the standalone repository:

```
/plugin marketplace add covagashi/eplan-development-skill
/plugin install eplan-development@eplan-skills
```

Or from this repository, which is also a plugin marketplace:

```
/plugin marketplace add covagashi/eplan-rag-mcp
/plugin install eplan-development@eplan-tools
```

Update later with `/plugin marketplace update eplan-skills` (or `eplan-tools`).

### Manually (copy the skill folder)

```bash
git clone https://github.com/covagashi/eplan-development-skill
```

```powershell
# Windows — personal skill, available in every project
xcopy /E /I eplan-development-skill\skills\eplan-development "$env:USERPROFILE\.claude\skills\eplan-development"
```

```bash
# macOS / Linux
cp -r eplan-development-skill/skills/eplan-development ~/.claude/skills/eplan-development
```

For a single project, copy to `<your-project>/.claude/skills/eplan-development` instead.

Restart Claude Code, or start a new session. The skill activates automatically on
EPLAN-related tasks, or you can invoke it explicitly with `/eplan-development`.

## Pairs well with

- **[`eplan-p8-mcp-server/`](../../eplan-p8-mcp-server/)** — lets Claude *execute* EPLAN
  actions live; this skill teaches it to write correct code and avoid the traps.
- **[`cloudflare-rag-eplan-2027/`](../../cloudflare-rag-eplan-2027/)** — keyword/FTS5 search
  over the 2027 EPLAN P8 docs (`POST https://rag2027.covaga.xyz/search`). The skill tells
  Claude to try an exact-name lookup here first.
- **[`cloudflare-rag-eplan-p8/`](../../cloudflare-rag-eplan-p8/)** — semantic search over the
  2026 docs (`POST https://rag2026.covaga.xyz/search`), for queries that share no vocabulary
  with the documentation.
- **[eplan-ctxmenu-kit](https://github.com/covagashi/eplan-ctxmenu-kit)** — a worked example
  of the context-menu material in `core-classes.md`.

## Coverage

EPLAN Electric P8 **2022–2027**, with version-specific notes where they matter: the ribbon
API since 2022, remoting on by default in 2023 versus "Remote Client Access" + gRPC in 2025,
the `...Netu`-suffixed managed assemblies in 2027, and .NET Framework 4.8.1 targeting.
