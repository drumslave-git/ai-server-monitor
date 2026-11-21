import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { McpToolsPage } from "../../../src/pages/McpTools";
import { makeDomain, makeServer, makeTool, mcpPage } from "../../mocks/fixtures/mcp";
import { server } from "../../mocks/server";
import { mockApi, panel, renderLoaded, row, setupBrowser, waitForLoads } from "./helpers";

setupBrowser();

const dialog = (name: string) => within(screen.getByRole("dialog", { name }));
const nameBox = (d: ReturnType<typeof dialog>) => d.getByRole("textbox", { name: /^name/ });
const descriptionBox = (d: ReturnType<typeof dialog>) => d.getByRole("textbox", { name: /^description/ });

describe("Domain dialog: rename or describe", () => {
  it("opens for a domain with its name and description filled in, named after the domain", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    const d = dialog("Domain storage");
    expect(nameBox(d)).toHaveValue("storage");
    expect(descriptionBox(d)).toHaveValue("Disks and caches");
    expect(d.getByText("Lowercase letters, digits and hyphens · 7 of 24 characters")).toBeInTheDocument();
    expect(d.getByRole("button", { name: "save" })).toBeEnabled();
  });

  it("opens from the panel of a domain without tools too", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().click(panel("idle").getByRole("button", { name: "Rename or describe idle" }));
    expect(nameBox(dialog("Domain idle"))).toHaveValue("idle");
  });

  it("saves the new name and description with a PUT, closes, and loads the page again", async () => {
    const api = mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    const d = dialog("Domain storage");
    await user.clear(nameBox(d));
    await user.type(nameBox(d), "  disks-2 ");
    await user.clear(descriptionBox(d));
    await user.type(descriptionBox(d), "Where the data lives");
    await user.click(d.getByRole("button", { name: "save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.seen).toEqual([{ method: "PUT", path: "/api/mcp/domains/2", body: { name: "disks-2", description: "Where the data lives" } }]);
    expect(api.loads).toBe(2);
  });

  it("shows the rule and the length while typing, and keeps save off for a name that breaks it", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    const d = dialog("Domain storage");
    await user.clear(nameBox(d));
    await user.type(nameBox(d), "Bad Name");
    expect(d.getByText("Only lowercase letters, digits and hyphens; start with a letter or digit · 8 of 24 characters")).toBeInTheDocument();
    expect(nameBox(d)).toHaveAttribute("aria-invalid", "true");
    expect(d.getByRole("button", { name: "save" })).toBeDisabled();
    await user.clear(nameBox(d));
    await user.type(nameBox(d), "-lead");
    expect(d.getByText(/start with a letter or digit/)).toBeInTheDocument();
    await user.clear(nameBox(d));
    await user.type(nameBox(d), "good-name1");
    expect(d.queryByText(/start with a letter or digit/)).not.toBeInTheDocument();
    expect(nameBox(d)).not.toHaveAttribute("aria-invalid");
    expect(d.getByRole("button", { name: "save" })).toBeEnabled();
  });

  it("keeps save off for an empty name, without an error", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    const d = dialog("Domain storage");
    await user.clear(nameBox(d));
    expect(d.getByRole("button", { name: "save" })).toBeDisabled();
    expect(d.getByText("Lowercase letters, digits and hyphens · 0 of 24 characters")).toBeInTheDocument();
    expect(nameBox(d)).not.toHaveAttribute("aria-invalid");
  });

  it("stops the name at 24 characters", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    const d = dialog("Domain storage");
    await user.clear(nameBox(d));
    await user.type(nameBox(d), "a".repeat(30));
    expect(nameBox(d)).toHaveValue("a".repeat(24));
    expect(d.getByText("Lowercase letters, digits and hyphens · 24 of 24 characters")).toBeInTheDocument();
  });

  it("allows an empty description when renaming", async () => {
    const api = mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    const d = dialog("Domain storage");
    await user.clear(descriptionBox(d));
    await user.click(d.getByRole("button", { name: "save" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0].body).toEqual({ name: "storage", description: "" });
  });

  it("shows the server's message when it refuses the name, keeps the dialog open and the save button usable", async () => {
    mockApi();
    server.use(http.put("/api/mcp/domains/:id", () => HttpResponse.json({ detail: "A domain called monitoring exists" }, { status: 409 })));
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    const d = dialog("Domain storage");
    await user.clear(nameBox(d));
    await user.type(nameBox(d), "monitoring");
    await user.click(d.getByRole("button", { name: "save" }));
    expect(await d.findByText("A domain called monitoring exists")).toBeInTheDocument();
    expect(d.getByRole("button", { name: "save" })).toBeEnabled();
  });

  it("closes with cancel and sends nothing", async () => {
    const api = mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    await user.click(dialog("Domain storage").getByRole("button", { name: "cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.seen).toEqual([]);
  });

  it("closes with Esc and sends nothing", async () => {
    const api = mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    expect(screen.getByRole("dialog", { name: "Domain storage" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.seen).toEqual([]);
  });
});

describe("Domain dialog: delete", () => {
  it("asks first, naming the domain and what happens to its tools, and does nothing on no", async () => {
    const api = mockApi();
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    await user.click(dialog("Domain storage").getByRole("button", { name: "delete domain" }));
    expect(confirm).toHaveBeenCalledWith('Delete the domain "storage"? Its 2 tools are sorted again at the next listing.');
    expect(api.seen).toEqual([]);
    expect(screen.getByRole("dialog", { name: "Domain storage" })).toBeInTheDocument();
  });

  it("deletes the domain on yes, closes the dialog and loads the page again", async () => {
    const api = mockApi();
    vi.stubGlobal("confirm", vi.fn(() => true));
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    await user.click(dialog("Domain storage").getByRole("button", { name: "delete domain" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.seen).toEqual([{ method: "DELETE", path: "/api/mcp/domains/2", body: undefined }]);
    expect(api.loads).toBe(2);
  });

  it("leaves out the tools sentence for a domain without tools", async () => {
    mockApi();
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("idle").getByRole("button", { name: "Rename or describe idle" }));
    await user.click(dialog("Domain idle").getByRole("button", { name: "delete domain" }));
    expect(confirm).toHaveBeenCalledWith('Delete the domain "idle"?');
  });

  it("cannot delete a domain a job uses, and says which jobs", async () => {
    const api = mockApi();
    await renderLoaded();
    await userEvent.setup().click(panel("monitoring").getByRole("button", { name: "Rename or describe monitoring" }));
    const button = dialog("Domain monitoring").getByRole("button", { name: "delete domain" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "Used by Example job, Other job");
    expect(api.seen).toEqual([]);
  });

  it("shows the server's message when the delete is refused", async () => {
    mockApi();
    vi.stubGlobal("confirm", vi.fn(() => true));
    server.use(http.delete("/api/mcp/domains/:id", () => HttpResponse.json({ detail: "Used by job example-job" }, { status: 409 })));
    await renderLoaded();
    const user = userEvent.setup();
    await user.click(panel("storage").getByRole("button", { name: "Rename or describe storage" }));
    await user.click(dialog("Domain storage").getByRole("button", { name: "delete domain" }));
    expect(await dialog("Domain storage").findByText("Used by job example-job")).toBeInTheDocument();
  });
});

describe("Moving a tool to a domain", () => {
  it("moves it to the chosen domain with one PATCH of its domain_id", async () => {
    const api = mockApi();
    await renderLoaded();
    await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "Move list_hosts to another domain" }), "storage");
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0]).toEqual({ method: "PATCH", path: "/api/mcp/tools/test-main/list_hosts", body: { domain_id: 2 } });
    await waitForLoads(api, 2);
  });

  it("moves a tool without a domain into one", async () => {
    const api = mockApi();
    await renderLoaded();
    await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "Move ping to another domain" }), "monitoring");
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0]).toEqual({ method: "PATCH", path: "/api/mcp/tools/test-down/ping", body: { domain_id: 1 } });
  });

  it("shows the server's message when the move is refused", async () => {
    mockApi();
    server.use(http.patch("/api/mcp/tools/*", () => HttpResponse.json({ detail: "No such domain" }, { status: 404 })));
    await renderLoaded();
    await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "Move list_hosts to another domain" }), "storage");
    expect(await screen.findByText("No such domain")).toBeInTheDocument();
  });

  it("opens a dialog for a new domain with 'new domain…'", async () => {
    mockApi();
    await renderLoaded();
    await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "Move list_hosts to another domain" }), "new domain…");
    const d = dialog("New domain");
    expect(nameBox(d)).toHaveValue("");
    expect(d.getByText("One line; the chat's model reads it to pick the domain.")).toBeInTheDocument();
    expect(d.getByRole("button", { name: "create and move" })).toBeDisabled();
    expect(d.queryByRole("button", { name: "delete domain" })).not.toBeInTheDocument();
  });

  it("needs a description for a new domain as well as a valid name", async () => {
    mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox", { name: "Move list_hosts to another domain" }), "new domain…");
    const d = dialog("New domain");
    await user.type(nameBox(d), "network");
    expect(d.getByRole("button", { name: "create and move" })).toBeDisabled();
    await user.type(descriptionBox(d), "   ");
    expect(d.getByRole("button", { name: "create and move" })).toBeDisabled();
    await user.type(descriptionBox(d), "Routers and links");
    expect(d.getByRole("button", { name: "create and move" })).toBeEnabled();
  });

  it("creates the domain, then moves the tool into it, then loads the page", async () => {
    const api = mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox", { name: "Move list_hosts to another domain" }), "new domain…");
    const d = dialog("New domain");
    await user.type(nameBox(d), "network");
    await user.type(descriptionBox(d), "Routers and links");
    await user.click(d.getByRole("button", { name: "create and move" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.seen).toEqual([
      { method: "POST", path: "/api/mcp/domains", body: { name: "network", description: "Routers and links" } },
      { method: "PATCH", path: "/api/mcp/tools/test-main/list_hosts", body: { domain_id: 9 } },
    ]);
    expect(api.loads).toBe(2);
  });

  it("shows the server's message when the new domain is refused, and does not move the tool", async () => {
    const api = mockApi();
    server.use(http.post("/api/mcp/domains", () => HttpResponse.json({ detail: "A domain called network exists" }, { status: 409 })));
    await renderLoaded();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox", { name: "Move list_hosts to another domain" }), "new domain…");
    const d = dialog("New domain");
    await user.type(nameBox(d), "network");
    await user.type(descriptionBox(d), "Routers");
    await user.click(d.getByRole("button", { name: "create and move" }));
    expect(await d.findByText("A domain called network exists")).toBeInTheDocument();
    expect(api.seen).toEqual([]);
    expect(row("test-main_list_hosts").getByRole("combobox")).toBeInTheDocument();
  });

  it("closes the new-domain dialog with cancel and moves nothing", async () => {
    const api = mockApi();
    await renderLoaded();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox", { name: "Move list_hosts to another domain" }), "new domain…");
    await user.click(dialog("New domain").getByRole("button", { name: "cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.seen).toEqual([]);
  });
});

describe("Sort into domains", () => {
  const openDialog = async () => {
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "sort into domains…" }));
    return { user, d: dialog("Sort into domains") };
  };

  it("opens a dialog that says what the sorting keeps", async () => {
    mockApi();
    await renderLoaded();
    const { d } = await openDialog();
    expect(d.getByText("The main model sorts the tools again; domain names and descriptions you wrote stay.")).toBeInTheDocument();
    expect(d.getByRole("button", { name: "sort" })).toBeEnabled();
  });

  it("offers all enabled servers, and the enabled servers by name", async () => {
    mockApi();
    await renderLoaded();
    const { d } = await openDialog();
    const menu = d.getByRole("combobox");
    expect([...menu.querySelectorAll("option")].map((o) => o.textContent)).toEqual(["all enabled servers", "test-main", "test-down"]);
  });

  it("counts the tools the owner placed by hand, which stay where they are", async () => {
    mockApi();
    await renderLoaded();
    const { d } = await openDialog();
    expect(d.getByText("1 tool you placed by hand stays where it is.")).toBeInTheDocument();
    expect(d.getByRole("checkbox", { name: "Also re-sort the tools I placed by hand" })).toBeEnabled();
  });

  it("says they in the plural for several tools placed by hand", async () => {
    mockApi(mcpPage({ tools: mcpPage().tools.map((t) => (t.domain_id != null ? { ...t, set_by: "owner" as const } : t)) }));
    await renderLoaded();
    const { d } = await openDialog();
    expect(d.getByText("4 tools you placed by hand stay where they are.")).toBeInTheDocument();
  });

  it("counts only the chosen server's tools and disables the re-sort box when none was placed by hand", async () => {
    mockApi();
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.selectOptions(d.getByRole("combobox"), "test-down");
    expect(d.getByText("You placed no tool by hand.")).toBeInTheDocument();
    expect(d.getByRole("checkbox", { name: "Also re-sort the tools I placed by hand" })).toBeDisabled();
  });

  it("sends the sorting for all servers, closes the dialog and shows that it runs", async () => {
    const api = mockApi();
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.click(d.getByRole("button", { name: "sort" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(api.seen).toEqual([{ method: "POST", path: "/api/mcp/sort", body: { server: null, include_owner: false } }]));
  });

  it("sends the chosen server and the re-sort choice", async () => {
    const api = mockApi();
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.selectOptions(d.getByRole("combobox"), "test-main");
    await user.click(d.getByRole("checkbox", { name: "Also re-sort the tools I placed by hand" }));
    await user.click(d.getByRole("button", { name: "sort" }));
    await waitFor(() => expect(api.seen).toHaveLength(1));
    expect(api.seen[0].body).toEqual({ server: "test-main", include_owner: true });
  });

  it("shows the sorting as running (and blocks a second one) until the answer comes, then reports it", async () => {
    mockApi();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.post("/api/mcp/sort", async () => {
        await gate;
        return HttpResponse.json({ sorted: 5, error: null, unreachable: {} });
      }),
    );
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.click(d.getByRole("button", { name: "sort" }));
    expect(screen.getByText("sorting tools into domains…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "sort into domains…" })).toBeDisabled();
    release();
    expect(await screen.findByText("Sorted 5 tools into domains.")).toBeInTheDocument();
  });

  it("loads the page again when the sorting is done", async () => {
    const api = mockApi();
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.click(d.getByRole("button", { name: "sort" }));
    await screen.findByText("Sorted 2 tools into domains.");
    await waitForLoads(api, 2);
    expect(screen.queryByText("sorting tools into domains…")).not.toBeInTheDocument();
  });

  it("says one tool in the singular", async () => {
    mockApi();
    server.use(http.post("/api/mcp/sort", () => HttpResponse.json({ sorted: 1, error: null, unreachable: {} })));
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.click(d.getByRole("button", { name: "sort" }));
    expect(await screen.findByText("Sorted 1 tool into domains.")).toBeInTheDocument();
  });

  it("reports the model's failure", async () => {
    mockApi();
    server.use(http.post("/api/mcp/sort", () => HttpResponse.json({ sorted: 0, error: "The model is not reachable", unreachable: {} })));
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.click(d.getByRole("button", { name: "sort" }));
    expect(await screen.findByText("Sorting failed: The model is not reachable")).toBeInTheDocument();
  });

  it("adds the servers that could not be listed to the report", async () => {
    mockApi();
    server.use(http.post("/api/mcp/sort", () => HttpResponse.json({ sorted: 3, error: null, unreachable: { "test-down": "connection refused", "test-extra": "timeout" } })));
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.click(d.getByRole("button", { name: "sort" }));
    expect(await screen.findByText("Sorted 3 tools into domains. Server test-down could not be listed: connection refused. Server test-extra could not be listed: timeout.")).toBeInTheDocument();
  });

  it("reports an HTTP error of the request itself", async () => {
    mockApi();
    server.use(http.post("/api/mcp/sort", () => HttpResponse.json({ detail: "Sorting is already running" }, { status: 409 })));
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.click(d.getByRole("button", { name: "sort" }));
    expect(await screen.findByText("Sorting failed: Sorting is already running")).toBeInTheDocument();
  });

  it("clears the earlier report when a new sorting starts", async () => {
    mockApi();
    await renderLoaded();
    const first = await openDialog();
    await first.user.click(first.d.getByRole("button", { name: "sort" }));
    await screen.findByText("Sorted 2 tools into domains.");
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.post("/api/mcp/sort", async () => {
        await gate;
        return HttpResponse.json({ sorted: 4, error: null, unreachable: {} });
      }),
    );
    const second = await openDialog();
    await second.user.click(second.d.getByRole("button", { name: "sort" }));
    expect(screen.queryByText("Sorted 2 tools into domains.")).not.toBeInTheDocument();
    release();
    expect(await screen.findByText("Sorted 4 tools into domains.")).toBeInTheDocument();
  });

  it("closes with cancel and sends nothing", async () => {
    const api = mockApi();
    await renderLoaded();
    const { user, d } = await openDialog();
    await user.click(d.getByRole("button", { name: "cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.seen).toEqual([]);
  });
});

describe("Sorting that is already running", () => {
  /** The page asks again every 2 s while a sorting runs: catch that timer and fire it by hand. */
  function catchTimer() {
    const ticks: Array<() => void> = [];
    const real = globalThis.setInterval;
    vi.spyOn(globalThis, "setInterval").mockImplementation(((fn: () => void, ms?: number, ...rest: unknown[]) => {
      if (ms === 2000) {
        ticks.push(fn);
        return 4242;
      }
      return real(fn, ms, ...(rest as []));
    }) as unknown as typeof setInterval);
    const cleared = vi.spyOn(globalThis, "clearInterval");
    return { ticks, cleared };
  }

  it("says so, keeps sort off, and says the tools may still be without a domain", async () => {
    mockApi(mcpPage({ sorting: true }));
    catchTimer();
    await renderLoaded();
    expect(screen.getByText("sorting tools into domains…")).toBeInTheDocument();
    expect(screen.getByText(/some tools may still be without a domain/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "sort into domains…" })).toBeDisabled();
  });

  it("looks again every 2 s, shows the result and stops looking when the sorting is done", async () => {
    const api = mockApi(mcpPage({ sorting: true }));
    const { ticks, cleared } = catchTimer();
    await renderLoaded();
    expect(ticks).toHaveLength(1);
    expect(api.loads).toBe(1);
    api.page = mcpPage({ sorting: true });
    ticks[0]();
    await waitForLoads(api, 2);
    expect(screen.getByText("sorting tools into domains…")).toBeInTheDocument();
    api.page = mcpPage({ sorting: false, tools: [makeTool({ tool: "list_hosts" })], domains: [makeDomain({ tools: 1 })], servers: [makeServer()] });
    ticks[0]();
    await waitFor(() => expect(screen.queryByText("sorting tools into domains…")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "sort into domains…" })).toBeEnabled();
    expect(cleared).toHaveBeenCalledWith(4242);
    expect(screen.queryByRole("switch", { name: "test-down_ping on" })).not.toBeInTheDocument();
  });

  it("does not ask again on a timer when no sorting runs", async () => {
    mockApi();
    const { ticks } = catchTimer();
    await renderLoaded();
    expect(ticks).toHaveLength(0);
  });
});

describe("Pages with a single tool", () => {
  it("renders a tool of a server that is not in the server list without a server state", async () => {
    mockApi(mcpPage({ servers: [makeServer()], domains: [], tools: [makeTool({ server: "test-gone", tool: "old", domain_id: null, domain: null })] }));
    render(<McpToolsPage />);
    const sw = await screen.findByRole("switch", { name: "test-gone_old on" });
    const r = within(sw.closest("tr")!);
    expect(r.queryByText("server off")).not.toBeInTheDocument();
    expect(r.queryByText("unreachable")).not.toBeInTheDocument();
    expect(r.getByRole("link", { name: "test-gone" })).toHaveAttribute("href", "#/settings/mcp/test-gone");
  });
});
