// Invented data for the Logs pages: LLM calls (list, one call, timeline, tool call, stats) and the app log.
// Everything is typed with the generated API types, so these stop compiling when the API changes.
import type { ApiResponse } from "../types";

export type CallSummary = ApiResponse<"/api/logs/llm">["items"][number];
export type CallListAnswer = ApiResponse<"/api/logs/llm">;
export type CallAnswer = ApiResponse<"/api/logs/llm/{call_id}">;
export type StatsAnswer = ApiResponse<"/api/logs/llm/stats">;
export type TimelineAnswer = ApiResponse<"/api/logs/llm/{call_id}/timeline">;
export type TimelineItem = TimelineAnswer["entries"][number];
export type ToolCallAnswer = ApiResponse<"/api/logs/tools/{tool_id}">;
export type AppLogAnswer = ApiResponse<"/api/logs/app">;
export type AppRecord = AppLogAnswer["items"][number];

export const CHAT_ID = "abcdef1234567890";

export const nowSeconds = () => Date.now() / 1000;

/** One LLM call as the list shows it. */
export function makeCall(over: Partial<CallSummary> = {}): CallSummary {
  return {
    id: 1,
    ts: nowSeconds() - 120,
    purpose: "chat",
    link_kind: "chat",
    link_id: CHAT_ID,
    link_step: null,
    turn: null,
    group_id: null,
    attempt: 1,
    model: "test-model",
    base_url: "http://host-a:8080/v1",
    stream: true,
    status: "ok",
    http_status: 200,
    error: null,
    finish_reason: "stop",
    prompt_tokens: 1200,
    completion_tokens: 300,
    usage_estimated: false,
    queued_ms: 0,
    ttft_ms: 400,
    total_ms: 2500,
    tokens_per_s: 120,
    messages_count: 3,
    tools_count: 0,
    tools_tokens: null,
    request_bytes: 4800,
    response_chars: 900,
    tool_calls_count: 0,
    request_preview: "What is the disk usage on host-a?",
    response_preview: "The disk on host-a is 61 percent full.",
    truncated: [],
    bodies: true,
    size_bytes: 6000,
    ...over,
  };
}

/** A page of the list. */
export function callList(items: CallSummary[], over: Partial<CallListAnswer> = {}): CallListAnswer {
  return { items, total: items.length, limit: 50, offset: 0, complete: true, ...over };
}

/** The default list: a chat call, a job call with a tool call, a failed call and a cancelled one. */
export function defaultCalls(): CallSummary[] {
  const now = nowSeconds();
  return [
    makeCall({ id: 4, ts: now - 60 }),
    makeCall({
      id: 3,
      ts: now - 600,
      purpose: "job",
      link_kind: "job",
      link_id: "example-job",
      link_step: 2,
      model: "test-worker",
      request_preview: "Check the backup folder",
      response_preview: "I will list the folder first.",
      tool_calls_count: 1,
      prompt_tokens: 800,
      completion_tokens: 40,
      ttft_ms: 900,
      total_ms: 1800,
    }),
    makeCall({
      id: 2,
      ts: now - 1800,
      status: "error",
      http_status: 503,
      error: "Upstream returned 503: model is loading",
      response_preview: null,
      prompt_tokens: null,
      completion_tokens: null,
      ttft_ms: null,
      total_ms: 90,
      attempt: 2,
    }),
    makeCall({ id: 1, ts: now - 7200, status: "cancelled", purpose: "chat_title", link_kind: null, link_id: null, request_preview: "Name this chat", response_preview: null, ttft_ms: null, total_ms: 300 }),
  ];
}

export function makeStats(over: Partial<StatsAnswer> = {}): StatsAnswer {
  return {
    calls: 4,
    failed: 1,
    tool_calls: 3,
    bytes: 24_000,
    oldest: nowSeconds() - 86_400,
    purposes: [
      { purpose: "chat", count: 2 },
      { purpose: "job", count: 1 },
      { purpose: "chat_title", count: 1 },
    ],
    enabled: true,
    store_bodies: true,
    retention_days: 7,
    max_total_mb: 200,
    ...over,
  };
}

/** One call in full: the messages sent, the tools, the answer. */
export function makeFullCall(over: Partial<CallAnswer> = {}): CallAnswer {
  return {
    ...makeCall(),
    id: 12,
    queued_ms: 30,
    messages_count: 5,
    tools_count: 2,
    tools_tokens: 150,
    tool_calls_count: 1,
    params: { temperature: 0.2, max_tokens: 1024, response_model: "ignored-model", tool_choice: "auto" },
    messages: [
      { role: "system", content: "You are the monitor assistant." },
      { role: "user", content: "Is host-a healthy?" },
      {
        role: "assistant",
        content: "",
        tool_calls: [{ id: "call_1", function: { name: "test-server__check_host", arguments: '{"host":"host-a"}' } }],
      },
      { role: "tool", name: "check_host", tool_call_id: "call_1", content: '{"status":"fine"}' },
      { role: "user", content: [{ type: "text", text: "Thanks, and the disk?" }] },
    ],
    tools: [
      { type: "function", function: { name: "check_host", description: "Check one host.\nSecond line of the description.", parameters: { type: "object" } } },
      { type: "function", function: { name: "list_disks", description: "List the disks of a host.", parameters: { type: "object" } } },
    ],
    content: "The disk on host-a is 61 percent full.",
    reasoning: "The user asks about the disk, so check the host first.",
    tool_calls: [{ id: "call_2", name: "list_disks", arguments: '{"host":"host-a"}' }],
    ...over,
  } as CallAnswer;
}

/** The calls and tool calls of one chat turn. */
export function makeTimeline(over: Partial<TimelineAnswer> = {}): TimelineAnswer {
  const t0 = nowSeconds() - 300;
  return {
    selected: 12,
    entries: [
      { type: "llm", id: 11, ts: t0, link_step: 1, status: "ok", prompt_tokens: 900, completion_tokens: 30, ttft_ms: 300, total_ms: 1000, tool_calls_count: 1, finish_reason: "tool_calls" },
      { type: "tool", id: 7, ts: t0 + 1, link_step: 1, tool: "test-server__check_host", ok: true, duration_ms: 500, result_size: 1200 },
      { type: "llm", id: 12, ts: t0 + 1.5, link_step: 2, status: "ok", prompt_tokens: 1200, completion_tokens: 300, ttft_ms: 400, total_ms: 1000, tool_calls_count: 0, finish_reason: "stop" },
    ],
    ...over,
  };
}

export function makeToolCall(over: Partial<ToolCallAnswer> = {}): ToolCallAnswer {
  return {
    id: 7,
    ts: nowSeconds() - 299,
    tool: "test-server__check_host",
    ok: true,
    duration_ms: 500,
    result_size: 1200,
    error: null,
    truncated: false,
    size_bytes: 1500,
    args: { host: "host-a" },
    result: { status: "fine", disks: 2 },
    ...over,
  };
}

/** One record of the app log. */
export function makeRecord(over: Partial<AppRecord> = {}): AppRecord {
  return {
    id: null,
    seq: 1,
    ts: nowSeconds() - 30,
    level: "INFO",
    levelno: 20,
    logger: "ai_server_monitor.scheduler",
    message: "Scheduler started",
    exc: null,
    ...over,
  };
}

export function appLog(items: AppRecord[], source: "memory" | "stored" = "memory"): AppLogAnswer {
  const seqs = items.map((r) => r.seq).filter((s): s is number => typeof s === "number");
  return { items, source, newest: seqs.length ? Math.max(...seqs) : null };
}

/** Newest first, as the server sends them: an info, a warning, an error with a traceback and a debug line. */
export function defaultRecords(): AppRecord[] {
  const now = nowSeconds();
  return [
    makeRecord({ seq: 4, ts: now - 5, level: "ERROR", levelno: 40, logger: "ai_server_monitor.llm", message: "Model call failed", exc: 'Traceback (most recent call last):\n  File "llm.py", line 10\nConnectError: refused' }),
    makeRecord({ seq: 3, ts: now - 20, level: "WARNING", levelno: 30, logger: "ai_server_monitor.mcp", message: "Server test-server is slow" }),
    makeRecord({ seq: 2, ts: now - 40, level: "INFO", levelno: 20, logger: "ai_server_monitor.scheduler", message: "Scheduler started" }),
    makeRecord({ seq: 1, ts: now - 50, level: "DEBUG", levelno: 10, logger: "httpx", message: "HTTP request sent" }),
  ];
}

export const defaultLoggers = ["ai_server_monitor.llm", "ai_server_monitor.mcp", "ai_server_monitor.scheduler", "httpx"];
