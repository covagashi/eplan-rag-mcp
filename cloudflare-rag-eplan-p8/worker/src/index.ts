/**
 * EPLAN RAG MCP Server on Cloudflare Workers
 *
 * MCP protocol via Streamable HTTP (POST /mcp)
 * Plus REST endpoints for direct API access.
 *
 * MCP tools:
 *   - eplan_search: Semantic search over EPLAN docs
 *   - eplan_stats:  Index statistics
 *
 * REST endpoints:
 *   - GET  /health
 *   - POST /search
 *   - GET  /stats
 *   - POST /add-vectors
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

// Types for Vectorize results
interface VectorMatch {
  id: string;
  score: number;
  metadata?: Record<string, string>;
}

// --- Search pipeline (embed + vector query, edge-cached) ---

const EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5";
const MAX_TOP_K = 20;
// The index is a static docs corpus, so results for a given (query, topK, category)
// are stable. Bump SEARCH_CACHE_VERSION after re-indexing to invalidate old entries.
const SEARCH_CACHE_VERSION = 1;
const SEARCH_CACHE_TTL_SECONDS = 6 * 60 * 60;
const SEARCH_CACHE_ORIGIN = "https://eplan-rag-search-cache.internal";

function searchCacheKey(query: string, topK: number, category?: string): Request {
  const params = new URLSearchParams({
    v: String(SEARCH_CACHE_VERSION),
    q: query.trim().replace(/\s+/g, " ").toLowerCase(),
    k: String(topK),
    c: category ?? "",
  });
  return new Request(`${SEARCH_CACHE_ORIGIN}/search?${params}`, { method: "GET" });
}

async function searchDocs(
  env: Env,
  query: string,
  topK: number,
  category?: string
): Promise<VectorMatch[]> {
  const k = Math.min(topK, MAX_TOP_K);
  const cache = caches.default;
  const cacheKey = searchCacheKey(query, k, category);

  const cached = await cache.match(cacheKey);
  if (cached) {
    return (await cached.json()) as VectorMatch[];
  }

  const embResponse = await env.AI.run(EMBEDDING_MODEL, { text: [query] });
  const queryVector = embResponse.data[0];

  const vectorQuery: VectorizeQueryOptions = {
    topK: k,
    returnValues: false,
    returnMetadata: "all",
  };
  if (category) {
    vectorQuery.filter = { category: { $eq: category } };
  }

  const results = await env.VECTOR_INDEX.query(queryVector, vectorQuery);
  const matches: VectorMatch[] = results.matches.map((m: VectorizeMatch) => ({
    id: m.id,
    score: m.score,
    metadata: (m.metadata ?? {}) as Record<string, string>,
  }));

  await cache.put(
    cacheKey,
    new Response(JSON.stringify(matches), {
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": `public, max-age=${SEARCH_CACHE_TTL_SECONDS}`,
      },
    })
  );

  return matches;
}

function formatMatchesMarkdown(query: string, matches: VectorMatch[]): string {
  const formatted = matches
    .map((m, i) => {
      const meta = m.metadata ?? {};
      const lines = [
        `### ${i + 1}. ${meta.title || "Untitled"} (score: ${m.score.toFixed(4)})`,
      ];
      if (meta.category) lines.push(`**Category:** ${meta.category}`);
      if (meta.source_url) lines.push(`**Source:** ${meta.source_url}`);
      if (meta.header_path) lines.push(`**Section:** ${meta.header_path}`);
      if (meta.content) lines.push("", meta.content);
      return lines.join("\n");
    })
    .join("\n\n---\n\n");
  return `Found ${matches.length} results for "${query}":\n\n${formatted}`;
}

// --- MCP Server Setup ---

function createMcpServer(env: Env): McpServer {
  const server = new McpServer({
    name: "eplan-rag",
    version: "1.0.0",
  });

  // Tool: eplan_search
  server.tool(
    "eplan_search",
    "Search EPLAN documentation (API Reference, User Guide, hidden actions). " +
      "Use natural language queries. Returns relevant chunks with metadata.",
    {
      query: z.string().describe("Natural language search query about EPLAN"),
      topK: z
        .number()
        .min(1)
        .max(20)
        .default(5)
        .describe("Number of results to return"),
      category: z
        .enum(["API Reference", "User Guide", "Api"])
        .optional()
        .describe("Filter by doc category"),
    },
    async ({ query, topK, category }) => {
      const matches = await searchDocs(env, query, topK ?? 5, category);
      return {
        content: [
          {
            type: "text" as const,
            text: formatMatchesMarkdown(query, matches),
          },
        ],
      };
    }
  );

  // Tool: eplan_stats
  server.tool(
    "eplan_stats",
    "Get statistics about the EPLAN RAG index (vector count, dimensions, metric).",
    {},
    async () => {
      const info = await env.VECTOR_INDEX.describe();
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(
              {
                index: "eplan-knowledge-base",
                model: "BAAI/bge-base-en-v1.5",
                dimensions: 768,
                ...info,
              },
              null,
              2
            ),
          },
        ],
      };
    }
  );

  return server;
}

// --- Streamable HTTP MCP Transport (manual implementation) ---

async function handleMcpRequest(
  request: Request,
  env: Env,
): Promise<Response> {
  if (request.method === "GET") {
    // SSE endpoint for server-to-client notifications
    // For stateless server, just return the endpoint info
    return json({
      name: "eplan-rag",
      version: "1.0.0",
      description: "EPLAN RAG MCP Server - use POST /mcp for JSON-RPC requests",
    });
  }

  if (request.method !== "POST") {
    return json({ error: "Method not allowed" }, 405);
  }

  const body = await request.json() as {
    jsonrpc: string;
    id?: number | string;
    method: string;
    params?: Record<string, unknown>;
  };

  // Handle JSON-RPC methods manually for Cloudflare Workers compatibility
  const method = body.method;

  if (method === "initialize") {
    return json({
      jsonrpc: "2.0",
      id: body.id,
      result: {
        protocolVersion: "2024-11-05",
        capabilities: {
          tools: { listChanged: false },
        },
        serverInfo: {
          name: "eplan-rag",
          version: "1.0.0",
        },
      },
    });
  }

  if (method === "notifications/initialized") {
    return new Response(null, { status: 204 });
  }

  if (method === "ping") {
    return json({ jsonrpc: "2.0", id: body.id, result: {} });
  }

  if (method === "tools/list") {
    return json({
      jsonrpc: "2.0",
      id: body.id,
      result: {
        tools: [
          {
            name: "eplan_search",
            description:
              "Search EPLAN documentation (API Reference, User Guide, hidden actions). " +
              "Use natural language queries. Returns relevant chunks with metadata.",
            inputSchema: {
              type: "object",
              properties: {
                query: {
                  type: "string",
                  description: "Natural language search query about EPLAN",
                },
                topK: {
                  type: "number",
                  minimum: 1,
                  maximum: 20,
                  default: 5,
                  description: "Number of results to return",
                },
                category: {
                  type: "string",
                  enum: ["API Reference", "User Guide", "Api"],
                  description: "Filter by doc category",
                },
              },
              required: ["query"],
            },
          },
          {
            name: "eplan_stats",
            description:
              "Get statistics about the EPLAN RAG index (vector count, dimensions, metric).",
            inputSchema: {
              type: "object",
              properties: {},
            },
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
      if (toolName === "eplan_search") {
        const query = args.query as string;
        const topK = (args.topK as number) || 5;
        const category = args.category as string | undefined;

        if (!query) {
          return json({
            jsonrpc: "2.0",
            id: body.id,
            result: {
              content: [{ type: "text", text: "Error: query is required" }],
              isError: true,
            },
          });
        }

        const matches = await searchDocs(env, query, topK, category);

        return json({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            content: [
              {
                type: "text",
                text: formatMatchesMarkdown(query, matches),
              },
            ],
          },
        });
      }

      if (toolName === "eplan_stats") {
        const info = await env.VECTOR_INDEX.describe();
        return json({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            content: [
              {
                type: "text",
                text: JSON.stringify(
                  {
                    index: "eplan-knowledge-base",
                    model: "BAAI/bge-base-en-v1.5",
                    dimensions: 768,
                    ...info,
                  },
                  null,
                  2
                ),
              },
            ],
          },
        });
      }

      return json({
        jsonrpc: "2.0",
        id: body.id,
        error: { code: -32601, message: `Unknown tool: ${toolName}` },
      });
    } catch (err: any) {
      return json({
        jsonrpc: "2.0",
        id: body.id,
        result: {
          content: [{ type: "text", text: `Error: ${err.message}` }],
          isError: true,
        },
      });
    }
  }

  return json({
    jsonrpc: "2.0",
    id: body.id,
    error: { code: -32601, message: `Unknown method: ${method}` },
  });
}

// --- Worker fetch handler ---

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const url = new URL(request.url);
    const path = url.pathname;

    const corsHeaders: Record<string, string> = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS, DELETE",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // --- MCP endpoint (Streamable HTTP) ---
    if (path === "/mcp" || path === "/sse") {
      const response = await handleMcpRequest(request, env);
      // Add CORS headers to MCP responses
      const newHeaders = new Headers(response.headers);
      for (const [k, v] of Object.entries(corsHeaders)) {
        newHeaders.set(k, v);
      }
      return new Response(response.body, {
        status: response.status,
        headers: newHeaders,
      });
    }

    // --- REST endpoints ---
    try {
      if (path === "/health") {
        return json(
          { status: "ok", mcp: true, timestamp: new Date().toISOString() },
          200,
          corsHeaders
        );
      }

      if (path === "/stats") {
        return await handleStats(env, corsHeaders);
      }

      if (path === "/search" && request.method === "POST") {
        return await handleSearch(request, env, corsHeaders);
      }

      if (path === "/add-vectors" && request.method === "POST") {
        const authError = checkAuth(request, env);
        if (authError) return authError;
        return await handleAddVectors(request, env, corsHeaders);
      }

      return json(
        { error: "Not found", endpoints: ["/health", "/search", "/stats", "/mcp"] },
        404,
        corsHeaders
      );
    } catch (err: any) {
      return json({ error: err.message }, 500, corsHeaders);
    }
  },
} satisfies ExportedHandler<Env>;

// --- Auth ---

function checkAuth(request: Request, env: Env): Response | null {
  // Fail CLOSED. `if (!env.WORKER_API_KEY) return null` treated a missing secret
  // as "authorized", so a deploy that never set the secret - or one where it was
  // renamed, emptied or rotated away - left the write endpoint open to anyone,
  // with CORS `*` so a browser could reach it too. Anything written here is later
  // served to an MCP client and read by a model that holds local code-execution
  // tools, which makes an unauthenticated write a prompt-injection channel.
  if (!env.WORKER_API_KEY) {
    return json(
      { error: "Unauthorized: server is missing WORKER_API_KEY" },
      401
    );
  }
  const auth = request.headers.get("Authorization");
  if (timingSafeEqual(auth ?? "", `Bearer ${env.WORKER_API_KEY}`)) return null;
  return json(
    { error: "Unauthorized. Set Authorization: Bearer <WORKER_API_KEY>" },
    401
  );
}

/**
 * Constant-time string compare, so a caller cannot recover the token one byte
 * at a time by measuring how long `===` takes to reject it.
 */
function timingSafeEqual(a: string, b: string): boolean {
  const ab = new TextEncoder().encode(a);
  const bb = new TextEncoder().encode(b);
  // Length is not secret, but bail before the loop so we never index past an end.
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

// --- REST handlers ---

async function handleSearch(
  request: Request,
  env: Env,
  corsHeaders: Record<string, string>
) {
  const data = (await request.json()) as {
    query?: string;
    topK?: number;
    category?: string;
  };
  const { query, topK = 5, category } = data;

  if (!query) {
    return json({ error: "Missing 'query' field" }, 400, corsHeaders);
  }

  const matches = await searchDocs(env, query, topK, category);

  return json(
    {
      query,
      results: matches.map((m: VectorMatch) => {
        const meta = m.metadata ?? {};
        return {
          id: m.id,
          score: m.score,
          title: meta.title || "",
          category: meta.category || "",
          source: meta.source || "",
          source_url: meta.source_url || "",
          content: meta.content || "",
          header_path: meta.header_path || "",
        };
      }),
      count: matches.length,
    },
    200,
    corsHeaders
  );
}

async function handleAddVectors(
  request: Request,
  env: Env,
  corsHeaders: Record<string, string>
) {
  const data = (await request.json()) as {
    vectors?: Array<{ id: string; values: number[]; metadata?: object }>;
  };
  const vectors = data.vectors || [];

  if (vectors.length === 0) {
    return json({ error: "No vectors provided" }, 400, corsHeaders);
  }

  const BATCH = 1000;
  let inserted = 0;

  for (let i = 0; i < vectors.length; i += BATCH) {
    const batch = vectors.slice(i, i + BATCH).map((v) => ({
      id: v.id,
      values: v.values,
      metadata: v.metadata || {},
    }));
    await env.VECTOR_INDEX.insert(batch);
    inserted += batch.length;
  }

  return json({ success: true, inserted }, 200, corsHeaders);
}

async function handleStats(
  env: Env,
  corsHeaders: Record<string, string>
) {
  const info = await env.VECTOR_INDEX.describe();
  return json(
    {
      index: "eplan-knowledge-base",
      model: "BAAI/bge-base-en-v1.5",
      dimensions: 768,
      ...info,
    },
    200,
    corsHeaders
  );
}

function json(
  data: unknown,
  status = 200,
  extraHeaders: Record<string, string> = {}
) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });
}
