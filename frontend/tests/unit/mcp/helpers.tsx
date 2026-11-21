import { render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, vi } from "vitest";
import { McpToolsPage } from "../../../src/pages/McpTools";
import { type McpPage, mcpPage } from "../../mocks/fixtures/mcp";
import { server } from "../../mocks/server";
import { installDialogBehaviour } from "../kit/part1-support";
import { toolsAnswered, watchGets, type GetSpy } from "../tools-answer";

export const CLOSED_KEY = "asm.mcp-tools.collapsed";

let getSpy: GetSpy; // calls through: only to see when the page has been answered

/** Every test starts with an empty browser storage and a <dialog> that behaves like a browser's. */
export function setupBrowser() {
  let dialogs: ReturnType<typeof installDialogBehaviour>;
  beforeEach(() => {
    getSpy = watchGets();
    localStorage.clear();
    dialogs = installDialogBehaviour();
  });
  afterEach(() => {
    dialogs.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
}

export type Seen = { method: string; path: string; body: unknown };

/** The page is answered from `current.page` (change it to change what the next load shows); every request the
 *  page sends is recorded in `seen` (method, raw path, JSON body). The write calls answer with success. */
export function mockApi(initial: McpPage = mcpPage()) {
  const state = { page: initial, seen: [] as Seen[], loads: 0 };
  const record = async (request: Request) => {
    const url = new URL(request.url);
    const text = await request.text();
    const body = text ? JSON.parse(text) : undefined;
    state.seen.push({ method: request.method, path: url.pathname, body });
    return body;
  };
  server.use(
    http.get("/api/mcp/tools", () => {
      state.loads += 1;
      return HttpResponse.json(state.page);
    }),
    http.post("/api/mcp/tools/switch", async ({ request }) => {
      await record(request);
      return HttpResponse.json(state.page);
    }),
    http.patch("/api/mcp/tools/*", async ({ request }) => {
      await record(request);
      return HttpResponse.json(state.page.tools[0]);
    }),
    http.post("/api/mcp/domains", async ({ request }) => {
      const body = (await record(request)) as { name: string; description: string };
      return HttpResponse.json({ id: 9, name: body.name, description: body.description, set_by: "owner", tools: 0, used_by: [] }, { status: 201 });
    }),
    http.put("/api/mcp/domains/:id", async ({ request, params }) => {
      const body = (await record(request)) as { name: string; description: string };
      return HttpResponse.json({ id: Number(params.id), name: body.name, description: body.description, set_by: "owner", tools: 0, used_by: [] });
    }),
    http.delete("/api/mcp/domains/:id", async ({ request }) => {
      await record(request);
      return new HttpResponse(null, { status: 204 });
    }),
    http.post("/api/mcp/sort", async ({ request }) => {
      await record(request);
      return HttpResponse.json({ sorted: 2, error: null, unreachable: {} });
    }),
  );
  return state;
}

/** The page, rendered and loaded (its heading and the tool list are there). */
export async function renderLoaded() {
  const view = render(<McpToolsPage />);
  await screen.findByRole("switch", { name: /test-main_list_hosts on/ });
  return view;
}

/** A panel (a domain's, or "Not sorted") by its title. */
export function panel(title: string) {
  const section = screen.getByRole("heading", { level: 2, name: title }).closest("section");
  if (!section) throw new Error(`no panel ${title}`);
  return within(section);
}

/** The table row of a tool, by the name the model sees (server_tool). */
export function row(name: string) {
  const tr = screen.getByRole("switch", { name: `${name} on` }).closest("tr");
  if (!tr) throw new Error(`no row ${name}`);
  return within(tr);
}

/** The domains list on the left. */
export const domainList = () => within(screen.getByRole("navigation", { name: "Domains" }));

/** The (method, path, body) of what the page sent, without the GETs. */
export const writes = (seen: Seen[]) => seen.map((s) => `${s.method} ${s.path}`);

/** Waits for the `n`-th load of the page: the request is answered and the page has taken the answer. */
export const waitForLoads = async (state: { loads: number }, n: number) => {
  await waitFor(() => expect(state.loads).toBe(n));
  await toolsAnswered(getSpy, n);
};
