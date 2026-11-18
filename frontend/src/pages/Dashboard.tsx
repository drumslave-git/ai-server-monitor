import { useEffect, useState } from "react";
import { useAutomation } from "../automation";
import { api, type StatusResponse } from "../api";
import { ago, clock, until } from "../fmt";
import {
  errorText,
  findingCounts,
  firstLine,
  runJob,
  SEVERITY_LEVEL,
  STATUS_LEVEL,
  STATUS_TEXT,
  useRunning,
  type Job,
  type JobRun,
} from "../jobs";
import { useStream } from "../live";
import { Badge, Dot, Empty, Markdown, PageHead, Panel, useNow, usePoll } from "../ui";
import { ExplainButton } from "../components/Explain";
import { Findings } from "./Jobs";

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
  useStream(["job.finished", "automation"], () => {
    status.reload();
    jobs.reload();
  });

  if ((status.error && !status.data) || (jobs.error && !jobs.data))
    return <p className="error">Backend unreachable: {status.error ?? jobs.error}</p>;
  if (!status.data || !jobs.data) return <p className="muted">Loading…</p>;
  const s = status.data;
  const paused = Boolean(auto?.paused);
  const summary = items.find((j) => j.kind === "summary");
  const topics = items.filter((j) => j.kind !== "summary");
  const neverRan = items.every((j) => !j.last_run) && running.size === 0;

  return (
    <div className="stack">
      <PageHead title="Dashboard" />
      {!s.llm_configured && (
        <div className="notice">
          No model is configured, so no job can run. <a href="#/settings/llm">Open the model settings</a>
        </div>
      )}
      <Overall
        overall={s.overall ?? null}
        run={overallRun.data ?? null}
        summary={summary}
        names={Object.fromEntries(items.map((j) => [j.slug, j.name]))}
        running={summary ? running.has(summary.slug) : false}
        paused={paused}
        now={now}
        neverRan={neverRan}
      />
      {topics.length === 0 ? (
        <Panel>
          <Empty>
            No topic jobs yet. <a href="#/jobs/new">Create one</a>
          </Empty>
        </Panel>
      ) : (
        <div className="job-cards">
          {topics.map((job) => (
            <JobCard key={job.slug} job={job} running={running.has(job.slug)} paused={paused} now={now} />
          ))}
        </div>
      )}
    </div>
  );
}

function Overall({
  overall,
  run,
  summary,
  names,
  running,
  paused,
  now,
  neverRan,
}: {
  overall: StatusResponse["overall"] | null;
  run: JobRun | null;
  summary: Job | undefined;
  /** Job names by slug, for the summary's topic list. */
  names: Record<string, string>;
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
  const nextText = !summary
    ? null
    : !summary.enabled
      ? "Summary job is turned off"
      : paused
        ? "Schedule paused"
        : summary.next_run
          ? `Next ${until(summary.next_run, now)}`
          : null;
  const shown = run && overall && run.id === overall.run_id ? run : null;

  return (
    <Panel className="overall">
      <div className="overall-head">
        <div className="overall-title">
          {running ? (
            <Badge level="info">Summary running</Badge>
          ) : overall ? (
            <Badge level={STATUS_LEVEL[overall.status]}>{STATUS_TEXT[overall.status]}</Badge>
          ) : (
            <Badge level="off">No summary yet</Badge>
          )}
          <h3 className="overall-headline">
            {overall ? overall.headline || "The summary has no headline" : neverRan ? "The jobs have not run yet" : "The summary job has not run yet"}
          </h3>
          <p className="muted small">
            {overall?.finished ? (
              <span title={new Date(overall.finished * 1000).toLocaleString()}>Summary from {ago(overall.finished, now)}</span>
            ) : neverRan ? (
              "The first runs come on schedule, or start one with Run now."
            ) : (
              "It sums up the latest results of the topic jobs."
            )}
            {nextText && <> · {nextText}</>}
            {summary && (
              <>
                {" · "}
                <a href={`#/jobs/${summary.slug}`}>History</a>
              </>
            )}
          </p>
        </div>
        <div className="overall-actions">
          {shown && <ExplainButton runId={shown.id} />}
          {summary && (
            <button className="btn" onClick={start} disabled={busy || running}>
              {running ? "Running…" : "Run summary now"}
            </button>
          )}
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {shown && shown.topics.length > 0 && (
        <ul className="topic-lines" aria-label="Topics">
          {shown.topics.map((t) => (
            <li key={t.slug}>
              <Dot level={t.status === "none" ? "off" : STATUS_LEVEL[t.status]} />
              <a href={`#/jobs/${t.slug}`}>{names[t.slug] ?? t.slug}</a>
              <span className="muted">{t.clause}</span>
            </li>
          ))}
        </ul>
      )}
      {shown?.status === "error" && shown.error && shown.error.trim() !== overall?.headline && <pre className="run-error">{shown.error}</pre>}
      {/* the headline above is the summary's first line */}
      {shown?.summary && withoutFirstLine(shown.summary) && <Markdown text={withoutFirstLine(shown.summary)} />}
      {shown && shown.findings.length > 0 && <Findings findings={shown.findings} runId={shown.id} />}
    </Panel>
  );
}

const SEVERITY_WORD = { crit: "critical", warn: "warning", info: "info" } as const;

const withoutFirstLine =(md: string) => md.trim().split("\n").slice(1).join("\n").trim();

function JobCard({ job, running, paused, now }: { job: Job; running: boolean; paused: boolean; now: number }) {
  const run = job.last_run ?? null;
  const counts = findingCounts(run?.findings);
  const line = run ? (run.status === "error" ? run.error ?? "" : firstLine(run.summary)) : "";
  const next = !job.enabled ? null : paused ? "Schedule paused" : job.next_run ? `Next ${until(job.next_run, now)}` : null;
  return (
    <a className={`job-card${job.enabled ? "" : " off"}`} href={`#/jobs/${job.slug}`}>
      <div className="job-card-top">
        <strong>{job.name}</strong>
        {running ? (
          <Badge level="info">Running</Badge>
        ) : !job.enabled ? (
          <Badge level="off">Disabled</Badge>
        ) : run ? (
          <Badge level={STATUS_LEVEL[run.status]}>{STATUS_TEXT[run.status]}</Badge>
        ) : (
          <Badge level="off">Never run</Badge>
        )}
      </div>
      <p className={`job-card-line${run?.status === "error" ? " error" : ""}`}>{line || <span className="muted">{run ? "No summary" : running ? "First run in progress" : job.enabled ? "Waiting for its first run" : "Turned off"}</span>}</p>
      {counts.crit + counts.warn + counts.info > 0 && (
        <div className="job-card-counts" aria-label="Findings">
          {(["crit", "warn", "info"] as const).map(
            (sev) =>
              counts[sev] > 0 && (
                <Badge key={sev} level={SEVERITY_LEVEL[sev]}>
                  {counts[sev]} {SEVERITY_WORD[sev]}
                </Badge>
              ),
          )}
        </div>
      )}
      <div className="job-card-foot">
        {run?.finished && <span title={clock(run.finished)}>Ran {ago(run.finished, now)}</span>}
        {next && <span title={job.next_run && !paused ? clock(job.next_run) : undefined}>{next}</span>}
      </div>
    </a>
  );
}
