// What the dashboard shows: jobs, their runs, the status of the summaries. All values are invented.
// Every time is relative to `now` (unix seconds, default: the real clock), so the texts "ran 5m ago" and
// "next in 10m" hold whenever a test or a layout check runs.
import type { ApiResponse } from "../types";

export type JobsAnswer = ApiResponse<"/api/jobs">;
export type JobItem = JobsAnswer["items"][number];
export type RunItem = ApiResponse<"/api/jobs/{slug}/runs">["items"][number];
export type StatusAnswer = ApiResponse<"/api/status">;

export const nowSeconds = () => Math.floor(Date.now() / 1000);

/** A run with every field filled; `over` changes what a test cares about. */
export function makeRun(over: Partial<RunItem> & Pick<RunItem, "id" | "job_slug">, now = nowSeconds()): RunItem {
  return {
    findings: [],
    queue: [],
    started: now - 600,
    finished: now - 590,
    status: "ok",
    summary: null,
    topics: [],
    trigger: "schedule",
    ...over,
  };
}

/** A job with every field filled; `over` changes what a test cares about. */
export function makeJob(over: Partial<JobItem> & Pick<JobItem, "slug" | "name">): JobItem {
  return {
    builtin: false,
    created: 1_700_000_000,
    created_by: "user",
    cron: null,
    domains: [],
    edited: false,
    enabled: true,
    interval_s: 1800,
    kind: "topic",
    last_run: null,
    members: [],
    model: "worker",
    next_run: null,
    notify: "changed",
    prompt: "Check the thing.",
    retired: false,
    running_run_id: null,
    summaries: [],
    updated: 1_700_000_000,
    ...over,
  };
}

/** A summary ("Example daily") over two topics, a topic in no summary, one turned off, one that never ran. */
export function dashboardJobs(now = nowSeconds()): JobsAnswer {
  const daily = makeJob({
    slug: "daily",
    name: "Example daily",
    kind: "summary",
    model: "main",
    interval_s: null,
    cron: "0 6 * * *",
    members: ["test:disk", "test:services"],
    next_run: now + 3 * 3600,
    last_run: makeRun(
      {
        id: 41,
        job_slug: "daily",
        status: "warn",
        started: now - 400,
        finished: now - 300,
        summary: "## Disk is filling up on host-a\n\nThe data disk is at 91 %.",
        findings: [
          { severity: "info", title: "Kernel update waiting", detail: "A newer kernel is installed.", evidence: [] },
          { severity: "crit", title: "Disk almost full", detail: "The data disk of host-a is at 91 %.", evidence: ["/dev/sda1  91%  /data"], verified: true },
          { severity: "warn", title: "Backup is old", detail: "The last backup finished two days ago.", evidence: [], verified: false },
        ],
        topics: [
          { slug: "test:disk", status: "warn", clause: "disk at 91 %" },
          { slug: "test:services", status: "ok", clause: "all services up" },
        ],
      },
      now,
    ),
  });
  const disk = makeJob({
    slug: "test:disk",
    name: "Disk usage",
    interval_s: 1800,
    summaries: ["daily"],
    next_run: now + 600,
    last_run: makeRun({ id: 31, job_slug: "test:disk", status: "warn", started: now - 1300, finished: now - 1200, summary: "Disk at 91 % on host-a." }, now),
  });
  const services = makeJob({
    slug: "test:services",
    name: "Services",
    interval_s: 3600,
    summaries: ["daily"],
    next_run: now + 1800,
    last_run: makeRun({ id: 32, job_slug: "test:services", status: "ok", started: now - 1900, finished: now - 1800, summary: "All services are up." }, now),
  });
  const solo = makeJob({ slug: "test:solo", name: "Solo check", enabled: false, interval_s: 7200, last_run: makeRun({ id: 33, job_slug: "test:solo", status: "ok", summary: "Fine." }, now) });
  const fresh = makeJob({ slug: "test:fresh", name: "Fresh check", interval_s: 900, next_run: now + 600 });
  return { items: [daily, disk, services, solo, fresh], retired: [] };
}

/** The runs of a job as the dashboard strips want them: its last run, and an older one of another status. */
export function dashboardRuns(slug: string, now = nowSeconds()): { items: RunItem[] } {
  const job = dashboardJobs(now).items.find((j) => j.slug === slug);
  if (!job?.last_run) return { items: [] };
  const older = makeRun({ id: job.last_run.id - 10, job_slug: slug, status: "ok", started: now - 5 * 3600, finished: now - 5 * 3600 + 20 }, now);
  return { items: [job.last_run, older] };
}

/** The status the dashboard needs for its summary cards: `overall` with the state of "daily".
 *  (The shell's own `status` fixture has no `overall`, so every summary there reads "has not run yet".) */
export function dashboardStatus(now = nowSeconds()): StatusAnswer {
  return {
    automation: { paused: false },
    jobs: [],
    llm_configured: true,
    main_model: "test-main",
    worker_model: "test-worker",
    now,
    pending_approvals: 0,
    uptime_s: 7200,
    version: "9.9.9",
    overall: {
      status: "warn",
      summaries: [{ slug: "daily", name: "Example daily", status: "warn", headline: "Disk is filling up on host-a", finished: now - 300, run_id: 41 }],
    },
  };
}
