// Shapes of /api/logs/*. Phase 2 replaces these with the generated api-types.ts.

export type CallStatus = "ok" | "error" | "cancelled";

export type LlmCallSummary = {
  id: number;
  ts: number;
  purpose: string;
  link_kind: string | null;
  link_id: string | null;
  link_step: number | null;
  turn: string | null;
  group_id: string | null;
  attempt: number;
  model: string | null;
  base_url: string | null;
  stream: boolean;
  status: CallStatus;
  http_status: number | null;
  error: string | null;
  finish_reason: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  usage_estimated: boolean;
  queued_ms: number | null;
  ttft_ms: number | null;
  total_ms: number | null;
  tokens_per_s: number | null;
  messages_count: number | null;
  tools_count: number | null;
  tools_tokens: number | null;
  request_bytes: number | null;
  response_chars: number | null;
  tool_calls_count: number;
  request_preview: string | null;
  response_preview: string | null;
  truncated: string[];
  bodies: boolean;
  size_bytes: number;
};

export type LlmCallList = {
  items: LlmCallSummary[];
  total: number;
  limit: number;
  offset: number;
  complete: boolean;
};

export type ChatMessage = {
  role: "system" | "user" | "assistant" | "tool" | string;
  content?: string | { type: string; text?: string }[] | null;
  tool_calls?: { id?: string; function?: { name?: string; arguments?: string } }[];
  tool_call_id?: string;
  name?: string;
};

export type ToolSpec = { type?: string; function?: { name?: string; description?: string; parameters?: unknown } };

export type ResponseToolCall = { id: string; name: string; arguments: string };

export type LlmCall = LlmCallSummary & {
  params: Record<string, unknown>;
  messages: ChatMessage[] | null;
  tools: ToolSpec[] | null;
  content: string | null;
  reasoning: string | null;
  tool_calls: ResponseToolCall[] | null;
};

export type TimelineEntry = {
  type: "llm" | "tool";
  id: number;
  ts: number;
  link_step: number | null;
  purpose?: string | null;
  model?: string | null;
  status?: string | null;
  finish_reason?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  queued_ms?: number | null;
  ttft_ms?: number | null;
  total_ms?: number | null;
  tool_calls_count?: number | null;
  response_preview?: string | null;
  attempt?: number | null;
  tool?: string | null;
  ok?: boolean | null;
  duration_ms?: number | null;
  result_size?: number | null;
  error?: string | null;
};

export type Timeline = { selected: number; entries: TimelineEntry[] };

export type ToolCallDetail = {
  id: number;
  ts: number;
  tool: string;
  ok: boolean;
  duration_ms: number | null;
  result_size: number | null;
  error: string | null;
  truncated: boolean;
  args: unknown;
  result: unknown;
};

export type TraceStats = {
  calls: number;
  failed: number;
  tool_calls: number;
  bytes: number;
  oldest: number | null;
  purposes: { purpose: string; count: number }[];
  enabled: boolean;
  store_bodies: boolean;
  retention_days: number;
  max_total_mb: number;
};

export type AppLogRecord = {
  id: number | null;
  seq: number | null;
  ts: number;
  level: string;
  levelno: number;
  logger: string;
  message: string;
  exc: string | null;
};

export type AppLogList = { items: AppLogRecord[]; source: "memory" | "stored"; newest: number | null };
