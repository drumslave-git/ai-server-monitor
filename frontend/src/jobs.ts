// Shared bits of the Dashboard and the Jobs page: types, status levels, schedules in words,
// and which jobs are running right now.
import cronstrue from "cronstrue";
import { useEffect, useState } from "react";
import { api, ApiError, type Schemas } from "./api";
import { useStream } from "./live";
import type { Level } from "./ui";

export type Job = Schemas["Job"];
export type JobRun = Schemas["JobRun"];
export type Finding = JobRun["findings"][number];
export type RunStatus = JobRun["status"];

export const STATUS_LEVEL: Record<RunStatus, Level> = { ok: "ok", warn: "warn", crit: "crit", error: "crit", running: "info" };
export const STATUS_TEXT: Record<RunStatus, string> = { ok: "OK", warn: "Warning", crit: "Critical", error: "Error", running: "Running" };
export const SEVERITY_LEVEL: Record<Finding["severity"], Level> = { info: "info", warn: "warn", crit: "crit" };
export const SEVERITY_TEXT: Record<Finding["severity"], string> = { info: "Info", warn: "Warning", crit: "Critical" };
export const TRIGGER_TEXT: Record<JobRun["trigger"], string> = { schedule: "Scheduled", manual: "Manual", llm: "By the LLM" };
/** "Builtin", "Builtin, edited", "Custom, made by the LLM". */
export function originText(job: Pick<Job, "builtin" | "edited" | "created_by">): string {
  if (job.builtin) return job.edited ? "Builtin, edited" : "Builtin";
  return job.created_by === "llm" ? "Custom, made by the LLM" : "Custom, made by a user";
}
export const MODEL_TEXT: Record<Job["model"], string> = { worker: "Worker", main: "Main" };

export const UNITS = [
  { key: "days", s: 86400 },
  { key: "hours", s: 3600 },
  { key: "minutes", s: 60 },
] as const;

/** 1800 → "every 30 minutes", 3600 → "every hour". */
export function intervalText(seconds: number): string {
  for (const u of UNITS) {
    if (seconds >= u.s && seconds % u.s === 0) {
      const n = seconds / u.s;
      const one = u.key.slice(0, -1);
      return n === 1 ? `every ${one}` : `every ${n} ${u.key}`;
    }
  }
  return `every ${seconds} seconds`;
}

/** The crontab expression in words, or null when cronstrue cannot read it. */
export function cronText(cron: string): string | null {
  try {
    return cronstrue.toString(cron, { use24HourTimeFormat: true, verbose: false });
  } catch {
    return null;
  }
}

export function scheduleText(job: Pick<Job, "interval_s" | "cron">): string {
  if (job.interval_s) return capitalize(intervalText(job.interval_s));
  if (job.cron) return cronText(job.cron) ?? job.cron;
  return "No schedule";
}

export const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** First readable line of a markdown summary, without heading and emphasis marks. */
export function firstLine(md: string | null | undefined): string {
  const line = (md ?? "").split("\n").map((l) => l.trim()).find((l) => l && !/^[-=*_]{3,}$/.test(l)) ?? "";
  return line.replace(/^(#+|[-*+]|\d+\.)\s+/, "").replace(/\*\*|__|`/g, "");
}

export function findingCounts(findings: Finding[] | undefined): Record<Finding["severity"], number> {
  const counts = { crit: 0, warn: 0, info: 0 };
  for (const f of findings ?? []) counts[f.severity] += 1;
  return counts;
}

/** FastAPI's error detail (a string, or a list of {loc, msg}) as one sentence. */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.detail;
    if (Array.isArray(d))
      return d
        .map((x: { msg?: string; loc?: string[] }) => {
          const field = x.loc && x.loc.length > 1 ? String(x.loc[x.loc.length - 1]).replace(/_s$/, "").replace(/_/g, " ") : "";
          const msg = (x.msg ?? "").replace(/^Value error, /, "");
          return field ? `${capitalize(field)}: ${msg}` : msg;
        })
        .join(". ");
    if (typeof d === "string") return d;
    return `HTTP ${e.status}`;
  }
  return e instanceof Error ? e.message : String(e);
}

export const logsHref = (run: Pick<JobRun, "job_slug" | "trace_turn">) =>
  `#/logs/llm/for/job/${encodeURIComponent(run.job_slug)}${run.trace_turn ? `/${encodeURIComponent(run.trace_turn)}` : ""}`;

/** Slugs of the jobs that are running now: `running_run_id` of the last load, corrected by the
 *  job.* events that came after it. */
export function useRunning(jobs: Pick<Job, "slug" | "running_run_id">[] | undefined): Set<string> {
  const [overlay, setOverlay] = useState<Record<string, boolean>>({});
  // a fresh load is the truth again
  useEffect(() => setOverlay({}), [jobs]);
  useStream(["job"], (event) => {
    const slug = event.data.slug as string | undefined;
    if (!slug || (event.type !== "job.started" && event.type !== "job.finished")) return;
    setOverlay((prev) => ({ ...prev, [slug]: event.type === "job.started" }));
  });
  const running = new Set<string>();
  for (const j of jobs ?? []) if (overlay[j.slug] ?? j.running_run_id != null) running.add(j.slug);
  return running;
}

export function runJob(slug: string): Promise<{ run_id: number }> {
  return api.post<{ run_id: number }>(`/api/jobs/${encodeURIComponent(slug)}/run`, undefined);
}
