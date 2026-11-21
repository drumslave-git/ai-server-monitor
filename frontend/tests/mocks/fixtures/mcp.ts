// Invented data for the MCP tools page (GET /api/mcp/tools). Three servers (one off, one down), three
// domains (one without tools) and tools without a domain, so every state of the page has something to show.
import type { ApiResponse } from "../types";

export type McpPage = ApiResponse<"/api/mcp/tools">;
export type McpTool = McpPage["tools"][number];
export type McpDomain = McpPage["domains"][number];
export type McpServer = McpPage["servers"][number];

const nowSeconds = () => Math.floor(Date.now() / 1000);

export function makeServer(over: Partial<McpServer> = {}): McpServer {
  return { name: "test-main", enabled: true, error: null, listed_ts: nowSeconds() - 300, ...over };
}

export function makeDomain(over: Partial<McpDomain> = {}): McpDomain {
  return { id: 1, name: "monitoring", description: "Which hosts and services are up", set_by: "model", tools: 0, used_by: [], ...over };
}

export function makeTool(over: Partial<McpTool> = {}): McpTool {
  const server = over.server ?? "test-main";
  const tool = over.tool ?? "list_hosts";
  return {
    server,
    tool,
    name: `${server}_${tool}`,
    description: "List the monitored hosts",
    read_only: true,
    kind: "read",
    enabled: true,
    domain_id: 1,
    domain: "monitoring",
    set_by: "model",
    sort_error: null,
    seen_ts: nowSeconds() - 300,
    ...over,
  };
}

/** The whole page: 3 servers, 3 domains, 6 tools.
 *  On: list_hosts, restart_service, ping. Writes: restart_service, purge_cache, new_tool.
 *  Without a domain: ping, new_tool (both of test-down). */
export function mcpPage(over: Partial<McpPage> = {}): McpPage {
  const down = makeServer({ name: "test-down", error: "connection refused", listed_ts: nowSeconds() - 7200 });
  return {
    sorting: false,
    servers: [makeServer(), makeServer({ name: "test-extra", enabled: false, listed_ts: null }), down],
    domains: [
      makeDomain({
        id: 1,
        name: "monitoring",
        description: "Which hosts and services are up",
        tools: 2,
        used_by: [
          { slug: "example-job", name: "Example job" },
          { slug: "other-job", name: "Other job" },
        ],
      }),
      makeDomain({ id: 2, name: "storage", description: "Disks and caches", tools: 2, set_by: "owner" }),
      makeDomain({ id: 3, name: "idle", description: "Reserved for later", tools: 0 }),
    ],
    tools: [
      makeTool({ tool: "list_hosts", description: "List the monitored hosts" }),
      makeTool({ tool: "restart_service", description: "Restart one service on a host", read_only: false, kind: "write", set_by: "owner" }),
      makeTool({ tool: "read_disk", description: "Read the disk usage of a host", enabled: false, domain_id: 2, domain: "storage" }),
      makeTool({ server: "test-extra", tool: "purge_cache", description: "Delete the cached files", read_only: false, kind: "write", enabled: false, domain_id: 2, domain: "storage" }),
      makeTool({ server: "test-down", tool: "ping", description: "Ping a host", domain_id: null, domain: null, sort_error: "The model did not name a domain" }),
      makeTool({ server: "test-down", tool: "new_tool", description: "A tool that was just listed", read_only: false, kind: "write", enabled: false, domain_id: null, domain: null }),
    ],
    ...over,
  };
}
