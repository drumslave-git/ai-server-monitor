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

// Texts inside a line of lowercase words (a table cell, a meta line). Names keep their capitals.
export const STATUS_WORD: Record<RunStatus, string> = { ok: "OK", warn: "warning", crit: "critical", error: "error", running: "running" };
export const TRIGGER_WORD: Record<JobRun["trigger"], string> = { schedule: "scheduled", manual: "manual", llm: "by the LLM" };
export const MODEL_WORD: Record<Job["model"], string> = { worker: "worker", main: "main" };
/** "builtin", "builtin, edited", "custom, made by the LLM". */
export function originWord(job: Pick<Job, "builtin" | "edited" | "created_by">): string {
  if (job.builtin) return job.edited ? "builtin, edited" : "builtin";
  return job.created_by === "llm" ? "custom, made by the LLM" : "custom, made by a user";
}

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


/** "every 30 minutes", "at 06:00", "no schedule": the schedule inside a line of lowercase words.
 *  (cronstrue starts its sentence with a capital; a cron expression it cannot read is shown as written) */
export function scheduleWord(job: Pick<Job, "interval_s" | "cron">): string {
  if (job.interval_s) return intervalText(job.interval_s);
  if (job.cron) {
    const words = cronText(job.cron);
    return words ? words.charAt(0).toLowerCase() + words.slice(1) : job.cron;
  }
  return "no schedule";
}

/** When a summary or a job in no summary sends its result to Telegram (the API's notify values). */
export const NOTIFY_CHOICES: { value: Job["notify"]; label: string }[] = [
  { value: "changed", label: "when the result changes" },
  { value: "warn_crit", label: "on warning, critical or error" },
  { value: "always", label: "after every run" },
  { value: "never", label: "never (only when asked)" },
];
export const NOTIFY_WORD = Object.fromEntries(NOTIFY_CHOICES.map((c) => [c.value, c.label])) as Record<Job["notify"], string>;

/** Where a job runs, inside a line of lowercase words: "in Health, Daily", its own schedule, or
 *  "no schedule (manual)". `names`: summary slug → name. */
export function whereWord(job: Pick<Job, "kind" | "summaries" | "interval_s" | "cron">, names: Record<string, string>): string {
  if (job.kind === "topic" && job.summaries.length) return `in ${job.summaries.map((s) => names[s] ?? s).join(", ")}`;
  if (job.interval_s || job.cron) return scheduleWord(job);
  return "no schedule (manual)";
}

/** The group a job is shown in: the part of its slug before ":", whoever made it; jobs whose slug
 *  has no colon form one group at the end. */
export const NO_PREFIX_GROUP = "no prefix";
export function jobGroups<T extends Pick<Job, "slug">>(jobs: T[]): { label: string; jobs: T[] }[] {
  const groups = new Map<string, T[]>();
  for (const job of jobs) {
    const colon = job.slug.indexOf(":");
    const label = colon > 0 ? job.slug.slice(0, colon) : NO_PREFIX_GROUP;
    groups.set(label, [...(groups.get(label) ?? []), job]);
  }
  const last = (label: string) => (label === NO_PREFIX_GROUP ? 1 : 0);
  return [...groups.entries()]
    .sort(([a], [b]) => last(a) - last(b) || a.localeCompare(b))
    .map(([label, list]) => ({ label, jobs: list }));
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
