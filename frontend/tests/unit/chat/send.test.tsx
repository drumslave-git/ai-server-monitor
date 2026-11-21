import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { answerEvents, frame, liveStream, sessionList, sseResponse } from "../../mocks/fixtures/chat";
import { server } from "../../mocks/server";
import { renderChat } from "./render";

afterEach(() => {
  vi.unstubAllGlobals();
  history.replaceState(null, "", "#/");
});

/** Records the bodies POSTed to /api/chat; the answer is the default one unless given. */
function chatPosts(answer: () => Response | Promise<Response> = () => sseResponse(answerEvents())) {
  const bodies: unknown[] = [];
  server.use(
    http.post("/api/chat", async ({ request }) => {
      bodies.push(await request.json());
      return answer();
    }),
  );
  return bodies;
}

const box = () => screen.getByRole("textbox", { name: "Message" });
const sendButton = () => screen.getByRole("button", { name: "send" });

describe("composer", () => {
  it("the send button is disabled while the box is empty or only whitespace", async () => {
    const user = userEvent.setup();
    renderChat("#/chat/new");
    expect(sendButton()).toHaveAttribute("aria-disabled", "true");
    await user.type(box(), "   ");
    expect(sendButton()).toHaveAttribute("aria-disabled", "true");
    await user.type(box(), "hi");
    expect(sendButton()).not.toHaveAttribute("aria-disabled");
  });

  it("pressing the disabled send button sends nothing", async () => {
    const user = userEvent.setup();
    const bodies = chatPosts();
    renderChat("#/chat/new");
    await user.click(sendButton());
    expect(bodies).toEqual([]);
  });

  it("Enter sends the trimmed message of a new conversation (no session id) and clears the box", async () => {
    const user = userEvent.setup();
    const bodies = chatPosts();
    renderChat("#/chat/new");
    await user.type(box(), "  How much memory is free?  {Enter}");
    expect(bodies).toEqual([{ message: "How much memory is free?", session_id: null }]);
    expect(box()).toHaveValue("");
    expect(await screen.findByText("How much memory is free?")).toBeInTheDocument();
  });

  it("Shift+Enter adds a line break and does not send", async () => {
    const user = userEvent.setup();
    const bodies = chatPosts();
    renderChat("#/chat/new");
    await user.type(box(), "first{Shift>}{Enter}{/Shift}second");
    expect(box()).toHaveValue("first\nsecond");
    expect(bodies).toEqual([]);
  });

  it("a multi-line message is sent with its line break", async () => {
    const user = userEvent.setup();
    const bodies = chatPosts();
    renderChat("#/chat/new");
    await user.type(box(), "first{Shift>}{Enter}{/Shift}second{Enter}");
    expect(bodies).toEqual([{ message: "first\nsecond", session_id: null }]);
  });

  it("the send button sends the message too", async () => {
    const user = userEvent.setup();
    const bodies = chatPosts();
    renderChat("#/chat/new");
    await user.type(box(), "ping");
    await user.click(sendButton());
    expect(bodies).toEqual([{ message: "ping", session_id: null }]);
  });

  it("Enter that confirms an input-method candidate (keyCode 229) is not a send", async () => {
    const user = userEvent.setup();
    const bodies = chatPosts();
    renderChat("#/chat/new");
    await user.type(box(), "konnichiwa");
    fireEvent.keyDown(box(), { key: "Enter", keyCode: 229 });
    expect(bodies).toEqual([]);
    expect(box()).toHaveValue("konnichiwa");
  });

  it("a message in an open conversation carries its session id and joins the thread", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    const bodies = chatPosts(() => stream.response());
    renderChat("#/chat/s-disk");
    await screen.findByText("What is using the disk on host-a?");
    await user.type(box(), "and the memory?{Enter}");
    expect(bodies).toEqual([{ message: "and the memory?", session_id: "s-disk" }]);
    expect(await screen.findByText("and the memory?")).toBeInTheDocument();
    expect(screen.getByText("What is using the disk on host-a?")).toBeInTheDocument();
    stream.send("session", { session_id: "s-disk" });
    stream.send("token", { text: "The memory is fine." });
    expect(await screen.findByText("The memory is fine.")).toBeInTheDocument();
    expect(screen.getByText("What is using the disk on host-a?")).toBeInTheDocument();
    stream.send("done", { session_id: "s-disk", message_id: 2, steps: 1, usage: {}, stopped: null, trace_turn: "t-x" });
    stream.close();
  });
});

describe("the streamed answer", () => {
  it("shows thinking, then the answer as it arrives, then the stored conversation", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    chatPosts(() => stream.response());
    renderChat("#/chat/new");
    await user.type(box(), "How much memory is free?{Enter}");

    expect(await screen.findByText("thinking…")).toBeInTheDocument();
    stream.send("session", { session_id: "s-new" });
    stream.send("token", { text: "About 12 GB " });
    expect(await screen.findByText(/About 12 GB/)).toBeInTheDocument();
    expect(screen.queryByText("thinking…")).not.toBeInTheDocument();
    expect(screen.getByText("answering…")).toBeInTheDocument();
    // the answer is not runnable while it is written, and the composer offers stop
    expect(screen.getByRole("button", { name: "stop" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "send" })).not.toBeInTheDocument();

    stream.send("token", { text: "of memory is free." });
    expect(await screen.findByText("About 12 GB of memory is free.")).toBeInTheDocument();
    stream.send("done", { session_id: "s-new", message_id: 2, steps: 1, usage: {}, stopped: null, trace_turn: "t-new" });
    stream.close();

    await waitFor(() => expect(screen.queryByText("answering…")).not.toBeInTheDocument());
    expect(await screen.findByRole("button", { name: "send" })).toBeInTheDocument();
    // the stored answer replaces the live one: it has the trace link
    expect(await screen.findByRole("link", { name: "trace" })).toHaveAttribute("href", "#/logs/llm/for/chat/s-new/t-new");
    expect(screen.getByText("About 12 GB of memory is free.")).toBeInTheDocument();
  });

  it("the page address becomes the new conversation's once the session event arrives", async () => {
    const user = userEvent.setup();
    chatPosts();
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    await waitFor(() => expect(location.hash).toBe("#/chat/s-new"));
    expect(await within(screen.getByRole("navigation", { name: "Conversations" })).findByText("Example disk usage")).toBeInTheDocument();
  });

  it("frames cut at any byte across reads still make whole events", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    chatPosts(() => stream.response());
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    const text = frame(["session", { session_id: "s-new" }]) + frame(["token", { text: "Split across many reads." }]);
    for (let i = 0; i < text.length; i += 5) stream.raw(text.slice(i, i + 5));
    expect(await screen.findByText("Split across many reads.")).toBeInTheDocument();
    stream.send("done", { session_id: "s-new", message_id: 2, steps: 1, usage: {}, stopped: null, trace_turn: "t-new" });
    stream.close();
  });

  it("the conversation list is reloaded when the title event arrives", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(
      http.get("/api/chat/sessions", () => {
        calls += 1;
        const titled = { id: "s-new", title: "Example new chat", channel: "web", created: 1, updated: Date.now() / 1000, messages: 2 };
        return HttpResponse.json({ ...sessionList, items: calls > 1 ? [titled, ...sessionList.items] : sessionList.items });
      }),
    );
    chatPosts();
    renderChat("#/chat/new");
    await screen.findByText("Example disk usage");
    await user.type(box(), "hello{Enter}");
    expect(await within(screen.getByRole("navigation", { name: "Conversations" })).findByRole("link", { name: /Example new chat/ })).toBeInTheDocument();
  });

  it("shows the tools: running with its elapsed time, then used, with the result", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    chatPosts(() => stream.response());
    renderChat("#/chat/new");
    await user.type(box(), "check the disk{Enter}");
    stream.send("session", { session_id: "s-new" });
    stream.send("token", { text: "Let me look." });
    stream.send("tool_call", { id: "call-1", name: "test-server__list_files", args: { path: "/data" }, kind: "read" });
    expect(await screen.findByText("running test-server · list_files…")).toBeInTheDocument();
    // what was streamed before a tool call was thinking aloud, not the answer
    expect(screen.queryByText("Let me look.")).not.toBeInTheDocument();
    stream.send("tool_progress", { id: "call-1", name: "test-server__list_files", elapsed_s: 125 });
    expect(await screen.findByText("running test-server · list_files for 2 min 5 s")).toBeInTheDocument();
    stream.send("tool_progress", { id: "call-1", name: "test-server__list_files", elapsed_s: 45 });
    expect(await screen.findByText("running test-server · list_files for 45 s")).toBeInTheDocument();
    stream.send("tool_result", { id: "call-1", name: "test-server__list_files", ok: true, truncated: false, size: 12, preview: "a.txt b.txt" });
    expect(await screen.findByText("used a tool: test-server · list_files")).toBeInTheDocument();
    expect(screen.queryByText(/^running /)).not.toBeInTheDocument();
    expect(screen.getByText('{"path":"/data"}')).toBeInTheDocument();
    expect(screen.getByText("a.txt b.txt")).toBeInTheDocument();
    stream.send("token", { text: "Two files." });
    stream.send("done", { session_id: "s-new", message_id: 2, steps: 2, usage: {}, stopped: null, trace_turn: "t-new" });
    stream.close();
    expect(await screen.findByText("Two files.")).toBeInTheDocument();
  });

  it("says when the answer broke off and is asked for again, until its first new token", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    chatPosts(() => stream.response());
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    stream.send("session", { session_id: "s-new" });
    stream.send("token", { text: "Half an ans" });
    expect(await screen.findByText(/Half an ans/)).toBeInTheDocument();
    stream.send("retry", { attempt: 2, of: 3 });
    expect(await screen.findByText("answer broke off, asking again (2 of 3)")).toBeInTheDocument();
    expect(screen.queryByText(/Half an ans/)).not.toBeInTheDocument();
    stream.send("token", { text: "A whole answer." });
    expect(await screen.findByText("A whole answer.")).toBeInTheDocument();
    expect(screen.queryByText(/asking again/)).not.toBeInTheDocument();
    stream.send("done", { session_id: "s-new", message_id: 2, steps: 1, usage: {}, stopped: null, trace_turn: "t-new" });
    stream.close();
  });

  it("stop ends the request and gives the composer back its send button", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    chatPosts(() => stream.response());
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    stream.send("session", { session_id: "s-new" });
    stream.send("token", { text: "Partial answer" });
    await screen.findByText(/Partial answer/);
    await user.click(screen.getByRole("button", { name: "stop" }));
    expect(await screen.findByRole("button", { name: "send" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "stop" })).not.toBeInTheDocument();
  });

  // The stop mark is not stored: the page keeps the live thread (keepThread) instead of reloading the stored one.
  it("after stop the thread still says the answer was stopped", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    chatPosts(() => stream.response());
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    stream.send("session", { session_id: "s-new" });
    stream.send("token", { text: "Partial answer" });
    await screen.findByText(/Partial answer/);
    await user.click(screen.getByRole("button", { name: "stop" }));
    await screen.findByRole("button", { name: "send" });
    await new Promise((r) => setTimeout(r, 150));
    expect(screen.getByText("stopped")).toBeInTheDocument();
  });

  it("a second message cannot be sent while an answer is running", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    const bodies = chatPosts(() => stream.response());
    renderChat("#/chat/new");
    await user.type(box(), "first{Enter}");
    await screen.findByText("thinking…");
    await user.type(box(), "second{Enter}");
    expect(bodies).toHaveLength(1);
    expect(box()).toHaveValue("second");
    stream.send("done", { session_id: "s-new", message_id: 2, steps: 1, usage: {}, stopped: null, trace_turn: "t-new" });
    stream.close();
  });
});

describe("failed answers", () => {
  it("an error event shows its message under the question, and the live thread stays", async () => {
    const user = userEvent.setup();
    let loaded = 0;
    server.use(
      http.get("/api/chat/sessions/s-new", () => {
        loaded += 1;
        return HttpResponse.json({ id: "s-new", title: null, channel: "web", created: 1, updated: 1, messages: [] });
      }),
    );
    chatPosts(() => sseResponse([["session", { session_id: "s-new" }], ["token", { text: "Starting to" }], ["error", { message: "The model is unreachable." }]]));
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    expect(await screen.findByText("The model is unreachable.")).toBeInTheDocument();
    await screen.findByRole("button", { name: "send" });
    await new Promise((r) => setTimeout(r, 150)); // longer than the page's reload delay
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByText(/Starting to/)).toBeInTheDocument();
    expect(screen.getByText("The model is unreachable.")).toBeInTheDocument();
    expect(loaded).toBe(0);
  });

  // A request that fails before a session exists (new conversation: HTTP error or no connection) keeps the question
  // and shows the error under it.
  it("an error answer of the server (JSON detail) is shown under the question", async () => {
    const user = userEvent.setup();
    chatPosts(() => HttpResponse.json({ detail: "No model is configured." }, { status: 503 }));
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    await screen.findByRole("button", { name: "send" });
    expect(await screen.findByText("No model is configured.")).toBeInTheDocument();
    expect(screen.getByText("hello")).toBeInTheDocument();
  });

  it("an error answer without detail shows its status", async () => {
    const user = userEvent.setup();
    chatPosts(() => new HttpResponse("oops", { status: 500 }));
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    await screen.findByRole("button", { name: "send" });
    expect(await screen.findByText("HTTP 500")).toBeInTheDocument();
  });

  it("a connection that fails shows the failure", async () => {
    const user = userEvent.setup();
    chatPosts(() => HttpResponse.error());
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    await screen.findByRole("button", { name: "send" });
    expect(await screen.findByText(/Network error/)).toBeInTheDocument();
  });

  it("a failed request frees the composer", async () => {
    const user = userEvent.setup();
    chatPosts(() => HttpResponse.json({ detail: "No model is configured." }, { status: 503 }));
    renderChat("#/chat/new");
    await user.type(box(), "hello{Enter}");
    expect(await screen.findByRole("button", { name: "send" })).toBeInTheDocument();
  });

  it("an error answer in an open conversation shows under the new question (the stored thread is reloaded over it)", async () => {
    const user = userEvent.setup();
    chatPosts(() => HttpResponse.json({ detail: "No model is configured." }, { status: 503 }));
    renderChat("#/chat/s-disk");
    await screen.findByText("What is using the disk on host-a?");
    await user.type(box(), "and the memory?{Enter}");
    await screen.findByRole("button", { name: "send" });
    await new Promise((r) => setTimeout(r, 150));
    expect(screen.getByText("No model is configured.")).toBeInTheDocument();
  });
});

describe("the composer's expand button", () => {
  /** happy-dom has no layout: the text box reports the heights the test gives it. */
  function layout(scrollHeight: number, clientHeight: number) {
    Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", { configurable: true, get: () => scrollHeight });
    Object.defineProperty(HTMLTextAreaElement.prototype, "clientHeight", { configurable: true, get: () => clientHeight });
  }
  afterEach(() => {
    // (back to the element's own getters)
    delete (HTMLTextAreaElement.prototype as unknown as Record<string, unknown>).scrollHeight;
    delete (HTMLTextAreaElement.prototype as unknown as Record<string, unknown>).clientHeight;
  });

  it("is not there while the text fits", async () => {
    layout(24, 24);
    const user = userEvent.setup();
    renderChat("#/chat/new");
    await user.type(box(), "short");
    expect(screen.queryByRole("button", { name: "expand" })).not.toBeInTheDocument();
  });

  it("appears when the text overflows, expands and collapses the box", async () => {
    layout(200, 60);
    const user = userEvent.setup();
    renderChat("#/chat/new");
    await user.type(box(), "a long text");
    const expand = await screen.findByRole("button", { name: "expand" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    await user.click(expand);
    const collapse = screen.getByRole("button", { name: "collapse" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    await user.click(collapse);
    expect(screen.getByRole("button", { name: "expand" })).toHaveAttribute("aria-expanded", "false");
  });

  it("sending an expanded text makes the box one line again", async () => {
    layout(200, 60);
    const user = userEvent.setup();
    chatPosts();
    renderChat("#/chat/new");
    await user.type(box(), "a long text");
    await user.click(await screen.findByRole("button", { name: "expand" }));
    expect(screen.getByRole("button", { name: "collapse" })).toBeInTheDocument();
    await user.type(box(), "{Enter}");
    await waitFor(() => expect(box()).toHaveValue(""));
    expect(screen.queryByRole("button", { name: "collapse" })).not.toBeInTheDocument();
  });
});
