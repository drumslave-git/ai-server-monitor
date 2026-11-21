// What the Jobs page shows beyond the dashboard's jobs: one job by slug, one run with its queue, the MCP tools
// page the domains field reads, and a few more jobs for the list. All values are invented.
// Every time is relative to `now` (unix seconds, default: the real clock).
import type { ApiRequest, ApiResponse } from "../types";
import { dashboardJobs, makeJob, makeRun, nowSeconds, type JobItem, type RunItem } from "./dashboard";

export type { JobItem, RunItem };
export type McpToolsAnswer = ApiResponse<"/api/mcp/tools">;
export type ClassifyAnswer = ApiResponse<"/api/jobs/classify-domains", "post">;
export type JobCreateBody = ApiRequest<"/api/jobs", "post">;
export type JobUpdateBody = ApiRequest<"/api/jobs/{slug}", "put">;

/** The dashboard's jobs plus a builtin, an edited builtin, one made by the LLM, one with a cron schedule and one
 *  without a colon in its slug: what the list shows when it has something of every kind. */
export function jobsList(now = nowSeconds()): ApiResponse<"/api/jobs"> {
  const base = dashboardJobs(now);
  return {
    ...base,
    items: [
      ...base.items,
      makeJob({ slug: "test:builtin", name: "Builtin check", builtin: true, created_by: "builtin", interval_s: 3600, next_run: now + 900 }),
      makeJob({ slug: "test:edited", name: "Edited builtin", builtin: true, edited: true, created_by: "builtin", interval_s: 86400, next_run: now + 7200 }),
      makeJob({ slug: "other:llm", name: "Made by the model", created_by: "llm", cron: "30 7 * * 1", interval_s: null, next_run: now + 86400 }),
      makeJob({ slug: "loose", name: "Loose check", interval_s: null, cron: null }),
    ],
  };
}

/** One job (or summary) of the dashboard's list by slug, or undefined. */
export const jobBySlug = (slug: string, now = nowSeconds()): JobItem | undefined => dashboardJobs(now).items.find((j) => j.slug === slug);

/** A run of a job with the fields the run view shows: model, steps, tokens, the MCP domains. */
export function detailedRun(over: Partial<RunItem> & Pick<RunItem, "id" | "job_slug">, now = nowSeconds()): RunItem {
  return makeRun(
    {
      duration_s: 95,
      model: "test-worker",
      steps: 4,
      prompt_tokens: 1200,
      completion_tokens: 340,
      mcp: { domains: ["docker"], unreachable: {}, unsorted: [] },
      ...over,
    },
    now,
  );
}

/** One run by id as `GET /api/jobs/runs/<id>` answers: a summary run carries its queue in the order the jobs ran.
 *  Known ids are the last runs of the dashboard's jobs; the queue of 41 is the runs 31 (test:disk) and 32 (test:services). */
export function runWithQueue(id: number, now = nowSeconds()): RunItem | undefined {
  const last = dashboardJobs(now).items.flatMap((j) => (j.last_run ? [j.last_run] : []));
  const run = last.find((r) => r.id === id);
  if (!run) return undefined;
  if (id !== 41) return detailedRun(run, now);
  const queue = last.filter((r) => r.id === 31 || r.id === 32).map((r) => ({ ...r, queue_run_id: 41 }));
  return detailedRun({ ...run, queue }, now);
}

/** The MCP tools the domains field offers: "docker" (2 of 3 tools on, one of them writes), "storage" (1 of 1 on),
 *  "unused" (a domain without tools) and "off" (tools, none on). */
export function mcpTools(): McpToolsAnswer {
  const tool = (server: string, name: string, over: Partial<McpToolsAnswer["tools"][number]> = {}): McpToolsAnswer["tools"][number] => ({
    name: `${server}_${name}`,
    server,
    tool: name,
    description: `Fake tool ${name}.`,
    domain: "docker",
    domain_id: 1,
    enabled: true,
    kind: "read",
    read_only: true,
    seen_ts: 1_700_000_000,
    set_by: "model",
    sort_error: null,
    ...over,
  });
  return {
    sorting: false,
    servers: [{ name: "fake-host", enabled: true, error: null, listed_ts: 1_700_000_000 }],
    domains: [
      { id: 1, name: "docker", description: "Containers on fake-host.", set_by: "model", tools: 3, used_by: [] },
      { id: 2, name: "storage", description: "Disks on fake-host.", set_by: "model", tools: 1, used_by: [] },
      { id: 3, name: "unused", description: "Nothing sorted into it.", set_by: "owner", tools: 0, used_by: [] },
      { id: 4, name: "off", description: "Tools nobody switched on.", set_by: "owner", tools: 1, used_by: [] },
    ],
    tools: [
      tool("fake-host", "list_containers"),
      tool("fake-host", "restart_container", { kind: "write", read_only: false }),
      tool("fake-host", "container_logs", { enabled: false }),
      tool("fake-host", "disk_usage", { domain: "storage", domain_id: 2 }),
      tool("fake-host", "old_tool", { domain: "off", domain_id: 4, enabled: false }),
    ],
  };
}
