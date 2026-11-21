import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { McpToolsPage } from "../../../src/pages/McpTools";
import { makeDomain, makeServer, makeTool, mcpPage } from "../../mocks/fixtures/mcp";
import { server } from "../../mocks/server";
import { CLOSED_KEY, domainList, mockApi, panel, renderLoaded, row, setupBrowser, waitForLoads } from "./helpers";

setupBrowser();

describe("McpToolsPage: loading and errors", () => {
  it("says Loading… under the title until the tools arrive", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.get("/api/mcp/tools", async () => {
        await gate;
        return HttpResponse.json(mcpPage());
      }),
    );
    render(<McpToolsPage />);
    expect(screen.getByRole("heading", { level: 1, name: "MCP tools" })).toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    release();
    expect(await screen.findByRole("switch", { name: "test-main_list_hosts on" })).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("shows the server's message when the tools cannot be loaded", async () => {
    server.use(http.get("/api/mcp/tools", () => HttpResponse.json({ detail: "The database is locked" }, { status: 500 })));
    render(<McpToolsPage />);
    expect(await screen.findByText("The database is locked")).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("falls back to the HTTP status when the error has no message", async () => {
    server.use(http.get("/api/mcp/tools", () => new HttpResponse(null, { status: 503 })));
    render(<McpToolsPage />);
    expect(await screen.findByText("HTTP 503")).toBeInTheDocument();
  });

  it("says no server is configured, with no list, no filters and no way to sort", async () => {
    mockApi(mcpPage({ servers: [], domains: [], tools: [] }));
    render(<McpToolsPage />);
    expect(await screen.findByText("No MCP server is configured.")).toBeInTheDocument();
    expect(screen.getByText("Add one under Settings → MCP servers.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "sort into domains…" })).toBeDisabled();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Domains" })).not.toBeInTheDocument();
  });
});

describe("McpToolsPage: summary", () => {
  it("counts the tools, servers, domains, tools on and write tools", async () => {
    mockApi();
    await renderLoaded();
    const counters = screen.getByText("tools on 3 servers").closest(".fd-counters");
    expect(counters).toHaveTextContent("6tools on 3 servers3domains3on3write");
  });

  it("says tool, server and domain in the singular for one of each", async () => {
    mockApi(
      mcpPage({
        servers: [makeServer()],
        domains: [makeDomain({ tools: 1 })],
        tools: [makeTool()],
      }),
    );
    await renderLoaded();
    const counters = screen.getByText("tool on 1 server").closest(".fd-counters");
    expect(counters).toHaveTextContent("1tool on 1 server1domain1on0write");
  });

  it("describes the strip of cells: read and on, write and on, off", async () => {
    mockApi();
    await renderLoaded();
    const strip = screen.getByRole("img", { name: "6 tools: 2 read and on, 1 write and on, 3 off" });
    const cells = [...strip.children].map((c) => c.className);
    expect(cells).toEqual(["fd-cell fd-cell-ok", "fd-cell fd-cell-ok", "fd-cell fd-cell-warn", "fd-cell", "fd-cell", "fd-cell"]);
  });

  it("names a server that could not be listed, when, and why; its tools stay", async () => {
    mockApi();
    await renderLoaded();
    const note = screen.getByText(/could not be listed/).closest('[role="status"]');
    expect(note).toHaveTextContent("Server test-down could not be listed 2h ago: connection refused. Its tools are shown as last listed.");
    expect(screen.getByRole("switch", { name: "test-down_ping on" })).toBeInTheDocument();
  });

  it("leaves out the time of a server that was never listed", async () => {
    mockApi(mcpPage({ servers: [makeServer({ name: "test-down", error: "no route", listed_ts: null })] }));
    await renderLoaded();
    expect(screen.getByText(/could not be listed/).closest('[role="status"]')).toHaveTextContent("Server test-down could not be listed: no route.");
  });

  it("shows no notice when every server was listed", async () => {
    mockApi(mcpPage({ servers: [makeServer()] }));
    await renderLoaded();
    expect(screen.queryByText(/could not be listed/)).not.toBeInTheDocument();
  });
});

describe("McpToolsPage: domains and tool rows", () => {
  it("lists the tools without a domain first, then the domains, with how many tools are on", async () => {
    mockApi();
    await renderLoaded();
    const list = domainList().getAllByRole("button");
    expect(list.map((b) => b.textContent)).toEqual(["not sorted1 of 2 on", "monitoring2 of 2 on", "storage0 of 2 on", "idleno tools"]);
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["Not sorted", "monitoring", "storage", "idle"]);
  });

  it("explains why the tools without a domain are not offered, and gives each one's reason", async () => {
    mockApi();
    await renderLoaded();
    const unsorted = panel("Not sorted");
    expect(unsorted.getByText("These tools have no domain: they are not offered to the chat or to jobs.")).toBeInTheDocument();
    expect(unsorted.getByText("The model did not name a domain")).toBeInTheDocument();
    expect(unsorted.getByText("Not sorted yet: it is sorted at the next listing.")).toBeInTheDocument();
    expect(unsorted.queryByRole("button", { name: /Rename or describe/ })).not.toBeInTheDocument();
  });

  it("shows no 'Not sorted' group when every tool has a domain", async () => {
    mockApi(mcpPage({ tools: [makeTool(), makeTool({ tool: "read_disk" })] }));
    await renderLoaded();
    expect(screen.queryByRole("heading", { name: "Not sorted" })).not.toBeInTheDocument();
    expect(domainList().queryByRole("button", { name: /^not sorted/ })).not.toBeInTheDocument();
  });

  it("shows a domain's description, its on count, read and write counts and the jobs that use it as links", async () => {
    mockApi();
    await renderLoaded();
    const monitoring = panel("monitoring");
    expect(monitoring.getByText("Which hosts and services are up")).toBeInTheDocument();
    const meta = screen.getByRole("heading", { level: 2, name: "monitoring" }).parentElement;
    expect(meta).toHaveTextContent("2 of 2 on · 1 read · 1 write · used by Example job Other job");
    expect(monitoring.getByRole("link", { name: "Example job" })).toHaveAttribute("href", "#/jobs/example-job");
    expect(monitoring.getByRole("link", { name: "Other job" })).toHaveAttribute("href", "#/jobs/other-job");
    expect(within(screen.getByRole("heading", { level: 2, name: "storage" }).parentElement!).queryByText(/used by/)).not.toBeInTheDocument();
  });

  it("shows a domain without tools as its own panel: no tools, its description, a way to rename it", async () => {
    mockApi();
    await renderLoaded();
    const idle = panel("idle");
    expect(idle.getByText("Reserved for later")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "idle" }).parentElement).toHaveTextContent("no tools");
    expect(idle.getByRole("button", { name: "Rename or describe idle" })).toBeInTheDocument();
    expect(idle.queryByRole("table")).not.toBeInTheDocument();
  });

  it("names the jobs that use a domain without tools", async () => {
    mockApi(mcpPage({ domains: [makeDomain({ id: 3, name: "idle", tools: 0, used_by: [{ slug: "example-job", name: "Example job" }] })], tools: [] }));
    render(<McpToolsPage />);
    const idle = within((await screen.findByRole("heading", { level: 2, name: "idle" })).closest("section")!);
    expect(idle.getByRole("link", { name: "Example job" })).toHaveAttribute("href", "#/jobs/example-job");
  });

  it("shows each tool with its name, kind, server link and description", async () => {
    mockApi();
    await renderLoaded();
    const restart = row("test-main_restart_service");
    expect(restart.getByText("restart_service")).toBeInTheDocument();
    expect(restart.getByText("write")).toBeInTheDocument();
    expect(restart.getByText("Restart one service on a host")).toBeInTheDocument();
    expect(restart.getByRole("link", { name: "test-main" })).toHaveAttribute("href", "#/settings/mcp/test-main");
    expect(row("test-main_list_hosts").getByText("read")).toBeInTheDocument();
  });

  it("shows the switches in the state of each tool", async () => {
    mockApi();
    await renderLoaded();
    expect(screen.getByRole("switch", { name: "test-main_list_hosts on" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "test-main_read_disk on" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "test-main_list_hosts on" })).toHaveAttribute("title", "Turn off");
    expect(screen.getByRole("switch", { name: "test-main_read_disk on" })).toHaveAttribute("title", "Turn on");
  });

  it("marks tools of a server that is switched off or unreachable", async () => {
    mockApi();
    await renderLoaded();
    expect(row("test-extra_purge_cache").getByText("server off")).toBeInTheDocument();
    expect(row("test-extra_purge_cache").queryByText("unreachable")).not.toBeInTheDocument();
    expect(row("test-down_ping").getByText("unreachable")).toBeInTheDocument();
    expect(row("test-main_list_hosts").queryByText("server off")).not.toBeInTheDocument();
    expect(row("test-main_list_hosts").queryByText("unreachable")).not.toBeInTheDocument();
  });

  it("offers every domain but the tool's own in its move menu, and a new domain", async () => {
    mockApi();
    await renderLoaded();
    const menu = screen.getByRole("combobox", { name: "Move list_hosts to another domain" });
    expect([...menu.querySelectorAll("option")].map((o) => o.textContent)).toEqual(["move to…", "storage", "idle", "new domain…"]);
    const unsortedMenu = screen.getByRole("combobox", { name: "Move ping to another domain" });
    expect([...unsortedMenu.querySelectorAll("option")].map((o) => o.textContent)).toEqual(["move to…", "monitoring", "storage", "idle", "new domain…"]);
  });
});

describe("McpToolsPage: switching tools", () => {
  it("switches a tool off with one PATCH of its enabled flag and loads the page again", async () => {
    const api = mockApi();
    await renderLoaded();
    expect(api.loads).toBe(1);
    await userEvent.setup().click(screen.getByRole("switch", { name: "test-main_list_hosts on" }));
    await waitForLoads(api, 2);
    expect(api.seen).toEqual([{ method: "PATCH", path: "/api/mcp/tools/test-main/list_hosts", body: { enabled: false } }]);
  });

  it("switches a tool on", async () => {
    const api = mockApi();
    await renderLoaded();
    await userEvent.setup().click(screen.getByRole("switch", { name: "test-main_read_disk on" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0]).toEqual({ method: "PATCH", path: "/api/mcp/tools/test-main/read_disk", body: { enabled: true } });
  });

  it("shows the new state from the page it loads after the switch", async () => {
    const api = mockApi();
    await renderLoaded();
    api.page = mcpPage({ tools: mcpPage().tools.map((t) => (t.tool === "read_disk" ? { ...t, enabled: true } : t)) });
    await userEvent.setup().click(screen.getByRole("switch", { name: "test-main_read_disk on" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "test-main_read_disk on" })).toBeChecked());
    expect(screen.getByRole("img", { name: "6 tools: 3 read and on, 1 write and on, 2 off" })).toBeInTheDocument();
    expect(screen.getByText("tools on 3 servers").closest(".fd-counters")).toHaveTextContent("3domains4on");
  });

  it("encodes server and tool names in the path", async () => {
    const api = mockApi(mcpPage({ servers: [makeServer({ name: "my server" })], domains: [], tools: [makeTool({ server: "my server", tool: "a/b c", domain_id: null, domain: null })] }));
    render(<McpToolsPage />);
    await userEvent.setup().click(await screen.findByRole("switch", { name: "my server_a/b c on" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0].path).toBe("/api/mcp/tools/my%20server/a%2Fb%20c");
  });

  it("shows the server's message when a switch is refused, and keeps the page", async () => {
    mockApi();
    server.use(http.patch("/api/mcp/tools/*", () => HttpResponse.json({ detail: "Settings are locked" }, { status: 409 })));
    await renderLoaded();
    await userEvent.setup().click(screen.getByRole("switch", { name: "test-main_list_hosts on" }));
    expect(await screen.findByText("Settings are locked")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "test-main_list_hosts on" })).toBeChecked();
  });

  it("clears the error when the next load works", async () => {
    const api = mockApi();
    await renderLoaded();
    server.use(http.patch("/api/mcp/tools/*", () => HttpResponse.json({ detail: "Settings are locked" }, { status: 409 }), { once: true }));
    const user = userEvent.setup();
    await user.click(screen.getByRole("switch", { name: "test-main_list_hosts on" }));
    await screen.findByText("Settings are locked");
    await user.click(screen.getByRole("switch", { name: "test-main_read_disk on" }));
    await waitFor(() => expect(screen.queryByText("Settings are locked")).not.toBeInTheDocument());
    expect(api.loads).toBeGreaterThanOrEqual(2);
  });
});

describe("McpToolsPage: select and deselect all", () => {
  it("counts the tools each bulk button would change", async () => {
    mockApi();
    await renderLoaded();
    expect(screen.getByRole("button", { name: "select all (3)" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "deselect all (3)" })).toBeEnabled();
  });

  it("switches every tool that is off on in one request", async () => {
    const api = mockApi();
    await renderLoaded();
    await userEvent.setup().click(screen.getByRole("button", { name: "select all (3)" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0]).toEqual({
      method: "POST",
      path: "/api/mcp/tools/switch",
      body: {
        enabled: true,
        tools: [
          { server: "test-main", tool: "read_disk" },
          { server: "test-extra", tool: "purge_cache" },
          { server: "test-down", tool: "new_tool" },
        ],
      },
    });
    await waitForLoads(api, 2);
  });

  it("switches every tool that is on off in one request", async () => {
    const api = mockApi();
    await renderLoaded();
    await userEvent.setup().click(screen.getByRole("button", { name: "deselect all (3)" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0].body).toEqual({
      enabled: false,
      tools: [
        { server: "test-main", tool: "list_hosts" },
        { server: "test-main", tool: "restart_service" },
        { server: "test-down", tool: "ping" },
      ],
    });
  });

  it("disables select all when every tool is on, and deselect all when every tool is off", async () => {
    mockApi(mcpPage({ tools: mcpPage().tools.map((t) => ({ ...t, enabled: true })) }));
    const { unmount } = await renderLoaded();
    expect(screen.getByRole("button", { name: "select all (0)" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "deselect all (6)" })).toBeEnabled();
    unmount();
    mockApi(mcpPage({ tools: mcpPage().tools.map((t) => ({ ...t, enabled: false })) }));
    render(<McpToolsPage />);
    expect(await screen.findByRole("button", { name: "deselect all (0)" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "select all (6)" })).toBeEnabled();
  });

  it("does nothing when a disabled bulk button is pressed", async () => {
    const api = mockApi(mcpPage({ tools: mcpPage().tools.map((t) => ({ ...t, enabled: true })) }));
    await renderLoaded();
    await userEvent.setup().click(screen.getByRole("button", { name: "select all (0)" }));
    expect(api.seen).toEqual([]);
  });

  it("acts on the tools of one domain with the domain's own buttons", async () => {
    const api = mockApi();
    await renderLoaded();
    await userEvent.setup().click(panel("storage").getByRole("button", { name: "select all" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0].body).toEqual({
      enabled: true,
      tools: [
        { server: "test-main", tool: "read_disk" },
        { server: "test-extra", tool: "purge_cache" },
      ],
    });
  });

  it("disables a domain's select all when its tools are all on, and its deselect all when none is", async () => {
    mockApi();
    await renderLoaded();
    expect(panel("monitoring").getByRole("button", { name: "select all" })).toHaveAttribute("aria-disabled", "true");
    expect(panel("monitoring").getByRole("button", { name: "deselect all" })).toBeEnabled();
    expect(panel("storage").getByRole("button", { name: "select all" })).toBeEnabled();
    expect(panel("storage").getByRole("button", { name: "deselect all" })).toHaveAttribute("aria-disabled", "true");
  });

  it("acts on the tools without a domain with that group's buttons (every tool of the group)", async () => {
    const api = mockApi();
    await renderLoaded();
    await userEvent.setup().click(panel("Not sorted").getByRole("button", { name: "deselect all" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0].body).toEqual({
      enabled: false,
      tools: [
        { server: "test-down", tool: "ping" },
        { server: "test-down", tool: "new_tool" },
      ],
    });
  });

  it("shows the server's message when a bulk switch is refused", async () => {
    mockApi();
    server.use(http.post("/api/mcp/tools/switch", () => HttpResponse.json({ detail: "No such tool" }, { status: 404 })));
    await renderLoaded();
    await userEvent.setup().click(screen.getByRole("button", { name: "select all (3)" }));
    expect(await screen.findByText("No such tool")).toBeInTheDocument();
  });
});

describe("McpToolsPage: search and filters", () => {
  it("lists the servers in the server filter", async () => {
    mockApi();
    await renderLoaded();
    const menu = screen.getByRole("combobox", { name: "Server" });
    expect([...menu.querySelectorAll("option")].map((o) => o.textContent)).toEqual(["all servers", "test-main", "test-extra", "test-down"]);
  });

  it("searches the tool names", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().type(screen.getByRole("searchbox", { name: "Search tools and descriptions" }), "DISK");
    expect(screen.getByRole("switch", { name: "test-main_read_disk on" })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "test-main_list_hosts on" })).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "test-extra_purge_cache on" })).not.toBeInTheDocument();
  });

  it("searches the descriptions too", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().type(screen.getByRole("searchbox", { name: "Search tools and descriptions" }), "cached files");
    expect(screen.getByRole("switch", { name: "test-extra_purge_cache on" })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "test-main_read_disk on" })).not.toBeInTheDocument();
  });

  it("ignores spaces around the search", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().type(screen.getByRole("searchbox", { name: "Search tools and descriptions" }), "  ping  ");
    expect(screen.getAllByRole("switch")).toHaveLength(1);
  });

  it("filters by server", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "Server" }), "test-down");
    expect(screen.getAllByRole("switch").map((s) => s.getAttribute("aria-label"))).toEqual(["test-down_ping on", "test-down_new_tool on"]);
  });

  it("filters by read or write", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox", { name: "Read or write" }), "write");
    expect(screen.getAllByRole("switch").map((s) => s.getAttribute("aria-label"))).toEqual(["test-down_new_tool on", "test-main_restart_service on", "test-extra_purge_cache on"]);
    await user.selectOptions(screen.getByRole("combobox", { name: "Read or write" }), "read");
    expect(screen.getAllByRole("switch")).toHaveLength(3);
    expect(screen.queryByRole("switch", { name: "test-main_restart_service on" })).not.toBeInTheDocument();
  });

  it("filters by on or off", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox", { name: "On or off" }), "off");
    expect(screen.getAllByRole("switch").map((s) => s.getAttribute("aria-label"))).toEqual(["test-down_new_tool on", "test-main_read_disk on", "test-extra_purge_cache on"]);
    await user.selectOptions(screen.getByRole("combobox", { name: "On or off" }), "on");
    expect(screen.getAllByRole("switch")).toHaveLength(3);
    expect(screen.queryByRole("switch", { name: "test-main_read_disk on" })).not.toBeInTheDocument();
  });

  it("combines the filters", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox", { name: "Read or write" }), "write");
    await user.selectOptions(screen.getByRole("combobox", { name: "On or off" }), "off");
    await user.selectOptions(screen.getByRole("combobox", { name: "Server" }), "test-extra");
    expect(screen.getAllByRole("switch").map((s) => s.getAttribute("aria-label"))).toEqual(["test-extra_purge_cache on"]);
  });

  it("says no tool matches when nothing is left, and keeps the filters", async () => {
    mockApi();
    await renderLoaded();
    const search = screen.getByRole("searchbox", { name: "Search tools and descriptions" });
    await userEvent.setup().type(search, "zzz");
    expect(screen.getByText("No tool matches.")).toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(search).toHaveValue("zzz");
  });

  it("lists only the domains with matches, with how many of their tools are shown", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().type(screen.getByRole("searchbox", { name: "Search tools and descriptions" }), "disk");
    expect(domainList().getAllByRole("button").map((b) => b.textContent)).toEqual(["storage1 shown · 0 of 2 on"]);
    expect(screen.getByRole("heading", { level: 2, name: "storage" }).parentElement).toHaveTextContent("1 shown · 0 of 2 on in this domain");
    expect(screen.queryByRole("heading", { name: "idle" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "monitoring" })).not.toBeInTheDocument();
  });

  it("calls the group of tools without a domain a group while filtering", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().type(screen.getByRole("searchbox", { name: "Search tools and descriptions" }), "ping");
    expect(screen.getByRole("heading", { level: 2, name: "Not sorted" }).parentElement).toHaveTextContent("1 shown · 1 of 2 on in this group");
  });

  it("makes the bulk buttons act on the shown tools only", async () => {
    const api = mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.type(screen.getByRole("searchbox", { name: "Search tools and descriptions" }), "disk");
    expect(screen.getByRole("button", { name: "select shown (1)" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "deselect shown (0)" })).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("button", { name: "select shown (1)" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0].body).toEqual({ enabled: true, tools: [{ server: "test-main", tool: "read_disk" }] });
  });

  it("makes a domain's buttons act on its shown tools only", async () => {
    const api = mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.type(screen.getByRole("searchbox", { name: "Search tools and descriptions" }), "disk");
    await user.click(panel("storage").getByRole("button", { name: "select shown" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0].body).toEqual({ enabled: true, tools: [{ server: "test-main", tool: "read_disk" }] });
  });

  it("brings the full list back when the filters are cleared", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    const search = screen.getByRole("searchbox", { name: "Search tools and descriptions" });
    await user.type(search, "zzz");
    await user.clear(search);
    expect(screen.getAllByRole("switch")).toHaveLength(6);
    expect(screen.queryByText("No tool matches.")).not.toBeInTheDocument();
  });
});

describe("McpToolsPage: opening and closing domains", () => {
  it("starts with every domain open", async () => {
    mockApi();
    await renderLoaded();
    for (const b of domainList().getAllByRole("button")) expect(b).toHaveAttribute("aria-expanded", "true");
  });

  it("closes a domain on a click in the list and remembers it by name", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().click(domainList().getByRole("button", { name: /^monitoring/ }));
    expect(screen.queryByRole("heading", { level: 2, name: "monitoring" })).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "test-main_list_hosts on" })).not.toBeInTheDocument();
    expect(domainList().getByRole("button", { name: /^monitoring/ })).toHaveAttribute("aria-expanded", "false");
    expect(JSON.parse(localStorage.getItem(CLOSED_KEY)!)).toEqual(["monitoring"]);
    expect(screen.getByRole("heading", { level: 2, name: "storage" })).toBeInTheDocument();
  });

  it("opens it again with a second click", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(domainList().getByRole("button", { name: /^monitoring/ }));
    await user.click(domainList().getByRole("button", { name: /^monitoring/ }));
    expect(screen.getByRole("heading", { level: 2, name: "monitoring" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(CLOSED_KEY)!)).toEqual([]);
  });

  it("closes the group of tools without a domain under its own key", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().click(domainList().getByRole("button", { name: /^not sorted/ }));
    expect(screen.queryByRole("heading", { name: "Not sorted" })).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(CLOSED_KEY)!)).toEqual(["(not sorted)"]);
  });

  it("closes a domain without tools too", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().click(domainList().getByRole("button", { name: /^idle/ }));
    expect(screen.queryByRole("heading", { name: "idle" })).not.toBeInTheDocument();
  });

  it("starts with the domains the owner closed before still closed", async () => {
    localStorage.setItem(CLOSED_KEY, JSON.stringify(["storage"]));
    mockApi();
    await renderLoaded();
    expect(screen.queryByRole("heading", { level: 2, name: "storage" })).not.toBeInTheDocument();
    expect(domainList().getByRole("button", { name: /^storage/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("heading", { level: 2, name: "monitoring" })).toBeInTheDocument();
  });

  it("ignores a stored value that is not a list of names, or not JSON", async () => {
    localStorage.setItem(CLOSED_KEY, JSON.stringify({ storage: true }));
    mockApi();
    const { unmount } = await renderLoaded();
    expect(screen.getByRole("heading", { level: 2, name: "storage" })).toBeInTheDocument();
    unmount();
    localStorage.setItem(CLOSED_KEY, "{not json");
    render(<McpToolsPage />);
    expect(await screen.findByRole("heading", { level: 2, name: "storage" })).toBeInTheDocument();
  });

  it("keeps only the names among the stored values", async () => {
    localStorage.setItem(CLOSED_KEY, JSON.stringify(["storage", 7, null]));
    mockApi();
    await renderLoaded();
    expect(screen.queryByRole("heading", { level: 2, name: "storage" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "monitoring" })).toBeInTheDocument();
  });

  it("collapses all, then expands all, with one button", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "collapse all" }));
    expect(screen.queryAllByRole("heading", { level: 2 })).toHaveLength(0);
    expect(JSON.parse(localStorage.getItem(CLOSED_KEY)!).sort()).toEqual(["(not sorted)", "idle", "monitoring", "storage"]);
    // the tools panel (counters) stays when every domain is closed
    expect(screen.getByRole("img", { name: /^6 tools/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "expand all" }));
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(4);
    expect(JSON.parse(localStorage.getItem(CLOSED_KEY)!)).toEqual([]);
  });

  it("offers expand all as soon as one domain is closed", async () => {
    localStorage.setItem(CLOSED_KEY, JSON.stringify(["idle"]));
    mockApi();
    await renderLoaded();
    expect(screen.queryByRole("button", { name: "collapse all" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "expand all" })).toBeEnabled();
  });

  it("forces the domains with matches open while filtering, and leaves what was remembered alone", async () => {
    localStorage.setItem(CLOSED_KEY, JSON.stringify(["storage"]));
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.type(screen.getByRole("searchbox", { name: "Search tools and descriptions" }), "disk");
    expect(screen.getByRole("heading", { level: 2, name: "storage" })).toBeInTheDocument();
    expect(domainList().getByRole("button", { name: /^storage/ })).toHaveAttribute("aria-expanded", "true");
    // clicking a domain does not change anything while filtering
    await user.click(domainList().getByRole("button", { name: /^storage/ }));
    expect(screen.getByRole("heading", { level: 2, name: "storage" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(CLOSED_KEY)!)).toEqual(["storage"]);
    // the filter is off again: the remembered state is back
    await user.clear(screen.getByRole("searchbox", { name: "Search tools and descriptions" }));
    expect(screen.queryByRole("heading", { level: 2, name: "storage" })).not.toBeInTheDocument();
  });

  it("disables collapse all while filtering, with a hint why", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    expect(screen.getByRole("button", { name: "collapse all" })).toBeEnabled();
    await user.selectOptions(screen.getByRole("combobox", { name: "On or off" }), "on");
    const button = screen.getByRole("button", { name: "collapse all" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("Matches stay open while filtering")).toHaveAttribute("role", "tooltip");
    await user.click(button);
    expect(JSON.parse(localStorage.getItem(CLOSED_KEY) ?? "[]")).toEqual([]);
  });

  it("works when the browser has no storage", async () => {
    mockApi();
    await renderLoaded();
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = () => {
      throw new Error("blocked");
    };
    try {
      await userEvent.setup().click(domainList().getByRole("button", { name: /^monitoring/ }));
      expect(screen.queryByRole("heading", { level: 2, name: "monitoring" })).not.toBeInTheDocument();
    } finally {
      Storage.prototype.setItem = setItem;
    }
  });
});
