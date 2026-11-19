export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(describe(status, detail));
  }
}

/** The message of an error response: its `detail` text, or each entry of a validation error
 *  list (FastAPI: `{loc, msg}`) as "field: message". */
function describe(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((item) => {
        if (typeof item !== "object" || item === null) return String(item);
        const { loc, msg } = item as { loc?: unknown; msg?: unknown };
        const field = Array.isArray(loc) ? loc.filter((p) => p !== "body" && p !== "query").join(".") : "";
        return field ? `${field}: ${String(msg ?? "")}` : String(msg ?? JSON.stringify(item));
      })
      .join("; ");
  }
  return `HTTP ${status}`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new ApiError(response.status, data?.detail ?? data);
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  post: <T>(path: string, body: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  del: (path: string) => request<null>("DELETE", path),
};

export type FieldError = { loc: string[]; msg: string };

export type SettingsDoc = {
  values: JsonObject;
  schema: JsonSchema;
  sections: Record<string, string>;
};

export type JsonSchema = {
  type?: string;
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  anyOf?: JsonSchema[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  additionalProperties?: boolean | JsonSchema;
  enum?: Json[];
  const?: Json;
  format?: string;
  pattern?: string;
  title?: string;
  description?: string;
  default?: Json;
  minimum?: number;
  maximum?: number;
  examples?: Json[];
  /** Set on a connection (schema extra): which test the Test button runs. */
  test?: string;
  /** Extra test buttons: query mode → label (e.g. Telegram: send → "Send test message"). */
  test_modes?: Record<string, string>;
};

export type LlmTestResult = {
  ok: boolean;
  reply?: string;
  latency_s?: number;
  models?: string[] | null;
  model_listed?: boolean | null;
  tool_call?: boolean | null;
  /** The structured call made the helper calls' way ("Structured output" of the profile). */
  structured_output?: { ok: boolean; text: string } | null;
  error?: string;
};

export type ModelInfo = {
  available: boolean;
  parameters: Record<string, number>;
  capabilities: string[];
  context_length: number | null;
};

// ---- generated types (src/api-types.ts is produced by `npm run gen:api`)
import type { components } from "./api-types";
export type Schemas = components["schemas"];
export type StatusResponse = Schemas["StatusResponse"];
export type ConnectionTestResult = Schemas["ConnectionTestResult"];

export type ActionRecord = {
  id: number;
  ts: number;
  action: string;
  target: string | null;
  args: Record<string, unknown> | null;
  policy: string;
  status: string;
  source: string;
  reason: string | null;
  result: Record<string, any> | null;
  decided_by: string | null;
  expires_at: number | null;
  batch_id: number | null;
};

/** One approval: the items an automation run (or a chat turn) asked for, decided together. */
export type ApprovalBatch = {
  id: number;
  ts: number;
  action: string;
  label: string | null;
  title: string;
  summary: string;
  source: string;
  expires_at: number | null;
  total: number;
  pending: number;
  queued: number;
  next_in_s: number | null;
  queue_note: string;
  counts: Record<string, number>;
  outcome: string;
  /** Kinds of its waiting items ("server · tool") that an approval "for the rest of the run"
   *  would cover; empty when it cannot cover later requests. */
  rest: string[];
  /** Kinds an approval of this batch covers for the rest of its run. */
  covered: string[];
  items: ActionRecord[];
};
