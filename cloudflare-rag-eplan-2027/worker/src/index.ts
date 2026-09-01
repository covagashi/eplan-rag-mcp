/**
 * EPLAN 2027 API Wiki -- keyword/full-text search on Cloudflare Workers + D1 (FTS5)
 *
 * Companion to rag2026.covaga.xyz (semantic/bge search over the 2026 docs),
 * NOT a replacement: this indexes the 2027 docs, and does exact/keyword
 * matching (SQLite FTS5 + bm25 ranking) instead of vector similarity.
 * Measured against each other on real queries: FTS5/grep-style search wins
 * for "what's the signature of X" / "what does property Y do" (the majority
 * of API-reference lookups); semantic search wins when the query uses none
 * of the source's vocabulary at all. Use both.
 *
 * MCP tools:
 *   - eplan2027_search: keyword search over the 2027 API wiki
 *   - eplan2027_get:    fetch one file's full content by path
 *   - eplan2027_stats:  row count / index info
 *
 * REST endpoints:
 *   - GET  /health
 *   - POST /search
 *   - GET  /file?path=...
 *   - GET  /stats
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

interface DocRow {
  path: string;
  title: string;
  kind: string;
  breadcrumb: string | null;
  source_url: string | null;
  snippet: string;
  score: number;
}

// Turn a free-text query into a forgiving FTS5 MATCH expression: split into
// word-like tokens (letters/digits/underscore -- covers identifiers like
// FUNC_TEXT and FindAction), each as a prefix match, joined with OR so
// partial/near-miss wording still returns ranked results instead of an FTS5
// syntax error on stray punctuation from a real signature like
// "FindAction(String)".
function buildMatchQuery(raw: string): string | null {
  const tokens = raw.match(/[\p{L}\p{N}_]+/gu) ?? [];
  if (tokens.length === 0) return null;
  return tokens.map((t) => `${t}*`).join(" OR ");
}

async function runSearch(db: D1Database, query: string, topK: number, kind?: string) {
  const matchQuery = buildMatchQuery(query);
  if (!matchQuery) return { rows: [] as DocRow[], matchQuery: "" };

  const kindFilter = kind ? "AND d.kind = ?3" : "";
  const sql = `
    SELECT
      d.path AS path,
      d.title AS title,
      d.kind AS kind,
      d.breadcrumb AS breadcrumb,
      d.source_url AS source_url,
      snippet(docs_fts, 2, '**', '**', ' ... ', 24) AS snippet,
      bm25(docs_fts) AS score
    FROM docs_fts
    JOIN docs d ON d.rowid = docs_fts.rowid
    WHERE docs_fts MATCH ?1
    ${kindFilter}
    ORDER BY score ASC
    LIMIT ?2
  `;
  const stmt = kind
    ? db.prepare(sql).bind(matchQuery, topK, kind)
    : db.prepare(sql).bind(matchQuery, topK);
  const { results } = await stmt.all<DocRow>();
  return { rows: results, matchQuery };
}

async function getFile(db: D1Database, path: string) {
  const row = await db
    .prepare("SELECT path, title, kind, breadcrumb, source_url, content FROM docs WHERE path = ?1")
    .bind(path)
    .first();
  return row;
}

// A handful of "...PropertyList" API classes (hundreds of constant fields,
// all documented on one page -- there's no per-member sub-page to bundle
// separately) run 100KB-1.3MB. eplan2027_get used to hand that back in one
// shot, which is a lot of a caller's context budget for what's usually one
// property lookup. Page it instead: same total content, just bounded per call.
const GET_PAGE_CHARS = 20_000;

function pageContent(content: string, offset: number): { text: string; note: string | null; nextOffset: number | null } {
  const total = content.length;
  if (total <= GET_PAGE_CHARS && offset === 0) {
    return { text: content, note: null, nextOffset: null };
  }
  const start = Math.min(Math.max(offset, 0), total);
  const end = Math.min(start + GET_PAGE_CHARS, total);
  const text = content.slice(start, end);
  const nextOffset = end < total ? end : null;
  const note = nextOffset === null
    ? `[end of file -- showing ${start}-${end} of ${total} chars]`
    : `[showing ${start}-${end} of ${total} chars -- call again with offset=${nextOffset} for more, ` +
      `or use eplan2027_search for a targeted lookup instead of reading the whole file]`;
  return { text, note, nextOffset };
}

// --- MCP Server Setup ---

function createMcpServer(env: Env): McpServer {
  const server = new McpServer({ name: "eplan-wiki-2027", version: "1.0.0" });

  server.tool(
    "eplan2027_search",
    "Keyword/full-text search over the EPLAN 2027 API documentation wiki " +
      "(API Reference: classes/interfaces/structs/enums with full member detail " +
      "bundled per type; User Guide: conceptual topics). Best for exact or " +
      "near-exact names (class/method/property names, error codes) -- for " +
      "genuinely vocabulary-free conceptual questions, rag2026.covaga.xyz's " +
      "semantic search may do better. Returns ranked hits with a snippet; " +
      "follow up with eplan2027_get(path) for the full file.",
    {
      query: z.string().describe("Search query -- exact identifiers work best (class/method/property names)"),
      topK: z.number().min(1).max(20).default(8).describe("Number of results to return"),
      kind: z.enum(["bundle", "standalone"]).optional().describe("Filter: 'bundle' = API reference types, 'standalone' = User Guide pages"),
    },
    async ({ query, topK, kind }) => {
      const { rows } = await runSearch(env.DB, query, topK ?? 8, kind);
      const text = formatResults(query, rows);
      return { content: [{ type: "text" as const, text }] };
    }
  );

  server.tool(
    "eplan2027_get",
    "Fetch the content of one file from the EPLAN 2027 API wiki by its path " +
      "(as returned by eplan2027_search). A few large '...PropertyList' classes " +
      "run past 100KB; those come back paginated (~20K chars/call) with an " +
      "offset to continue -- prefer eplan2027_search first if you only need one property.",
    {
      path: z.string().describe("File path as returned by eplan2027_search, e.g. 'API Reference/.../Action.md'"),
      offset: z.number().min(0).default(0).describe("Char offset to resume from, for paginated files (see the trailing note in a prior response)"),
    },
    async ({ path, offset }) => {
      const row = await getFile(env.DB, path);
      if (!row) {
        return { content: [{ type: "text" as const, text: `Not found: ${path}` }], isError: true };
      }
      const { text, note } = pageContent(row.content as string, offset ?? 0);
      return { content: [{ type: "text" as const, text: note ? `${text}\n\n${note}` : text }] };
    }
  );

  server.tool(
    "eplan2027_stats",
    "Get statistics about the EPLAN 2027 API wiki index (document count).",
    {},
    async () => {
      const info = await getStats(env.DB);
      return { content: [{ type: "text" as const, text: JSON.stringify(info, null, 2) }] };
    }
  );

  return server;
}

function formatResults(query: string, rows: DocRow[]): string {
  if (rows.length === 0) return `No results for "${query}".`;
  const formatted = rows
    .map((r, i) => {
      const lines = [`### ${i + 1}. ${r.title} (${r.kind})`];
      if (r.breadcrumb) lines.push(`**Path:** ${r.breadcrumb}`);
      lines.push(`**File:** ${r.path}`);
      lines.push("", r.snippet);
      return lines.join("\n");
    })
    .join("\n\n---\n\n");
  return `Found ${rows.length} results for "${query}":\n\n${formatted}`;
}

async function getStats(db: D1Database) {
  const row = await db.prepare("SELECT COUNT(*) AS n, SUM(size) AS total_bytes FROM docs").first();
  return {
    index: "eplan-wiki-2027",
    engine: "SQLite FTS5 (bm25)",
    documents: row?.n ?? 0,
    total_bytes: row?.total_bytes ?? 0,
  };
}

// --- Streamable HTTP MCP Transport (manual implementation, mirrors rag2026) ---

async function handleMcpRequest(request: Request, env: Env): Promise<Response> {
  if (request.method === "GET") {
    return json({
      name: "eplan-wiki-2027",
      version: "1.0.0",
      description: "EPLAN 2027 API Wiki keyword-search MCP server - use POST /mcp for JSON-RPC requests",
    });
  }
  if (request.method !== "POST") return json({ error: "Method not allowed" }, 405);

  const body = (await request.json()) as {
    jsonrpc: string;
    id?: number | string;
    method: string;
    params?: Record<string, unknown>;
  };
  const method = body.method;

  if (method === "initialize") {
    return json({
      jsonrpc: "2.0",
      id: body.id,
      result: {
        protocolVersion: "2024-11-05",
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "eplan-wiki-2027", version: "1.0.0" },
      },
    });
  }
  if (method === "notifications/initialized") return new Response(null, { status: 204 });
  if (method === "ping") return json({ jsonrpc: "2.0", id: body.id, result: {} });

  if (method === "tools/list") {
    return json({
      jsonrpc: "2.0",
      id: body.id,
      result: {
        tools: [
          {
            name: "eplan2027_search",
            description:
              "Keyword/full-text search over the EPLAN 2027 API documentation wiki. " +
              "Best for exact or near-exact names. Returns ranked hits with a snippet.",
            inputSchema: {
              type: "object",
              properties: {
                query: { type: "string", description: "Search query -- exact identifiers work best" },
                topK: { type: "number", minimum: 1, maximum: 20, default: 8 },
                kind: { type: "string", enum: ["bundle", "standalone"] },
              },
              required: ["query"],
            },
          },
          {
            name: "eplan2027_get",
            description:
              "Fetch the content of one file from the EPLAN 2027 API wiki by its path. " +
              "Large files (a few '...PropertyList' classes past 100KB) come back paginated " +
              "with an offset to continue.",
            inputSchema: {
              type: "object",
              properties: {
                path: { type: "string" },
                offset: { type: "number", minimum: 0, default: 0, description: "Char offset to resume from" },
              },
              required: ["path"],
            },
          },
          {
            name: "eplan2027_stats",
            description: "Get statistics about the EPLAN 2027 API wiki index.",
            inputSchema: { type: "object", properties: {} },
          },
        ],
      },
    });
  }

  if (method === "tools/call") {
    const params = body.params as { name: string; arguments?: Record<string, unknown> };
    const toolName = params?.name;
    const args = params?.arguments || {};
    try {
      if (toolName === "eplan2027_search") {
        const query = args.query as string;
        if (!query) {
          return json({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: "Error: query is required" }], isError: true } });
        }
        const topK = Math.min((args.topK as number) || 8, 20);
        const kind = args.kind as string | undefined;
        const { rows } = await runSearch(env.DB, query, topK, kind);
        return json({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: formatResults(query, rows) }] } });
      }
      if (toolName === "eplan2027_get") {
        const path = args.path as string;
        const row = await getFile(env.DB, path);
        if (!row) {
          return json({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: `Not found: ${path}` }], isError: true } });
        }
        const { text, note } = pageContent(row.content as string, (args.offset as number) ?? 0);
        return json({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: note ? `${text}\n\n${note}` : text }] } });
      }
      if (toolName === "eplan2027_stats") {
        const info = await getStats(env.DB);
        return json({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: JSON.stringify(info, null, 2) }] } });
      }
      return json({ jsonrpc: "2.0", id: body.id, error: { code: -32601, message: `Unknown tool: ${toolName}` } });
    } catch (err: any) {
      return json({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true } });
    }
  }

  return json({ jsonrpc: "2.0", id: body.id, error: { code: -32601, message: `Unknown method: ${method}` } });
}

// --- Worker fetch handler ---

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const url = new URL(request.url);
    const path = url.pathname;

    const corsHeaders: Record<string, string> = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

    if (path === "/mcp" || path === "/sse") {
      const response = await handleMcpRequest(request, env);
      const newHeaders = new Headers(response.headers);
      for (const [k, v] of Object.entries(corsHeaders)) newHeaders.set(k, v);
      return new Response(response.body, { status: response.status, headers: newHeaders });
    }

    try {
      if (path === "/health") {
        return json({ status: "ok", mcp: true, timestamp: new Date().toISOString() }, 200, corsHeaders);
      }
      if (path === "/stats") {
        return json(await getStats(env.DB), 200, corsHeaders);
      }
      if (path === "/search" && request.method === "POST") {
        const data = (await request.json()) as { query?: string; topK?: number; kind?: string };
        if (!data.query) return json({ error: "Missing 'query' field" }, 400, corsHeaders);
        const { rows, matchQuery } = await runSearch(env.DB, data.query, Math.min(data.topK || 8, 20), data.kind);
        return json({ query: data.query, matchQuery, results: rows, count: rows.length }, 200, corsHeaders);
      }
      if (path === "/file" && request.method === "GET") {
        const filePath = url.searchParams.get("path");
        if (!filePath) return json({ error: "Missing 'path' query param" }, 400, corsHeaders);
        const row = await getFile(env.DB, filePath);
        if (!row) return json({ error: "Not found" }, 404, corsHeaders);
        if (url.searchParams.get("full") === "1") return json(row, 200, corsHeaders); // opt out of paging, e.g. for bulk export
        const offset = Number(url.searchParams.get("offset") ?? "0") || 0;
        const { text, note, nextOffset } = pageContent(row.content as string, offset);
        return json({ ...row, content: text, truncated: note !== null, note, nextOffset }, 200, corsHeaders);
      }
      // One-time bulk-load endpoint (ingest.py). Bound parameters, not SQL
      // text -- avoids the SQLITE_TOOBIG limit `wrangler d1 execute --file`
      // hits on large inlined string literals. Remove after ingesting, or
      // leave it: it no-ops (401) once INGEST_TOKEN is unset/rotated.
      if (path === "/admin/ingest" && request.method === "POST") {
        if (!env.INGEST_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.INGEST_TOKEN}`) {
          return json({ error: "Unauthorized" }, 401, corsHeaders);
        }
        const data = (await request.json()) as {
          rows: Array<{ path: string; title: string; kind: string; breadcrumb: string; source_url: string; content: string; size: number }>;
        };
        const stmts = data.rows.map((r) =>
          env.DB.prepare(
            "INSERT OR REPLACE INTO docs (path, title, kind, breadcrumb, source_url, content, size) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)"
          ).bind(r.path, r.title, r.kind, r.breadcrumb, r.source_url, r.content, r.size)
        );
        await env.DB.batch(stmts);
        return json({ inserted: stmts.length }, 200, corsHeaders);
      }
      return json({ error: "Not found", endpoints: ["/health", "/search", "/file", "/stats", "/mcp"] }, 404, corsHeaders);
    } catch (err: any) {
      return json({ error: err.message }, 500, corsHeaders);
    }
  },
} satisfies ExportedHandler<Env>;

function json(data: unknown, status = 200, extraHeaders: Record<string, string> = {}) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json", ...extraHeaders } });
}
