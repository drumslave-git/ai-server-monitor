// #/settings/mcp/<server> on a phone: the linked server's card scrolls into view when it appears, and once more
// after the page has stopped changing size (what loads after it, such as the shell's data or the text size, can
// push it under the bottom bar). The page's size is watched with a ResizeObserver; happy-dom has none, so a fake
// one stands in and the test says when the page "grew".
import { act, render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SettingsDoc } from "../../../src/api";
import { CARD_SETTLED_MS } from "../../../src/kit";
import { SettingsPage } from "../../../src/pages/Settings";
import { server } from "../../mocks/server";

/** A small invented settings document: one section, MCP servers, three entries. */
const doc: SettingsDoc = {
  sections: { mcp: "MCP servers" },
  values: {
    mcp: {
      servers: [
        { name: "server-a", enabled: true, url: "http://mcp-a.test/mcp" },
        { name: "server-b", enabled: true, url: "http://mcp-b.test/mcp" },
        { name: "server-c", enabled: false, url: "http://mcp-c.test/sse" },
      ],
    },
  },
  schema: {
    type: "object",
    properties: { mcp: { $ref: "#/$defs/McpSettings" } },
    $defs: {
      McpServerSettings: {
        type: "object",
        properties: {
          name: { type: "string", default: "", description: "Short name." },
          enabled: { type: "boolean", default: true, description: "Off = never connected." },
          url: { type: "string", default: "", description: "MCP endpoint." },
        },
      },
      McpSettings: {
        type: "object",
        properties: {
          servers: { type: "array", items: { $ref: "#/$defs/McpServerSettings" }, default: [], test: "mcp", description: "MCP servers." },
        },
      },
    },
  },
};

/** The ResizeObservers made while the page is shown: what each watches, and a way to tell it the size changed. */
const observers: { targets: Element[]; disconnected: boolean; changed: () => void }[] = [];
class FakeResizeObserver {
  private entry: (typeof observers)[number];
  constructor(callback: ResizeObserverCallback) {
    this.entry = { targets: [], disconnected: false, changed: () => callback([], this as unknown as ResizeObserver) };
    observers.push(this.entry);
  }
  observe(target: Element) {
    this.entry.targets.push(target);
  }
  unobserve() {}
  disconnect() {
    this.entry.disconnected = true;
  }
}

/** Which element each scrollIntoView call scrolled. */
const scrolled: Element[] = [];

beforeEach(() => {
  observers.length = 0;
  scrolled.length = 0;
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  vi.stubGlobal("scrollTo", vi.fn());
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    writable: true,
    value: function (this: Element) {
      scrolled.push(this);
    },
  });
  server.use(http.get("/api/settings", () => HttpResponse.json(doc)));
});
afterEach(() => {
  vi.useRealTimers();
  history.replaceState(null, "", "#");
  vi.unstubAllGlobals();
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

/** The card of an MCP server, found by its toggle. */
const card = (name: string) => screen.getByRole("button", { name }).closest("section")!;

describe("a link to one MCP server in Settings", () => {
  it("scrolls the card into view, and once more after the page has kept its size", async () => {
    history.replaceState(null, "", "#/settings/mcp/server-b");
    render(<SettingsPage />);
    await screen.findByRole("button", { name: "server-b" });

    // the first scroll, as the card appears; the page's size is watched from then on
    expect(scrolled).toEqual([card("server-b")]);
    expect(observers).toHaveLength(1);
    expect(observers[0].targets).toEqual([document.documentElement]);

    vi.useFakeTimers();
    // the page grows twice (data arriving): each change starts the wait again
    act(() => observers[0].changed());
    act(() => vi.advanceTimersByTime(CARD_SETTLED_MS - 1));
    act(() => observers[0].changed());
    act(() => vi.advanceTimersByTime(CARD_SETTLED_MS - 1));
    expect(scrolled).toHaveLength(1);

    // it has kept its size long enough: the card scrolls into view again, and the watching ends
    act(() => vi.advanceTimersByTime(1));
    expect(scrolled).toEqual([card("server-b"), card("server-b")]);
    expect(observers[0].disconnected).toBe(true);

    // one extra scroll only
    act(() => vi.advanceTimersByTime(CARD_SETTLED_MS * 4));
    expect(scrolled).toHaveLength(2);
  });

  it("the page that has not changed at all also gets the one extra scroll (the observer's first report starts the wait)", async () => {
    history.replaceState(null, "", "#/settings/mcp/server-c");
    render(<SettingsPage />);
    await screen.findByRole("button", { name: "server-c" });
    vi.useFakeTimers();
    act(() => observers[0].changed()); // a ResizeObserver reports the size it first sees
    act(() => vi.advanceTimersByTime(CARD_SETTLED_MS));
    expect(scrolled).toEqual([card("server-c"), card("server-c")]);
  });

  it("the section without a server in the address scrolls no card and watches nothing", async () => {
    history.replaceState(null, "", "#/settings/mcp");
    render(<SettingsPage />);
    await screen.findByRole("button", { name: "server-a" });
    expect(scrolled).toEqual([]);
    expect(observers).toHaveLength(0);
  });

  it("leaving the page before it settles ends the watching, and nothing scrolls later", async () => {
    history.replaceState(null, "", "#/settings/mcp/server-a");
    const view = render(<SettingsPage />);
    await screen.findByRole("button", { name: "server-a" });
    vi.useFakeTimers();
    act(() => observers[0].changed());
    view.unmount();
    expect(observers[0].disconnected).toBe(true);
    act(() => vi.advanceTimersByTime(CARD_SETTLED_MS * 2));
    expect(scrolled).toHaveLength(1);
  });
});
