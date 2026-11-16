import { ms } from "../logs/format";
import type { ColdStart, Effort, Run } from "./types";

export { ms };

/** How the chat routed its tools in a run: "off", "always", "auto (above 2000 tokens)";
 *  "not recorded" for runs made before the chat settings were kept. */
export const routingLabel = (run: Pick<Run, "meta">): string => {
  const chat = run.meta?.chat;
  if (!chat?.tool_routing) return "not recorded";
  return chat.tool_routing === "auto" && chat.tool_routing_tokens
    ? `auto (above ${chat.tool_routing_tokens} tokens)`
    : chat.tool_routing;
};

/** 0-1 score as a percentage. */
export const score = (s: number | null | undefined): string => (s === null || s === undefined ? "—" : `${Math.round(s * 100)}%`);

export const scoreLevel = (s: number | null | undefined): "ok" | "warn" | "crit" | "off" =>
  s === null || s === undefined ? "off" : s >= 0.9 ? "ok" : s >= 0.5 ? "warn" : "crit";

export const tps = (n: number | null | undefined): string => (n === null || n === undefined ? "—" : `${n.toFixed(n < 10 ? 1 : 0)} t/s`);

export const shortDate = (ts: number): string => {
  const d = new Date(ts * 1000);
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
};

export const elapsed = (from: number, to: number | null): string => {
  const s = Math.max(0, Math.round((to ?? Date.now() / 1000) - from));
  return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
};

export const backendLabel = (r: Pick<Run, "backend_kind" | "backend_version" | "base_url">): string => {
  const host = r.base_url ? r.base_url.replace(/^https?:\/\//, "").replace(/\/v1\/?$/, "") : "";
  const kind = r.backend_kind && r.backend_kind !== "openai-compatible" ? `${r.backend_kind}${r.backend_version ? ` ${r.backend_version}` : ""}` : "OpenAI-compatible";
  return host ? `${kind} · ${host}` : kind;
};

export const sizeLabel = (r: Pick<Run, "params" | "quantization" | "context_length">): string =>
  [r.params, r.quantization, r.context_length ? `${Math.round(r.context_length / 1024)}k ctx` : null].filter(Boolean).join(" · ") || "—";

/** "8.4 s (model was not loaded)" for the warm-up request of a run. */
export const coldStartText = (c: ColdStart | null | undefined): string => {
  if (!c || !c.ok) return "—";
  const state = c.loaded_before === null ? "load state unknown" : c.loaded_before ? "model was already loaded" : "model was not loaded";
  return `${ms(c.total_ms ?? null)} (${state})`;
};

export const EFFORTS: Effort[] = ["auto", "none", "low", "medium", "high"];

export const EFFORT_HELP: Record<Effort, string> = {
  auto: "not sent: the model decides",
  none: "no thinking",
  low: "a little",
  medium: "some",
  high: "a lot",
};

/** "auto" is the only level whose name needs a note. */
export const effortLabel = (e: Effort | null | undefined): string => (e ? (e === "auto" ? "auto" : e) : "—");

/** Characters as "12.4k" (reasoning is counted in characters, not tokens: the API reports no split). */
export const chars = (n: number | null | undefined): string => (n === null || n === undefined ? "—" : n >= 10000 ? `${(n / 1000).toFixed(0)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(Math.round(n)));

export const GROUP_SHORT: Record<string, string> = { report: "Reports", chat: "Chat", prom_plan: "Prom", robust: "Robust" };

export const statusLevel = (s: string): "ok" | "warn" | "crit" | "off" | "info" =>
  s === "done" ? "ok" : s === "running" ? "info" : s === "cancelled" ? "warn" : s === "failed" ? "crit" : "off";

/** A compact label for a run in comparisons and lists. */
export const runLabel = (r: Run): string => `${r.model ?? "?"}${r.quantization ? ` ${r.quantization}` : ""}`;

// ---- hardware

const GIB = 1024 ** 3;

/** Bytes as "18.2 GB" (binary gigabytes, the way GPUs report them). */
export const gb = (bytes: number | null | undefined): string => (bytes === null || bytes === undefined ? "—" : `${(bytes / GIB).toFixed(bytes / GIB < 10 ? 1 : 0)} GB`);

export const pct = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : `${Math.round(v)}%`);

export const watts = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : `${Math.round(v)} W`);

/** "18 GB · 76%" of a total. */
export const gbOf = (bytes: number | null | undefined, total: number | null | undefined): string =>
  bytes === null || bytes === undefined ? "—" : total ? `${gb(bytes)} · ${Math.round((bytes / total) * 100)}%` : gb(bytes);

/** Seconds since the start as "0:42" / "1:05:03". */
export const clock = (s: number): string => {
  const t = Math.max(0, Math.round(s));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
};

/** What the run says about the monitor's scheduled jobs: paused for the run, or left running. */
export function automationText(a: { paused: boolean; by_run: boolean; was_paused: boolean } | null | undefined): string {
  if (!a) return "running (not paused)";
  if (!a.paused) return "running (not paused)";
  return a.by_run ? "paused for the run" : "paused (already paused before the run)";
}

// ---- comparing

/** One cell of a comparison: what it prints and the number behind it (null = not comparable). */
export type Shown = { text: string; raw: number | null };

/** The cells holding the best value, judged on what they print: cells that print the same tie and
 * are all marked; when every cell prints the same, nothing is marked. The number is the part of
 * the text before " · " ("8.9 GB · 37%" compares as "8.9 GB"). `high`: larger is better. */
export function bestOf(cells: Shown[], high: boolean): Set<number> {
  const real = cells.map((c, i) => [c, i] as const).filter((p): p is readonly [Shown & { raw: number }, number] => p[0].raw !== null);
  if (real.length < 2) return new Set();
  const top = real.reduce((a, b) => ((high ? b[0].raw > a[0].raw : b[0].raw < a[0].raw) ? b : a));
  const key = (c: Shown) => c.text.split(" · ")[0];
  const winners = real.filter((p) => key(p[0]) === key(top[0])).map((p) => p[1]);
  return winners.length === real.length ? new Set() : new Set(winners);
}

/** temperature / top_p / top_k as they applied, with where the value came from ("1 (model default)",
 * "0.2 (set)"). The run recorded it, or the server filled it in from the model's current defaults for
 * an old run ("model default, read now"). Without either it says so; "not recorded" for runs made
 * before the setting was kept. */
export const samplingText = (llm: Record<string, unknown> | undefined, key: string): string => {
  const shown = (llm?.effective as Record<string, { text?: string } | undefined> | undefined)?.[key]?.text;
  if (shown) return shown;
  const v = llm?.[key];
  if (v === undefined || v === null) return "not recorded";
  return v === "auto" ? "auto · not recorded" : `${v} (set)`;
};

/** A sampling text that is not a value (muted in the tables). */
export const samplingMissing = (text: string): boolean => /not (recorded|reported)/.test(text);

const PROVIDERS: Record<string, string> = { ollama: "Ollama", "llama.cpp": "llama.cpp", vllm: "vLLM" };

/** The provider as the run recorded it ("Ollama"); null for a generic OpenAI-compatible endpoint. */
export const providerName = (kind: string | null | undefined): string | null => (kind ? PROVIDERS[kind] ?? null : null);
