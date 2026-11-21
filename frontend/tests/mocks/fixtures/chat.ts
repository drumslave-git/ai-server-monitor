// Invented chat data: conversations, their stored messages and the events of an answer stream.
import { HttpResponse } from "msw";
import type { ApiResponse } from "../types";

type SessionList = ApiResponse<"/api/chat/sessions">;
type SessionDoc = ApiResponse<"/api/chat/sessions/{session_id}">;
type Message = SessionDoc["messages"][number];

const now = Date.now() / 1000;

export const LONG_TITLE =
  "Example conversation about why the container test-main on host-a restarts every night after the backup job and what to do about it";

export const sessionList: SessionList = {
  items: [
    { id: "s-disk", title: "Example disk usage", channel: "web", created: now - 4000, updated: now - 300, messages: 4 },
    { id: "s-telegram", title: "Example backup question", channel: "telegram", created: now - 90000, updated: now - 7200, messages: 2 },
    { id: "s-long", title: LONG_TITLE, channel: "web", created: now - 200000, updated: now - 90000, messages: 2 },
    { id: "s-untitled", title: null, channel: "web", created: now - 300000, updated: now - 200000, messages: 1 },
  ],
  limit: 50,
  offset: 0,
  total: 4,
};

let nextId = 1;
const message = (m: Omit<Message, "id" | "ts">): Message => ({ id: nextId++, ts: now - 1000, ...m });

/** The answer of s-disk: Markdown with a read-only command, a command without a mark and a non-shell block. */
export const DISK_ANSWER = [
  "`/var/lib/docker` holds most of the disk.",
  "",
  "```bash",
  "# [read-only] the biggest directories",
  "du -sh /var/lib/docker/*",
  "",
  "# [write] remove unused images",
  "docker image prune -f",
  "",
  "df -h",
  "```",
  "",
  "```json",
  '{"free": "40 GB"}',
  "```",
].join("\n");

export const diskSession: SessionDoc = {
  id: "s-disk",
  title: "Example disk usage",
  channel: "web",
  created: now - 4000,
  updated: now - 300,
  messages: [
    message({ role: "user", content: "What is using the disk on host-a?" }),
    message({
      role: "assistant",
      content: null,
      tool_calls: [{ id: "call-1", type: "function", function: { name: "shell", arguments: { command: "df -h" } } }],
    }),
    message({ role: "tool", name: "shell", tool_call_id: "call-1", content: "Filesystem  Size  Used\n/dev/sda1   100G  60G", meta: { ok: true } }),
    message({
      role: "assistant",
      content: DISK_ANSWER,
      meta: { trace_turn: "t-1", mcp_domains: { picked: ["files"], loaded: ["torrents"], unreachable: { "test-server": "timeout" } } },
    }),
  ],
};

export const telegramSession: SessionDoc = {
  id: "s-telegram",
  title: "Example backup question",
  channel: "telegram",
  created: now - 90000,
  updated: now - 7200,
  messages: [
    message({ role: "user", content: "Did the backup run last night?" }),
    message({ role: "assistant", content: "Yes, it finished at 03:12.", meta: { trace_turn: "t-2", unmarked: { commands: 2, error: "labelling timed out" } } }),
  ],
};

export const longSession: SessionDoc = {
  id: "s-long",
  title: LONG_TITLE,
  channel: "web",
  created: now - 200000,
  updated: now - 90000,
  messages: [message({ role: "user", content: "Why does it restart?" }), message({ role: "assistant", content: "The backup stops it." })],
};

export const untitledSession: SessionDoc = {
  id: "s-untitled",
  title: null,
  channel: "web",
  created: now - 300000,
  updated: now - 200000,
  messages: [message({ role: "user", content: "Hello" })],
};

/** What the server stores once the default answer of POST /api/chat is complete. */
export const newSession: SessionDoc = {
  id: "s-new",
  title: "Example new chat",
  channel: "web",
  created: now,
  updated: now,
  messages: [
    message({ role: "user", content: "How much memory is free?" }),
    message({ role: "assistant", content: "About 12 GB of memory is free.", meta: { trace_turn: "t-new" } }),
  ],
};

export const sessionDocs: Record<string, SessionDoc> = {
  "s-disk": diskSession,
  "s-telegram": telegramSession,
  "s-long": longSession,
  "s-untitled": untitledSession,
  "s-new": newSession,
};

// ---- the answer stream: Server-Sent Events as the backend writes them ----

export type ChatEvent = [type: string, data: Record<string, unknown>];

export const frame = ([type, data]: ChatEvent) => `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;

const SSE_HEADERS = { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" };

/** A complete answer stream. chunk: cut the bytes into pieces of this many characters (frames then
 *  arrive split across reads, as they can over a real connection); default: one piece per frame. */
export function sseResponse(events: ChatEvent[], chunk?: number): Response {
  const encoder = new TextEncoder();
  const text = events.map(frame).join("");
  const pieces = chunk ? text.match(new RegExp(`[\\s\\S]{1,${chunk}}`, "g")) ?? [] : events.map(frame);
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const piece of pieces) controller.enqueue(encoder.encode(piece));
      controller.close();
    },
  });
  return new HttpResponse(body, { headers: SSE_HEADERS });
}

/** An answer stream the test feeds by hand: send() one event at a time, close() ends it. */
export function liveStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: () => new HttpResponse(body, { headers: SSE_HEADERS }),
    send: (type: string, data: Record<string, unknown>) => controller.enqueue(encoder.encode(frame([type, data]))),
    /** Bytes as they are, e.g. half of a frame. */
    raw: (text: string) => controller.enqueue(encoder.encode(text)),
    close: () => controller.close(),
  };
}

/** The events of a plain answer in a new conversation. */
export const answerEvents = (text = "About 12 GB of memory is free.", session = "s-new"): ChatEvent[] => [
  ["session", { session_id: session }],
  ...text.split(/(?<= )/).map((piece): ChatEvent => ["token", { text: piece }]),
  ["done", { session_id: session, message_id: 2, steps: 1, usage: {}, stopped: null, trace_turn: "t-new" }],
  ["title", { session_id: session, title: "Example new chat" }],
];

/** The events of the answer to a command the owner ran. */
export const runEvents = (command: string): ChatEvent[] => [
  ["run", { command, mode: "read-only", action_id: null, exit_code: 0, duration_s: 0.4, stopped: "exit", truncated: false, output: "3.1G  /var/lib/docker/overlay2" }],
  ["session", { session_id: "s-disk" }],
  ["token", { text: "Overlay storage is the biggest part." }],
  ["done", { session_id: "s-disk", message_id: 9, steps: 1, usage: {}, stopped: null, trace_turn: "t-run" }],
];

/** The events of an explain conversation (the session event carries the label and the composed message). */
export const explainEvents = (label: string, source: string): ChatEvent[] => [
  ["session", { session_id: "s-explain", explain: { label, source } }],
  ["token", { text: "This run found one problem." }],
  ["done", { session_id: "s-explain", message_id: 2, steps: 1, usage: {}, stopped: null, trace_turn: "t-explain" }],
];
