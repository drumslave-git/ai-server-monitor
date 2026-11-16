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
  effort?: Effort | null;
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
  automation_paused: boolean;
};

export type StartBody = { group?: string; repeats?: number; efforts?: Effort[]; pause_automation?: boolean };

/** Peak and average of one measurement over some samples. */
export type HwStat = { avg: number; peak: number } | null;

/** Hardware numbers over a set of samples (one attempt, one scenario, or the whole run). */
export type HwNumbers = {
  n: number;
  gpu_util: HwStat;
  vram_used: HwStat;
  llm_vram: HwStat;
  power_w: HwStat;
  temp_c: HwStat;
  cpu: HwStat;
  ram_used: HwStat;
  vram_total: number | null;
  ram_total: number | null;
} | null;

/** What the recording says about itself. `llm_here`: the LLM server was seen on this host's GPU
 * ("here"), is not on this host ("elsewhere"), or the endpoint is local but no process was
 * recognised ("unknown"). */
export type HwMeta = {
  recorded: boolean;
  interval_s?: number;
  samples?: number;
  gpu?: boolean;
  gpu_name?: string | null;
  gpu_count?: number;
  llm_process?: string | null;
  llm_here?: "here" | "elsewhere" | "unknown";
  baseline?: { vram_used: number | null; ram_used: number | null; cpu: number | null; gpu_util: number | null } | null;
};

/** What the backend has in memory for the model. */
export type LoadedModel = {
  source: string;
  size: number | null;
  size_vram: number | null;
  gpu_percent: number | null;
  context_length: number | null;
} | null;

/** The time series of a run, one array per column. */
export type HwSeries = {
  recorded: boolean;
  interval_s: number | null;
  count: number;
  points: Record<"ts" | "gpu_util" | "vram_used" | "vram_total" | "llm_vram" | "power_w" | "temp_c" | "cpu" | "ram_used" | "ram_total", (number | null)[]>;
};

/** The thinking levels: the API's reasoning_effort values (auto = not sent). */
export type Effort = "auto" | "none" | "low" | "medium" | "high";

/** One thinking level of a run: what the model did at it, over every attempt made at it. */
export type LevelStat = {
  attempts: number;
  overall: number | null;
  group_scores: Record<string, number>;
  pass_rate: number | null;
  failed: number;
  /** Answers that never came: the model used the whole output limit on thinking, or only thought. */
  empty: number;
  /** Answers that were written but cut off by the output limit. */
  limit: number;
  median_total_ms: number | null;
  median_tokens_out: number | null;
  median_reasoning_chars: number | null;
  tokens_out: number;
  reasoning_chars: number;
  hw?: HwNumbers;
};

export type ScenarioLevelStat = { score: number; attempts: number; failed: number; empty: number; median_total_ms: number | null };

export type ScenarioStat = {
  levels?: Partial<Record<Effort, ScenarioLevelStat>>;
  group: string;
  score: number;
  attempts: number;
  passed: number;
  failed: number;
  median_total_ms: number | null;
  median_ttft_ms: number | null;
  hw?: HwNumbers;
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
  empty_answers?: number;
  limit_hits?: number;
  /** Per thinking level; absent on runs made at one level before levels existed. */
  levels?: Partial<Record<Effort, LevelStat>>;
  reload_suspected?: number;
  hardware?: HwNumbers;
};

/** meta.chat: the chat settings of the app that the run's chat scenarios ran under. */
export type ChatMeta = {
  tool_routing?: "auto" | "always" | "off";
  tool_routing_tokens?: number;
  max_steps?: number;
  history_messages?: number;
  max_tool_result_chars?: number;
  tool_timeout_s?: number;
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

/** A short plain-language reason for a scenario's score, built from the recorded checks. */
export type Explanation = { ok: boolean; lines: string[]; attempts: number; errored: number; empty: number; limit: number };

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
  temperature?: number | string | null;
  // detail view
  meta?: {
    cold_start?: ColdStart | null;
    llm?: Record<string, unknown>;
    tool_mode?: string;
    /** The chat settings the run used. Absent on runs made before they were recorded. */
    chat?: ChatMeta;
    backend?: Record<string, unknown>;
    gpu?: GpuInfo;
    /** Absent on runs made before hardware was recorded. */
    hardware?: HwMeta;
    loaded_model?: LoadedModel;
    judge?: { model: string; base_url: string } | null;
    /** Whether the monitor's scheduled jobs were paused during the run; absent on older runs
     *  (they were not). `by_run`: this run paused them (and put them back), not the user. */
    automation?: { paused: boolean; by_run: boolean; was_paused: boolean } | null;
    /** The thinking levels the run tested; absent on older runs. */
    levels?: { tested: Effort[]; applies_to: string; fixed: string };
  } | null;
  attempts?: AttemptRow[];
  /** Per scenario id; detail view only. */
  explanations?: Record<string, Explanation>;
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
  /** Hardware numbers while this attempt ran; null when not recorded. */
  hw?: HwNumbers;
  /** The thinking level it ran at; null = one no level changes (Prometheus planner) or an older run. */
  effort?: Effort | null;
  /** Characters of reasoning the model wrote over the attempt's calls. */
  reasoning_chars?: number | null;
  answer_issue?: "empty" | "limit" | null;
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
