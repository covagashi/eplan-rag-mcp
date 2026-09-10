# eplan-development — Claude Code Skill

> **This is a mirror.** The canonical, host-agnostic copy lives in its own repo:
> **[covagashi/eplan-development-skill](https://github.com/covagashi/eplan-development-skill)**
> — install it with `/plugin marketplace add covagashi/eplan-development-skill`
> and `/plugin install eplan-development@eplan-skills`. Use that one if you don't
> run the MCP server; it assumes no runner and no particular docs index.

A Claude Code **skill** for developing with EPLAN Electric P8: C# scripting, the EPLAN API (parts database, properties), and Remote Client automation (including headless EPLAN and Cogineer). Distilled from working production code, with the pitfalls that actually bite (command blocking, message loop, dispose discipline, EPLAN 2025 remoting changes).

When installed, Claude automatically loads this knowledge whenever you ask it to write EPLAN scripts, call EPLAN actions, access the parts database, or build apps that drive EPLAN remotely. It also instructs Claude to verify action names and parameters against the [EPLAN P8 docs RAG](../../cloudflare-rag-eplan-p8/) instead of guessing.

## Contents

```
eplan-development/                    # plugin root
├── .claude-plugin/plugin.json
└── skills/eplan-development/
    ├── SKILL.md                      # Entry point: dev models, golden rules, RAG usage
    └── references/
        ├── script-basics.md          # Script structure, [Start]/[DeclareAction]/events, deployment
        ├── actions-reference.md      # CommandLineInterpreter + verified action catalog
        ├── core-classes.md           # Progress, PathMap, Settings, MultiLangString, ribbon
        ├── api-data-access.md        # Parts DB (MDPartsManagement), properties, multilang parsing
        ├── remoting.md               # EplanRemoteClient, dynamic ports, headless, Cogineer
        ├── pitfalls.md               # Blocking issue, threading, dispose, error handling
        └── integration-patterns.md   # HTTP, SignalR, error forwarding, HTML tools
```

## Install

### As a plugin (recommended — one-time setup, easy updates)

Inside Claude Code:

```
/plugin marketplace add covagashi/eplan-rag-mcp
/plugin install eplan-development@eplan-tools
```

Update later with `/plugin marketplace update eplan-tools`.

### Manual (copy the skill folder)

```bash
git clone https://github.com/covagashi/eplan-rag-mcp
# Windows — personal skill, available in all projects
xcopy /E /I eplan-rag-mcp\claude-skills\eplan-development\skills\eplan-development %USERPROFILE%\.claude\skills\eplan-development
# macOS / Linux
cp -r eplan-rag-mcp/claude-skills/eplan-development/skills/eplan-development ~/.claude/skills/eplan-development
```

For a single project, copy to `<your-project>/.claude/skills/eplan-development` instead.

Restart Claude Code (or start a new session). The skill activates automatically on EPLAN-related tasks, or invoke it explicitly with `/eplan-development`.

## Pairs well with

- **[eplan-p8-mcp-server](../../eplan-p8-mcp-server/)** — lets Claude *execute* EPLAN actions live; this skill teaches it to write correct code and avoid the traps.
- **[cloudflare-rag-eplan-p8](../../cloudflare-rag-eplan-p8/)** — semantic search over the 2026 EPLAN P8 docs (`POST https://rag2026.covaga.xyz/search`).
- **[cloudflare-rag-eplan-2027](../../cloudflare-rag-eplan-2027/)** — keyword/FTS5 search over the 2027 EPLAN P8 docs (`POST https://rag2027.covaga.xyz/search`). The skill tells Claude to consult this one first for exact action/parameter names, and the 2026 one when the query shares no vocabulary with the docs at all.

## Coverage

EPLAN Electric P8 2022–2025. Version-specific notes included (ribbon API since 2022, remoting default-on in 2023 vs "Remote Client Access" + gRPC in 2025, .NET Framework 4.8.1 targeting).
