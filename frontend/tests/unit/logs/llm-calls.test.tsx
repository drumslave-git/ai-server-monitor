// The LLM calls list: rows from the API, the filters and what they ask the server for, paging, the chat/job
// link, notices from the log settings, errors, and the reload on a stream event and on a timer.
// `/api/stream` is a fake EventSource (fake-event-source.ts).
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LlmCallsList } from "../../../src/pages/logs/LlmCalls";
import type { CallsLink } from "../../../src/pages/logs/parts";
import { CHAT_ID, callList, defaultCalls, makeCall, makeStats, nowSeconds, type CallSummary } from "../../mocks/fixtures/logs";
import { server } from "../../mocks/server";
import { FakeEventSource, installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();
afterEach(() => {
  location.hash = "";
  vi.restoreAllMocks();
});

const renderList = (link: CallsLink | null = null) => render(<LlmCallsList link={link} />);

/** Record the query of every list request; answer with `answer(query)` (the default fixtures when omitted). */
function recordList(answer: (q: URLSearchParams) => Response = () => HttpResponse.json(callList(defaultCalls()))) {
  const seen: URLSearchParams[] = [];
  server.use(
    http.get("/api/logs/llm", ({ request }) => {
      const q = new URL(request.url).searchParams;
      seen.push(q);
      return answer(q);
    }),
  );
  return seen;
}

/** A row of the table by the text of its link to the call. */
const rowOf = (link: string) => within(screen.getByRole("link", { name: link }).closest("tr")!);

/** `n` calls numbered from `first` downwards. */
const manyCalls = (n: number, first: number): CallSummary[] => Array.from({ length: n }, (_, i) => makeCall({ id: first - i, response_preview: `answer ${first - i}` }));

describe("LLM calls list: rows", () => {
  it("shows the Logs page head with the LLM calls tab selected", async () => {
    renderList();
    await screen.findByRole("table");
    expect(screen.getByRole("heading", { level: 1, name: "Logs" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "LLM calls" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "App log" })).toHaveAttribute("href", "#/logs/app");
  });

  it("has the six columns of a call", async () => {
    renderList();
    await screen.findByRole("table");
    const heads = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(heads).toEqual(["time", "purpose · model", "prompt or answer · where it ran", "status", "tokens in → out · bar = in", "timefirst tokentotal"]);
  });

  it("shows one row per call, the newest first as the server sent them", async () => {
    renderList();
    await screen.findByRole("table");
    const links = screen.getAllByRole("link", { name: /^(The disk|I will list|Upstream returned|Name this chat)/ }).map((a) => a.getAttribute("href"));
    expect(links).toEqual(["#/logs/llm/4", "#/logs/llm/3", "#/logs/llm/2", "#/logs/llm/1"]);
  });

  it("shows the answer of a call as a link that opens it, with its purpose, model, tokens and times", async () => {
    renderList();
    await screen.findByRole("table");
    const row = rowOf("The disk on host-a is 61 percent full.");
    expect(row.getByRole("link", { name: "The disk on host-a is 61 percent full." })).toHaveAttribute("href", "#/logs/llm/4");
    expect(row.getByText("chat")).toBeInTheDocument();
    expect(row.getByText("test-model")).toBeInTheDocument();
    expect(row.getByText("OK")).toBeInTheDocument();
    expect(row.getByText(`${(1200).toLocaleString()} → 300`)).toBeInTheDocument();
    expect(row.getByText("first 400 ms · total 2.50 s")).toBeInTheDocument();
  });

  it("says how many tool calls a call asked for and what it belongs to", async () => {
    renderList();
    await screen.findByRole("table");
    const row = rowOf("I will list the folder first.");
    expect(row.getByText("job · 1 tool call")).toBeInTheDocument();
    expect(row.getByText("test-worker")).toBeInTheDocument();
    expect(row.getByRole("link", { name: "job example-job · step 2" })).toHaveAttribute("href", "#/jobs/example-job");
  });

  it("links the chat a call belongs to", async () => {
    renderList();
    await screen.findByRole("table");
    expect(rowOf("The disk on host-a is 61 percent full.").getByRole("link", { name: "chat abcdef12" })).toHaveAttribute("href", `#/chat/${CHAT_ID}`);
  });

  it("puts the error of a failed call where the answer would be, with its status and retry number", async () => {
    renderList();
    await screen.findByRole("table");
    const row = rowOf("Upstream returned 503: model is loading");
    expect(row.getByRole("link", { name: "Upstream returned 503: model is loading" })).toHaveAttribute("href", "#/logs/llm/2");
    expect(row.getByText("failed")).toBeInTheDocument();
    expect(row.getByText("chat · retry 2")).toBeInTheDocument();
    // no tokens were counted: a dash, and no first token
    expect(row.getByText("—", { selector: ".fd-meta" })).toBeInTheDocument();
    expect(row.getByText("first — · total 90 ms")).toBeInTheDocument();
  });

  it("shows a cancelled call with its request as the text and no owner", async () => {
    renderList();
    await screen.findByRole("table");
    const row = rowOf("Name this chat");
    expect(row.getByText("cancelled")).toBeInTheDocument();
    expect(row.getByText("chat title")).toBeInTheDocument();
    expect(row.getAllByRole("link")).toHaveLength(1);
  });

  it("names a call by its number when nothing was stored to show", async () => {
    server.use(http.get("/api/logs/llm", () => HttpResponse.json(callList([makeCall({ id: 77, request_preview: null, response_preview: null })]))));
    renderList();
    expect(await screen.findByRole("link", { name: "call #77" })).toHaveAttribute("href", "#/logs/llm/77");
  });

  it("says a tool call in the plural for more than one", async () => {
    server.use(http.get("/api/logs/llm", () => HttpResponse.json(callList([makeCall({ tool_calls_count: 3 })]))));
    renderList();
    expect(await screen.findByText("chat · 3 tool calls")).toBeInTheDocument();
  });

  it("calls the purposes of removed features 'earlier feature'", async () => {
    server.use(http.get("/api/logs/llm", () => HttpResponse.json(callList([makeCall({ purpose: "benchmark", link_kind: "benchmark", link_id: "x" })]))));
    renderList();
    await screen.findByRole("table");
    expect(screen.getAllByText("earlier feature").length).toBeGreaterThan(0);
  });
});

describe("LLM calls list: counting", () => {
  it("says which calls of how many are shown", async () => {
    renderList();
    await screen.findByRole("table");
    expect(screen.getByText("1–4 of 4")).toBeInTheDocument();
  });

  it("asks for the first 50 calls", async () => {
    const seen = recordList();
    renderList();
    await screen.findByRole("table");
    expect(seen).toHaveLength(1);
    expect(seen[0].get("limit")).toBe("50");
    expect(seen[0].get("offset")).toBe("0");
    expect(seen[0].has("purpose")).toBe(false);
    expect(seen[0].has("status")).toBe(false);
    expect(seen[0].has("since")).toBe(false);
    expect(seen[0].has("q")).toBe(false);
  });

  it("pages older and newer, 50 at a time", async () => {
    const user = userEvent.setup();
    const seen = recordList((q) => {
      const offset = Number(q.get("offset"));
      return HttpResponse.json(callList(manyCalls(Math.min(50, 120 - offset), 120 - offset), { total: 120, offset }));
    });
    renderList();
    await screen.findByText("1–50 of 120");
    expect(screen.getByRole("button", { name: "newer" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "older" }));
    await screen.findByText("51–100 of 120");
    expect(seen.at(-1)?.get("offset")).toBe("50");
    expect(screen.getByRole("button", { name: "newer" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "older" }));
    await screen.findByText("101–120 of 120");
    expect(seen.at(-1)?.get("offset")).toBe("100");
    expect(screen.getByRole("button", { name: "older" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "newer" }));
    await screen.findByText("51–100 of 120");
    expect(seen.at(-1)?.get("offset")).toBe("50");
  });

  it("keeps the page it was paged to when the search box, unchanged, runs out its wait after the start", async () => {
    const user = userEvent.setup();
    const seen = recordList((q) => HttpResponse.json(callList(manyCalls(50, 120 - Number(q.get("offset"))), { total: 120, offset: Number(q.get("offset")) })));
    renderList();
    await screen.findByText("1–50 of 120");
    await user.click(screen.getByRole("button", { name: "older" })); // at once, before the 350 ms of the search have passed
    await screen.findByText("51–100 of 120");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 450));
    });
    expect(screen.getByText("51–100 of 120")).toBeInTheDocument();
    expect(seen.map((q) => q.get("offset"))).toEqual(["0", "50"]);
  });

  it("goes back to the first page when the search text changes", async () => {
    const user = userEvent.setup();
    const seen = recordList((q) => HttpResponse.json(callList(manyCalls(50, 120 - Number(q.get("offset"))), { total: 120, offset: Number(q.get("offset")) })));
    renderList();
    await screen.findByText("1–50 of 120");
    await user.click(screen.getByRole("button", { name: "older" }));
    await screen.findByText("51–100 of 120");
    await user.type(screen.getByRole("searchbox", { name: "Search in messages" }), "refused");
    await waitFor(() => expect(seen.at(-1)?.get("q")).toBe("refused"));
    expect(seen.at(-1)?.get("offset")).toBe("0");
  });

  it("has nothing older when everything fits on one page", async () => {
    renderList();
    await screen.findByRole("table");
    expect(screen.getByRole("button", { name: "older" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "newer" })).toBeDisabled();
  });

  it("says when a text search covered only the newest calls", async () => {
    server.use(http.get("/api/logs/llm", () => HttpResponse.json(callList(defaultCalls(), { complete: false }))));
    renderList();
    expect(await screen.findByText(/search covers the newest 2,000 calls/)).toBeInTheDocument();
  });

  it("does not say it for a complete search", async () => {
    renderList();
    await screen.findByRole("table");
    expect(screen.queryByText(/search covers the newest/)).not.toBeInTheDocument();
  });
});

describe("LLM calls list: filters", () => {
  it("offers the purposes the log holds, with their counts", async () => {
    renderList();
    const purpose = await screen.findByRole("combobox", { name: "Purpose" });
    await waitFor(() => expect(within(purpose).getAllByRole("option")).toHaveLength(4));
    expect(within(purpose).getAllByRole("option").map((o) => o.textContent)).toEqual(["all purposes", "Chat (2)", "Job (1)", "Chat title (1)"]);
  });

  it("asks for one purpose and goes back to the first page", async () => {
    const user = userEvent.setup();
    const seen = recordList((q) => HttpResponse.json(callList(manyCalls(50, 120 - Number(q.get("offset"))), { total: 120, offset: Number(q.get("offset")) })));
    renderList();
    await screen.findByText("1–50 of 120");
    await user.click(screen.getByRole("button", { name: "older" }));
    await screen.findByText("51–100 of 120");

    const purpose = screen.getByRole("combobox", { name: "Purpose" });
    await waitFor(() => expect(within(purpose).getByRole("option", { name: "Job (1)" })).toBeInTheDocument());
    await user.selectOptions(purpose, "job");
    await waitFor(() => expect(seen.at(-1)?.get("purpose")).toBe("job"));
    expect(seen.at(-1)?.get("offset")).toBe("0");
  });

  it("asks for one status", async () => {
    const user = userEvent.setup();
    const seen = recordList();
    renderList();
    await screen.findByRole("table");
    const status = screen.getByRole("combobox", { name: "Status" });
    expect(within(status).getAllByRole("option").map((o) => o.textContent)).toEqual(["any status", "OK", "failed", "cancelled"]);
    await user.selectOptions(status, "failed");
    await waitFor(() => expect(seen.at(-1)?.get("status")).toBe("error"));
    await user.selectOptions(status, "cancelled");
    await waitFor(() => expect(seen.at(-1)?.get("status")).toBe("cancelled"));
    await user.selectOptions(status, "");
    await waitFor(() => expect(seen.at(-1)?.has("status")).toBe(false));
  });

  it("asks only for calls since the start of the chosen time range", async () => {
    const user = userEvent.setup();
    const seen = recordList();
    renderList();
    await screen.findByRole("table");
    const range = screen.getByRole("combobox", { name: "Time range" });
    expect(within(range).getAllByRole("option").map((o) => o.textContent)).toEqual(["last hour", "last 24 hours", "last 7 days", "everything stored"]);
    expect(range).toHaveValue("all");

    const before = nowSeconds();
    await user.selectOptions(range, "1h");
    await waitFor(() => expect(seen.at(-1)?.has("since")).toBe(true));
    expect(Number(seen.at(-1)?.get("since"))).toBeGreaterThanOrEqual(before - 3600 - 1);
    expect(Number(seen.at(-1)?.get("since"))).toBeLessThanOrEqual(nowSeconds() - 3600 + 1);

    await user.selectOptions(range, "7d");
    await waitFor(() => expect(Number(seen.at(-1)?.get("since"))).toBeLessThan(before - 86_400 * 6));
    await user.selectOptions(range, "all");
    await waitFor(() => expect(seen.at(-1)?.has("since")).toBe(false));
  });

  it("searches the text a moment after the typing stops, with the text trimmed", async () => {
    const user = userEvent.setup();
    const seen = recordList();
    renderList();
    await screen.findByRole("table");
    await user.type(screen.getByRole("searchbox", { name: "Search in messages" }), "  disk usage ");
    // not at every key: the search goes out once
    await waitFor(() => expect(seen.at(-1)?.get("q")).toBe("disk usage"));
    expect(seen.filter((q) => q.has("q"))).toHaveLength(1);
  });

  it("clears the search again", async () => {
    const user = userEvent.setup();
    const seen = recordList();
    renderList();
    await screen.findByRole("table");
    const box = screen.getByRole("searchbox", { name: "Search in messages" });
    await user.type(box, "disk");
    await waitFor(() => expect(seen.at(-1)?.get("q")).toBe("disk"));
    await user.clear(box);
    await waitFor(() => expect(seen.at(-1)?.has("q")).toBe(false));
  });

  it("says no call matches when a filter leaves nothing", async () => {
    const user = userEvent.setup();
    recordList((q) => HttpResponse.json(callList(q.get("status") ? [] : defaultCalls())));
    renderList();
    await screen.findByRole("table");
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "failed");
    expect(await screen.findByText("No calls match these filters.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("explains an empty log that has no filter", async () => {
    server.use(http.get("/api/logs/llm", () => HttpResponse.json(callList([]))));
    renderList();
    expect(await screen.findByText(/No LLM calls recorded yet/)).toBeInTheDocument();
    expect(screen.queryByText("No calls match these filters.")).not.toBeInTheDocument();
  });

  it("keeps the newest answer when an older request answers late", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    let n = 0;
    server.use(
      http.get("/api/logs/llm", async () => {
        const mine = ++n;
        if (mine === 1) await gate;
        return HttpResponse.json(callList([makeCall({ id: mine, response_preview: mine === 1 ? "the late answer" : "the newest answer" })]));
      }),
    );
    renderList();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Status" }), "ok");
    expect(await screen.findByRole("link", { name: "the newest answer" })).toBeInTheDocument();
    release();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.getByRole("link", { name: "the newest answer" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "the late answer" })).not.toBeInTheDocument();
  });
});

describe("LLM calls list: the log settings", () => {
  it("says when logging is switched off", async () => {
    server.use(http.get("/api/logs/llm/stats", () => HttpResponse.json(makeStats({ enabled: false }))));
    renderList();
    expect(await screen.findByText("LLM call logging is switched off. New calls are not recorded.")).toBeInTheDocument();
  });

  it("says when only metadata is stored", async () => {
    server.use(http.get("/api/logs/llm/stats", () => HttpResponse.json(makeStats({ store_bodies: false }))));
    renderList();
    expect(await screen.findByText(/Only metadata is stored/)).toBeInTheDocument();
  });

  it("has no notice for a log that records everything", async () => {
    renderList();
    await screen.findByRole("table");
    await screen.findByRole("option", { name: "Chat (2)" });
    expect(screen.queryByText(/switched off/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Only metadata/)).not.toBeInTheDocument();
  });

  it("still lists the calls when the statistics cannot be read", async () => {
    server.use(http.get("/api/logs/llm/stats", () => HttpResponse.json({ detail: "no stats" }, { status: 500 })));
    renderList();
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.queryByText("no stats")).not.toBeInTheDocument();
    expect(within(screen.getByRole("combobox", { name: "Purpose" })).getAllByRole("option")).toHaveLength(1);
  });
});

describe("LLM calls list: errors", () => {
  it("shows the message of a failed request and no table", async () => {
    server.use(http.get("/api/logs/llm", () => HttpResponse.json({ detail: "The log database is locked" }, { status: 500 })));
    renderList();
    expect(await screen.findByText("The log database is locked")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says the status when the server gives no reason", async () => {
    server.use(http.get("/api/logs/llm", () => new HttpResponse(null, { status: 502 })));
    renderList();
    expect(await screen.findByText("HTTP 502")).toBeInTheDocument();
  });

  it("shows nothing but the head while the first answer is on its way", async () => {
    server.use(http.get("/api/logs/llm", () => new Promise<Response>(() => undefined)));
    renderList();
    expect(screen.getByRole("heading", { name: "Logs" })).toBeInTheDocument();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(/No LLM calls recorded yet/)).not.toBeInTheDocument();
  });
});

describe("LLM calls list: calls of one chat or job", () => {
  it("asks for the calls of the owner and names it in the head", async () => {
    const seen = recordList((q) => HttpResponse.json(callList(defaultCalls().filter((c) => c.link_kind === q.get("link_kind")), { total: 2 })));
    renderList({ kind: "chat", id: CHAT_ID });
    await screen.findByText(/Calls belonging to/);
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    expect(seen[0].get("link_kind")).toBe("chat");
    expect(seen[0].get("link_id")).toBe(CHAT_ID);
    expect(seen[0].has("turn")).toBe(false);
    expect(screen.getAllByRole("link", { name: "chat abcdef12" }).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "show all" })).toHaveAttribute("href", "#/logs/llm");
  });

  it("stays on the list when the owner has several calls", async () => {
    renderList({ kind: "chat", id: CHAT_ID });
    await screen.findByRole("table");
    expect(location.hash).toBe("");
  });

  it("opens the call when the owner has only one", async () => {
    recordList(() => HttpResponse.json(callList([makeCall({ id: 31 })])));
    renderList({ kind: "job", id: "example-job" });
    await waitFor(() => expect(location.hash).toBe("#/logs/llm/31"));
  });

  it("opens the last call of a chat turn, where the detail carries the whole timeline", async () => {
    const seen = recordList(() => HttpResponse.json(callList([makeCall({ id: 42 }), makeCall({ id: 41 }), makeCall({ id: 40 })])));
    renderList({ kind: "chat", id: CHAT_ID, turn: "turn-1" });
    await waitFor(() => expect(location.hash).toBe("#/logs/llm/40"));
    expect(seen[0].get("turn")).toBe("turn-1");
  });

  it("says no call matches when the owner has none", async () => {
    recordList(() => HttpResponse.json(callList([])));
    renderList({ kind: "chat", id: CHAT_ID });
    expect(await screen.findByText("No calls match these filters.")).toBeInTheDocument();
    expect(location.hash).toBe("");
  });
});

describe("LLM calls list: staying current", () => {
  it("reloads the list and the statistics when a call is logged", async () => {
    const seen = recordList();
    renderList();
    await screen.findByRole("table");
    expect(seen).toHaveLength(1);
    act(() => FakeEventSource.current().emit("llm.call", { id: 5 }));
    await waitFor(() => expect(seen).toHaveLength(2));
  });

  it("ignores other stream events", async () => {
    const seen = recordList();
    renderList();
    await screen.findByRole("table");
    act(() => FakeEventSource.current().emit("job.started", {}));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(seen).toHaveLength(1);
  });

  it("does not reload for a new call while an older page is open", async () => {
    const user = userEvent.setup();
    const seen = recordList((q) => HttpResponse.json(callList(manyCalls(50, 120 - Number(q.get("offset"))), { total: 120, offset: Number(q.get("offset")) })));
    renderList();
    await screen.findByText("1–50 of 120");
    await user.click(screen.getByRole("button", { name: "older" }));
    await screen.findByText("51–100 of 120");
    const before = seen.length;
    act(() => FakeEventSource.current().emit("llm.call", {}));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(seen).toHaveLength(before);
  });

  it("reloads every 30 seconds", async () => {
    const timers = vi.spyOn(globalThis, "setInterval");
    const seen = recordList();
    renderList();
    await screen.findByRole("table");
    const poll = timers.mock.calls.find((c) => c[1] === 30_000);
    expect(poll).toBeDefined();
    act(() => (poll![0] as () => void)());
    await waitFor(() => expect(seen).toHaveLength(2));
  });
});
