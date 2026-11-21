// Prompt cards in the chat thread: the questions the model asked the owner (`ask_owner`).
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { liveStream } from "../../mocks/fixtures/chat";
import { server } from "../../mocks/server";
import type { ApiResponse } from "../../mocks/types";
import { FakeEventSource } from "../shell/fake-event-source";
import { renderChat } from "./render";

type Doc = ApiResponse<"/api/chat/sessions/{session_id}">;
type Card = Doc["cards"][number];

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  history.replaceState(null, "", "#/");
});

const QUESTIONS = [
  { text: "Which disk?", kind: "single", options: ["sda", "sdb"] },
  { text: "Which checks?", kind: "multi", options: ["smart", "fsck"] },
  { text: "Anything else?", kind: "text", options: [] },
];

const card = (over: Partial<Card> = {}): Card => ({
  id: 7,
  session_id: "s-x",
  call_id: "call-ask",
  status: "open",
  questions: QUESTIONS,
  answers: null,
  created: 1,
  answered: null,
  answered_via: null,
  ...over,
});

/** The stored messages of a turn that asked: the call, the waiting note, and (answered) the answers and the rest. */
function thread(opts: { answered?: boolean } = {}) {
  const messages: Doc["messages"] = [
    { id: 1, ts: 1, role: "user", content: "check my disks" },
    { id: 2, ts: 1, role: "assistant", content: null, tool_calls: [{ id: "call-ask", type: "function", function: { name: "ask_owner", arguments: { questions: QUESTIONS } } }] },
    { id: 3, ts: 1, role: "assistant", content: opts.answered ? "Answered." : "Waiting for your answer.", meta: { cards: [7] } },
  ];
  if (opts.answered)
    messages.push(
      { id: 4, ts: 1, role: "tool", name: "ask_owner", tool_call_id: "call-ask", content: "The owner answered:\n1. Which disk?\n   sdb" },
      { id: 5, ts: 1, role: "assistant", content: "Checking sdb." },
    );
  return messages;
}

function store(cards: Card[], messages = thread()) {
  server.use(http.get("/api/chat/sessions/s-x", () => HttpResponse.json({ id: "s-x", title: "Example x", channel: "web", created: 1, updated: 1, cards, messages } satisfies Doc)));
}

const sendButton = () => screen.getByRole("button", { name: "send answers" });

describe("an open card", () => {
  it("shows every question with its controls; the tool call and the waiting note are not shown", async () => {
    store([card()]);
    renderChat("#/chat/s-x");
    expect(await screen.findByText("Questions")).toBeInTheDocument();
    const disk = screen.getByRole("group", { name: "1. Which disk?" });
    expect(within(disk).getAllByRole("radio").map((r) => r.parentElement!.textContent)).toEqual(["sda", "sdb", "own answer"]);
    const checks = screen.getByRole("group", { name: "2. Which checks?" });
    expect(within(checks).getAllByRole("checkbox").map((c) => c.parentElement!.textContent)).toEqual(["smart", "fsck", "own answer"]);
    expect(screen.getByRole("textbox", { name: "3. Anything else?" })).toBeInTheDocument();
    expect(screen.queryByText("Waiting for your answer.")).not.toBeInTheDocument();
    expect(screen.queryByText(/used a tool/)).not.toBeInTheDocument();
    expect(sendButton()).toBeDisabled();
  });

  it("sends the answers once every question has one: a choice, several, an own answer, a text", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    server.use(
      http.post("/api/chat/cards/7/answer", async ({ request }) => {
        bodies.push(await request.json());
        store([card({ status: "answered", answered_via: "web" })], thread({ answered: true }));
        return HttpResponse.json(card({ status: "answered", answered_via: "web" }));
      }),
    );
    store([card()]);
    renderChat("#/chat/s-x");
    await screen.findByText("Questions");
    await user.click(screen.getByRole("radio", { name: "sda" }));
    await user.click(screen.getByRole("radio", { name: "sdb" })); // one of them only
    await user.click(screen.getByRole("checkbox", { name: "smart" }));
    await user.click(screen.getByRole("checkbox", { name: "own answer" }));
    await user.type(screen.getByRole("textbox", { name: "Own answer to: Which checks?" }), "the cables");
    expect(sendButton()).toBeDisabled(); // the text question is open
    await user.type(screen.getByRole("textbox", { name: "3. Anything else?" }), "  no  ");
    await user.click(sendButton());
    expect(bodies).toEqual([
      {
        answers: [
          { chosen: ["sdb"], text: "" },
          { chosen: ["smart"], text: "the cables" },
          { chosen: [], text: "no" },
        ],
      },
    ]);
    // the thread is loaded again: the card is closed and the answer of the model follows
    expect(await screen.findByText("answered in the web chat")).toBeInTheDocument();
    expect(await screen.findByText("Checking sdb.")).toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("an own answer to a single question replaces the choice; an empty own answer is no answer", async () => {
    const user = userEvent.setup();
    store([card({ questions: [QUESTIONS[0]] })]);
    renderChat("#/chat/s-x");
    await screen.findByText("Question");
    await user.click(screen.getByRole("radio", { name: "sda" }));
    expect(sendButton()).toBeEnabled();
    await user.click(screen.getByRole("radio", { name: "own answer" }));
    expect(screen.getByRole("radio", { name: "sda" })).not.toBeChecked();
    expect(sendButton()).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Own answer to: Which disk?" }), "sdc");
    expect(sendButton()).toBeEnabled();
  });

  it("shows why the answers were refused and keeps what was typed", async () => {
    const user = userEvent.setup();
    server.use(http.post("/api/chat/cards/7/answer", () => HttpResponse.json({ detail: "question 1: unknown option 'sdc'" }, { status: 422 })));
    store([card({ questions: [QUESTIONS[0]] })]);
    renderChat("#/chat/s-x");
    await screen.findByText("Question");
    await user.click(screen.getByRole("radio", { name: "sda" }));
    await user.click(sendButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("question 1: unknown option 'sdc'");
    expect(screen.getByRole("radio", { name: "sda" })).toBeChecked();
  });

  it("a card answered elsewhere in the meantime (409) is shown as it is now", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/chat/cards/7/answer", () => {
        store([card({ questions: [QUESTIONS[0]], status: "answered", answered_via: "telegram", answers: [{ chosen: ["sdb"], text: "" }] })], thread({ answered: true }));
        return HttpResponse.json({ detail: "card 7 is answered already" }, { status: 409 });
      }),
    );
    store([card({ questions: [QUESTIONS[0]] })]);
    renderChat("#/chat/s-x");
    await screen.findByText("Question");
    await user.click(screen.getByRole("radio", { name: "sda" }));
    await user.click(sendButton());
    expect(await screen.findByText("answered in Telegram")).toBeInTheDocument();
    expect(screen.getByText("sdb")).toBeInTheDocument();
  });
});

describe("an answered card", () => {
  it("shows what was answered, without controls", async () => {
    store(
      [
        card({
          status: "answered",
          answered_via: "telegram",
          answers: [
            { chosen: ["sdb"], text: "" },
            { chosen: ["smart"], text: "the cables" },
            { chosen: [], text: "no" },
          ],
        }),
      ],
      thread({ answered: true }),
    );
    renderChat("#/chat/s-x");
    expect(await screen.findByText("answered in Telegram")).toBeInTheDocument();
    expect(screen.getByText("Which disk?")).toBeInTheDocument();
    expect(screen.getByText("sdb")).toBeInTheDocument();
    expect(screen.getByText("smart, the cables")).toBeInTheDocument();
    expect(screen.getByText("no")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "send answers" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    // the answers the model read are not shown a second time, the rest of the turn is
    expect(screen.queryByText(/The owner answered/)).not.toBeInTheDocument();
    expect(screen.getByText("Checking sdb.")).toBeInTheDocument();
  });
});

describe("a card while the answer streams, and cards from the event stream", () => {
  it("shows the card as soon as its event comes, in place of the tool call", async () => {
    const user = userEvent.setup();
    const stream = liveStream();
    server.use(http.post("/api/chat", () => stream.response()));
    renderChat("#/chat/new");
    await user.type(screen.getByRole("textbox", { name: "Message" }), "check my disks{Enter}");
    act(() => {
      stream.send("session", { session_id: "s-x" });
      stream.send("tool_call", { id: "call-ask", name: "ask_owner", args: { questions: QUESTIONS }, kind: null });
    });
    expect(await screen.findByText("running ask_owner…")).toBeInTheDocument();
    store([card()]);
    act(() => stream.send("card", { card: card() }));
    expect(await screen.findByText("Questions")).toBeInTheDocument();
    expect(screen.queryByText("running ask_owner…")).not.toBeInTheDocument();
    expect(screen.queryByText(/used a tool/)).not.toBeInTheDocument();
    act(() => {
      stream.send("done", { session_id: "s-x", message_id: 3, steps: 1, usage: {}, stopped: null, trace_turn: "t-1", cards: [7] });
      stream.close();
    });
    // the stored thread replaces the live one: still one card
    await waitFor(() => expect(screen.getAllByText("Questions")).toHaveLength(1));
    expect(screen.getByRole("group", { name: "1. Which disk?" })).toBeInTheDocument();
  });

  it("loads the thread again when a card is answered elsewhere (event chat.card.answered)", async () => {
    store([card({ questions: [QUESTIONS[0]] })]);
    renderChat("#/chat/s-x");
    await screen.findByText("Question");
    store([card({ questions: [QUESTIONS[0]], status: "answered", answered_via: "telegram", answers: [{ chosen: ["sda"], text: "" }] })], thread({ answered: true }));
    act(() => FakeEventSource.current().emit("chat.card.answered", { id: 7, session_id: "s-x" }));
    expect(await screen.findByText("answered in Telegram")).toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("shows the line that Telegram refused the card as soon as the event says so (event chat.card.telegram), and no line while it is fine", async () => {
    store([card({ questions: [QUESTIONS[0]] })]);
    renderChat("#/chat/s-x");
    await screen.findByText("Question");
    expect(screen.queryByText(/Not sent to Telegram/)).not.toBeInTheDocument();
    const refused = card({ questions: [QUESTIONS[0]], telegram_error: "bot was blocked by the user" });
    store([refused]);
    act(() => FakeEventSource.current().emit("chat.card.telegram", refused));
    expect(await screen.findByText("Not sent to Telegram: bot was blocked by the user")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "sda" })).toBeInTheDocument(); // the card stays answerable here
    store([card({ questions: [QUESTIONS[0]] })]); // a later send went through: the error is cleared
    act(() => FakeEventSource.current().emit("chat.card.telegram", card({ questions: [QUESTIONS[0]] })));
    await waitFor(() => expect(screen.queryByText(/Not sent to Telegram/)).not.toBeInTheDocument());
  });

  it("an answered card shows no Telegram line, whatever Telegram said before", async () => {
    store([card({ status: "answered", answered_via: "web", answers: [{ chosen: ["sda"], text: "" }], questions: [QUESTIONS[0]], telegram_error: "chat not found" })], thread({ answered: true }));
    renderChat("#/chat/s-x");
    expect(await screen.findByText("answered in the web chat")).toBeInTheDocument();
    expect(screen.queryByText(/Not sent to Telegram/)).not.toBeInTheDocument();
  });

  it("does not load another conversation's card", async () => {
    store([card({ questions: [QUESTIONS[0]] })]);
    renderChat("#/chat/s-x");
    await screen.findByText("Question");
    store([card({ questions: [QUESTIONS[0]], status: "answered", answered_via: "web", answers: [{ chosen: ["sda"], text: "" }] })]);
    act(() => FakeEventSource.current().emit("chat.card.answered", { id: 9, session_id: "s-other" }));
    await new Promise((r) => setTimeout(r, 80));
    expect(screen.getByRole("radio", { name: "sda" })).toBeInTheDocument();
  });
});
