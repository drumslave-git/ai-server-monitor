// src/pages/Settings.tsx: the page around the schema form. It loads the settings document, shows one section at a
// time, saves all values at once (PUT /api/settings), shows the server's errors at the fields, and has the model
// forms (what the model reports about itself, the connection test) and the address #/settings/<section>[/<entry>].
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JsonObject, LlmTestResult, ModelInfo, SettingsDoc } from "../../../src/api";
import { SettingsPage } from "../../../src/pages/Settings";
import { llmTestOk, makeSettingsDoc, modelInfoOllama, modelInfoUnavailable } from "../../mocks/fixtures/settings";
import { server } from "../../mocks/server";

type Seen = { path: string; body: unknown };

/** The shell's own GET /api/settings answers first (handlers/index.ts), so the page's document is set here.
 *  Each call the page makes is recorded; `state.put` decides what a save answers (default: the settings echoed,
 *  every secret that was set as the mask). */
function mockApi(change?: (doc: SettingsDoc) => void) {
  const state = {
    doc: makeSettingsDoc(change),
    puts: [] as unknown[],
    infos: [] as Seen[],
    tests: [] as Seen[],
    connTests: [] as Seen[],
    info: modelInfoUnavailable as ModelInfo,
    llmTest: llmTestOk as LlmTestResult | (() => Response | Promise<Response>),
    put: undefined as undefined | ((body: JsonObject) => Response | Promise<Response>),
  };
  const seen = async (request: Request): Promise<Seen> => {
    const url = new URL(request.url);
    return { path: url.pathname + url.search, body: await request.json() };
  };
  server.use(
    http.get("/api/settings", () => HttpResponse.json(state.doc)),
    http.put("/api/settings", async ({ request }) => {
      const body = (await request.json()) as JsonObject;
      state.puts.push(body);
      if (state.put) return state.put(body);
      return HttpResponse.json({ values: withMasks(body) });
    }),
    http.post("/api/settings/llm-model-info", async ({ request }) => {
      state.infos.push(await seen(request));
      return HttpResponse.json(state.info);
    }),
    http.post("/api/settings/test-llm", async ({ request }) => {
      state.tests.push(await seen(request));
      return typeof state.llmTest === "function" ? state.llmTest() : HttpResponse.json(state.llmTest);
    }),
    http.post("/api/settings/test/:kind", async ({ request }) => {
      state.connTests.push(await seen(request));
      return HttpResponse.json({ ok: true, message: "Bot is reachable.", details: {} });
    }),
  );
  return state;
}

/** What the backend answers for a saved document: a secret that is set is the mask again. */
function withMasks(value: unknown, key = ""): JsonObject {
  const walk = (v: unknown, k: string): unknown => {
    if (Array.isArray(v)) return v.map((x) => walk(x, k));
    if (v && typeof v === "object") {
      const o = v as Record<string, unknown>;
      return Object.fromEntries(Object.entries(o).map(([kk, vv]) => [kk, kk === "value" && "name" in o ? mask(vv) : walk(vv, kk)]));
    }
    return ["api_key", "token", "docker_hub_token"].includes(k) ? mask(v) : v;
  };
  const mask = (v: unknown) => (typeof v === "string" && v !== "" ? "********" : v);
  return walk(value, key) as JsonObject;
}

const scrollTo = vi.fn();
const scrollIntoView = vi.fn();

beforeEach(() => {
  scrollTo.mockClear();
  scrollIntoView.mockClear();
  vi.stubGlobal("scrollTo", scrollTo);
  Object.defineProperty(Element.prototype, "scrollIntoView", { value: scrollIntoView, configurable: true, writable: true });
  document.documentElement.style.fontSize = "";
});
afterEach(() => {
  history.replaceState(null, "", "#");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

/** The page at an address, loaded (the section's heading is there). */
async function open(hash = "", heading = "Main model") {
  history.replaceState(null, "", hash || "#");
  const user = userEvent.setup();
  const view = render(<SettingsPage />);
  await screen.findByRole("heading", { name: heading });
  return { user, ...view };
}

const field = (name: string) => screen.getByLabelText(new RegExp(`^${name}`), { selector: "input,textarea,select" });
const save = () => screen.getByRole("button", { name: "save" });
const discard = () => screen.getByRole("button", { name: "discard" });
/** An icon button is marked aria-disabled (it keeps its place in the tab order), a text button is disabled. */
const off = (el: HTMLElement) => el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
const link = (title: RegExp | string) => screen.getByRole("link", { name: title });

describe("loading", () => {
  it("says Loading… until the document is there, then shows the first section", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    mockApi();
    server.use(
      http.get("/api/settings", async () => {
        await gate;
        return HttpResponse.json(makeSettingsDoc());
      }),
    );
    render(<SettingsPage />);
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "save" })).not.toBeInTheDocument();
    release();
    expect(await screen.findByRole("heading", { name: "Main model" })).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("says why the settings could not be loaded, and shows no form", async () => {
    server.use(http.get("/api/settings", () => HttpResponse.json({ detail: "Database is locked" }, { status: 500 })));
    render(<SettingsPage />);
    expect(await screen.findByText("Could not load settings: Database is locked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "save" })).not.toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });
});

describe("sections", () => {
  it("lists every section of the document, in its order, with a lowercase title", async () => {
    mockApi();
    await open();
    const nav = within(screen.getByRole("navigation", { name: "Settings sections" }));
    expect(nav.getAllByRole("link").map((a) => a.textContent)).toEqual([
      "general", "main model", "worker model", "chat", "jobs", "data retention", "logs", "approvals", "outputs", "mcp servers",
    ]);
    expect(nav.getByRole("link", { name: "main model" })).toHaveAttribute("href", "#/settings/llm");
    expect(nav.getByRole("link", { name: "mcp servers" })).toHaveAttribute("href", "#/settings/mcp");
  });

  it("opens the main model, takes its note from the schema, and puts its address in the bar", async () => {
    mockApi();
    await open();
    expect(screen.getByText(/The big model: it answers chat questions/)).toBeInTheDocument();
    expect(field("base url")).toHaveValue("http://llm.test:11434/v1");
    expect(location.hash).toBe("#/settings/llm");
    expect(scrollTo).toHaveBeenCalledWith({ top: 0 });
    expect(link("main model")).toHaveAttribute("aria-current", "true");
    expect(link("chat")).not.toHaveAttribute("aria-current");
  });

  it("opens the section its address names", async () => {
    mockApi();
    await open("#/settings/chat", "Chat");
    expect(field("max steps")).toHaveValue(15);
    expect(location.hash).toBe("#/settings/chat");
    expect(link("chat")).toHaveAttribute("aria-current", "true");
  });

  it("switches by a click on a link without leaving the page, and the address follows", async () => {
    mockApi();
    const { user } = await open();
    scrollTo.mockClear();
    await user.click(link("chat"));
    expect(await screen.findByRole("heading", { name: "Chat" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Main model" })).not.toBeInTheDocument();
    expect(location.hash).toBe("#/settings/chat");
    expect(scrollTo).toHaveBeenCalledWith({ top: 0 });
    expect(link("chat")).toHaveAttribute("aria-current", "true");
  });

  it("switches by the select (the narrow screen's list), which has the same sections", async () => {
    mockApi();
    const { user } = await open();
    const select = screen.getByRole("combobox", { name: "section" });
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toContain("data retention");
    expect(select).toHaveValue("llm");
    await user.selectOptions(select, "storage");
    expect(await screen.findByRole("heading", { name: "Data retention" })).toBeInTheDocument();
    expect(location.hash).toBe("#/settings/storage");
  });

  it("keeps the edits of one section when another is looked at", async () => {
    mockApi();
    const { user } = await open();
    await user.type(field("model"), "-x");
    await user.click(link("chat"));
    await screen.findByRole("heading", { name: "Chat" });
    expect(screen.getByText("unsaved changes")).toBeInTheDocument();
    await user.click(link("main model"));
    expect(await screen.findByDisplayValue("test-main-x")).toBeInTheDocument();
  });

  it("puts the fields of a section into the named rows of the page", async () => {
    mockApi();
    const { user } = await open("#/settings/general", "General");
    for (const [title, label] of [["Service", "timezone"], ["Language and display", "root font size"], ["Instructions", "instructions"]]) {
      expect(within(screen.getByRole("region", { name: title })).getByLabelText(new RegExp(`^${label}`), { selector: "input,textarea,select" })).toBeInTheDocument();
    }
    expect(within(screen.getByRole("region", { name: "Service" })).getByRole("radiogroup", { name: "Log level" })).toBeInTheDocument();
    expect(field("instructions")).toHaveValue("Keep answers short.");
    await user.click(link("logs"));
    await screen.findByRole("heading", { name: "Logs" });
    expect(within(screen.getByRole("region", { name: "Recording" })).getByRole("switch", { name: "LLM trace" })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Retention" })).getByLabelText(/^retention/)).toHaveValue("7d");
  });

  it("shows a section's own note", async () => {
    mockApi();
    await open("#/settings/jobs", "Jobs");
    expect(screen.getByText("Scheduled checks by the language model.")).toBeInTheDocument();
  });
});

describe("a link to one entry of a list", () => {
  it("opens the card of the server the address names and keeps the address", async () => {
    mockApi();
    await open("#/settings/mcp/second-server", "MCP servers");
    expect(screen.getByRole("button", { name: "second-server" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "test-server" })).toHaveAttribute("aria-expanded", "false");
    expect(location.hash).toBe("#/settings/mcp/second-server");
    expect(scrollIntoView).toHaveBeenCalled();
  });

  it("decodes the name in the address and encodes it again", async () => {
    mockApi((doc) => {
      ((doc.values.mcp as JsonObject).servers as JsonObject[])[1].name = "my server";
    });
    await open("#/settings/mcp/my%20server", "MCP servers");
    expect(screen.getByRole("button", { name: "my server" })).toHaveAttribute("aria-expanded", "true");
    expect(location.hash).toBe("#/settings/mcp/my%20server");
  });

  it("lets go of the entry when another section is chosen", async () => {
    mockApi();
    const { user } = await open("#/settings/mcp/second-server", "MCP servers");
    await user.click(link("chat"));
    await screen.findByRole("heading", { name: "Chat" });
    expect(location.hash).toBe("#/settings/chat");
    await user.click(link("mcp servers"));
    await screen.findByRole("heading", { name: "MCP servers" });
    expect(location.hash).toBe("#/settings/mcp");
    expect(screen.getByRole("button", { name: "second-server" })).toHaveAttribute("aria-expanded", "false");
  });
});

describe("edit, discard, save", () => {
  it("starts clean: save and discard are off and nothing is said", async () => {
    mockApi();
    await open();
    expect(off(save())).toBe(true);
    expect(off(discard())).toBe(true);
    expect(screen.queryByText("unsaved changes")).not.toBeInTheDocument();
  });

  it("says unsaved changes and enables both buttons on an edit, and is clean again when the edit is undone", async () => {
    mockApi();
    const { user } = await open();
    await user.type(field("model"), "x");
    expect(screen.getByText("unsaved changes")).toBeInTheDocument();
    expect(off(save())).toBe(false);
    expect(off(discard())).toBe(false);
    await user.type(field("model"), "{Backspace}");
    expect(screen.queryByText("unsaved changes")).not.toBeInTheDocument();
    expect(off(save())).toBe(true);
    expect(off(discard())).toBe(true);
  });

  it("discards every edit of every section", async () => {
    mockApi();
    const { user } = await open();
    await user.type(field("model"), "x");
    await user.click(link("chat"));
    await screen.findByRole("heading", { name: "Chat" });
    await user.clear(field("max steps"));
    await user.type(field("max steps"), "30");
    await user.click(discard());
    expect(field("max steps")).toHaveValue(15);
    await user.click(link("main model"));
    expect(await screen.findByDisplayValue("test-main")).toBeInTheDocument();
    expect(screen.queryByText("unsaved changes")).not.toBeInTheDocument();
    expect(off(save())).toBe(true);
  });

  it("saves all values in one PUT and says so, with the buttons off again", async () => {
    const api = mockApi();
    const { user } = await open();
    await user.type(field("model"), "-v2");
    await user.click(link("chat"));
    await screen.findByRole("heading", { name: "Chat" });
    await user.clear(field("max steps"));
    await user.type(field("max steps"), "30");
    await user.click(save());
    expect(await screen.findByText("Saved and applied.")).toBeInTheDocument();
    expect(api.puts).toHaveLength(1);
    const body = api.puts[0] as JsonObject;
    expect(Object.keys(body).sort()).toEqual(["approvals", "chat", "general", "jobs", "llm", "logs", "mcp", "outputs", "storage", "worker"]);
    expect((body.llm as JsonObject).model).toBe("test-main-v2");
    expect((body.chat as JsonObject).max_steps).toBe(30);
    expect((body.worker as JsonObject).model).toBe("test-worker");
    expect(screen.queryByText("unsaved changes")).not.toBeInTheDocument();
    expect(off(save())).toBe(true);
    expect(off(discard())).toBe(true);
  });

  it("shows what the server answers: a typed secret comes back as saved", async () => {
    const api = mockApi();
    const { user } = await open();
    await user.type(field("api key"), "new-key");
    await user.click(save());
    await screen.findByText("Saved and applied.");
    expect((api.puts[0] as { llm: JsonObject }).llm.api_key).toBe("new-key");
    expect(field("api key")).toHaveValue("");
    expect(field("api key")).toHaveAttribute("placeholder", "Saved — type to replace");
  });

  it("sends a secret that was not touched as the mask, and a cleared one as empty", async () => {
    const api = mockApi();
    const { user } = await open();
    await user.type(field("model"), "x");
    await user.click(save());
    await screen.findByText("Saved and applied.");
    expect((api.puts[0] as { llm: JsonObject }).llm.api_key).toBe("********");
    await user.click(screen.getByText("clear"));
    await user.click(save());
    await waitFor(() => expect(api.puts).toHaveLength(2));
    expect((api.puts[1] as { llm: JsonObject }).llm.api_key).toBe("");
    await waitFor(() => expect(field("api key")).toHaveAttribute("placeholder", "Not set"));
  });

  it("applies the saved text size of the page", async () => {
    mockApi();
    const { user } = await open("#/settings/general", "General");
    await user.clear(field("root font size"));
    await user.type(field("root font size"), "18");
    await user.click(save());
    await screen.findByText("Saved and applied.");
    expect(document.documentElement.style.fontSize).toBe("18px");
  });

  it("shows saving… and keeps both buttons off while the request is out", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const api = mockApi();
    api.put = async (body) => {
      await gate;
      return HttpResponse.json({ values: withMasks(body) });
    };
    const { user } = await open();
    await user.type(field("model"), "x");
    await user.click(save());
    const busy = await screen.findByRole("button", { name: "saving…" });
    expect(off(busy)).toBe(true);
    expect(off(discard())).toBe(true);
    release();
    expect(await screen.findByText("Saved and applied.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "save" })).toBeDisabled();
  });

  it("takes the saved message away after four seconds", async () => {
    mockApi();
    const timers = vi.spyOn(globalThis, "setTimeout");
    const { user } = await open();
    await user.type(field("model"), "x");
    await user.click(save());
    await screen.findByText("Saved and applied.");
    const call = timers.mock.calls.find((c) => c[1] === 4000);
    expect(call).toBeDefined();
    act(() => (call![0] as () => void)());
    expect(screen.queryByText("Saved and applied.")).not.toBeInTheDocument();
  });

  it("takes the saved message away at the next edit", async () => {
    mockApi();
    const { user } = await open();
    await user.type(field("model"), "x");
    await user.click(save());
    await screen.findByText("Saved and applied.");
    await user.type(field("model"), "y");
    expect(screen.queryByText("Saved and applied.")).not.toBeInTheDocument();
    expect(screen.getByText("unsaved changes")).toBeInTheDocument();
  });

  it("says the save failed, with the server's words, and keeps the edits", async () => {
    const api = mockApi();
    api.put = () => HttpResponse.json({ detail: "Disk is full" }, { status: 500 });
    const { user } = await open();
    await user.type(field("model"), "x");
    await user.click(save());
    expect(await screen.findByText("Save failed: Disk is full")).toBeInTheDocument();
    expect(field("model")).toHaveValue("test-mainx");
    expect(off(save())).toBe(false);
  });

  it("says the save failed when the server cannot be reached", async () => {
    const api = mockApi();
    api.put = () => HttpResponse.error();
    const { user } = await open();
    await user.type(field("model"), "x");
    await user.click(save());
    expect(await screen.findByText(/^Save failed: /)).toBeInTheDocument();
    expect(off(save())).toBe(false);
  });

  it("saves when its form is submitted (the Enter key in a field does that in a browser)", async () => {
    // happy-dom does not do the implicit submission of a form whose submit button stands outside it (form=<id>),
    // so the form's submit event is sent instead of the key.
    const api = mockApi();
    const { user } = await open();
    await user.type(field("model"), "x");
    const form = field("model").closest("form")!;
    const event = new Event("submit", { bubbles: true, cancelable: true });
    act(() => {
      form.dispatchEvent(event);
    });
    expect(event.defaultPrevented).toBe(true); // (the page does not reload)
    await waitFor(() => expect(api.puts).toHaveLength(1));
    expect(await screen.findByText("Saved and applied.")).toBeInTheDocument();
  });

  it("asks the browser before the page is left with unsaved changes, and not otherwise", async () => {
    mockApi();
    const { user } = await open();
    const leave = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(leave()).toBe(false);
    await user.type(field("model"), "x");
    expect(leave()).toBe(true);
    await user.click(discard());
    expect(leave()).toBe(false);
  });
});

describe("errors of the server", () => {
  const rejected = (detail: unknown) => () => HttpResponse.json({ detail }, { status: 422 });

  it("go to their fields, count in the section list, and take the page to the first one's section", async () => {
    const api = mockApi();
    api.put = rejected([
      { loc: ["llm", "base_url"], msg: "Must be an http(s) address" },
      { loc: ["mcp", "servers", 1, "url"], msg: "Address is wrong" },
      { loc: ["mcp", "servers", 1, "name"], msg: "Name is taken" },
    ]);
    const { user } = await open("#/settings/chat", "Chat");
    await user.clear(field("max steps"));
    await user.type(field("max steps"), "20");
    await user.click(save());
    expect(await screen.findByRole("heading", { name: "Main model" })).toBeInTheDocument();
    expect(screen.getByText("Some values are invalid — see the highlighted fields.")).toBeInTheDocument();
    expect(screen.getByText("Must be an http(s) address")).toBeInTheDocument();
    expect(field("base url")).toHaveAttribute("aria-invalid", "true");
    expect(location.hash).toBe("#/settings/llm");
    expect(link(/^main model/)).toHaveTextContent("1");
    expect(link(/^mcp servers/)).toHaveTextContent("2");
    expect(link(/^chat/)).toHaveTextContent(/^chat$/);
    expect(screen.getByRole("option", { name: "main model (1 to fix)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "mcp servers (2 to fix)" })).toBeInTheDocument();
    // the other section shows its errors at its fields, in the card of the entry that has them
    await user.click(link(/^mcp servers/));
    await screen.findByRole("heading", { name: "MCP servers" });
    expect(screen.getByRole("button", { name: "second-server" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Address is wrong")).toBeInTheDocument();
    expect(screen.getByText("Name is taken")).toBeInTheDocument();
  });

  it("stay where they are when the first one is not in a section", async () => {
    const api = mockApi();
    api.put = rejected([{ loc: ["body"], msg: "Unreadable" }]);
    const { user } = await open("#/settings/chat", "Chat");
    await user.clear(field("max steps"));
    await user.type(field("max steps"), "20");
    await user.click(save());
    expect(await screen.findByText("Some values are invalid — see the highlighted fields.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Chat" })).toBeInTheDocument();
    expect(location.hash).toBe("#/settings/chat");
  });

  it("are a failed save when they come without a list of fields", async () => {
    const api = mockApi();
    api.put = rejected("Not a JSON object");
    const { user } = await open();
    await user.type(field("model"), "x");
    await user.click(save());
    expect(await screen.findByText(/^Save failed: /)).toBeInTheDocument();
    expect(screen.queryByText("Some values are invalid — see the highlighted fields.")).not.toBeInTheDocument();
  });

  it("go away with a discard, and with a save that works", async () => {
    const api = mockApi();
    api.put = rejected([{ loc: ["llm", "base_url"], msg: "Must be an http(s) address" }]);
    const { user } = await open();
    await user.type(field("model"), "x");
    await user.click(save());
    await screen.findByText("Must be an http(s) address");
    await user.click(discard());
    expect(screen.queryByText("Must be an http(s) address")).not.toBeInTheDocument();
    expect(screen.queryByText("Some values are invalid — see the highlighted fields.")).not.toBeInTheDocument();
    expect(link("main model")).toHaveTextContent(/^main model$/);

    await user.type(field("model"), "y");
    await user.click(save());
    await screen.findByText("Must be an http(s) address");
    api.put = undefined;
    await user.click(save());
    expect(await screen.findByText("Saved and applied.")).toBeInTheDocument();
    expect(screen.queryByText("Must be an http(s) address")).not.toBeInTheDocument();
    expect(field("base url")).not.toHaveAttribute("aria-invalid");
  });
});

describe("the main model's form", () => {
  it("puts its fields into Connection, Output, Sampling, Requests and Advanced", async () => {
    mockApi();
    await open();
    const rows: [string, string[]][] = [
      ["Connection", ["base url", "api key", "model"]],
      ["Output", ["max output tokens", "reasoning effort"]],
      ["Sampling", ["temperature", "top p", "top k"]],
      ["Requests", ["timeout", "max retries"]],
      ["Advanced", ["structured output", "extra body"]],
    ];
    for (const [title, labels] of rows) {
      const region = within(screen.getByRole("region", { name: title }));
      for (const label of labels) {
        const found = region.queryByLabelText(new RegExp(`^${label}`), { selector: "input,textarea,select" }) ?? region.queryByRole("radiogroup", { name: new RegExp(`^${label}`, "i") });
        expect(found, `${label} in ${title}`).not.toBeNull();
      }
    }
  });

  it("shows no note of inheriting (that is the worker's), and its sampling values as auto", async () => {
    mockApi();
    await open();
    expect(screen.queryByText(/Empty base URL and API key/)).not.toBeInTheDocument();
    expect(field("temperature")).toHaveAttribute("placeholder", "auto");
    expect(field("temperature")).toHaveValue(null);
    expect(field("extra body")).toHaveValue("{}");
  });

  it("offers no suggestions for the model until a test has found some", async () => {
    mockApi();
    await open();
    expect(field("model")).not.toHaveAttribute("list");
  });
});

describe("what the model says about itself", () => {
  it("asks after a short wait, with the values in the form, and shows the model's defaults", async () => {
    const api = mockApi();
    api.info = modelInfoOllama;
    await open();
    expect(api.infos).toHaveLength(0); // (asked after a pause, not at once)
    expect(await screen.findByText("Model default: temperature 0.7, top k 20, top p 0.9 · tools, thinking · context 32768", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(api.infos).toHaveLength(1);
    expect(api.infos[0].path).toBe("/api/settings/llm-model-info?profile=main");
    expect(api.infos[0].body).toMatchObject({ base_url: "http://llm.test:11434/v1", model: "test-main" });
    const note = screen.getByText(/^Model default:/);
    expect(within(screen.getByRole("region", { name: "Connection" })).getByText(note.textContent!)).toBeInTheDocument();
  });

  it("shows nothing when the endpoint does not report", async () => {
    const api = mockApi();
    await open();
    await waitFor(() => expect(api.infos).toHaveLength(1), { timeout: 3000 });
    expect(screen.queryByText(/^Model default:/)).not.toBeInTheDocument();
  });

  it("says that no sampling values are set, and leaves out the completion capability", async () => {
    const api = mockApi();
    api.info = { available: true, parameters: {}, capabilities: ["completion", "tools"], context_length: null };
    await open();
    expect(await screen.findByText("Model default: no sampling values set · tools", {}, { timeout: 3000 })).toBeInTheDocument();
  });

  it("shows nothing and goes on when the question fails", async () => {
    const api = mockApi();
    server.use(http.post("/api/settings/llm-model-info", () => HttpResponse.json({ detail: "no" }, { status: 500 })));
    const { user } = await open();
    await new Promise((r) => setTimeout(r, 800));
    expect(screen.queryByText(/^Model default:/)).not.toBeInTheDocument();
    await user.type(field("model"), "x");
    expect(field("model")).toHaveValue("test-mainx");
    expect(api.infos).toHaveLength(0);
  });

  it("does not ask for a main model that has no endpoint or no name", async () => {
    const api = mockApi((doc) => {
      (doc.values.llm as JsonObject).base_url = "";
    });
    await open();
    await new Promise((r) => setTimeout(r, 800));
    expect(api.infos).toHaveLength(0);
  });

  it("asks again, once, after the name is changed (not at every key)", async () => {
    const api = mockApi();
    const { user } = await open();
    await waitFor(() => expect(api.infos).toHaveLength(1), { timeout: 3000 });
    await user.type(field("model"), "-big");
    await waitFor(() => expect(api.infos).toHaveLength(2), { timeout: 3000 });
    await new Promise((r) => setTimeout(r, 700));
    expect(api.infos).toHaveLength(2);
    expect(api.infos[1].body).toMatchObject({ model: "test-main-big" });
  });

  it("hides the reasoning effort for a model that does not think, and shows it for one that does", async () => {
    const api = mockApi();
    api.info = { available: true, parameters: {}, capabilities: ["completion", "tools"], context_length: null };
    await open();
    expect(screen.getByRole("radiogroup", { name: "Reasoning effort" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("radiogroup", { name: "Reasoning effort" })).not.toBeInTheDocument(), { timeout: 3000 });
    expect(screen.getByRole("region", { name: "Output" })).toBeInTheDocument();
  });

  it("keeps the reasoning effort for a model that reports thinking", async () => {
    const api = mockApi();
    api.info = modelInfoOllama;
    await open();
    await screen.findByText(/^Model default:/, {}, { timeout: 3000 });
    expect(screen.getByRole("radiogroup", { name: "Reasoning effort" })).toBeInTheDocument();
  });
});

describe("the worker model's form", () => {
  it("says an empty endpoint and key mean the main model's, and has that for the placeholder", async () => {
    mockApi();
    await open("#/settings/worker", "Worker model");
    expect(screen.getByText("Empty base URL and API key = the main model's. Empty model = the main model does the sub-tasks too.")).toBeInTheDocument();
    expect(field("base url")).toHaveAttribute("placeholder", "main model's endpoint");
    expect(field("model")).toHaveValue("test-worker");
    expect(field("temperature")).toHaveValue(0.2);
    expect(field("top k")).toHaveValue(40);
    expect(field("extra body")).toHaveValue('{\n  "keep_alive": "5m"\n}');
  });

  it("asks about the model even when its own endpoint is empty", async () => {
    const api = mockApi();
    await open("#/settings/worker", "Worker model");
    await waitFor(() => expect(api.infos).toHaveLength(1), { timeout: 3000 });
    expect(api.infos[0].path).toBe("/api/settings/llm-model-info?profile=worker");
    expect(api.infos[0].body).toMatchObject({ base_url: "", model: "test-worker" });
  });

  it("has no placeholder for the endpoint on the main model", async () => {
    mockApi();
    await open();
    expect(field("base url")).not.toHaveAttribute("placeholder");
  });
});

describe("connection test of a model", () => {
  const testButton = () => screen.getByRole("button", { name: "test" });

  it("sends the values in the form, and says what came back", async () => {
    const api = mockApi();
    const { user } = await open();
    await user.type(field("model"), "-x"); // (unsaved values are tested, as the text says)
    await user.click(testButton());
    expect(await screen.findByText("Model replied “pong” in 0.42s. Tool call OK.")).toBeInTheDocument();
    expect(screen.getByText("Structured answer OK.")).toBeInTheDocument();
    expect(api.tests).toHaveLength(1);
    expect(api.tests[0].path).toBe("/api/settings/test-llm?profile=main");
    expect(api.tests[0].body).toMatchObject({ base_url: "http://llm.test:11434/v1", model: "test-main-x" });
    expect(screen.getAllByText("ok").length).toBe(2);
  });

  it("tests the worker profile on the worker page", async () => {
    const api = mockApi();
    const { user } = await open("#/settings/worker", "Worker model");
    await user.click(testButton());
    await screen.findByText(/^Model replied/);
    expect(api.tests[0].path).toBe("/api/settings/test-llm?profile=worker");
    expect(api.tests[0].body).toMatchObject({ model: "test-worker" });
  });

  it("notes a model the endpoint does not list, and a reply that is empty", async () => {
    const api = mockApi();
    api.llmTest = { ...llmTestOk, reply: "", tool_call: false, model_listed: false, structured_output: null };
    const { user } = await open();
    await user.click(testButton());
    expect(await screen.findByText("Model replied “(empty)” in 0.42s. Note: the endpoint's model list doesn't include this model name.")).toBeInTheDocument();
    expect(screen.queryByText("Structured answer OK.")).not.toBeInTheDocument();
  });

  it("adds the models of the endpoint as suggestions for the model fields", async () => {
    const api = mockApi();
    api.llmTest = { ...llmTestOk, models: ["alpha-7b", "beta-70b", "gamma-1b"] };
    const { user } = await open();
    await user.click(testButton());
    expect(await screen.findByText(/3 models available — suggestions added to the Model field\./)).toBeInTheDocument();
    const list = field("model").getAttribute("list");
    expect(list).toBeTruthy();
    const options = Array.from(document.getElementById(list!)!.querySelectorAll("option")).map((o) => o.getAttribute("value"));
    expect(options).toEqual(["alpha-7b", "beta-70b", "gamma-1b"]);
    // the worker's model field is offered the same
    await user.click(link("worker model"));
    await screen.findByRole("heading", { name: "Worker model" });
    const workerList = field("model").getAttribute("list");
    expect(workerList).toBeTruthy();
    expect(document.getElementById(workerList!)!.querySelectorAll("option")).toHaveLength(3);
  });

  it("says failed with the error the test gives, or a plain failure without one", async () => {
    const api = mockApi();
    api.llmTest = { ok: false, error: "401 Unauthorized" };
    const { user } = await open();
    await user.click(testButton());
    expect(await screen.findByText("401 Unauthorized")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    api.llmTest = { ok: false };
    await user.click(testButton());
    expect(await screen.findByText("The test failed.")).toBeInTheDocument();
    expect(screen.queryByText("401 Unauthorized")).not.toBeInTheDocument();
  });

  it("says the test could not run when the request fails", async () => {
    const api = mockApi();
    api.llmTest = () => HttpResponse.json({ detail: "Worker is down" }, { status: 502 });
    const { user } = await open();
    await user.click(testButton());
    expect(await screen.findByText("The test could not run: Worker is down")).toBeInTheDocument();
    expect(off(testButton())).toBe(false);
  });

  it("shows testing… while it runs and takes only one test at a time", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const api = mockApi();
    api.llmTest = async () => {
      await gate;
      return HttpResponse.json(llmTestOk);
    };
    const { user } = await open();
    await user.click(testButton());
    const running = await screen.findByRole("button", { name: "testing…" });
    expect(off(running)).toBe(true);
    release();
    await screen.findByText(/^Model replied/);
    expect(api.tests).toHaveLength(1);
    expect(off(testButton())).toBe(false);
  });

  it("takes the result away when the values are edited, and drops an answer that comes after an edit", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const api = mockApi();
    const { user } = await open();
    await user.click(testButton());
    await screen.findByText(/^Model replied/);
    await user.type(field("model"), "x");
    expect(screen.queryByText(/^Model replied/)).not.toBeInTheDocument();

    api.llmTest = async () => {
      await gate;
      return HttpResponse.json(llmTestOk);
    };
    await user.click(testButton());
    await waitFor(() => expect(api.tests).toHaveLength(2));
    await user.type(field("model"), "y");
    release();
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText(/^Model replied/)).not.toBeInTheDocument();
    expect(off(testButton())).toBe(false);
  });
});

describe("other sections", () => {
  it("lists the MCP servers as cards, and a new server is saved with the others", async () => {
    const api = mockApi();
    const { user } = await open("#/settings/mcp", "MCP servers");
    expect(screen.getByRole("button", { name: "test-server" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "second-server" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Dry run" })).toHaveAttribute("aria-checked", "false");
    await user.click(screen.getByRole("button", { name: "add" }));
    await user.type(screen.getAllByLabelText(/^name/, { selector: "input" }).at(-1)!, "third");
    await user.click(save());
    await screen.findByText("Saved and applied.");
    const servers = (api.puts[0] as { mcp: { servers: JsonObject[] } }).mcp.servers;
    expect(servers.map((s) => s.name)).toEqual(["test-server", "second-server", "third"]);
    expect(servers[2]).toMatchObject({ enabled: true, url: "", transport: "auto", timeout: "30s", default_policy: "approve", tool_filter: [], headers: [] });
  });

  it("tests the Telegram bot with the values in the form, in a mode of its own too", async () => {
    const api = mockApi();
    const { user } = await open("#/settings/outputs", "Outputs");
    await user.click(screen.getByRole("button", { name: "send test message" }));
    expect(await screen.findByText("Bot is reachable.")).toBeInTheDocument();
    expect(api.connTests[0].path).toBe("/api/settings/test/telegram?mode=send");
    expect(api.connTests[0].body).toMatchObject({ enabled: false, allowed_chat_ids: [1001, 1002], new_session_after: "6h" });
    await user.click(screen.getByRole("button", { name: "test" }));
    await waitFor(() => expect(api.connTests).toHaveLength(2));
    expect(api.connTests[1].path).toBe("/api/settings/test/telegram");
  });

  it("marks a duration that is no duration, and sends what was typed on save", async () => {
    const api = mockApi();
    const { user } = await open("#/settings/storage", "Data retention");
    await user.clear(field("retention"));
    await user.type(field("retention"), "soon");
    expect(field("retention")).toHaveAttribute("aria-invalid", "true");
    await user.clear(field("retention"));
    await user.type(field("retention"), "90d");
    expect(field("retention")).not.toHaveAttribute("aria-invalid");
    await user.click(save());
    await screen.findByText("Saved and applied.");
    expect((api.puts[0] as { storage: JsonObject }).storage.retention).toBe("90d");
  });

  it("flips a switch of the approvals and logs sections and saves it", async () => {
    const api = mockApi();
    const { user } = await open("#/settings/logs", "Logs");
    await user.click(screen.getByRole("switch", { name: "Store bodies" }));
    await user.click(save());
    await screen.findByText("Saved and applied.");
    expect((api.puts[0] as { logs: JsonObject }).logs.store_bodies).toBe(false);
    expect((api.puts[0] as { logs: JsonObject }).logs.llm_trace).toBe(true);
  });
});
