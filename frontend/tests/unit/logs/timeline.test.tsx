// The timeline of a chat turn or an agent run: the panel that fetches it (and stays away for a call that
// stands alone) and the rows, with a tool call that opens to show what it was given and what it returned.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { TimelinePanel, TimelineView } from "../../../src/pages/logs/Timeline";
import type { Timeline } from "../../../src/pages/logs/types";
import { makeTimeline, makeToolCall, type TimelineAnswer, type TimelineItem } from "../../mocks/fixtures/logs";
import { server } from "../../mocks/server";

/** Whole seconds, so the sums of the bars are exact. */
const T0 = 1_700_000_000;

const llm = (over: Partial<TimelineItem> = {}): TimelineItem => ({
  type: "llm",
  id: 11,
  ts: T0,
  link_step: 1,
  status: "ok",
  prompt_tokens: 900,
  completion_tokens: 30,
  ttft_ms: 300,
  total_ms: 1000,
  tool_calls_count: 0,
  finish_reason: "stop",
  ...over,
});

const tool = (over: Partial<TimelineItem> = {}): TimelineItem => ({
  type: "tool",
  id: 7,
  ts: T0 + 1,
  link_step: 1,
  tool: "test-server__check_host",
  ok: true,
  duration_ms: 500,
  result_size: 1200,
  ...over,
});

/** Two seconds in all: an LLM call, a tool call of half a second, an LLM call. */
const twoSeconds = (selected = 12): TimelineAnswer => ({
  selected,
  entries: [llm({ tool_calls_count: 1, finish_reason: "tool_calls" }), tool(), llm({ id: 12, ts: T0 + 1, link_step: 2, prompt_tokens: 1200, completion_tokens: 300 })],
});

const view = (timeline: TimelineAnswer) => render(<TimelineView timeline={timeline as Timeline} />);

/** The row of the timeline that holds `text`. */
const rowOf = (text: string | RegExp) => within(screen.getByText(text).closest(".fd-row") as HTMLElement);

describe("TimelineView", () => {
  it("is a panel that counts the LLM calls and the tool calls and gives the span", () => {
    view(twoSeconds());
    expect(screen.getByRole("heading", { name: "Timeline" })).toBeInTheDocument();
    expect(screen.getByText("2 LLM calls, 1 tool call · 2.00 s")).toBeInTheDocument();
  });

  it("says 'call' in the singular for one of each", () => {
    view({ selected: 11, entries: [llm(), tool()] });
    expect(screen.getByText(/^1 LLM call, 1 tool call · /)).toBeInTheDocument();
  });

  it("says 'calls' for none", () => {
    view({ selected: 0, entries: [tool({ id: 1 }), tool({ id: 2 })] });
    expect(screen.getByText(/^0 LLM calls, 2 tool calls · /)).toBeInTheDocument();
  });

  it("ticks the axis at every quarter of the span", () => {
    view(twoSeconds());
    const labels = Array.from(document.querySelector(".fd-axis")!.querySelectorAll("span")).map((s) => s.textContent);
    expect(labels).toEqual(["0", "500 ms", "1.00 s", "1.50 s", "2.00 s"]);
  });

  it("shows a row for each entry in the order it came, naming the step of an LLM call", () => {
    view(twoSeconds());
    expect(screen.getAllByText(/^(LLM · step \d|LLM|tool)$/).map((e) => e.textContent)).toEqual(["LLM · step 1", "tool", "LLM · step 2"]);
  });

  it("names an LLM call with no step just 'LLM'", () => {
    view({ selected: 0, entries: [llm({ link_step: null }), llm({ id: 12, ts: T0 + 1, link_step: null })] });
    expect(screen.getAllByText("LLM")).toHaveLength(2);
  });

  it("marks the selected call 'this call' and links the others to their own page", () => {
    view(twoSeconds(12));
    expect(screen.getByText("this call")).toBeInTheDocument();
    expect(screen.getByText("this call").closest(".fd-row")).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("link", { name: "call #11" })).toHaveAttribute("href", "#/logs/llm/11");
    expect(screen.queryByRole("link", { name: "call #12" })).not.toBeInTheDocument();
  });

  it("does not mark a tool call as the selected one even when its number is the same", () => {
    view({ selected: 7, entries: [llm({ id: 11 }), tool({ id: 7 }), llm({ id: 12, ts: T0 + 1 })] });
    expect(screen.queryByText("this call")).not.toBeInTheDocument();
  });

  it("says what an LLM call used and what it did next", () => {
    view(twoSeconds(12));
    expect(screen.getByText("900 → 30 tokens · asked for 1 tool")).toBeInTheDocument();
    expect(screen.getByText("1,200 → 300 tokens · stop".replace("1,200", (1200).toLocaleString()))).toBeInTheDocument();
  });

  it("says 'tools' for several tool calls asked for and nothing for no reason given", () => {
    view({ selected: 0, entries: [llm({ tool_calls_count: 3 }), llm({ id: 12, ts: T0 + 1, finish_reason: null })] });
    expect(screen.getByText("900 → 30 tokens · asked for 3 tools")).toBeInTheDocument();
    expect(screen.getByText("900 → 30 tokens")).toBeInTheDocument();
  });

  it("shows dashes for tokens that were not counted", () => {
    view({ selected: 0, entries: [llm({ prompt_tokens: null, completion_tokens: null, finish_reason: null }), tool()] });
    expect(screen.getByText("— → — tokens")).toBeInTheDocument();
  });

  it("shows how long each entry took", () => {
    view(twoSeconds(12));
    expect(rowOf("tool").getByText("500 ms")).toBeInTheDocument();
    expect(rowOf("LLM · step 1").getByText("1.00 s")).toBeInTheDocument();
  });

  it("shows the size of what a tool returned, and nothing when it is not known", () => {
    view({ selected: 0, entries: [llm(), tool({ result_size: 1200 }), tool({ id: 8, ts: T0 + 1.5, result_size: null, tool: "other__tool" })] });
    expect(rowOf("test-server__check_host").getByText("1.2 KB")).toBeInTheDocument();
    expect(rowOf("other__tool").queryByText(/KB|MB| B$/)).not.toBeInTheDocument();
  });

  it("has a bar for each entry", () => {
    view(twoSeconds());
    expect(document.querySelectorAll(".fd-bar")).toHaveLength(3);
  });

  it("says a failed LLM call failed and adds 'failed' to the legend", () => {
    view({ selected: 0, entries: [llm({ status: "error", total_ms: 90 }), llm({ id: 12, ts: T0 + 1, link_step: 2 })] });
    expect(rowOf("LLM · step 1").getByText("failed")).toBeInTheDocument();
    expect(screen.getAllByText("failed")).toHaveLength(2);
  });

  it("says a cancelled LLM call was cancelled", () => {
    view({ selected: 0, entries: [llm({ status: "cancelled" }), llm({ id: 12, ts: T0 + 1, link_step: 2 })] });
    expect(rowOf("LLM · step 1").getByText("cancelled")).toBeInTheDocument();
  });

  it("says a failed tool call failed", () => {
    view({ selected: 0, entries: [llm(), tool({ ok: false })] });
    expect(rowOf("test-server__check_host").getByText("failed")).toBeInTheDocument();
  });

  it("keeps 'failed' out of the legend when everything went well", () => {
    view(twoSeconds());
    expect(screen.queryByText("failed")).not.toBeInTheDocument();
    expect(screen.getByText("waiting for the first token")).toBeInTheDocument();
    expect(screen.getByText("generating")).toBeInTheDocument();
    expect(screen.getByText("tool running")).toBeInTheDocument();
  });

  it("copes with entries that took no time at all", () => {
    view({ selected: 0, entries: [llm({ total_ms: null, ttft_ms: null }), tool({ duration_ms: null, ts: T0 })] });
    expect(screen.getByText(/^1 LLM call, 1 tool call · /)).toBeInTheDocument();
    expect(rowOf("LLM · step 1").getByText("<1 ms")).toBeInTheDocument();
    expect(rowOf("tool").getByText("<1 ms")).toBeInTheDocument();
  });
});

describe("TimelineView: a tool call", () => {
  it("opens on its button and shows the arguments and the result it was stored with", async () => {
    const user = userEvent.setup();
    view(twoSeconds());
    const button = screen.getByRole("button", { name: "test-server__check_host" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("arguments")).not.toBeInTheDocument();

    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    const args = (await screen.findByText("arguments")).closest(".fd-labeled") as HTMLElement;
    expect(args.querySelector("pre")).toHaveTextContent('{ "host": "host-a" }');
    const result = screen.getByText("result").closest(".fd-labeled") as HTMLElement;
    expect(result.querySelector("pre")?.textContent).toBe(JSON.stringify({ status: "fine", disks: 2 }, null, 2));
  });

  it("asks the server for the tool call by its number", async () => {
    const user = userEvent.setup();
    const seen: string[] = [];
    server.use(
      http.get("/api/logs/tools/:id", ({ params }) => {
        seen.push(String(params.id));
        return HttpResponse.json(makeToolCall());
      }),
    );
    view(twoSeconds());
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    await screen.findByText("arguments");
    expect(seen).toEqual(["7"]);
  });

  it("says Loading… until the answer is in", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/logs/tools/:id", () => new Promise<Response>(() => undefined)));
    view(twoSeconds());
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("closes again on a second press", async () => {
    const user = userEvent.setup();
    view(twoSeconds());
    const button = screen.getByRole("button", { name: "test-server__check_host" });
    await user.click(button);
    await screen.findByText("arguments");
    await user.click(button);
    expect(screen.queryByText("arguments")).not.toBeInTheDocument();
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("shows the message of a failed request", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/logs/tools/:id", () => HttpResponse.json({ detail: "Tool call 7 was purged" }, { status: 404 })));
    view(twoSeconds());
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    expect(await screen.findByText("Tool call 7 was purged")).toBeInTheDocument();
  });

  it("shows text results as they are, indenting those that are JSON", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/logs/tools/:id", () => HttpResponse.json(makeToolCall({ args: '{"a":1}' as never, result: "plain words" as never }))));
    view(twoSeconds());
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    const args = (await screen.findByText("arguments")).closest(".fd-labeled") as HTMLElement;
    expect(args.querySelector("pre")?.textContent).toBe('{\n  "a": 1\n}');
    expect((screen.getByText("result").closest(".fd-labeled") as HTMLElement).querySelector("pre")?.textContent).toBe("plain words");
  });

  it("shows a dash for arguments that were not stored", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/logs/tools/:id", () => HttpResponse.json(makeToolCall({ args: null }))));
    view(twoSeconds());
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    const args = (await screen.findByText("arguments")).closest(".fd-labeled") as HTMLElement;
    expect(args.querySelector("pre")?.textContent).toBe("—");
  });

  it("says when the result was cut at the storage cap", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/logs/tools/:id", () => HttpResponse.json(makeToolCall({ truncated: true }))));
    view(twoSeconds());
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    expect(await screen.findByText("result (cut at the storage cap)")).toBeInTheDocument();
  });

  it("calls the result of a failed tool call an error and shows what the tool said", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/logs/tools/:id", () => HttpResponse.json(makeToolCall({ ok: false, result: null, error: "Connection refused" }))));
    view({ selected: 0, entries: [llm(), tool({ ok: false })] });
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    const label = (await screen.findByText("error")).closest(".fd-labeled") as HTMLElement;
    expect(label.querySelector("pre")?.textContent).toBe("Connection refused");
    // said once: the error line under the boxes is only for an error that comes with a result
    expect(screen.getAllByText("Connection refused")).toHaveLength(1);
  });

  it("repeats the error under a stored result", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/logs/tools/:id", () => HttpResponse.json(makeToolCall({ ok: false, result: { partial: true }, error: "Timed out after 30 s" }))));
    view({ selected: 0, entries: [llm(), tool({ ok: false })] });
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    expect(await screen.findByText("Timed out after 30 s")).toBeInTheDocument();
    expect(screen.getByText("error")).toBeInTheDocument();
  });

  it("falls back to the error of the timeline entry when the stored call has none", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/logs/tools/:id", () => HttpResponse.json(makeToolCall({ ok: false, result: null, error: null }))));
    view({ selected: 0, entries: [llm(), tool({ ok: false, error: "Entry said: no route" })] });
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    const label = (await screen.findByText("error")).closest(".fd-labeled") as HTMLElement;
    expect(label.querySelector("pre")?.textContent).toBe("Entry said: no route");
  });

  it("shows a dash when a failed tool call has nothing to say", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/logs/tools/:id", () => HttpResponse.json(makeToolCall({ ok: false, result: null, error: null }))));
    view({ selected: 0, entries: [llm(), tool({ ok: false })] });
    await user.click(screen.getByRole("button", { name: "test-server__check_host" }));
    const label = (await screen.findByText("error")).closest(".fd-labeled") as HTMLElement;
    expect(label.querySelector("pre")?.textContent).toBe("—");
  });

  it("opens each tool call on its own", async () => {
    const user = userEvent.setup();
    view({ selected: 0, entries: [llm(), tool({ id: 7 }), tool({ id: 8, ts: T0 + 1.5, tool: "other__tool" })] });
    await user.click(screen.getByRole("button", { name: "other__tool" }));
    await screen.findByText("arguments");
    expect(screen.getAllByText("arguments")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "test-server__check_host" })).toHaveAttribute("aria-expanded", "false");
  });
});

describe("TimelinePanel", () => {
  it("asks for the timeline of its call and shows it", async () => {
    const seen: string[] = [];
    server.use(
      http.get("/api/logs/llm/:id/timeline", ({ params }) => {
        seen.push(String(params.id));
        return HttpResponse.json(makeTimeline({ selected: Number(params.id) }));
      }),
    );
    render(<TimelinePanel callId={12} />);
    expect(await screen.findByRole("heading", { name: "Timeline" })).toBeInTheDocument();
    expect(seen).toEqual(["12"]);
    expect(screen.getByText("this call")).toBeInTheDocument();
  });

  it("shows nothing for a call that stands alone", async () => {
    let asked = false;
    server.use(
      http.get("/api/logs/llm/:id/timeline", () => {
        asked = true;
        return HttpResponse.json({ selected: 12, entries: [llm({ id: 12 })] });
      }),
    );
    const { container } = render(<TimelinePanel callId={12} />);
    await waitFor(() => expect(asked).toBe(true));
    await new Promise((r) => setTimeout(r, 30));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows nothing when the call has no entries at all", async () => {
    server.use(http.get("/api/logs/llm/:id/timeline", () => HttpResponse.json({ selected: 12, entries: [] })));
    const { container } = render(<TimelinePanel callId={12} />);
    await new Promise((r) => setTimeout(r, 30));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows nothing, and no error, when the timeline cannot be read", async () => {
    let asked = false;
    server.use(
      http.get("/api/logs/llm/:id/timeline", () => {
        asked = true;
        return HttpResponse.json({ detail: "boom" }, { status: 500 });
      }),
    );
    const { container } = render(<TimelinePanel callId={12} />);
    await waitFor(() => expect(asked).toBe(true));
    await new Promise((r) => setTimeout(r, 30));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("boom")).not.toBeInTheDocument();
  });

  it("asks again for another call", async () => {
    const seen: string[] = [];
    server.use(
      http.get("/api/logs/llm/:id/timeline", ({ params }) => {
        seen.push(String(params.id));
        return HttpResponse.json(makeTimeline({ selected: Number(params.id) }));
      }),
    );
    const { rerender } = render(<TimelinePanel callId={11} />);
    await screen.findByRole("heading", { name: "Timeline" });
    rerender(<TimelinePanel callId={12} />);
    await waitFor(() => expect(seen).toEqual(["11", "12"]));
    // the selected row follows: call 12 is 'this call' now and 11 is a link
    await waitFor(() => expect(screen.getByRole("link", { name: "call #11" })).toBeInTheDocument());
  });
});
