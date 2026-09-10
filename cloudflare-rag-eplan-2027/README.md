# EPLAN 2027 API Wiki — Cloudflare MCP Server (Keyword Search)

*Part of the [EPLAN AI Automation Toolkit](../README.md).*

Remote MCP server that lets Claude search the **EPLAN 2027 API documentation** through
full-text/keyword search (SQLite FTS5 + bm25), over the wiki produced by
[`eplan_wiki_scraper.py`](https://github.com/covagashi/scrapping_eplan/blob/main/eplan_wiki_scraper.py)
(classes/interfaces/structs/enums bundled per type with every member inlined —
see that script's docstring for why).

> **Companion to [`cloudflare-rag-eplan-p8/`](../cloudflare-rag-eplan-p8/), not a replacement.**
> That one does semantic search (Vectorize + bge-base) over the **2026** docs. This one does
> keyword/exact-match search (FTS5) over the **2027** docs. Measured against each other on
> real queries: FTS5 wins for "what's the signature of X" / "what does property Y do" (most
> API-reference lookups); semantic search wins when the query uses none of the source's
> vocabulary at all. Use both — they fail differently.

## Architecture

```
Claude Code  -->  MCP (Streamable HTTP)  -->  Cloudflare Worker (eplan-wiki-2027)
                                                   |
                                              D1 (SQLite + FTS5, bm25 ranking)
                                              1,754 documents / ~36 MB
```

No embeddings, no Vectorize, no Workers AI call per query — a `docs_fts MATCH` query
against D1 is the entire search path.

## Install the MCP in Claude Code

```bash
claude mcp add eplan-wiki-2027 -- cmd /c npx mcp-remote https://rag2027.covaga.xyz/mcp
claude mcp list   # should list "eplan-wiki-2027"
```

## Available Tools

| Tool | Description |
|------|-------------|
| `eplan2027_search` | Keyword/full-text search. Best for exact or near-exact names. |
| `eplan2027_get` | Fetch one file's full content by path (from a search result). |
| `eplan2027_stats` | Document count / index info. |

## REST Endpoints

Base URL: `https://rag2027.covaga.xyz`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET  | `/health` | Health check |
| POST | `/search` | Keyword search (body: `{"query": "...", "topK": 8, "kind": "bundle"\|"standalone"}`) |
| GET  | `/file?path=...` | Full content of one file by path |
| GET  | `/stats` | Document count |

```bash
curl -X POST https://rag2027.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "FindAction", "topK": 5}'
```

## Re-ingesting after a wiki re-scrape

The D1 schema (`schema.sql`) defines `docs` + an external-content FTS5 table
(`docs_fts`, synced via triggers — every `INSERT INTO docs` auto-populates it).

```bash
# 1. Apply schema (idempotent — DROP+CREATE)
cd worker && npx wrangler d1 execute eplan-wiki-2027 --remote --file=../schema.sql

# 2. Re-add the temporary bulk-load endpoint's secret (see worker/src/index.ts,
#    POST /admin/ingest -- bound parameters, avoids the SQLITE_TOOBIG limit
#    `wrangler d1 execute --file` hits on large inlined SQL string literals)
npx wrangler secret put INGEST_TOKEN   # paste a random token
npx wrangler deploy

# 3. Ingest
cd .. && python ingest.py --url https://rag2027.covaga.xyz --token <the token>

# 4. Remove the secret again -- the endpoint no-ops (401) without it
cd worker && npx wrangler secret delete INGEST_TOKEN
```

## Deployment notes

- D1 database: `eplan-wiki-2027` (`632a6de2-d0f2-498c-921e-cb1e77b040aa`, region WEUR).
- Custom domain `rag2027.covaga.xyz` was attached via the account-level Workers
  Custom Domains API (`PUT /accounts/{id}/workers/domains`) rather than
  `wrangler deploy`'s automatic route setup — the deploying API token had
  `#d1:edit`/`#worker:edit` but not the zone-scoped Workers Routes permission
  `wrangler` tries first. If re-deploying hits the same
  `A request to the Cloudflare API (/zones/.../workers/routes) failed` error,
  either add that zone permission to the token or call the Custom Domains
  endpoint directly instead of relying on the `routes` block in
  `wrangler.jsonc`.
- This machine sits behind a Zscaler TLS-inspecting proxy; Node's own CA
  bundle doesn't include Zscaler's root, so `wrangler`/`npm` need
  `NODE_EXTRA_CA_CERTS` pointed at an exported copy of it or every HTTPS call
  fails with `UNABLE_TO_GET_ISSUER_CERT_LOCALLY` (surfaces from `wrangler` as
  a generic `fetch failed`, including during `wrangler login`'s OAuth code
  exchange).
