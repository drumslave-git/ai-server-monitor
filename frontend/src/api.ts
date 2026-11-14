export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
  }
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
};

export type CollectorStatus = {
  state: "active" | "disabled" | "unavailable";
  reason: string | null;
  last_ts: number | null;
  ok: boolean | null;
  error: string | null;
  duration_s: number | null;
};

export type Info = {
  version: string;
  uptime_s: number;
  host_root: string;
  llm: {
    configured: boolean;
    base_url: string;
    model: string;
    tool_mode: { effective: string | null; source: string | null; last_error: string | null };
    stats: { requests: number; errors: number; last_error: string | null; last_latency_s: number | null };
  };
  collectors: Record<string, CollectorStatus>;
};

export type LlmTestResult = {
  ok: boolean;
  reply?: string;
  latency_s?: number;
  models?: string[] | null;
  model_listed?: boolean | null;
  error?: string;
};
