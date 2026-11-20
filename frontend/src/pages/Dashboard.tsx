import { useEffect, useState } from "react";
import { useAutomation } from "../automation";
import { api, type StatusResponse } from "../api";
import { ExplainButton } from "../components/Explain";
import { Findings } from "../components/Findings";
import { ago, clock, until } from "../fmt";
import { errorText, findingCounts, firstLine, runJob, scheduleWord, STATUS_LEVEL, STATUS_TEXT, useRunning, type Job, type JobRun } from "../jobs";
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
  LinkButton,
  Meta,
  Notice,
  Panel,
  Schedule,
  Split,
  Status,
  Text,
  type ScheduleRun,
  type Tone,
} from "../kit";
import { useStream } from "../live";
import { useNow, usePoll } from "../ui";

/** The clock of the lanes: half-hour slots, twelve hours back and three ahead. */
const SLOT_S = 1800;
const PAST_SLOTS = 24;
const FUTURE_SLOTS = 6;

type SummaryState = NonNullable<StatusResponse["overall"]>["summaries"][number];

export function DashboardPage() {
  const status = usePoll(() => api.get<StatusResponse>("/api/status"), 30_000);
  const jobs = usePoll(() => api.get<{ items: Job[] }>("/api/jobs"), 30_000);
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
  const summaries = items.filter((j) => j.kind === "summary");
  const enabled = summaries.filter((j) => j.enabled);
  const states: Record<string, SummaryState> = Object.fromEntries((s.overall?.summaries ?? []).map((x) => [x.slug, x]));
  const topics = items.filter((j) => j.kind !== "summary");
  // what the summaries said about each job, the newest summary run winning
  const clauses: Record<string, string> = {};
  for (const job of [...enabled].sort((a, b) => (a.last_run?.finished ?? 0) - (b.last_run?.finished ?? 0)))
    for (const t of job.last_run?.topics ?? []) clauses[t.slug] = t.clause;
  const withFindings = enabled.filter((j) => (j.last_run?.findings ?? []).length > 0);
  const findings = withFindings.reduce((n, j) => n + (j.last_run?.findings.length ?? 0), 0);

  return (
    <>
      {!s.llm_configured && (
        <Notice title="No model is configured, so no job can run.">
          <a href="#/settings/llm">Open the model settings</a>
        </Notice>
      )}
      {enabled.length === 0 ? (
        <Panel>
          <Hero meta="no summary is on" headline="Nothing sums up the jobs yet">
            <Text dim block>
              A summary runs its jobs one after another and sets the status shown here.{" "}
              {summaries.length > 0 ? "Turn one on, or create one, on the Jobs page." : "Create one on the Jobs page."}
            </Text>
            <Actions>
              <LinkButton variant="primary" icon="jobs" href="#/jobs">
                open jobs
              </LinkButton>
            </Actions>
          </Hero>
        </Panel>
      ) : (
        <Split stretch>
          {enabled.map((job) => (
            <SummaryCard key={job.slug} job={job} state={states[job.slug]} running={running.has(job.slug)} paused={paused} now={now} />
          ))}
        </Split>
      )}

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
          [...topics, ...summaries].map((job) => (
            <JobLane key={job.slug} job={job} runs={history.data[job.slug] ?? []} clause={clauses[job.slug]} running={running.has(job.slug)} paused={paused} now={now} />
          ))
        )}
      </Panel>

      {withFindings.length > 0 && (
        <Panel title="Findings" meta={`${findings} from ${withFindings.length === 1 ? withFindings[0].name : `${withFindings.length} summaries`}`}>
          {withFindings.map((job) => (
            <Findings key={job.slug} findings={job.last_run?.findings ?? []} runId={job.last_run?.id} source={withFindings.length > 1 ? job.name : undefined} />
          ))}
        </Panel>
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

/** One enabled summary: its state, headline and counts, when it ran and runs next, run now. */
function SummaryCard({ job, state, running, paused, now }: { job: Job; state: SummaryState | undefined; running: boolean; paused: boolean; now: number }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = job.last_run ?? null;
  const result = state?.status ?? null;
  const finished = state?.finished ?? run?.finished ?? null;
  const tone: Tone = result ? STATUS_LEVEL[result] : "off";
  const counts = findingCounts(run?.findings);
  const jobsWord = `${job.members.length} job${job.members.length === 1 ? "" : "s"}`;
  const next = paused ? "schedule paused" : job.next_run ? `next ${until(job.next_run, now)}` : null;

  const start = () => {
    setBusy(true);
    setError(null);
    runJob(job.slug).then(
      () => setBusy(false),
      (e) => {
        setBusy(false);
        setError(errorText(e));
      },
    );
  };

  return (
    <Panel hazard={result === "crit" || result === "error" ? "crit" : undefined} flex="1 1 460px">
      <Hero
        compact
        meta={
          <>
            {job.name} · {scheduleWord(job)}
            {finished ? (
              <>
                {" · "}
                <span title={clock(finished)}>ran {ago(finished, now)}</span>
              </>
            ) : null}
            {next && <> · {next}</>}
            {" · "}
            <a href={`#/jobs/${job.slug}`} aria-label={`History of ${job.name}`}>
              history
            </a>
          </>
        }
        status={
          running ? (
            <Status tone="info" blink>
              running: its {jobsWord}, then the summary
            </Status>
          ) : undefined
        }
        word={result ? STATUS_TEXT[result] : undefined}
        tone={tone}
        headline={result ? state?.headline || firstLine(run?.summary) || `${job.name} has no headline` : `${job.name} has not run yet`}
        aside={
          run ? (
            <Counters flex="1 1 220px">
              <Counter size="sm" value={counts.crit} label="critical" tone={counts.crit ? "crit" : "off"} />
              <Counter size="sm" value={counts.warn} label="warning" tone={counts.warn ? "warn" : "off"} />
              <Counter size="sm" value={counts.info} label="info" tone={counts.info ? "info" : "off"} />
            </Counters>
          ) : undefined
        }
      >
        {run?.status === "error" && run.error && run.error.trim() !== state?.headline && <Code wrap>{run.error}</Code>}
        {error && <ErrorText>{error}</ErrorText>}
        <Actions>
          <Button
            variant="primary"
            icon={running ? "running" : "run"}
            onClick={start}
            disabled={busy || running}
            title={`Runs its ${jobsWord} one after another, then the summary`}
            aria-label={`Run ${job.name} now: its ${jobsWord}, then the summary`}
          >
            {running ? "running…" : "run now"}
          </Button>
          {run && <ExplainButton runId={run.id} />}
        </Actions>
      </Hero>
    </Panel>
  );
}
