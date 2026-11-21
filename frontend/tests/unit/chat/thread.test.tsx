import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { liveStream, runEvents, sseResponse } from "../../mocks/fixtures/chat";
import type { ApiResponse } from "../../mocks/types";
import { server } from "../../mocks/server";
import { renderChat } from "./render";

type Doc = ApiResponse<"/api/chat/sessions/{session_id}">;
type Message = Doc["messages"][number];

afterEach(() => {
  vi.unstubAllGlobals();
  history.replaceState(null, "", "#/");
});

let id = 100;
const msg = (m: Omit<Message, "id" | "ts">): Message => ({ id: id++, ts: 1, ...m });

/** A stored conversation "s-x" with these messages. */
function store(messages: Message[], cards: Doc["cards"] = []) {
  server.use(http.get("/api/chat/sessions/s-x", () => HttpResponse.json({ id: "s-x", title: "Example x", channel: "web", created: 1, updated: 1, cards, messages } satisfies Doc)));
}

describe("a stored conversation", () => {
  it("folds the tools an answer used: name, arguments and the start of the result", async () => {
    renderChat("#/chat/s-disk");
    const fold = (await screen.findByText("used a tool: shell")).closest("details")!;
    expect(fold).not.toHaveAttribute("open");
    expect(within(fold).getByText("shell")).toBeInTheDocument();
    expect(within(fold).getByText('{"command":"df -h"}')).toBeInTheDocument();
    expect(within(fold).getByText(/Filesystem\s+Size\s+Used/)).toBeInTheDocument();
  });

  it("names the tools of an answer that used several (MCP tools as server · tool, once each)", async () => {
    store([
      msg({ role: "user", content: "look around" }),
      msg({ role: "assistant", content: null, tool_calls: [{ id: "c1", function: { name: "test-server__list_files", arguments: {} } }, { id: "c2", function: { name: "shell", arguments: {} } }, { id: "c3", function: { name: "shell", arguments: {} } }] }),
      msg({ role: "tool", name: "shell", tool_call_id: "c2", content: "ok", meta: { ok: true } }),
      msg({ role: "assistant", content: "Done." }),
    ]);
    renderChat("#/chat/s-x");
    expect(await screen.findByText("used 3 tools: test-server · list_files, shell")).toBeInTheDocument();
    expect(screen.getByText("Done.")).toBeInTheDocument();
  });

  it("an answer with a trace has a link to it, the MCP domains it used, and what was unreachable", async () => {
    renderChat("#/chat/s-disk");
    const trace = await screen.findByRole("link", { name: "trace" });
    expect(trace).toHaveAttribute("href", "#/logs/llm/for/chat/s-disk/t-1");
    expect(trace.parentElement).toHaveTextContent("domains: files (picked), torrents (loaded) · server test-server unreachable");
  });

  it("says when the model did not mark commands read-only or write, and why labelling failed", async () => {
    renderChat("#/chat/s-telegram");
    const text = await screen.findByText("The model did not state read-only or write for 2 commands: they run with change rights");
    expect(text).toHaveAttribute("title", "Labelling failed: labelling timed out");
  });

  it("uses the singular for one unmarked command", async () => {
    store([
      msg({ role: "user", content: "q" }),
      msg({ role: "assistant", content: "a", meta: { trace_turn: "t-9", unmarked: { commands: 1 } } }),
    ]);
    renderChat("#/chat/s-x");
    const text = await screen.findByText("The model did not state read-only or write for 1 command: it runs with change rights");
    expect(text).not.toHaveAttribute("title");
  });

  it("an explain conversation shows its label, and its source data folded", async () => {
    store([
      msg({ role: "user", content: "Run 7 of Example job\nfinding: disk almost full", meta: { explain: { label: "Explain this run", run_id: 7, job: "example-job", finding: null } } }),
      msg({ role: "assistant", content: "The disk is nearly full." }),
    ]);
    renderChat("#/chat/s-x");
    expect(await screen.findByText("Explain this run")).toBeInTheDocument();
    const fold = screen.getByText("source data").closest("details")!;
    expect(fold).not.toHaveAttribute("open");
    expect(within(fold).getByText(/finding: disk almost full/)).toBeInTheDocument();
  });

  it("a typed message keeps its line breaks", async () => {
    store([msg({ role: "user", content: "line one\nline two" }), msg({ role: "assistant", content: "ok" })]);
    renderChat("#/chat/s-x");
    const text = await screen.findByText(/line one/);
    expect(text.textContent).toBe("line one\nline two");
    expect(text).toHaveClass("fd-keep");
  });

  it("a command the owner ran is a card with its exit code and output", async () => {
    store([
      msg({ role: "user", content: "", meta: { run: { command: "du -sh /var", mode: "read-only", exit_code: 0, duration_s: 0.5, stopped: "exit", truncated: false, output: "3.1G /var" } } }),
      msg({ role: "assistant", content: "That is 3 GB." }),
    ]);
    renderChat("#/chat/s-x");
    expect(await screen.findByText("you ran this command in the read-only shell")).toBeInTheDocument();
    expect(screen.getByText("du -sh /var")).toBeInTheDocument();
    expect(screen.getByText("exit code 0")).toBeInTheDocument();
    expect(screen.getByText("3.1G /var")).toBeInTheDocument();
    expect(screen.getByText("That is 3 GB.")).toBeInTheDocument();
  });

  it("a write command is said to run as root", async () => {
    store([msg({ role: "user", content: "", meta: { run: { command: "rm x", mode: "write", exit_code: 1, duration_s: 0.1, stopped: "exit", truncated: true, output: "no such file" } } })]);
    renderChat("#/chat/s-x");
    expect(await screen.findByText("you ran this command as root (it can change the server)")).toBeInTheDocument();
    expect(screen.getByText("exit code 1")).toBeInTheDocument();
    expect(screen.getByText("· output shortened")).toBeInTheDocument();
  });

  it("a command that could not run shows why", async () => {
    store([msg({ role: "user", content: "", meta: { run: { command: "ls", exit_code: null, duration_s: 0, stopped: "error", truncated: false, output: "the shell is not available" } } })]);
    renderChat("#/chat/s-x");
    expect(await screen.findByText("Could not run: the shell is not available")).toBeInTheDocument();
    expect(screen.queryByText(/exit code/)).not.toBeInTheDocument();
  });
});

describe("running a command from an answer", () => {
  const runButtons = () => screen.getAllByRole("button", { name: /^run/ });

  function runPosts(answer: () => Response | Promise<Response>) {
    const seen: { path: string; body: unknown }[] = [];
    server.use(
      http.post("/api/chat/sessions/:id/run", async ({ request, params }) => {
        seen.push({ path: `/api/chat/sessions/${String(params.id)}/run`, body: await request.json() });
        return answer();
      }),
    );
    return seen;
  }

  it("the answer's commands have run buttons: read-only, write and unmarked, each with its name", async () => {
    renderChat("#/chat/s-disk");
    await screen.findByText("/var/lib/docker", { selector: "code" });
    expect(runButtons().map((b) => b.getAttribute("aria-label"))).toEqual(["run", "run (changes the server)", "run (may change the server)"]);
  });

  it("run posts the command to the conversation's run endpoint, shows the card and the model's answer", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    const seen = runPosts(() => stream.response());
    renderChat("#/chat/s-disk");
    await screen.findByText("/var/lib/docker", { selector: "code" });
    await user.click(screen.getByRole("button", { name: "run" }));
    expect(seen).toEqual([{ path: "/api/chat/sessions/s-disk/run", body: { command: "# [read-only] the biggest directories\ndu -sh /var/lib/docker/*" } }]);

    // before the server says anything the card says it is running
    expect(await screen.findByText("running this command…")).toBeInTheDocument();
    for (const [type, data] of runEvents("du -sh /var/lib/docker/*").slice(0, 3)) stream.send(type, data);
    expect(await screen.findByText("you ran this command in the read-only shell")).toBeInTheDocument();
    expect(screen.getByText("exit code 0")).toBeInTheDocument();
    expect(screen.getByText(/3\.1G\s+\/var\/lib\/docker\/overlay2/)).toBeInTheDocument();
    expect(screen.getByText("Overlay storage is the biggest part.")).toBeInTheDocument();
    // the earlier answer stays above, and nothing is runnable while the new answer is written
    expect(screen.getByText("/var/lib/docker", { selector: "code" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^run/ })).not.toBeInTheDocument();
    stream.send("done", { session_id: "s-disk", message_id: 9, steps: 1, usage: {}, stopped: null, trace_turn: "t-run" });
    stream.close();
    // done: the stored conversation is shown again, its commands runnable again
    await waitFor(() => expect(screen.queryByText("running this command…")).not.toBeInTheDocument());
    await waitFor(() => expect(runButtons()).toHaveLength(3));
  });

  it("sends a write command as it is written, with its mark", async () => {
    const user = userEvent.setup();
    const seen = runPosts(() => sseResponse(runEvents("x")));
    renderChat("#/chat/s-disk");
    await user.click(await screen.findByRole("button", { name: "run (changes the server)" }));
    expect(seen[0].body).toEqual({ command: "# [write] remove unused images\ndocker image prune -f" });
  });

  it("a command that fails to start shows its error card", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    runPosts(() => stream.response());
    renderChat("#/chat/s-disk");
    await user.click(await screen.findByRole("button", { name: "run (may change the server)" }));
    stream.send("run", { command: "df -h", mode: "write", action_id: 3, exit_code: null, duration_s: 0, stopped: "error", truncated: false, output: "no change shell" });
    expect(await screen.findByText("Could not run: no change shell")).toBeInTheDocument();
    stream.send("error", { message: "The model could not answer." });
    expect(await screen.findByText("The model could not answer.")).toBeInTheDocument();
    stream.close();
  });

  it("there are no run buttons while a question is being answered", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    server.use(http.post("/api/chat", () => stream.response()));
    renderChat("#/chat/s-disk");
    await screen.findByText("/var/lib/docker", { selector: "code" });
    expect(runButtons()).toHaveLength(3);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "and now?{Enter}");
    await screen.findByText("thinking…");
    expect(screen.queryByRole("button", { name: /^run/ })).not.toBeInTheDocument();
    stream.send("done", { session_id: "s-disk", message_id: 2, steps: 1, usage: {}, stopped: null, trace_turn: "t-y" });
    stream.close();
    await waitFor(() => expect(runButtons()).toHaveLength(3));
  });
});
