// The app shell: hash routing, the title of the page, the rail (nav, badge, version, lamp, text size).
// The eight pages are replaced by stubs that print the params they got: their own tests live elsewhere.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../../src/App";
import { automationRunning, hardware, info, settingsShell, status } from "../../mocks/fixtures/shell";
import { server } from "../../mocks/server";
import { FakeEventSource, installFakeEventSource } from "./fake-event-source";

vi.mock("../../../src/pages/Dashboard", () => ({ DashboardPage: () => <div>dashboard page</div> }));
vi.mock("../../../src/pages/Jobs", () => ({ JobsPage: ({ params }: { params: string[] }) => <div>jobs page [{params.join("|")}]</div> }));
vi.mock("../../../src/pages/Approvals", () => ({ ApprovalsPage: ({ params }: { params: string[] }) => <div>approvals page [{params.join("|")}]</div> }));
vi.mock("../../../src/pages/Chat", () => ({ ChatPage: () => <div>chat page</div> }));
vi.mock("../../../src/pages/McpTools", () => ({ McpToolsPage: () => <div>mcp page</div> }));
vi.mock("../../../src/pages/Memory", () => ({ MemoryPage: ({ params }: { params: string[] }) => <div>memory page [{params.join("|")}]</div> }));
vi.mock("../../../src/pages/Logs", () => ({ LogsPage: ({ params }: { params: string[] }) => <div>logs page [{params.join("|")}]</div> }));
vi.mock("../../../src/pages/Settings", () => ({ SettingsPage: () => <div>settings page</div> }));

installFakeEventSource();

const PAGE_LINKS = ["dashboard", "jobs", "approvals", "chat", "mcp tools", "memory", "logs", "settings"];

function renderAt(hash: string) {
  location.hash = hash;
  return render(<App />);
}

/** Wait until a request has been answered and the page had time to act on the answer. */
const settle = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 60)));

beforeEach(() => {
  // live.ts keeps one connection for the whole file: start every test "not connected".
  FakeEventSource.instances.at(-1)?.fail();
});

afterEach(async () => {
  // Let the requests the shell sent on mount finish before the page is torn down (an aborted fetch is noise).
  await settle();
  location.hash = "";
  document.title = "";
  document.documentElement.style.fontSize = "";
});

describe("routing", () => {
  it("shows the dashboard for an empty address, with its title, and marks it in both navigations", () => {
    renderAt("");
    expect(screen.getByText("dashboard page")).toBeInTheDocument();
    expect(document.title).toBe("Dashboard · AI Server Monitor");
    for (const link of screen.getAllByRole("link", { name: "dashboard" })) expect(link).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(2);
  });

  it.each([
    ["jobs", "Jobs", "jobs page []", "jobs"],
    ["approvals", "Approvals", "approvals page []", "approvals"],
    ["chat", "Chat", "chat page", "chat"],
    ["mcp", "MCP tools", "mcp page", "mcp tools"],
    ["memory", "Memory", "memory page []", "memory"],
    ["logs", "Logs", "logs page []", "logs"],
    ["settings", "Settings", "settings page", "settings"],
  ])("#/%s shows that page, titles the tab %s and marks %s in the navigation", (key, title, text, linkName) => {
    renderAt(`#/${key}`);
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.queryByText("dashboard page")).not.toBeInTheDocument();
    expect(document.title).toBe(`${title} · AI Server Monitor`);
    const current = screen.getAllByRole("link", { current: "page" });
    expect(current).toHaveLength(2);
    for (const link of current) expect(link).toHaveAccessibleName(linkName);
  });

  it("passes the rest of the address to the page as params", () => {
    renderAt("#/approvals/12");
    expect(screen.getByText("approvals page [12]")).toBeInTheDocument();
  });

  it("passes every further segment, and ignores a trailing or doubled slash", () => {
    renderAt("#/jobs/test:disk//runs/41/");
    expect(screen.getByText("jobs page [test:disk|runs|41]")).toBeInTheDocument();
  });

  it("passes the params of the logs page", () => {
    renderAt("#/logs/llm/for/job/x");
    expect(screen.getByText("logs page [llm|for|job|x]")).toBeInTheDocument();
  });

  it("accepts an address without the slash after the hash", () => {
    renderAt("#jobs");
    expect(screen.getByText("jobs page []")).toBeInTheDocument();
  });

  it("falls back to the dashboard for an unknown page, and keeps its params to itself", () => {
    renderAt("#/nowhere/1");
    expect(screen.getByText("dashboard page")).toBeInTheDocument();
    expect(document.title).toBe("Dashboard · AI Server Monitor");
    expect(screen.getAllByRole("link", { current: "page" })[0]).toHaveAccessibleName("dashboard");
  });

  it("does not take a page name from the middle of the address", () => {
    renderAt("#/x/jobs");
    expect(screen.getByText("dashboard page")).toBeInTheDocument();
  });

  it("follows the address when it changes, and the title with it", () => {
    renderAt("#/jobs");
    expect(screen.getByText("jobs page []")).toBeInTheDocument();
    act(() => {
      location.hash = "#/approvals/7";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(screen.getByText("approvals page [7]")).toBeInTheDocument();
    expect(screen.queryByText("jobs page []")).not.toBeInTheDocument();
    expect(document.title).toBe("Approvals · AI Server Monitor");
    act(() => {
      location.hash = "";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(screen.getByText("dashboard page")).toBeInTheDocument();
    expect(document.title).toBe("Dashboard · AI Server Monitor");
  });

  it("stops listening for address changes when it is closed", () => {
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    const { unmount } = renderAt("#/jobs");
    const added = add.mock.calls.filter(([type]) => type === "hashchange").map(([, listener]) => listener);
    expect(added).toHaveLength(1);
    expect(remove.mock.calls.filter(([type]) => type === "hashchange")).toHaveLength(0);
    unmount();
    expect(remove.mock.calls.filter(([type]) => type === "hashchange").map(([, listener]) => listener)).toEqual(added);
    add.mockRestore();
    remove.mockRestore();
  });
});

describe("navigation", () => {
  it("has the same eight links, in order, in the rail and in the bottom bar", () => {
    renderAt("");
    const navs = screen.getAllByRole("navigation", { name: "Main" });
    expect(navs).toHaveLength(2);
    for (const nav of navs) {
      const links = within(nav).getAllByRole("link");
      expect(links.map((a) => a.textContent)).toEqual(PAGE_LINKS);
      expect(links.map((a) => a.getAttribute("href"))).toEqual(["#/dashboard", "#/jobs", "#/approvals", "#/chat", "#/mcp", "#/memory", "#/logs", "#/settings"]);
    }
  });

  it("marks only the page that is open", () => {
    renderAt("#/chat");
    const nav = screen.getAllByRole("navigation", { name: "Main" })[0];
    const marked = within(nav)
      .getAllByRole("link")
      .filter((a) => a.hasAttribute("aria-current"));
    expect(marked.map((a) => a.textContent)).toEqual(["chat"]);
  });
});

describe("pending approvals badge", () => {
  const serveStatus = (pending: number) => server.use(http.get("/api/status", () => HttpResponse.json({ ...status, pending_approvals: pending })));

  it("shows no number while nothing waits", async () => {
    serveStatus(0);
    renderAt("");
    await settle();
    for (const link of screen.getAllByRole("link", { name: /approvals/ })) expect(link).toHaveAccessibleName("approvals");
  });

  it("shows the number of waiting approvals beside the approvals link, in both navigations, and nowhere else", async () => {
    serveStatus(3);
    renderAt("");
    await waitFor(() => expect(screen.getAllByRole("link", { name: "approvals 3" })).toHaveLength(2));
    expect(screen.getAllByRole("link", { name: "jobs" })).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "chat" })).toHaveLength(2);
  });

  it("shows nothing and does not break when the status cannot be read", async () => {
    server.use(http.get("/api/status", () => HttpResponse.json({ detail: "down" }, { status: 500 })));
    renderAt("");
    await settle();
    expect(screen.getAllByRole("link", { name: "approvals" })).toHaveLength(2);
    expect(screen.getByText("dashboard page")).toBeInTheDocument();
  });

  it("asks again when an approval or an action event arrives, and ignores the other events", async () => {
    let pending = 1;
    let asked = 0;
    server.use(
      http.get("/api/status", () => {
        asked += 1;
        return HttpResponse.json({ ...status, pending_approvals: pending });
      }),
    );
    renderAt("");
    await waitFor(() => expect(screen.getAllByRole("link", { name: "approvals 1" })).toHaveLength(2));
    const before = asked;

    act(() => FakeEventSource.current().emit("job.finished", { slug: "x" }));
    act(() => FakeEventSource.current().emit("chat.message", {}));
    await settle();
    expect(asked).toBe(before);

    pending = 2;
    act(() => FakeEventSource.current().emit("approval.pending", { id: 5 }));
    await waitFor(() => expect(screen.getAllByRole("link", { name: "approvals 2" })).toHaveLength(2));

    pending = 0;
    act(() => FakeEventSource.current().emit("action.result", { id: 5 }));
    await waitFor(() => expect(screen.getAllByRole("link", { name: "approvals" })).toHaveLength(2));
    expect(asked).toBeGreaterThan(before);
  });
});

describe("version and live lamp", () => {
  it("shows the version and the commit once the server has told, and 'reconnecting' until the stream is open", async () => {
    renderAt("");
    expect(screen.getByText("reconnecting")).toBeInTheDocument();
    expect(await screen.findByText(`v${info.version} · ${info.commit} · reconnecting`)).toBeInTheDocument();
  });

  it("says live and turns the lamp when the stream opens, and goes back when it breaks", async () => {
    renderAt("");
    await screen.findByText(`v${info.version} · ${info.commit} · reconnecting`);
    expect(screen.getByTitle("Reconnecting")).toBeInTheDocument();
    expect(screen.queryByTitle("Live updates on")).not.toBeInTheDocument();

    act(() => FakeEventSource.current().open());
    expect(screen.getByText(`v${info.version} · ${info.commit} · live`)).toBeInTheDocument();
    expect(screen.getByTitle("Live updates on")).toBeInTheDocument();
    expect(screen.queryByTitle("Reconnecting")).not.toBeInTheDocument();

    act(() => FakeEventSource.current().fail());
    expect(screen.getByText(`v${info.version} · ${info.commit} · reconnecting`)).toBeInTheDocument();
    expect(screen.getByTitle("Reconnecting")).toBeInTheDocument();
  });

  it("leaves the commit out when the server does not know it", async () => {
    server.use(http.get("/api/info", () => HttpResponse.json({ version: "1.2.3", commit: null })));
    renderAt("");
    expect(await screen.findByText("v1.2.3 · reconnecting")).toBeInTheDocument();
  });

  it("shows no version when the server does not answer, and the rest of the page still works", async () => {
    let asked = false;
    server.use(
      http.get("/api/info", () => {
        asked = true;
        return HttpResponse.json({ detail: "down" }, { status: 500 });
      }),
    );
    renderAt("");
    await waitFor(() => expect(asked).toBe(true));
    await settle();
    expect(screen.getByText("reconnecting")).toBeInTheDocument();
    expect(screen.queryByText(/^v\d/)).not.toBeInTheDocument();
    expect(screen.getByText("dashboard page")).toBeInTheDocument();
  });
});

describe("text size", () => {
  it("sets the root font size to the server's setting", async () => {
    server.use(http.get("/api/settings", () => HttpResponse.json({ ...settingsShell, values: { general: { root_font_size: 20 } } })));
    renderAt("");
    await waitFor(() => expect(document.documentElement.style.fontSize).toBe("20px"));
  });

  it("leaves the size alone when the settings cannot be read", async () => {
    let asked = false;
    server.use(
      http.get("/api/settings", () => {
        asked = true;
        return HttpResponse.json({ detail: "down" }, { status: 500 });
      }),
    );
    renderAt("");
    await waitFor(() => expect(asked).toBe(true));
    await settle();
    expect(document.documentElement.style.fontSize).toBe("");
  });
});

describe("what stands around the page", () => {
  it("draws the hardware meters and the automation switch in the rail on every page", async () => {
    renderAt("#/settings");
    expect(await screen.findByRole("meter", { name: "CPU" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "pause automation" })).toBeInTheDocument();
    expect(screen.getByText("automation running")).toBeInTheDocument();
    expect(hardware.gpus).toHaveLength(1);
  });

  it("shows the paused notice above the page, whichever page it is", async () => {
    server.use(http.get("/api/automation", () => HttpResponse.json({ ...automationRunning, paused: true, since: 1_800_000_000 })));
    renderAt("#/chat");
    expect(await screen.findByRole("status")).toHaveTextContent("Automation paused since");
    expect(screen.getByText("chat page")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "resume automation" })).toBeInTheDocument();
  });

  it("scrolls the main part back to the top when another page opens", () => {
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollTo").mockImplementation(() => undefined);
    renderAt("#/jobs");
    const afterFirst = scroll.mock.calls.length;
    expect(afterFirst).toBeGreaterThan(0);
    act(() => {
      location.hash = "#/jobs/test:disk";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(scroll.mock.calls.length).toBeGreaterThan(afterFirst);
    scroll.mockRestore();
  });
});
