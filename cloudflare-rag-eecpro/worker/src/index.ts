/**
 * EPLAN EEC Pro RAG MCP Server on Cloudflare Workers
 *
 * MCP protocol via Streamable HTTP (POST /mcp)
 * Plus REST endpoints for direct API access.
 *
 * MCP tools:
 *   - eecpro_search: Semantic search over EEC Pro docs
 *   - eecpro_stats:  Index statistics
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

// EEC Pro documentation categories (36 categories from docs_md/)
const EECPRO_CATEGORIES = [
  "admin", "concept", "eecbase", "eececad", "eecgraph2d",
  "eecjobserver", "eecoffice", "eecplc", "eecpropanel",
  "eecsap", "eecscripting", "eectext", "gui", "main",
  "refcommands", "refcsv", "refexcel", "refformui",
  "refformulas", "refimx", "refjobserver", "refquickrefguide",
  "refregex", "refscripting", "reftextgen", "refxml",
  "tutgraph2d", "tutimp", "tutjobs", "tutmechatronic",
  "tutorial", "tutp8", "tutpropanel", "tutstep7",
  "tuttext", "tutword",
] as const;

// --- MCP Server Setup ---

function createMcpServer(env: Env): McpServer {
  const server = new McpServer({
    name: "eecpro-rag",
    version: "1.0.0",
  });

  // Tool: eecpro_search
  server.tool(
    "eecpro_search",
    "Search EPLAN EEC Pro documentation (formulas, scripting, ECAD, PLC, JobServer, Graph2D, etc). " +
      "Use natural language queries. Returns relevant chunks with metadata.",
    {
      query: z.string().describe("Natural language search query about EPLAN EEC Pro"),
      topK: z
        .number()
        .min(1)
        .max(20)
        .default(5)
        .describe("Number of results to return"),
      category: z
        .string()
        .optional()
        .describe("Filter by doc category (e.g. refformulas, eececad, eecplc, eecscripting, admin, refcommands)"),
    },
    async ({ query, topK, category }) => {
      const embResponse = await env.AI.run("@cf/baai/bge-base-en-v1.5", {
        text: [query],
      });
      const queryVector = embResponse.data[0];

      const vectorQuery: VectorizeQueryOptions = {
        topK: topK ?? 5,
        returnValues: false,
        returnMetadata: "all",
      };

      if (category) {
        vectorQuery.filter = { category: { $eq: category } };
      }

      const results = await env.VECTOR_INDEX.query(queryVector, vectorQuery);

      const formatted = results.matches
        .map((m: VectorMatch, i: number) => {
          const meta = (m.metadata ?? {}) as Record<string, string>;
          const lines = [
            `### ${i + 1}. ${meta.title || "Untitled"} (score: ${m.score.toFixed(4)})`,
          ];
          if (meta.category) lines.push(`**Category:** ${meta.category}`);
          if (meta.source) lines.push(`**Source:** ${meta.source}`);
          if (meta.header_path)
            lines.push(`**Section:** ${meta.header_path}`);
          if (meta.content) lines.push("", meta.content);
          return lines.join("\n");
        })
        .join("\n\n---\n\n");

      return {
        content: [
          {
            type: "text" as const,
            text: `Found ${results.matches.length} results for "${query}":\n\n${formatted}`,
          },
        ],
      };
    }
  );

  // Tool: eecpro_stats
  server.tool(
    "eecpro_stats",
    "Get statistics about the EPLAN EEC Pro RAG index (vector count, dimensions, metric).",
    {},
    async () => {
      const info = await env.VECTOR_INDEX.describe();
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(
              {
                index: "eecpro-knowledge-base",
                product: "EPLAN EEC Pro 2026",
                model: "BAAI/bge-base-en-v1.5",
                dimensions: 768,
                categories: EECPRO_CATEGORIES.length,
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
    return json({
      name: "eecpro-rag",
      version: "1.0.0",
      description: "EPLAN EEC Pro RAG MCP Server - use POST /mcp for JSON-RPC requests",
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
          name: "eecpro-rag",
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
            name: "eecpro_search",
            description:
              "Search EPLAN EEC Pro documentation (formulas, scripting, ECAD, PLC, JobServer, Graph2D, etc). " +
              "Use natural language queries. Returns relevant chunks with metadata.",
            inputSchema: {
              type: "object",
              properties: {
                query: {
                  type: "string",
                  description: "Natural language search query about EPLAN EEC Pro",
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
                  description:
                    "Filter by doc category (e.g. refformulas, eececad, eecplc, eecscripting, admin, refcommands)",
                },
              },
              required: ["query"],
            },
          },
          {
            name: "eecpro_stats",
            description:
              "Get statistics about the EPLAN EEC Pro RAG index (vector count, dimensions, metric).",
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
      if (toolName === "eecpro_search") {
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

        const embResponse = await env.AI.run("@cf/baai/bge-base-en-v1.5", {
          text: [query],
        });
        const queryVector = embResponse.data[0];

        const vectorQuery: VectorizeQueryOptions = {
          topK: Math.min(topK, 20),
          returnValues: false,
          returnMetadata: "all",
        };

        if (category) {
          vectorQuery.filter = { category: { $eq: category } };
        }

        const results = await env.VECTOR_INDEX.query(queryVector, vectorQuery);

        const formatted = results.matches
          .map((m: VectorMatch, i: number) => {
            const meta = (m.metadata ?? {}) as Record<string, string>;
            const lines = [
              `### ${i + 1}. ${meta.title || "Untitled"} (score: ${m.score.toFixed(4)})`,
            ];
            if (meta.category) lines.push(`**Category:** ${meta.category}`);
            if (meta.source) lines.push(`**Source:** ${meta.source}`);
            if (meta.header_path) lines.push(`**Section:** ${meta.header_path}`);
            if (meta.content) lines.push("", meta.content);
            return lines.join("\n");
          })
          .join("\n\n---\n\n");

        return json({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            content: [
              {
                type: "text",
                text: `Found ${results.matches.length} results for "${query}":\n\n${formatted}`,
              },
            ],
          },
        });
      }

      if (toolName === "eecpro_stats") {
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
                    index: "eecpro-knowledge-base",
                    product: "EPLAN EEC Pro 2026",
                    model: "BAAI/bge-base-en-v1.5",
                    dimensions: 768,
                    categories: EECPRO_CATEGORIES.length,
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
          { status: "ok", product: "eecpro", mcp: true, timestamp: new Date().toISOString() },
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

  const embResponse = await env.AI.run("@cf/baai/bge-base-en-v1.5", {
    text: [query],
  });
  const queryVector = embResponse.data[0];

  const vectorQuery: VectorizeQueryOptions = {
    topK: Math.min(topK, 20),
    returnValues: false,
    returnMetadata: "all",
  };

  if (category) {
    vectorQuery.filter = { category: { $eq: category } };
  }

  const results = await env.VECTOR_INDEX.query(queryVector, vectorQuery);

  return json(
    {
      query,
      results: results.matches.map((m: VectorMatch) => {
        const meta = (m.metadata ?? {}) as Record<string, string>;
        return {
          id: m.id,
          score: m.score,
          title: meta.title || "",
          category: meta.category || "",
          source: meta.source || "",
          file: meta.file || "",
          content: meta.content || "",
          header_path: meta.header_path || "",
        };
      }),
      count: results.matches.length,
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
      index: "eecpro-knowledge-base",
      product: "EPLAN EEC Pro 2026",
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
