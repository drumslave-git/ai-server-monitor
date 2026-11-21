// The app log: the records, the filters and what they ask the server for, the summary over the loaded records,
// the traceback fold, older records, and the live tail (a second EventSource, /api/logs/app/stream, faked).
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AppLogView } from "../../../src/pages/logs/AppLog";
import { appLog, defaultLoggers, defaultRecords, makeRecord, type AppRecord } from "../../mocks/fixtures/logs";
import { server } from "../../mocks/server";
import { FakeEventSource, installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();
beforeEach(() => {
  // AppLog opens its own EventSource and live.ts is not involved here: start every test from none.
  FakeEventSource.instances.length = 0;
});
afterEach(() => {
  location.hash = "";
});

const tails = () => FakeEventSource.instances.filter((s) => s.url.startsWith("/api/logs/app/stream"));
const tail = () => tails().at(-1)!;
const tailQuery = () => new URL(tail().url, "http://localhost").searchParams;
const openTails = () => tails().filter((s) => !s.closed);
const openQuery = () => new URL(openTails()[0].url, "http://localhost").searchParams;
const waitForTail = (count = 1) => waitFor(() => expect(tails()).toHaveLength(count));

/** Record the query of every log request and answer with `answer(query)` (the default records when omitted). */
function recordLog(answer: (q: URLSearchParams) => Response = (q) => HttpResponse.json(appLog(defaultRecords().filter((r) => r.levelno >= (q.get("level") === "INFO" ? 20 : 0)), "memory"))) {
  const seen: URLSearchParams[] = [];
  server.use(
    http.get("/api/logs/app", ({ request }) => {
      const q = new URL(request.url).searchParams;
      seen.push(q);
      return answer(q);
    }),
  );
  return seen;
}

/** Wait for the first page: the table and the 'connected' tail have no meaning before it. */
const loaded = () => screen.findByRole("table");

/** The messages of the rows, top to bottom (a row with a traceback has its button's label after the message). */
const messages = () => within(screen.getByRole("table")).getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[3].textContent);

/** The number a counter of the summary shows. */
const counter = (label: string) => screen.getByText(label, { selector: ".fd-counter-label" }).parentElement!.querySelector(".fd-counter-value")!.textContent;

/** A record at a fixed minute so a test knows which cell of the strip it lands in. */
const BASE = Date.UTC(2026, 10, 20, 12, 0, 10) / 1000;
const at = (seconds: number, over: Partial<AppRecord> = {}) => makeRecord({ ts: BASE + seconds, ...over });
const error = (seq: number, seconds: number) => at(seconds, { seq, level: "ERROR", levelno: 40, message: `error ${seq}` });
const info = (seq: number, seconds: number) => at(seconds, { seq, message: `info ${seq}` });

/** `n` records, newest first, numbered from `first` downwards. */
const manyRecords = (n: number, first: number): AppRecord[] => Array.from({ length: n }, (_, i) => makeRecord({ seq: first - i, ts: BASE + first - i, message: `record ${first - i}` }));

describe("app log: rows", () => {
  it("shows the Logs head with the App log tab selected", async () => {
    render(<AppLogView />);
    await loaded();
    expect(screen.getByRole("tab", { name: "App log" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "LLM calls" })).toHaveAttribute("href", "#/logs/llm");
  });

  it("has the four columns of a record", async () => {
    render(<AppLogView />);
    await loaded();
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["time", "level", "logger", "message"]);
  });

  it("shows the records newest first, from INFO up by default", async () => {
    render(<AppLogView />);
    await loaded();
    expect(messages()).toEqual(["Model call failedtraceback", "Server test-server is slow", "Scheduler started"]);
  });

  it("shows the level, the logger without the package name and the time of each record", async () => {
    const ts = BASE + 5;
    server.use(http.get("/api/logs/app", () => HttpResponse.json(appLog([at(5, { seq: 1, level: "WARNING", levelno: 30, logger: "ai_server_monitor.mcp.client", message: "slow" })]))));
    render(<AppLogView />);
    await loaded();
    const row = within(screen.getByText("slow").closest("tr")!);
    expect(row.getByText("WARN")).toBeInTheDocument();
    expect(row.getByText("mcp.client")).toHaveAttribute("title", "ai_server_monitor.mcp.client");
    expect(row.getByText(new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false }))).toBeInTheDocument();
  });

  it("keeps the whole name of a logger outside the package", async () => {
    server.use(http.get("/api/logs/app", () => HttpResponse.json(appLog([at(0, { seq: 1, logger: "uvicorn.error", message: "up" })]))));
    render(<AppLogView />);
    await loaded();
    expect(screen.getByText("uvicorn.error", { selector: "td span" })).toBeInTheDocument();
  });

  it("says nothing matches when there are no records", async () => {
    server.use(http.get("/api/logs/app", () => HttpResponse.json(appLog([]))));
    render(<AppLogView />);
    expect(await screen.findByText("Nothing matches.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("Loaded records")).not.toBeInTheDocument();
  });

  it("shows nothing but the head before the first answer", async () => {
    server.use(http.get("/api/logs/app", () => new Promise<Response>(() => undefined)));
    render(<AppLogView />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing matches.")).not.toBeInTheDocument();
  });

  it("shows the message of a failed request", async () => {
    server.use(http.get("/api/logs/app", () => HttpResponse.json({ detail: "The log is unreadable" }, { status: 500 })));
    render(<AppLogView />);
    expect(await screen.findByText("The log is unreadable")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("opens and closes the traceback of a record that has one", async () => {
    const user = userEvent.setup();
    render(<AppLogView />);
    await loaded();
    expect(screen.queryByText(/ConnectError: refused/)).not.toBeInTheDocument();
    // only the failed call has a traceback
    expect(screen.getAllByRole("button", { name: "traceback" })).toHaveLength(1);

    const open = screen.getByRole("button", { name: "traceback" });
    expect(open).toHaveAttribute("aria-expanded", "false");
    await user.click(open);
    expect(screen.getByRole("button", { name: "hide traceback" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/ConnectError: refused/)).toBeInTheDocument();
    expect(screen.getByText(/File "llm.py", line 10/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "hide traceback" }));
    expect(screen.queryByText(/ConnectError: refused/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "traceback" })).toBeInTheDocument();
  });
});

describe("app log: filters", () => {
  it("asks the in-memory log for 200 records from INFO up", async () => {
    const seen = recordLog();
    render(<AppLogView />);
    await loaded();
    expect(seen).toHaveLength(1);
    expect(seen[0].get("source")).toBe("memory");
    expect(seen[0].get("limit")).toBe("200");
    expect(seen[0].get("level")).toBe("INFO");
    expect(seen[0].has("logger")).toBe(false);
    expect(seen[0].has("q")).toBe(false);
    expect(seen[0].has("before")).toBe(false);
  });

  it("offers the four minimum levels", async () => {
    render(<AppLogView />);
    await loaded();
    const level = screen.getByRole("combobox", { name: "Minimum level" });
    expect(level).toHaveValue("INFO");
    expect(within(level).getAllByRole("option").map((o) => o.textContent)).toEqual(["all levels", "info and above", "warning and above", "error and above"]);
  });

  it("asks for a higher minimum level", async () => {
    const user = userEvent.setup();
    const seen = recordLog();
    render(<AppLogView />);
    await loaded();
    await user.selectOptions(screen.getByRole("combobox", { name: "Minimum level" }), "ERROR");
    await waitFor(() => expect(seen.at(-1)?.get("level")).toBe("ERROR"));
  });

  it("leaves the level out when all levels are wanted, and then counts the debug lines", async () => {
    const user = userEvent.setup();
    const seen = recordLog();
    render(<AppLogView />);
    await loaded();
    expect(screen.queryByText("debug", { selector: ".fd-counter-label" })).not.toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Minimum level" }), "DEBUG");
    await waitFor(() => expect(seen).toHaveLength(2));
    expect(seen[1].has("level")).toBe(false);
    await screen.findByText("HTTP request sent");
    expect(counter("debug")).toBe("1");
  });

  it("lists the loggers the log holds, without the package name", async () => {
    render(<AppLogView />);
    await loaded();
    const logger = screen.getByRole("combobox", { name: "Logger" });
    await waitFor(() => expect(within(logger).getAllByRole("option")).toHaveLength(1 + defaultLoggers.length));
    expect(within(logger).getAllByRole("option").map((o) => o.textContent)).toEqual(["all loggers", "llm", "mcp", "scheduler", "httpx"]);
  });

  it("asks for one logger by its whole name", async () => {
    const user = userEvent.setup();
    const seen = recordLog();
    render(<AppLogView />);
    await loaded();
    const logger = screen.getByRole("combobox", { name: "Logger" });
    await waitFor(() => expect(within(logger).getAllByRole("option")).toHaveLength(5));
    await user.selectOptions(logger, "ai_server_monitor.mcp");
    await waitFor(() => expect(seen.at(-1)?.get("logger")).toBe("ai_server_monitor.mcp"));
    await user.selectOptions(logger, "");
    await waitFor(() => expect(seen.at(-1)?.has("logger")).toBe(false));
  });

  it("works with an empty list of loggers when it cannot be read", async () => {
    server.use(http.get("/api/logs/app/loggers", () => HttpResponse.json({ detail: "x" }, { status: 500 })));
    render(<AppLogView />);
    await loaded();
    expect(within(screen.getByRole("combobox", { name: "Logger" })).getAllByRole("option")).toHaveLength(1);
  });

  it("searches the text a moment after the typing stops", async () => {
    const user = userEvent.setup();
    const seen = recordLog();
    render(<AppLogView />);
    await loaded();
    await user.type(screen.getByRole("searchbox", { name: "Search the log" }), "  refused ");
    await waitFor(() => expect(seen.at(-1)?.get("q")).toBe("refused"));
    expect(seen.filter((q) => q.has("q"))).toHaveLength(1);
  });

  it("offers the two sources and asks the stored log for its own records", async () => {
    const user = userEvent.setup();
    const seen = recordLog();
    render(<AppLogView />);
    await loaded();
    const source = screen.getByRole("combobox", { name: "Source" });
    expect(within(source).getAllByRole("option").map((o) => o.textContent)).toEqual(["since the last start", "saved warnings and errors"]);
    await user.selectOptions(source, "stored");
    await waitFor(() => expect(seen.at(-1)?.get("source")).toBe("stored"));
  });

  it("reads the loggers again for the other source", async () => {
    const user = userEvent.setup();
    let loggerReads = 0;
    server.use(
      http.get("/api/logs/app/loggers", () => {
        loggerReads += 1;
        return HttpResponse.json(defaultLoggers);
      }),
    );
    render(<AppLogView />);
    await loaded();
    expect(loggerReads).toBe(1);
    await user.selectOptions(screen.getByRole("combobox", { name: "Source" }), "stored");
    await waitFor(() => expect(loggerReads).toBe(2));
  });

  it("shows the saved records of the stored log", async () => {
    const user = userEvent.setup();
    render(<AppLogView />);
    await loaded();
    await user.selectOptions(screen.getByRole("combobox", { name: "Source" }), "stored");
    await waitFor(() => expect(messages()).toEqual(["Model call failedtraceback", "Server test-server is slow"]));
  });
});

describe("app log: the summary of the loaded records", () => {
  it("counts the records by level and says how many are loaded", async () => {
    render(<AppLogView />);
    await loaded();
    expect(screen.getByText("Loaded records")).toBeInTheDocument();
    expect(screen.getByText("3 on this page, counted by level")).toBeInTheDocument();
    expect(counter("info")).toBe("1");
    expect(counter("warn")).toBe("1");
    expect(counter("error")).toBe("1");
  });

  it("counts several records of one level", async () => {
    server.use(http.get("/api/logs/app", () => HttpResponse.json(appLog([error(5, 4), error(4, 3), error(3, 2), info(2, 1), info(1, 0)]))));
    render(<AppLogView />);
    await loaded();
    expect(counter("error")).toBe("3");
    expect(counter("info")).toBe("2");
    expect(counter("warn")).toBe("0");
    expect(screen.getByText("5 on this page, counted by level")).toBeInTheDocument();
  });

  it("draws a strip with one cell for each minute the records span, marked by the worst level in it", async () => {
    // three minutes: an info and an error in the first, nothing in the second, a warning in the third
    server.use(
      http.get("/api/logs/app", () =>
        HttpResponse.json(appLog([at(125, { seq: 4, level: "WARNING", levelno: 30 }), error(3, 20), info(2, 10), info(1, 0)])),
      ),
    );
    render(<AppLogView />);
    await loaded();
    const strip = screen.getByRole("img", { name: /^Worst level logged in each stretch of one minute, from .+ to .+/ });
    const cells = Array.from(strip.querySelectorAll(".fd-cell")).map((c) => c.className.replace("fd-cell", "").trim());
    expect(cells).toEqual(["fd-cell-crit", "", "fd-cell-warn"]);
  });

  it("leaves a cell empty when a minute logged nothing above debug", async () => {
    const debugOnly = [at(0, { seq: 2, level: "DEBUG", levelno: 10 }), at(1, { seq: 1, level: "DEBUG", levelno: 10 })];
    server.use(http.get("/api/logs/app", () => HttpResponse.json(appLog(debugOnly))));
    const user = userEvent.setup();
    render(<AppLogView />);
    await loaded();
    await user.selectOptions(screen.getByRole("combobox", { name: "Minimum level" }), "DEBUG");
    await waitFor(() => expect(counter("debug")).toBe("2"));
    const cells = screen.getByRole("img", { name: /Worst level/ }).querySelectorAll(".fd-cell");
    expect(cells).toHaveLength(1);
    expect(cells[0].className).toBe("fd-cell");
  });

  it("makes a cell cover several minutes when the records span more than 60", async () => {
    // 130 minutes: three minutes to a cell
    server.use(http.get("/api/logs/app", () => HttpResponse.json(appLog([info(2, 130 * 60), info(1, 0)]))));
    render(<AppLogView />);
    await loaded();
    const strip = screen.getByRole("img", { name: /^Worst level logged in each stretch of 3 minutes, from/ });
    expect(strip.querySelectorAll(".fd-cell")).toHaveLength(44);
    expect(screen.getByText(/one cell = 3 minutes/)).toBeInTheDocument();
  });

  it("labels the ends of the strip with the time of the oldest and the newest minute", async () => {
    server.use(http.get("/api/logs/app", () => HttpResponse.json(appLog([info(2, 125), info(1, 0)]))));
    render(<AppLogView />);
    await loaded();
    const axis = document.querySelector(".fd-axis")!;
    const [from, to] = Array.from(axis.querySelectorAll("span")).map((s) => s.textContent);
    expect(from).toMatch(/\d/);
    expect(to).toMatch(/\d/);
    expect(from).not.toBe(to);
    expect(screen.getByText("one cell = one minute · colour = the worst level logged in it")).toBeInTheDocument();
  });
});

describe("app log: older records", () => {
  it("offers older records only when a whole page of 200 came", async () => {
    render(<AppLogView />);
    await loaded();
    expect(screen.queryByRole("button", { name: "older records" })).not.toBeInTheDocument();
  });

  it("asks for the records before the last one shown and appends them", async () => {
    const user = userEvent.setup();
    const seen = recordLog((q) => HttpResponse.json(appLog(q.has("before") ? manyRecords(5, Number(q.get("before")) - 1) : manyRecords(200, 300))));
    render(<AppLogView />);
    await loaded();
    expect(screen.getByText("200 on this page, counted by level")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "older records" }));
    await waitFor(() => expect(screen.getByText("205 on this page, counted by level")).toBeInTheDocument());
    expect(seen.at(-1)?.get("before")).toBe("101");
    expect(seen.at(-1)?.get("source")).toBe("memory");
    expect(messages().at(0)).toBe("record 300");
    expect(messages().at(-1)).toBe("record 96");
    // a short page was the last one
    expect(screen.queryByRole("button", { name: "older records" })).not.toBeInTheDocument();
  });

  it("keeps offering older records while pages come full", async () => {
    const user = userEvent.setup();
    recordLog((q) => HttpResponse.json(appLog(manyRecords(200, q.has("before") ? Number(q.get("before")) - 1 : 600))));
    render(<AppLogView />);
    await loaded();
    await user.click(screen.getByRole("button", { name: "older records" }));
    await waitFor(() => expect(screen.getByText("400 on this page, counted by level")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "older records" })).toBeInTheDocument();
  });

  it("pages the stored log by the id of its last record", async () => {
    const user = userEvent.setup();
    const stored = (first: number, n: number) => manyRecords(n, first).map((r) => ({ ...r, id: r.seq, seq: null }));
    const seen = recordLog((q) => HttpResponse.json(appLog(q.has("before") ? stored(Number(q.get("before")) - 1, 3) : stored(500, 200), "stored")));
    render(<AppLogView />);
    await loaded();
    await user.selectOptions(screen.getByRole("combobox", { name: "Source" }), "stored");
    await waitFor(() => expect(seen.at(-1)?.get("source")).toBe("stored"));
    await user.click(await screen.findByRole("button", { name: "older records" }));
    await waitFor(() => expect(screen.getByText("203 on this page, counted by level")).toBeInTheDocument());
    expect(seen.at(-1)?.get("before")).toBe("301");
  });

  it("shows the error of a failed page of older records, the way a failed first page is shown, and keeps the rows and the button", async () => {
    const user = userEvent.setup();
    recordLog((q) => (q.has("before") ? HttpResponse.json({ detail: "the log is not readable" }, { status: 500 }) : HttpResponse.json(appLog(manyRecords(200, 300)))));
    render(<AppLogView />);
    await loaded();
    await user.click(screen.getByRole("button", { name: "older records" }));
    expect(await screen.findByText("the log is not readable")).toBeInTheDocument();
    expect(screen.getByText("200 on this page, counted by level")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "older records" })).toBeInTheDocument();
  });

  it("shows that error by the older records button, in the same row, and not at the top of the page", async () => {
    const user = userEvent.setup();
    recordLog((q) => (q.has("before") ? HttpResponse.json({ detail: "the log is not readable" }, { status: 500 }) : HttpResponse.json(appLog(manyRecords(200, 300)))));
    render(<AppLogView />);
    await loaded();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const button = screen.getByRole("button", { name: "older records" });
    await user.click(button);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("the log is not readable");
    expect(alert.parentElement).toBe(button.parentElement);
  });

  it("takes the message away when the next page of older records comes", async () => {
    const user = userEvent.setup();
    let fail = true;
    recordLog((q) => {
      if (!q.has("before")) return HttpResponse.json(appLog(manyRecords(200, 300)));
      if (fail) return HttpResponse.json({ detail: "the log is not readable" }, { status: 500 });
      return HttpResponse.json(appLog(manyRecords(3, 100)));
    });
    render(<AppLogView />);
    await loaded();
    await user.click(screen.getByRole("button", { name: "older records" }));
    await screen.findByText("the log is not readable");
    fail = false;
    await user.click(screen.getByRole("button", { name: "older records" }));
    await waitFor(() => expect(screen.queryByText("the log is not readable")).not.toBeInTheDocument());
  });

  it("asks for nothing when the last record has no number to page from", async () => {
    const user = userEvent.setup();
    const noNumber = Array.from({ length: 200 }, (_, i) => makeRecord({ seq: null, id: null, ts: BASE + 200 - i, message: `row ${i}` }));
    const seen = recordLog(() => HttpResponse.json(appLog(noNumber)));
    render(<AppLogView />);
    await loaded();
    await user.click(screen.getByRole("button", { name: "older records" }));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(seen).toHaveLength(1);
  });
});

describe("app log: the live tail", () => {
  it("opens once the first page is in, asking for what is newer than it, with the same filters", async () => {
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    expect(tailQuery().get("after")).toBe("4");
    expect(tailQuery().get("level")).toBe("INFO");
    expect(tailQuery().has("logger")).toBe(false);
    expect(tailQuery().has("q")).toBe(false);
  });

  it("does not open before the first page is in", async () => {
    server.use(http.get("/api/logs/app", () => new Promise<Response>(() => undefined)));
    render(<AppLogView />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(tails()).toHaveLength(0);
  });

  it("opens no tail with the old starting point while the page of a changed filter is on its way, and then starts from the new one", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    let requests = 0;
    server.use(
      http.get("/api/logs/app", async ({ request }) => {
        requests += 1;
        if (new URL(request.url).searchParams.get("level") !== "ERROR") return HttpResponse.json(appLog(defaultRecords()));
        await gate;
        return HttpResponse.json(appLog([makeRecord({ seq: 9, message: "only error", level: "ERROR", levelno: 40 })]));
      }),
    );
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    expect(tailQuery().get("after")).toBe("4");

    await user.selectOptions(screen.getByRole("combobox", { name: "Minimum level" }), "ERROR");
    await waitFor(() => expect(requests).toBe(2));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    // the first tail is closed and no other one was opened (it would have started after record 4 with the new filter)
    expect(tails()).toHaveLength(1);
    expect(openTails()).toHaveLength(0);

    release();
    await waitForTail(2);
    expect(openTails()).toHaveLength(1);
    expect(openQuery().get("level")).toBe("ERROR");
    expect(openQuery().get("after")).toBe("9");
  });

  it("starts from the beginning when the log is empty", async () => {
    server.use(http.get("/api/logs/app", () => HttpResponse.json(appLog([]))));
    render(<AppLogView />);
    await screen.findByText("Nothing matches.");
    await waitForTail();
    expect(tailQuery().has("after")).toBe(false);
  });

  it("says whether it is connected", async () => {
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    expect(screen.getByText("not connected")).toBeInTheDocument();
    act(() => tail().open());
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.queryByText("not connected")).not.toBeInTheDocument();
    act(() => tail().fail());
    expect(screen.getByText("not connected")).toBeInTheDocument();
  });

  it("puts the new records on top, the newest first", async () => {
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    act(() => tail().emit("log", { records: [info(5, 100), error(6, 101)], skipped: 0 }));
    expect(messages().slice(0, 3)).toEqual(["error 6", "info 5", "Model call failedtraceback"]);
    expect(screen.getByText("5 on this page, counted by level")).toBeInTheDocument();
    expect(counter("error")).toBe("2");
  });

  it("says when the server left records out, adding up what it left out", async () => {
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    expect(screen.queryByText(/rate-limited/)).not.toBeInTheDocument();
    act(() => tail().emit("log", { records: [info(5, 100)], skipped: 1200 }));
    expect(screen.getByText(`The live view is rate-limited: ${(1200).toLocaleString()} records were left out. Reload to see them all.`)).toBeInTheDocument();
    act(() => tail().emit("log", { records: [], skipped: 30 }));
    expect(screen.getByText(`The live view is rate-limited: ${(1230).toLocaleString()} records were left out. Reload to see them all.`)).toBeInTheDocument();
  });

  it("says nothing of skipped records when none were", async () => {
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    act(() => tail().emit("log", { records: [info(5, 100)], skipped: 0 }));
    expect(screen.queryByText(/rate-limited/)).not.toBeInTheDocument();
  });

  it("ignores a frame it cannot read", async () => {
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    act(() => tail().emit("log", "not json {"));
    expect(messages()).toHaveLength(3);
    expect(screen.queryByText(/rate-limited/)).not.toBeInTheDocument();
  });

  it("keeps at most 1,500 records", async () => {
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    act(() => tail().emit("log", { records: manyRecords(1600, 2000), skipped: 0 }));
    expect(screen.getByText(`${(1500).toLocaleString()} on this page, counted by level`)).toBeInTheDocument();
  });

  it("stops when Live is switched off and starts a new tail when it is switched on", async () => {
    const user = userEvent.setup();
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    const first = tail();
    act(() => first.open());
    expect(screen.getByRole("switch", { name: "Live" })).toHaveAttribute("aria-checked", "true");

    await user.click(screen.getByRole("switch", { name: "Live" }));
    expect(first.closed).toBe(true);
    expect(screen.getByRole("switch", { name: "Live" })).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByText("connected")).not.toBeInTheDocument();
    expect(screen.queryByText("not connected")).not.toBeInTheDocument();

    await user.click(screen.getByRole("switch", { name: "Live" }));
    await waitForTail(2);
    expect(tail()).not.toBe(first);
    expect(tail().closed).toBe(false);
  });

  it("starts a new tail with the new filters when a filter changes, and closes the old one", async () => {
    const user = userEvent.setup();
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    const first = tail();
    await user.selectOptions(screen.getByRole("combobox", { name: "Minimum level" }), "ERROR");
    // the page of the new filter is in: only errors are left
    await waitFor(() => expect(messages()).toEqual(["Model call failedtraceback"]));
    expect(first.closed).toBe(true);
    expect(openTails()).toHaveLength(1);
    expect(openQuery().get("level")).toBe("ERROR");
  });

  it("carries the search text and the logger to the tail", async () => {
    const user = userEvent.setup();
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    const logger = screen.getByRole("combobox", { name: "Logger" });
    await waitFor(() => expect(within(logger).getAllByRole("option")).toHaveLength(5));
    await user.selectOptions(logger, "ai_server_monitor.llm");
    await user.type(screen.getByRole("searchbox", { name: "Search the log" }), "refused");
    await waitFor(() => expect(tailQuery().get("q")).toBe("refused"));
    expect(tailQuery().get("logger")).toBe("ai_server_monitor.llm");
  });

  it("leaves the level out of the tail when all levels are shown", async () => {
    const user = userEvent.setup();
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    await user.selectOptions(screen.getByRole("combobox", { name: "Minimum level" }), "DEBUG");
    await waitFor(() => expect(messages()).toHaveLength(4));
    expect(openTails()).toHaveLength(1);
    expect(openQuery().has("level")).toBe(false);
  });

  it("has no tail and no Live switch for the stored log", async () => {
    const user = userEvent.setup();
    render(<AppLogView />);
    await loaded();
    await waitForTail();
    const first = tail();
    await user.selectOptions(screen.getByRole("combobox", { name: "Source" }), "stored");
    await waitFor(() => expect(first.closed).toBe(true));
    await waitFor(() => expect(messages()).toHaveLength(2));
    expect(screen.queryByRole("switch", { name: "Live" })).not.toBeInTheDocument();
    expect(tails()).toHaveLength(1);
  });

  it("closes the tail when the page is left", async () => {
    const { unmount } = render(<AppLogView />);
    await loaded();
    await waitForTail();
    const first = tail();
    unmount();
    expect(first.closed).toBe(true);
  });
});

