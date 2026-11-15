import { ms } from "../logs/format";
import type { ColdStart, Run } from "./types";

export { ms };

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

export const GROUP_SHORT: Record<string, string> = { report: "Reports", chat: "Chat", prom_plan: "Prom", robust: "Robust" };

export const statusLevel = (s: string): "ok" | "warn" | "crit" | "off" | "info" =>
  s === "done" ? "ok" : s === "running" ? "info" : s === "cancelled" ? "warn" : s === "failed" ? "crit" : "off";

/** A compact label for a run in comparisons and lists. */
export const runLabel = (r: Run): string => `${r.model ?? "?"}${r.quantization ? ` ${r.quantization}` : ""}`;
