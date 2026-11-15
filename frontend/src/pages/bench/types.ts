export type CheckInfo = { type: string; weight: number; describe: string; note: string };

export type ScenarioInfo = {
  id: string;
  group: string;
  title: string;
  description: string;
  snapshot: string;
  tool_mode: string;
  input: { kind: string; messages?: { role: string; content: string }[]; catalog?: { jobs?: { job: string; metrics: Record<string, unknown> }[] } };
  checks: CheckInfo[];
  judge: boolean;
  tags: string[];
  smoke: boolean;
};

export type Catalog = {
  groups: { key: string; title: string }[];
  scenarios: ScenarioInfo[];
  hash: string;
  problems: string[];
  repeats: number;
};

export type Progress = {
  run_id: number;
  status: string;
  stage: string;
  total: number;
  done: number;
  scenario: string | null;
  title?: string;
  attempt: number | null;
  scenarios: number;
  started: number;
  last_score?: number;
  error?: string | null;
};

export type BenchStatus = {
  running: boolean;
  progress: Progress | null;
  llm: { configured: boolean; model: string; base_url: string };
  judge: boolean;
  repeats: number;
  report_running: boolean;
};

export type ScenarioStat = {
  group: string;
  score: number;
  attempts: number;
  passed: number;
  failed: number;
  median_total_ms: number | null;
  median_ttft_ms: number | null;
};

export type RunSummaryData = {
  scenarios: Record<string, ScenarioStat>;
  attempts: number;
  pass_rate: number | null;
  failures: number;
  median_ttft_ms: number | null;
  median_total_ms: number | null;
  median_tokens_per_s: number | null;
  tokens_in: number;
  tokens_out: number;
  overlapped: number;
  reload_suspected?: number;
};

export type GpuInfo = {
  driver?: string | null;
  gpus?: { name?: string; mem_total?: number; mem_used?: number; util_percent?: number; temperature_c?: number; power_w?: number }[];
} | null;

export type ColdStart = {
  ok: boolean;
  supported: boolean;
  loaded_before: boolean | null;
  size_vram_before?: number | null;
  size_vram?: number | null;
  total_ms?: number;
  ttft_ms?: number | null;
  tries?: number;
};

export type Run = {
  id: number;
  started: number;
  finished: number | null;
  status: "running" | "done" | "cancelled" | "failed";
  error: string | null;
  scope: string;
  repeats: number;
  scenario_hash: string | null;
  app_version: string | null;
  model: string | null;
  base_url: string | null;
  backend_kind: string | null;
  backend_version: string | null;
  params: string | null;
  quantization: string | null;
  context_length: number | null;
  overall: number | null;
  group_scores: Record<string, number> | null;
  summary: RunSummaryData | null;
  // list view
  gpu?: GpuInfo;
  cold_start?: ColdStart | null;
  tool_mode?: string | null;
  temperature?: number | null;
  // detail view
  meta?: {
    cold_start?: ColdStart | null;
    llm?: Record<string, unknown>;
    tool_mode?: string;
    backend?: Record<string, unknown>;
    gpu?: GpuInfo;
    judge?: { model: string; base_url: string } | null;
  } | null;
  attempts?: AttemptRow[];
};

export type AttemptRow = {
  id: number;
  run_id: number;
  scenario_id: string;
  grp: string;
  attempt: number;
  started: number;
  status: "ok" | "error" | "timeout";
  score: number | null;
  error: string | null;
  ttft_ms: number | null;
  total_ms: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  tokens_per_s: number | null;
  llm_calls: number | null;
  tool_calls: number | null;
  tool_mode: string | null;
  report_overlap: boolean;
  judge_score: number | null;
  /** Why the model may have been (re)loaded during this attempt; null = no sign of it. */
  reload_suspected: string | null;
};

export type CheckResult = { type: string; describe: string; weight: number; passed: boolean; reason: string };

export type Attempt = AttemptRow & {
  checks: CheckResult[] | null;
  answer: string | null;
  trace_ids: number[] | null;
  judge_reason: string | null;
  detail: {
    tool_calls?: { name: string; args: Record<string, unknown>; ok: boolean | null; kind: string | null }[];
    actions?: { action: string; status: string; target: string | null }[];
    repairs?: number;
    report?: { parsed: boolean; health: string | null; score: number | null; summary: string | null; findings: { severity: string; title: string; detail: string }[] } | null;
    prom?: { status: string; accepted: { name: string; query: string }[]; first_replies: { job: string; query: string; bare_counters: string[]; job_scoped: boolean }[]; rejected: { name?: string; query?: string; reason?: string }[] } | null;
  } | null;
};

export type SnapshotInfo = { name: string; created: number; description: string | null; size: number };
