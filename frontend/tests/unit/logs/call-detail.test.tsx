// One LLM call in full: the header and its buttons, the facts, the notices, the timing bar, the parameters,
// the messages sent (folded), the tools, what came back, and the timeline of the turn it belongs to.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CallDetail } from "../../../src/pages/logs/CallDetail";
import { approxTokens } from "../../../src/pages/logs/format";
import { CHAT_ID, makeFullCall, type CallAnswer } from "../../mocks/fixtures/logs";
import { server } from "../../mocks/server";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Answer the call request with `makeFullCall(over)` and record the ids asked for. */
function serveCall(over: Partial<CallAnswer> = {}) {
  const ids: string[] = [];
  server.use(
    http.get("/api/logs/llm/:id", ({ params }) => {
      ids.push(String(params.id));
      return HttpResponse.json(makeFullCall({ ...over, id: Number(params.id) }));
    }),
  );
  return ids;
}

/** A loaded page. The timeline is left to the default handler unless a test sets its own. */
async function open(over: Partial<CallAnswer> = {}, id = 12) {
  serveCall(over);
  const view = render(<CallDetail id={id} />);
  await screen.findByRole("heading", { level: 1 });
  return view;
}

/** The value next to a name of the facts list. */
const fact = (name: string) => screen.getByText(name, { selector: "dt" }).nextElementSibling as HTMLElement;

/** The `<details>` of a fold, found by its title or its tag. */
const fold = (text: string | RegExp) => screen.getByText(text).closest("details") as HTMLDetailsElement;

/** The text of every closed or open fold's summary. */
const summaries = () => Array.from(document.querySelectorAll("details > summary")).map((s) => s.textContent);

describe("CallDetail: waiting and failing", () => {
  it("shows the way back, the call's number and Loading… while the call is on its way", () => {
    server.use(http.get("/api/logs/llm/:id", () => new Promise<Response>(() => undefined)));
    render(<CallDetail id={12} />);
    expect(screen.getByRole("heading", { level: 1, name: "Logs" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /all LLM calls/ })).toHaveAttribute("href", "#/logs/llm");
    expect(screen.getByText("call #12")).toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows the message of a failed request under the same header", async () => {
    server.use(http.get("/api/logs/llm/:id", () => HttpResponse.json({ detail: "Call 12 was cleaned up" }, { status: 404 })));
    render(<CallDetail id={12} />);
    expect(await screen.findByText("Call 12 was cleaned up")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /all LLM calls/ })).toHaveAttribute("href", "#/logs/llm");
    expect(screen.getByText("call #12")).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("asks for the call by its number", async () => {
    const ids = serveCall();
    render(<CallDetail id={12} />);
    await screen.findByRole("heading", { level: 1, name: "Chat" });
    expect(ids).toEqual(["12"]);
  });

  it("starts over for another call, showing Loading… until it is in", async () => {
    const view = await open();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.get("/api/logs/llm/:id", async ({ params }) => {
        await gate;
        return HttpResponse.json(makeFullCall({ id: Number(params.id), purpose: "job", link_kind: "job", link_id: "example-job" }));
      }),
    );
    view.rerender(<CallDetail id={13} />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.getByText("call #13")).toBeInTheDocument();
    release();
    expect(await screen.findByRole("heading", { level: 1, name: "Job" })).toBeInTheDocument();
    expect(screen.getByText("call #13")).toBeInTheDocument();
  });
});

describe("CallDetail: the header", () => {
  it("names the purpose, the number and the status, with the way back", async () => {
    await open();
    expect(screen.getByRole("heading", { level: 1, name: "Chat" })).toBeInTheDocument();
    expect(screen.getByText("call #12")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /all LLM calls/ })).toHaveAttribute("href", "#/logs/llm");
    expect(within(screen.getByRole("banner")).getByText("OK")).toBeInTheDocument();
  });

  it("says failed in the header for a failed call", async () => {
    await open({ status: "error", error: "x" });
    expect(within(screen.getByRole("banner")).getByText("failed")).toBeInTheDocument();
  });

  it("downloads the call as JSON from the export address", async () => {
    await open();
    const link = screen.getByRole("link", { name: "download" });
    expect(link).toHaveAttribute("href", "/api/logs/llm/12/export");
    expect(link).toHaveAttribute("download");
  });

  it("copies the whole call as JSON and says so for a moment", async () => {
    const user = userEvent.setup();
    const written: string[] = [];
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: async (t: string) => void written.push(t) } });
    await open();
    await user.click(screen.getByRole("button", { name: "copy as JSON" }));
    expect(await screen.findByRole("button", { name: "copied" })).toBeInTheDocument();
    expect(written).toHaveLength(1);
    expect(written[0]).toBe(JSON.stringify(JSON.parse(written[0]), null, 2));
    const copy = JSON.parse(written[0]);
    expect(copy.id).toBe(12);
    expect(copy.messages).toHaveLength(5);
    expect(copy.content).toBe("The disk on host-a is 61 percent full.");
    // back to the first label
    await waitFor(() => expect(screen.getByRole("button", { name: "copy as JSON" })).toBeInTheDocument(), { timeout: 3000 });
  });

  it("keeps its label when the clipboard refuses", async () => {
    const user = userEvent.setup();
    const attempts: string[] = [];
    vi.stubGlobal("navigator", {
      ...navigator,
      clipboard: {
        writeText: async (t: string) => {
          attempts.push(t);
          throw new Error("denied");
        },
      },
    });
    await open();
    await user.click(screen.getByRole("button", { name: "copy as JSON" }));
    await waitFor(() => expect(attempts).toHaveLength(1));
    expect(screen.getByRole("button", { name: "copy as JSON" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "copied" })).not.toBeInTheDocument();
  });
});

describe("CallDetail: the facts", () => {
  it("lists what the call was", async () => {
    await open();
    expect(screen.getByRole("heading", { level: 2, name: "Overview" })).toBeInTheDocument();
    expect(Array.from(document.querySelectorAll("dl.fd-kv dt")).map((d) => d.textContent)).toEqual(["when", "model", "endpoint", "tokens", "finish", "request", "belongs to"]);
    expect(fact("model")).toHaveTextContent("test-model");
    expect(fact("endpoint")).toHaveTextContent("http://host-a:8080/v1 · streamed");
    expect(fact("finish")).toHaveTextContent("stop");
    expect(fact("request")).toHaveTextContent("4.8 KB, 5 messages, 2 tools");
    expect(fact("when").textContent).toMatch(/\d/);
  });

  it("shows tokens in and out with the speed", async () => {
    await open();
    expect(fact("tokens")).toHaveTextContent(`${(1200).toLocaleString()} → 300 · 120 tokens/s`);
  });

  it("leaves the speed out when it is not known and marks an estimate", async () => {
    await open({ tokens_per_s: null, usage_estimated: true });
    expect(fact("tokens").textContent).toBe(`${(1200).toLocaleString()} → 300 (est.)`);
  });

  it("does not say streamed for a call that was not, and shows dashes for what is missing", async () => {
    await open({ stream: false, base_url: null, model: null, finish_reason: null, request_bytes: null, messages_count: null, tools_count: 0 });
    expect(fact("endpoint").textContent).toBe("—");
    expect(fact("model").textContent).toBe("—");
    expect(fact("finish").textContent).toBe("—");
    expect(fact("request").textContent).toBe("—, 0 messages");
  });

  it("names the chat it belongs to and links it", async () => {
    await open();
    expect(within(fact("belongs to")).getByRole("link", { name: "chat abcdef12" })).toHaveAttribute("href", `#/chat/${CHAT_ID}`);
  });

  it("names the job and step it belongs to", async () => {
    await open({ link_kind: "job", link_id: "example-job", link_step: 3 });
    expect(within(fact("belongs to")).getByRole("link", { name: "job example-job · step 3" })).toHaveAttribute("href", "#/jobs/example-job");
  });

  it("has no 'belongs to' for a call that belongs to nothing", async () => {
    await open({ link_kind: null, link_id: null });
    expect(screen.queryByText("belongs to", { selector: "dt" })).not.toBeInTheDocument();
  });

  it("says which attempt it was after the first", async () => {
    await open({ attempt: 3 });
    expect(fact("attempt")).toHaveTextContent("3 (earlier ones failed)");
  });

  it("does not mention attempts for the first", async () => {
    await open({ attempt: 1 });
    expect(screen.queryByText("attempt", { selector: "dt" })).not.toBeInTheDocument();
  });
});

describe("CallDetail: notices", () => {
  it("shows no notice for a call that went well", async () => {
    await open();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows the error of a failed call with its HTTP status", async () => {
    await open({ status: "error", http_status: 503, error: "Upstream returned 503: model is loading" });
    const notice = screen.getByRole("status");
    expect(within(notice).getByText("HTTP 503")).toBeInTheDocument();
    expect(within(notice).getByText("Upstream returned 503: model is loading")).toBeInTheDocument();
  });

  it("says the request failed when there was no HTTP status", async () => {
    await open({ status: "error", http_status: null, error: "Connection refused" });
    expect(within(screen.getByRole("status")).getByText("Request failed")).toBeInTheDocument();
    expect(screen.getByText("Connection refused")).toBeInTheDocument();
  });

  it("says only metadata was stored when the bodies are off, and shows 'Not stored.' for both sides", async () => {
    await open({ bodies: false, messages: null, tools: null, content: null, reasoning: null, tool_calls: null });
    expect(screen.getByText("Only metadata was stored for this call (full bodies are switched off in the log settings).")).toBeInTheDocument();
    expect(screen.getAllByText("Not stored.")).toHaveLength(2);
    expect(screen.getByText("0 messages")).toBeInTheDocument();
  });

  it("says which parts were cut by the size cap", async () => {
    await open({ truncated: ["messages", "content"] });
    expect(screen.getByText("Stored with a size cap: messages, content cut off. The marker “chars cut” shows where.")).toBeInTheDocument();
  });
});

describe("CallDetail: timing", () => {
  it("draws one bar with the wait, the first token and the generating, and says each", async () => {
    await open({ queued_ms: 30, ttft_ms: 400, total_ms: 2500 });
    expect(screen.getByRole("img", { name: "Waited 30 ms, To first token 400 ms, Generating 2.10 s" })).toBeInTheDocument();
    expect(screen.getByText("waited 30 ms")).toBeInTheDocument();
    expect(screen.getByText("to first token 400 ms")).toBeInTheDocument();
    expect(screen.getByText("generating 2.10 s")).toBeInTheDocument();
    expect(screen.getByText("total 2.53 s")).toBeInTheDocument();
  });

  it("leaves out a wait shorter than 5 ms", async () => {
    await open({ queued_ms: 4, ttft_ms: 400, total_ms: 2500 });
    expect(screen.getByRole("img", { name: "To first token 400 ms, Generating 2.10 s" })).toBeInTheDocument();
    expect(screen.queryByText(/^waited/)).not.toBeInTheDocument();
    expect(screen.getByText("total 2.50 s")).toBeInTheDocument();
  });

  it("shows one part for a call that never got a first token", async () => {
    await open({ queued_ms: 0, ttft_ms: null, total_ms: 90, status: "error", error: "x" });
    expect(screen.getByRole("img", { name: "Waiting for the reply 90 ms" })).toBeInTheDocument();
    expect(screen.getByText("waiting for the reply 90 ms")).toBeInTheDocument();
  });

  it("does not let the generating time go below nothing", async () => {
    await open({ queued_ms: 0, ttft_ms: 500, total_ms: 300 });
    expect(screen.getByRole("img", { name: "To first token 500 ms, Generating <1 ms" })).toBeInTheDocument();
  });

  it("treats missing times as nothing", async () => {
    await open({ queued_ms: null, ttft_ms: null, total_ms: null });
    expect(screen.getByRole("img", { name: "Waiting for the reply <1 ms" })).toBeInTheDocument();
  });
});

describe("CallDetail: parameters", () => {
  it("lists the parameters sent, without the response model", async () => {
    await open();
    const dim = (name: string) => screen.getByText(name, { selector: ".fd-text-dim, span" }).parentElement!;
    expect(dim("temperature")).toHaveTextContent("temperature 0.2");
    expect(dim("max_tokens")).toHaveTextContent("max_tokens 1024");
    expect(dim("tool_choice")).toHaveTextContent("tool_choice auto");
    expect(screen.queryByText("response_model")).not.toBeInTheDocument();
    expect(screen.queryByText(/ignored-model/)).not.toBeInTheDocument();
  });

  it("writes a value that is not text as JSON", async () => {
    await open({ params: { stop: ["a", "b"], extra: { x: 1 } }, finish_reason: "length" });
    expect(screen.getByText("stop").parentElement).toHaveTextContent('stop ["a","b"]');
    expect(screen.getByText("extra").parentElement).toHaveTextContent('extra {"x":1}');
  });

  it("shows nothing for a call with no parameters, or only a response model", async () => {
    await open({ params: { response_model: "m" } });
    expect(screen.queryByText("response_model")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".fd-text-dim")).toHaveLength(0);
  });
});

describe("CallDetail: what was sent", () => {
  it("names the panel and counts the messages", async () => {
    await open();
    expect(screen.getByRole("heading", { level: 2, name: "Sent to the model" })).toBeInTheDocument();
    expect(screen.getByText("5 messages")).toBeInTheDocument();
  });

  it("has a fold for each message with its role, its number, its size and a taste of the text", async () => {
    await open();
    const first = summaries().filter((s) => /^(system|user|assistant|tool)#/.test(s ?? ""));
    expect(first).toHaveLength(5);
    expect(first[0]).toBe(`system#130 chars · ~${approxTokens(30)} tokens · You are the monitor assistant.`);
    expect(first[1]).toBe(`user#218 chars · ~${approxTokens(18)} tokens · Is host-a healthy?`);
  });

  it("names the tool a tool message answers, in its size line", async () => {
    await open();
    expect(summaries().some((s) => s?.startsWith("tool#4check_host · 17 chars"))).toBe(true);
  });

  it("starts with only the last user message open", async () => {
    await open();
    expect(fold("Thanks, and the disk?").open).toBe(true);
    expect(fold(/^#2$/).open).toBe(false);
    expect(fold(/^#1$/).open).toBe(false);
    // a closed message builds no body: its text is in its summary, as a taste, and nowhere else
    expect(screen.queryByText("Is host-a healthy?")).not.toBeInTheDocument();
    expect(screen.getByText("Thanks, and the disk?")).toBeInTheDocument();
  });

  it("does not show the taste of an open message", async () => {
    await open();
    const open5 = fold(/^#5$/);
    expect(open5.querySelector("summary")!.textContent).not.toContain("Thanks");
  });

  it("starts with the last message open when it is a tool's", async () => {
    await open({
      messages: [
        { role: "user", content: "Check the host" },
        { role: "assistant", content: "", tool_calls: [{ id: "c1", function: { name: "check_host", arguments: "{}" } }] },
        { role: "tool", name: "check_host", tool_call_id: "c1", content: "all fine" },
      ],
    } as Partial<CallAnswer>);
    expect(fold(/^#3$/).open).toBe(true);
    expect(fold(/^#1$/).open).toBe(false);
    expect(screen.getByText("all fine")).toBeInTheDocument();
    expect(screen.getByText("answers call c1")).toBeInTheDocument();
  });

  it("starts with nothing open when the last message is the assistant's", async () => {
    await open({ messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "hello" }] } as Partial<CallAnswer>);
    expect(fold(/^#1$/).open).toBe(false);
    expect(fold(/^#2$/).open).toBe(false);
  });

  it("opens a message on its summary and shows its text exactly", async () => {
    const user = userEvent.setup();
    await open();
    await user.click(fold(/^#2$/).querySelector("summary")!);
    expect(await screen.findByText("Is host-a healthy?")).toBeInTheDocument();
    expect(fold(/^#2$/).open).toBe(true);
    await user.click(fold(/^#2$/).querySelector("summary")!);
    await waitFor(() => expect(screen.queryByText("Is host-a healthy?")).not.toBeInTheDocument());
  });

  it("shows the tool calls an assistant message asked for, with their arguments read as JSON", async () => {
    const user = userEvent.setup();
    await open();
    await user.click(fold(/^#3$/).querySelector("summary")!);
    const label = (await screen.findByText("tool calls requested")).closest(".fd-labeled") as HTMLElement;
    const json = JSON.parse(label.querySelector("pre")!.textContent!);
    expect(json).toEqual({ id: "call_1", name: "test-server__check_host", arguments: { host: "host-a" } });
  });

  it("keeps arguments that are not JSON as the text they are, and gives {} for none", async () => {
    await open({
      messages: [
        {
          role: "assistant",
          content: "",
          tool_calls: [
            { id: "a", function: { name: "broken", arguments: "not json" } },
            { id: "b", function: { name: "bare" } },
          ],
        },
        { role: "user", content: "go" },
      ],
    } as Partial<CallAnswer>);
    await userEvent.setup().click(screen.getByRole("button", { name: "expand all" }));
    const label = (await screen.findByText("tool calls requested")).closest(".fd-labeled") as HTMLElement;
    const parts = Array.from(label.querySelectorAll("pre")).map((p) => JSON.parse(p.textContent!));
    expect(parts).toEqual([
      { id: "a", name: "broken", arguments: "not json" },
      { id: "b", name: "bare", arguments: {} },
    ]);
  });

  it("shows no body for an empty message and says nothing of tool calls it did not make", async () => {
    await open({ messages: [{ role: "user", content: "" }] } as Partial<CallAnswer>);
    expect(fold(/^#1$/).open).toBe(true);
    expect(screen.queryByText("tool calls requested")).not.toBeInTheDocument();
    expect(summaries().find((s) => s?.startsWith("user#1"))).toBe(`user#10 chars · ~${approxTokens(0)} tokens`);
  });

  it("says 'Not stored.' when the messages were not kept", async () => {
    await open({ messages: null });
    expect(screen.getByText("0 messages")).toBeInTheDocument();
    expect(screen.getAllByText("Not stored.").length).toBeGreaterThan(0);
  });
});

describe("CallDetail: expand all and collapse all", () => {
  it("opens every fold, the reasoning and the tools among them", async () => {
    const user = userEvent.setup();
    await open();
    await user.click(screen.getByRole("button", { name: "expand all" }));
    await waitFor(() => expect(Array.from(document.querySelectorAll("details")).every((d) => d.open)).toBe(true));
    expect(screen.getByText("Is host-a healthy?")).toBeInTheDocument();
    expect(screen.getByText("The user asks about the disk, so check the host first.")).toBeInTheDocument();
    expect(screen.getByText("List the disks of a host.")).toBeInTheDocument();
  });

  it("closes every fold, the one that was open too", async () => {
    const user = userEvent.setup();
    await open();
    await user.click(screen.getByRole("button", { name: "collapse all" }));
    await waitFor(() => expect(Array.from(document.querySelectorAll("details")).every((d) => !d.open)).toBe(true));
    expect(screen.queryByText("Thanks, and the disk?")).not.toBeInTheDocument();
  });

  it("acts again when pressed twice, after a fold was closed by hand in between", async () => {
    const user = userEvent.setup();
    await open();
    await user.click(screen.getByRole("button", { name: "expand all" }));
    await waitFor(() => expect(fold(/^#2$/).open).toBe(true));
    await user.click(fold(/^#2$/).querySelector("summary")!);
    await waitFor(() => expect(fold(/^#2$/).open).toBe(false));
    await user.click(screen.getByRole("button", { name: "expand all" }));
    await waitFor(() => expect(fold(/^#2$/).open).toBe(true));
  });
});

describe("CallDetail: the tools", () => {
  it("has a fold that counts the tools and shows the size and the names", async () => {
    await open();
    const json = JSON.stringify(makeFullCall().tools, null, 2);
    expect(summaries()).toContain(`tools2 defined${json.length.toLocaleString()} chars · ~150 tokens · check_host, list_disks`);
  });

  it("estimates the tokens when the log did not count them", async () => {
    await open({ tools_tokens: null });
    const json = JSON.stringify(makeFullCall().tools, null, 2);
    expect(summaries().some((s) => s?.startsWith("tools2 defined") && s.includes(`~${approxTokens(json.length).toLocaleString()} tokens`))).toBe(true);
  });

  it("lists each tool with the first line of its description once opened", async () => {
    const user = userEvent.setup();
    await open();
    await user.click(fold("2 defined").querySelector("summary")!);
    const list = within((await screen.findByText("check_host", { selector: "dt" })).closest("dl") as HTMLElement);
    expect(list.getByText("Check one host.", { selector: "dd" })).toBeInTheDocument();
    expect(list.queryByText(/Second line/)).not.toBeInTheDocument();
    expect(list.getByText("list_disks", { selector: "dt" })).toBeInTheDocument();
    expect(list.getByText("List the disks of a host.", { selector: "dd" })).toBeInTheDocument();
  });

  it("cuts a long description at 140 characters", async () => {
    const user = userEvent.setup();
    await open({ tools: [{ type: "function", function: { name: "wordy", description: "x".repeat(300), parameters: {} } }] } as Partial<CallAnswer>);
    await user.click(fold("1 defined").querySelector("summary")!);
    expect(await screen.findByText("x".repeat(140))).toBeInTheDocument();
  });

  it("has no tools fold for a call without tools", async () => {
    await open({ tools: [], tools_count: 0 });
    expect(screen.queryByText(/defined$/)).not.toBeInTheDocument();
  });

  it("has no tools fold when the tools were not stored", async () => {
    await open({ tools: null });
    expect(screen.queryByText(/defined$/)).not.toBeInTheDocument();
  });
});

describe("CallDetail: what came back", () => {
  it("shows the answer with its length and the tool calls it asked for", async () => {
    await open();
    expect(screen.getByRole("heading", { level: 2, name: "Received" })).toBeInTheDocument();
    const answer = screen.getByText("answer · 38 chars").closest(".fd-labeled") as HTMLElement;
    expect(within(answer).getByText("The disk on host-a is 61 percent full.")).toBeInTheDocument();
    const calls = screen.getByText("tool calls · 1").closest(".fd-labeled") as HTMLElement;
    expect(JSON.parse(calls.querySelector("pre")!.textContent!)).toEqual({ id: "call_2", name: "list_disks", arguments: { host: "host-a" } });
  });

  it("has the reasoning in a fold of its own, closed until asked", async () => {
    const user = userEvent.setup();
    await open();
    const reasoning = makeFullCall().reasoning as string;
    const summary = summaries().find((s) => s?.startsWith("reasoning"));
    expect(summary).toBe(`reasoning${reasoning.length.toLocaleString()} chars~${approxTokens(reasoning.length)} tokens`);
    expect(screen.queryByText(reasoning)).not.toBeInTheDocument();
    await user.click(screen.getByText("reasoning").closest("summary")!);
    expect(await screen.findByText(reasoning)).toBeInTheDocument();
  });

  it("has no reasoning fold when there is none", async () => {
    await open({ reasoning: null });
    expect(screen.queryByText("reasoning")).not.toBeInTheDocument();
  });

  it("shows only the answer when the model made no tool calls", async () => {
    await open({ tool_calls: [] });
    expect(screen.getByText("answer · 38 chars")).toBeInTheDocument();
    expect(screen.queryByText(/^tool calls · /)).not.toBeInTheDocument();
  });

  it("shows only the tool calls when there is no answer text, and says nothing is missing", async () => {
    await open({ content: "" });
    expect(screen.queryByText(/^answer · /)).not.toBeInTheDocument();
    expect(screen.getByText("tool calls · 1")).toBeInTheDocument();
    expect(screen.queryByText(/empty answer|No answer/)).not.toBeInTheDocument();
  });

  it("says the model returned an empty answer when a successful call has nothing", async () => {
    await open({ content: "", reasoning: null, tool_calls: [] });
    expect(screen.getByText("The model returned an empty answer.")).toBeInTheDocument();
  });

  it("says no answer was received when a failed call has nothing", async () => {
    await open({ content: "", reasoning: null, tool_calls: [], status: "error", error: "x" });
    expect(screen.getByText("No answer was received.")).toBeInTheDocument();
  });

  it("does not call an answer empty when there is only reasoning", async () => {
    await open({ content: "", tool_calls: [] });
    expect(screen.queryByText(/empty answer|No answer/)).not.toBeInTheDocument();
  });

  it("shows a missing list of tool calls as none", async () => {
    await open({ tool_calls: null });
    expect(screen.queryByText(/^tool calls · /)).not.toBeInTheDocument();
    expect(screen.getByText("answer · 38 chars")).toBeInTheDocument();
  });
});

describe("CallDetail: a very long text", () => {
  const long = "abcdefghij".repeat(2500);

  it("cuts the text at 20,000 characters and offers the rest", async () => {
    await open({ content: long });
    const answer = screen.getByText(`answer · ${long.length.toLocaleString()} chars`).closest(".fd-labeled") as HTMLElement;
    expect(answer.querySelector("pre")!.textContent).toHaveLength(20_000);
    expect(within(answer).getByRole("button", { name: `show all ${long.length.toLocaleString()} characters` })).toBeInTheDocument();
  });

  it("shows all of it on request and drops the button", async () => {
    const user = userEvent.setup();
    await open({ content: long });
    await user.click(screen.getByRole("button", { name: /^show all/ }));
    const answer = screen.getByText(/^answer · /).closest(".fd-labeled") as HTMLElement;
    expect(answer.querySelector("pre")!.textContent).toHaveLength(long.length);
    expect(screen.queryByRole("button", { name: /^show all/ })).not.toBeInTheDocument();
  });

  it("does not cut a text of exactly 20,000 characters", async () => {
    await open({ content: "z".repeat(20_000) });
    expect(screen.queryByRole("button", { name: /^show all/ })).not.toBeInTheDocument();
  });
});

describe("CallDetail: the timeline of the turn", () => {
  it("shows the timeline of its own call under the overview", async () => {
    const asked: string[] = [];
    server.use(
      http.get("/api/logs/llm/:id/timeline", ({ params }) => {
        asked.push(String(params.id));
        return HttpResponse.json({
          selected: 12,
          entries: [
            { type: "llm", id: 11, ts: 1_700_000_000, link_step: 1, status: "ok", prompt_tokens: 1, completion_tokens: 1, ttft_ms: 1, total_ms: 10, tool_calls_count: 0, finish_reason: "stop" },
            { type: "llm", id: 12, ts: 1_700_000_001, link_step: 2, status: "ok", prompt_tokens: 1, completion_tokens: 1, ttft_ms: 1, total_ms: 10, tool_calls_count: 0, finish_reason: "stop" },
          ],
        });
      }),
    );
    await open();
    expect(await screen.findByRole("heading", { name: "Timeline" })).toBeInTheDocument();
    // (a timeline request left over from the test before may also reach this handler: only the id counts)
    expect(asked.length).toBeGreaterThan(0);
    expect(new Set(asked)).toEqual(new Set(["12"]));
    expect(screen.getByRole("link", { name: "call #11" })).toHaveAttribute("href", "#/logs/llm/11");
  });

  it("shows no timeline for a call that stands alone", async () => {
    server.use(http.get("/api/logs/llm/:id/timeline", () => HttpResponse.json({ selected: 12, entries: [] })));
    await open();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(screen.queryByRole("heading", { name: "Timeline" })).not.toBeInTheDocument();
  });
});
