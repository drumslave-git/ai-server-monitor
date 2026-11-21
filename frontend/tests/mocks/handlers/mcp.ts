// The calls of the MCP tools page. The answers do not change with the writes (the page loads again after each
// one and gets the same data); a test that needs the change shown keeps its own state with server.use(...).
import { http, HttpResponse, type RequestHandler } from "msw";
import { makeDomain, makeTool, mcpPage } from "../fixtures/mcp";

export const mcpHandlers: RequestHandler[] = [
  http.get("/api/mcp/tools", () => HttpResponse.json(mcpPage())),
  http.post("/api/mcp/tools/switch", () => HttpResponse.json(mcpPage())),
  http.patch("/api/mcp/tools/:server/:tool", () => HttpResponse.json(makeTool())),
  http.post("/api/mcp/domains", async ({ request }) => {
    const body = (await request.json()) as { name: string; description: string };
    return HttpResponse.json(makeDomain({ id: 9, name: body.name, description: body.description }), { status: 201 });
  }),
  http.put("/api/mcp/domains/:id", async ({ request, params }) => {
    const body = (await request.json()) as { name: string; description: string };
    return HttpResponse.json(makeDomain({ id: Number(params.id), name: body.name, description: body.description }));
  }),
  http.delete("/api/mcp/domains/:id", () => new HttpResponse(null, { status: 204 })),
  // no server signs in with OAuth unless a test says so with server.use(...)
  http.get("/api/mcp/oauth", () => HttpResponse.json([])),
  http.post("/api/mcp/oauth/:server/connect", () => HttpResponse.json({ authorize_url: "http://login.test/authorize?state=s1" })),
  http.delete("/api/mcp/oauth/:server", () => new HttpResponse(null, { status: 204 })),
  http.post("/api/mcp/sort", () => HttpResponse.json({ sorted: 2, error: null, unreachable: {} })),
];
