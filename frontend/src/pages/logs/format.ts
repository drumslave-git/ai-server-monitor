import type { LlmCallSummary } from "./types";

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1) return "<1 ms";
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 2 : 1)} s`;
  const minutes = Math.floor(value / 60_000);
  return `${minutes}m ${Math.round((value % 60_000) / 1000)}s`;
}

export function num(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toLocaleString();
}

export function tokens(call: Pick<LlmCallSummary, "prompt_tokens" | "completion_tokens" | "usage_estimated">): string {
  if (call.prompt_tokens === null && call.completion_tokens === null) return "—";
  return `${num(call.prompt_tokens)} → ${num(call.completion_tokens)}${call.usage_estimated ? " (est.)" : ""}`;
}

export function approxTokens(chars: number): number {
  return Math.max(1, Math.round(chars / 4));
}

export function stamp(ts: number): string {
  const d = new Date(ts * 1000);
  const day = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return `${day}, ${time}`;
}

export function clockTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false });
}

export function messageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => (p && typeof p === "object" && "text" in p ? String((p as { text?: string }).text ?? "") : "")).join("\n");
  return content === null || content === undefined ? "" : JSON.stringify(content, null, 2);
}

/** Pretty JSON when the text is JSON, otherwise the text as it is. */
export function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export const PURPOSE_LABEL: Record<string, string> = {
  chat: "Chat",
  chat_title: "Chat title",
  subagent: "Worker sub-task",
  job: "Job",
  test: "Connection test",
  other: "Other",
};

/** Purposes of removed features (benchmarks, reports, ...) still sit in old rows: one neutral label. */
export const purposeLabel = (p: string) => PURPOSE_LABEL[p] ?? "Earlier feature";
