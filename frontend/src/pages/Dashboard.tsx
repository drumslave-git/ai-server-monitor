import { useEffect, useState } from "react";
import { useAutomation } from "../automation";
import { api, type StatusResponse } from "../api";
import { ExplainButton } from "../components/Explain";
import { Findings } from "../components/Findings";
import { ago, clock, until } from "../fmt";
import { errorText, findingCounts, firstLine, runJob, STATUS_LEVEL, STATUS_TEXT, useRunning, type Job, type JobRun } from "../jobs";
import {
  Actions,
  Button,
  Code,
  Counter,
  Counters,
  Empty,
  ErrorText,
  Hero,
  Lane,
  Legend,
  Meta,
  Notice,
  Panel,
  Schedule,
  Split,
  Status,
  type ScheduleRun,
  type Tone,
} from "../kit";
import { useStream } from "../live";
import { Markdown, useNow, usePoll } from "../ui";

/** The clock of the lanes: half-hour slots, twelve hours back and three ahead. */
const SLOT_S = 1800;
const PAST_SLOTS = 24;
const FUTURE_SLOTS = 6;

export function DashboardPage() {
  const status = usePoll(() => api.get<StatusResponse>("/api/status"), 30_000);
  const jobs = usePoll(() => api.get<{ items: Job[] }>("/api/jobs"), 30_000);
  const runId = status.data?.overall?.run_id ?? null;
  const overallRun = usePoll(() => (runId ? api.get<JobRun>(`/api/jobs/runs/${runId}`) : Promise.resolve(null)), 300_000);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => overallRun.reload(), [runId]);
  const { state: auto } = useAutomation();
  const now = useNow();
  const items = jobs.data?.items ?? [];
  const running = useRunning(jobs.data?.items);
  const history = useRunHistory(items);
  useStream(["job.finished", "automation"], () => {
    status.reload();
    jobs.reload();
    history.reload();
  });

  if ((status.error && !status.data) || (jobs.error && !jobs.data)) return <ErrorText>Backend unreachable: {status.error ?? jobs.error}</ErrorText>;
  if (!status.data || !jobs.data) return <Meta>Loading…</Meta>;
  const s = status.data;
  const paused = Boolean(auto?.paused);
  const summary = items.find((j) => j.kind === "summary");
  const topics = items.filter((j) => j.kind !== "summary");
  const neverRan = items.every((j) => !j.last_run) && running.size === 0;
  const overall = s.overall ?? null;
  const shown = overallRun.data && overall && overallRun.data.id === overall.run_id ? overallRun.data : null;
  // what the summary said about each topic, by job
  const clauses = Object.fromEntries((shown?.topics ?? []).map((t) => [t.slug, t.clause]));

  return (
    <>
      {!s.llm_configured && (
        <Notice title="No model is configured, so no job can run.">
          <a href="#/settings/llm">Open the model settings</a>
        </Notice>
      )}
      <Overall overall={overall} run={shown} summary={summary} running={summary ? running.has(summary.slug) : false} paused={paused} now={now} neverRan={neverRan} />

      <Panel
        title="Jobs on the clock"
        meta="one cell = 30 minutes · the last 12 h, then the next 3 h"
        aside={
          <Legend>
            <Status tone="ok" plain>ok</Status>
            <Status tone="warn" plain>warning</Status>
            <Status tone="crit" plain>critical</Status>
            <Status tone="info" plain>running</Status>
            <Status tone="next" plain>scheduled</Status>
          </Legend>
        }
      >
        {items.length === 0 ? (
          <Empty>
            No jobs yet. <a href="#/jobs/new">Create one</a>
          </Empty>
        ) : (
          [...topics, ...(summary ? [summary] : [])].map((job) => (
            <JobLane key={job.slug} job={job} runs={history.data[job.slug] ?? []} clause={clauses[job.slug]} running={running.has(job.slug)} paused={paused} now={now} />
          ))
        )}
      </Panel>

      {shown && (
        <Split>
          <Panel title="Findings" meta={shown.findings.length > 0 ? `${shown.findings.length} from the summary` : undefined} flex="3 1 520px">
            {shown.findings.length > 0 ? <Findings findings={shown.findings} runId={shown.id} /> : <Empty>No findings in the summary.</Empty>}
          </Panel>
          {withoutFirstLine(shown.summary ?? "") && (
            <Panel title="What the summary says" pad flex="2 1 340px">
              <Markdown text={withoutFirstLine(shown.summary ?? "")} />
            </Panel>
          )}
        </Split>
      )}
    </>
  );
}

/** The recent runs of every job, as marks for its lane. One request per job, refreshed every minute. */
function useRunHistory(jobs: Job[]) {
  const slugs = jobs.map((j) => j.slug).join(",");
  const poll = usePoll(async () => {
    const entries = await Promise.all(
      jobs.map(async (job) => {
        const res = await api.get<{ items: JobRun[] }>(`/api/jobs/${encodeURIComponent(job.slug)}/runs?limit=${PAST_SLOTS + 1}`);
        return [job.slug, res.items.map((r): ScheduleRun => ({ at: r.started, tone: STATUS_LEVEL[r.status] }))] as const;
      }),
    );
    return Object.fromEntries(entries);
  }, 60_000);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => poll.reload(), [slugs]);
  return { data: poll.data ?? ({} as Record<string, ScheduleRun[]>), reload: poll.reload };
}

function JobLane({ job, runs, clause, running, paused, now }: { job: Job; runs: ScheduleRun[]; clause?: string; running: boolean; paused: boolean; now: number }) {
  const run = job.last_run ?? null;
  const tone: Tone = running ? "info" : !job.enabled || !run ? "off" : STATUS_LEVEL[run.status];
  const line = running
    ? run
      ? "running now"
      : "first run in progress"
    : !job.enabled
      ? "turned off"
      : !run
        ? "waiting for its first run"
        : run.status === "error"
          ? (run.error ?? "the run failed")
          : clause || firstLine(run.summary) || "no summary";
  const next = !job.enabled ? null : paused ? "schedule paused" : job.next_run ? `next ${until(job.next_run, now)}` : null;
  const when = [run?.finished ? `ran ${ago(run.finished, now)}` : null, next].filter(Boolean).join(" · ");
  return (
    <Lane name={job.name} tone={tone} blink={running} href={`#/jobs/${job.slug}`} line={line} when={when}>
      <Schedule
        runs={runs}
        now={now}
        slot={SLOT_S}
        past={PAST_SLOTS}
        future={FUTURE_SLOTS}
        next={job.enabled && !paused ? job.next_run : null}
        every={job.interval_s}
      />
    </Lane>
  );
}

function Overall({
  overall,
  run,
  summary,
  running,
  paused,
  now,
  neverRan,
}: {
  overall: StatusResponse["overall"] | null;
  /** The summary run behind `overall`, once loaded. */
  run: JobRun | null;
  summary: Job | undefined;
  running: boolean;
  paused: boolean;
  now: number;
  neverRan: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const start = () => {
    if (!summary) return;
    setBusy(true);
    setError(null);
    runJob(summary.slug).then(
      () => setBusy(false),
      (e) => {
        setBusy(false);
        setError(errorText(e));
      },
    );
  };
  const nextText = !summary ? null : !summary.enabled ? "summary job is turned off" : paused ? "schedule paused" : summary.next_run ? `next ${until(summary.next_run, now)}` : null;
  const tone: Tone = overall ? STATUS_LEVEL[overall.status] : "off";
  const counts = findingCounts(run?.findings);
  const needsOwner = overall?.status === "crit" || overall?.status === "error";

  return (
    <Panel hazard={needsOwner ? "crit" : undefined}>
      <Hero
        meta={
          <>
            {overall?.finished ? (
              <span title={clock(overall.finished)}>summary · ran {ago(overall.finished, now)}</span>
            ) : neverRan ? (
              "the first runs come on schedule, or start one with run summary now"
            ) : (
              "the summary job sums up the latest results of the topic jobs"
            )}
            {nextText && <> · {nextText}</>}
            {summary && (
              <>
                {" · "}
                <a href={`#/jobs/${summary.slug}`}>history</a>
              </>
            )}
          </>
        }
        status={
          running ? (
            <Status tone="info" blink>
              summary running
            </Status>
          ) : undefined
        }
        word={overall ? STATUS_TEXT[overall.status] : undefined}
        tone={tone}
        headline={overall ? overall.headline || "The summary has no headline" : neverRan ? "The jobs have not run yet" : "The summary job has not run yet"}
        aside={
          run ? (
            <Counters flex="1 1 320px">
              <Counter value={counts.crit} label="critical" tone={counts.crit ? "crit" : "off"} />
              <Counter value={counts.warn} label="warning" tone={counts.warn ? "warn" : "off"} />
              <Counter value={counts.info} label="info" tone={counts.info ? "info" : "off"} />
            </Counters>
          ) : undefined
        }
      >
        {run?.status === "error" && run.error && run.error.trim() !== overall?.headline && <Code wrap>{run.error}</Code>}
        {error && <ErrorText>{error}</ErrorText>}
        <Actions>
          {summary && (
            <Button variant="primary" onClick={start} disabled={busy || running}>
              {running ? "running…" : "run summary now"}
            </Button>
          )}
          {run && <ExplainButton runId={run.id} />}
        </Actions>
      </Hero>
    </Panel>
  );
}

const withoutFirstLine = (md: string) => md.trim().split("\n").slice(1).join("\n").trim();
