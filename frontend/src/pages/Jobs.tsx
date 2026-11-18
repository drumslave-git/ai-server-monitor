import { ArrowLeft, Play, X } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { useAutomation } from "../automation";
import { ExplainButton } from "../components/Explain";
import { api } from "../api";
import type { components } from "../api-types";
import { ago, clock, until } from "../fmt";
import {
  originText,
  cronText,
  errorText,
  logsHref,
  MODEL_TEXT,
  runJob,
  scheduleText,
  SEVERITY_LEVEL,
  SEVERITY_TEXT,
  STATUS_LEVEL,
  STATUS_TEXT,
  TRIGGER_TEXT,
  UNITS,
  useRunning,
  type Finding,
  type Job,
  type JobRun,
} from "../jobs";
import { useStream } from "../live";
import { Badge, Empty, Markdown, PageHead, Panel, useNow, usePoll } from "../ui";

/** Routes (params after `#/jobs`):
 *    (none)               list
 *    new                  create a custom job
 *    <slug>               a job: its runs, the newest one opened
 *    <slug>/runs/<id>     a job with that run opened
 *    <slug>/edit          edit form
 */
export function JobsPage({ params }: { params: string[] }) {
  const [slug, sub, id] = params;
  if (slug === "new") return <JobForm job={null} />;
  if (slug && sub === "edit") return <JobEdit slug={slug} />;
  if (slug) return <JobDetail slug={slug} runId={sub === "runs" && /^\d+$/.test(id ?? "") ? Number(id) : null} />;
  return <JobList />;
}

// ---------------------------------------------------------------- list

function JobList() {
  const jobs = usePoll(() => api.get<{ items: Job[] }>("/api/jobs"), 30_000);
  const items = jobs.data?.items ?? [];
  const running = useRunning(jobs.data?.items);
  const { state: auto } = useAutomation();
  const now = useNow();
  const [error, setError] = useState<string | null>(null);
  useStream(["job.finished"], () => jobs.reload());

  const toggle = (job: Job) =>
    api.put<Job>(`/api/jobs/${job.slug}`, { enabled: !job.enabled }).then(
      () => jobs.reload(),
      (e) => setError(errorText(e)),
    );
  const start = (job: Job) => runJob(job.slug).catch((e) => setError(errorText(e)));

  return (
    <div className="stack">
      <PageHead title="Jobs">
        <a className="btn" href="#/jobs/new">New job</a>
      </PageHead>
      <p className="muted">
        Each job is an LLM run on a schedule with a narrow topic. The summary job reads their latest results and sets the status on the Dashboard.
      </p>
      {error && <p className="error">{error}</p>}
      {jobs.error && !jobs.data && <p className="error">Backend unreachable: {jobs.error}</p>}
      {!jobs.data ? (
        !jobs.error && <p className="muted">Loading…</p>
      ) : items.length === 0 ? (
        <Panel>
          <Empty>No jobs.</Empty>
        </Panel>
      ) : (
        <Panel className="table-panel">
          <table className="table jobs-table">
            <thead>
              <tr>
                <th>On</th>
                <th>Job</th>
                <th>Model</th>
                <th>Schedule</th>
                <th>Last result</th>
                <th>Next run</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((job) => {
                const run = job.last_run;
                const isRunning = running.has(job.slug);
                const status = isRunning ? (
                  <Badge level="info">Running</Badge>
                ) : run ? (
                  <Badge level={STATUS_LEVEL[run.status]}>{STATUS_TEXT[run.status]}</Badge>
                ) : (
                  <Badge level="off">Never run</Badge>
                );
                const next = !job.enabled ? null : auto?.paused ? "paused" : job.next_run ? until(job.next_run, now) : null;
                return (
                  <tr key={job.slug} className={job.enabled ? undefined : "off"}>
                    <td>
                      <label className="switch" title={job.enabled ? "Turn off" : "Turn on"}>
                        <input type="checkbox" checked={job.enabled} onChange={() => void toggle(job)} aria-label={`${job.name} enabled`} />
                        <span />
                      </label>
                    </td>
                    <td data-label="Job">
                      <a href={`#/jobs/${job.slug}`} className="job-name">{job.name}</a>
                      <span className="muted small block hide-sm">{originText(job)}</span>
                    </td>
                    {/* phone: one line instead of the four columns that follow */}
                    <td className="jobs-sm small">
                      {status}
                      <span className="muted">
                        {next && <> · next {next}</>}
                        {" · "}
                        {scheduleText(job).replace(/^Every/, "every")}
                      </span>
                    </td>
                    <td data-label="Model">{MODEL_TEXT[job.model]}</td>
                    <td data-label="Schedule">{scheduleText(job)}</td>
                    <td data-label="Last result">
                      {status}
                      {run && !isRunning && (
                        <span className="muted small block" title={run.finished ? clock(run.finished) : undefined}>{ago(run.finished, now)}</span>
                      )}
                    </td>
                    <td data-label="Next run">
                      {!job.enabled ? (
                        <span className="muted">Off</span>
                      ) : auto?.paused ? (
                        <span className="muted">Paused</span>
                      ) : job.next_run ? (
                        <span title={clock(job.next_run)}>{until(job.next_run, now)}</span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="jobs-actions">
                      <button className="btn btn-sm" onClick={() => void start(job)} disabled={isRunning} aria-label={`Run ${job.name} now`}>
                        <Play size={14} strokeWidth={1.5} aria-hidden /> <span className="hide-sm">Run now</span>
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- form

type Unit = (typeof UNITS)[number]["key"];

function splitInterval(seconds: number | null | undefined): { n: string; unit: Unit } {
  if (!seconds) return { n: "1", unit: "hours" };
  for (const u of UNITS) if (seconds % u.s === 0) return { n: String(seconds / u.s), unit: u.key };
  return { n: String(Math.max(1, Math.round(seconds / 60))), unit: "minutes" };
}

function JobEdit({ slug }: { slug: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.get<Job>(`/api/jobs/${encodeURIComponent(slug)}`).then(setJob, (e) => setError(errorText(e)));
  }, [slug]);
  if (error) return <p className="error">{error}</p>;
  if (!job) return <p className="muted">Loading…</p>;
  return <JobForm key={job.updated} job={job} onReset={setJob} />;
}

function JobForm({ job, onReset }: { job: Job | null; onReset?: (job: Job) => void }) {
  const creating = job === null;
  const [name, setName] = useState(job?.name ?? "");
  const [slug, setSlug] = useState(job?.slug ?? "");
  const [slugTouched, setSlugTouched] = useState(false);
  const [prompt, setPrompt] = useState(job?.prompt ?? "");
  const [mode, setMode] = useState<"interval" | "cron">(job?.cron ? "cron" : "interval");
  const initial = splitInterval(job?.interval_s);
  const [n, setN] = useState(initial.n);
  const [unit, setUnit] = useState<Unit>(initial.unit);
  const [cron, setCron] = useState(job?.cron ?? "0 6 * * *");
  const [model, setModel] = useState<Job["model"]>(job?.model ?? "worker");
  const [enabled, setEnabled] = useState(job?.enabled ?? true);
  const [timeout, setTimeoutMin] = useState(job?.timeout_s ? String(job.timeout_s / 60) : "");
  const [steps, setSteps] = useState(job?.max_steps ? String(job.max_steps) : "");
  const [domains, setDomains] = useState<string[]>(job?.domains ?? []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cronWords = mode === "cron" ? cronText(cron) : null;
  const back = creating ? "#/jobs" : `#/jobs/${job.slug}`;

  const onName = (v: string) => {
    setName(v);
    if (creating && !slugTouched)
      setSlug(v.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40));
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const body: Record<string, unknown> = {
      name: name.trim(),
      prompt,
      model,
      enabled,
      timeout_s: timeout ? Math.round(Number(timeout) * 60) : null,
      max_steps: steps ? Number(steps) : null,
      domains,
    };
    if (mode === "interval") body.interval_s = Math.round(Number(n) * (UNITS.find((u) => u.key === unit)?.s ?? 60));
    else body.cron = cron.trim();
    if (creating) body.slug = slug;
    setBusy(true);
    setError(null);
    const req = creating ? api.post<Job>("/api/jobs", body) : api.put<Job>(`/api/jobs/${job.slug}`, body);
    req.then(
      (saved) => {
        location.hash = `#/jobs/${saved.slug}`;
      },
      (err) => {
        setBusy(false);
        setError(errorText(err));
      },
    );
  };

  const reset = () => {
    if (!job || !confirm(`Reset "${job.name}" to the version of this release? Your changes to it are lost.`)) return;
    setBusy(true);
    api.post<Job>(`/api/jobs/${job.slug}/reset`, undefined).then(
      (fresh) => {
        setBusy(false);
        onReset?.(fresh);
      },
      (err) => {
        setBusy(false);
        setError(errorText(err));
      },
    );
  };

  const remove = () => {
    if (!job || !confirm(`Delete the job "${job.name}" and all its runs? This cannot be undone.`)) return;
    setBusy(true);
    api.del(`/api/jobs/${job.slug}`).then(
      () => {
        location.hash = "#/jobs";
      },
      (err) => {
        setBusy(false);
        setError(errorText(err));
      },
    );
  };

  return (
    <div className="stack">
      <a className="back-link" href={back}>
        <ArrowLeft size={14} strokeWidth={1.5} aria-hidden /> {creating ? "Jobs" : job.name}
      </a>
      <PageHead title={creating ? "New job" : `Edit ${job.name}`}>
        {job?.builtin && job.edited && (
          <button className="btn" type="button" onClick={reset} disabled={busy}>
            Reset to default
          </button>
        )}
        {job && !job.builtin && (
          <button className="btn" type="button" onClick={remove} disabled={busy}>
            Delete
          </button>
        )}
      </PageHead>
      {job?.builtin && (
        <p className="muted">
          {job.edited
            ? "A builtin job you changed: it keeps your version and no longer follows later releases. Reset to default to follow them again."
            : "A builtin job. As long as you do not change it, it follows the version of later releases. Turning it on or off does not count as a change."}
        </p>
      )}
      <form className="panel job-form" onSubmit={submit}>
        <div className="field">
          <div className="field-label"><label htmlFor="job-name">Name</label></div>
          <div className="field-control">
            <input id="job-name" value={name} onChange={(e) => onName(e.target.value)} required maxLength={80} />
          </div>
        </div>
        <div className="field">
          <div className="field-label">
            <label htmlFor="job-slug">Slug</label>
            <p className="help">Its id in links, logs and metrics. {creating ? "Cannot be changed later." : ""}</p>
          </div>
          <div className="field-control">
            {creating ? (
              <input
                id="job-slug"
                className="mono"
                value={slug}
                onChange={(e) => {
                  setSlug(e.target.value);
                  setSlugTouched(true);
                }}
                required
                pattern="[a-z0-9][a-z0-9_-]*"
                title="Lower-case letters, digits, - and _"
              />
            ) : (
              <span className="mono">{job.slug}</span>
            )}
          </div>
        </div>
        <div className="field field-wide">
          <div className="field-label">
            <label htmlFor="job-prompt">Prompt</label>
            <p className="help">What the model checks, with which commands, and when a result counts as warning or critical. Keep the topic narrow.</p>
          </div>
          <textarea id="job-prompt" className="mono job-prompt" value={prompt} onChange={(e) => setPrompt(e.target.value)} required rows={16} />
        </div>
        <div className="field">
          <div className="field-label">
            <span className="label">Schedule</span>
            <p className="help">Cron times use the time zone set in Settings → General (the host's by default).</p>
          </div>
          <div className="field-control schedule">
            <div className="segmented" role="radiogroup" aria-label="Schedule type">
              <label className={mode === "interval" ? "active" : undefined}>
                <input type="radio" name="mode" checked={mode === "interval"} onChange={() => setMode("interval")} /> Interval
              </label>
              <label className={mode === "cron" ? "active" : undefined}>
                <input type="radio" name="mode" checked={mode === "cron"} onChange={() => setMode("cron")} /> Cron
              </label>
            </div>
            {mode === "interval" ? (
              <div className="schedule-row">
                <span>Every</span>
                <input type="number" min={1} step={1} className="input-num" value={n} onChange={(e) => setN(e.target.value)} required aria-label="Interval" />
                <select value={unit} onChange={(e) => setUnit(e.target.value as Unit)} aria-label="Unit">
                  {[...UNITS].reverse().map((u) => (
                    <option key={u.key} value={u.key}>{Number(n) === 1 ? u.key.slice(0, -1) : u.key}</option>
                  ))}
                </select>
              </div>
            ) : (
              <>
                <input className={`mono${cronWords ? "" : " invalid"}`} value={cron} onChange={(e) => setCron(e.target.value)} required aria-label="Cron expression" placeholder="0 6 * * *" />
                <p className={cronWords ? "help" : "error small"}>{cronWords ?? "Not a cron expression this page can read: minute hour day month weekday."}</p>
              </>
            )}
          </div>
        </div>
        <div className="field">
          <div className="field-label">
            <label htmlFor="job-model">Model</label>
            <p className="help">The worker model suits one narrow topic; the main model is the bigger one.</p>
          </div>
          <div className="field-control">
            <select id="job-model" value={model} onChange={(e) => setModel(e.target.value as Job["model"])}>
              <option value="worker">Worker</option>
              <option value="main">Main</option>
            </select>
          </div>
        </div>
        <DomainsField prompt={prompt} value={domains} onChange={setDomains} />
        <div className="field">
          <div className="field-label"><label htmlFor="job-enabled">Enabled</label></div>
          <div className="field-control">
            <label className="switch">
              <input id="job-enabled" type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              <span />
            </label>
          </div>
        </div>
        <div className="field">
          <div className="field-label">
            <label htmlFor="job-timeout">Time limit</label>
            <p className="help">Minutes. Empty: the default from Settings → Jobs.</p>
          </div>
          <div className="field-control">
            <input id="job-timeout" type="number" min={1} step="any" className="input-short" value={timeout} onChange={(e) => setTimeoutMin(e.target.value)} placeholder="Default" />
          </div>
        </div>
        <div className="field">
          <div className="field-label">
            <label htmlFor="job-steps">Step limit</label>
            <p className="help">Most tool-use rounds per run. Empty: the default from Settings → Jobs.</p>
          </div>
          <div className="field-control">
            <input id="job-steps" type="number" min={1} max={60} step={1} className="input-short" value={steps} onChange={(e) => setSteps(e.target.value)} placeholder="Default" />
          </div>
        </div>
        {job && (
          <p className="field-note">
            {job.created_by === "builtin" ? "Added with the app" : job.created_by === "llm" ? "Made by the LLM" : "Made by a user"}{" "}
            <span title={clock(job.created)}>{ago(job.created)}</span>, changed{" "}
            <span title={clock(job.updated)}>{ago(job.updated)}</span>.
          </p>
        )}
        {error && <p className="error">{error}</p>}
        <div className="form-buttons">
          <button className="btn btn-primary" type="submit" disabled={busy}>
            {creating ? "Create job" : "Save"}
          </button>
          <a className="btn btn-quiet" href={back}>Cancel</a>
        </div>
      </form>
    </div>
  );
}

type McpToolsPage = components["schemas"]["McpToolsPage"];

/** The job's MCP domains: its runs get exactly their enabled tools. "Classify" asks the worker
 *  model which domains the prompt needs and fills the field; nothing is saved until Save. */
function DomainsField({ prompt, value, onChange }: { prompt: string; value: string[]; onChange: (v: string[]) => void }) {
  const [page, setPage] = useState<McpToolsPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => {
    api.get<McpToolsPage>("/api/mcp/tools").then(setPage, () => setPage(null));
  }, []);
  const known = (page?.domains ?? []).filter((d) => d.tools > 0 || value.includes(d.name));
  const free = known.filter((d) => !value.includes(d.name));
  const tools = (page?.tools ?? []).filter((t) => t.enabled && t.domain && value.includes(t.domain));
  const writes = tools.filter((t) => t.kind === "write").length;
  const onIn = (name: string) => (page?.tools ?? []).filter((t) => t.enabled && t.domain === name).length;
  const classifiable = known.some((d) => onIn(d.name) > 0);
  const classify = () => {
    setBusy(true);
    setNote(null);
    api.post<{ domains: string[] }>("/api/jobs/classify-domains", { prompt }).then(
      (r) => {
        setBusy(false);
        onChange(r.domains);
        setNote(r.domains.length ? null : "The model picked no domain for this prompt.");
      },
      (err) => {
        setBusy(false);
        setNote(errorText(err));
      },
    );
  };
  return (
    <div className="field">
      <div className="field-label">
        <label htmlFor="job-domains">Domains</label>
        <p className="help">MCP tools the runs get: exactly the enabled tools of these domains. Empty: the shell only.</p>
      </div>
      <div className="field-control">
        <div className="domain-picker">
          {value.map((name) => (
            <span key={name} className="domain-chip">
              {name}
              <button type="button" className="domain-chip-x" aria-label={`Remove ${name}`} onClick={() => onChange(value.filter((v) => v !== name))}>
                <X size={12} strokeWidth={1.5} aria-hidden />
              </button>
            </span>
          ))}
          <select
            id="job-domains"
            value=""
            disabled={free.length === 0}
            onChange={(e) => e.target.value && onChange([...value, e.target.value].sort())}
          >
            <option value="">{free.length ? "Add domain…" : known.length ? "All added" : "No domains yet"}</option>
            {free.map((d) => (
              <option key={d.id} value={d.name}>
                {d.name} ({onIn(d.name)} of {d.tools} on)
              </option>
            ))}
          </select>
          <button className="btn" type="button" onClick={classify} disabled={busy || prompt.trim().length < 10 || !classifiable}
            title={classifiable ? "The worker model picks the domains this prompt needs" : "No domain has a tool switched on"}>
            {busy ? "Classifying…" : "Classify"}
          </button>
        </div>
        <p className="field-note">
          {value.length === 0
            ? "This job uses only the shell."
            : `This job can use ${tools.length} tool${tools.length === 1 ? "" : "s"}: ${tools.length - writes} read, ${writes} write.`}
        </p>
        {note && <p className="error">{note}</p>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- detail

function JobDetail({ slug, runId }: { slug: string; runId: number | null }) {
  const job = usePoll(() => api.get<Job>(`/api/jobs/${encodeURIComponent(slug)}`), 60_000);
  const runs = usePoll(() => api.get<{ items: JobRun[] }>(`/api/jobs/${encodeURIComponent(slug)}/runs?limit=50`), 60_000);
  const now = useNow();
  const [error, setError] = useState<string | null>(null);
  useStream(["job"], (e) => {
    if (e.data.slug !== slug) return;
    runs.reload();
    if (e.type === "job.finished") job.reload();
  });

  if (job.error && !job.data) return <p className="error">{job.error}</p>;
  if (!job.data) return <p className="muted">Loading…</p>;
  const j = job.data;
  const items = runs.data?.items ?? [];
  const selected = runId !== null ? items.find((r) => r.id === runId) ?? null : items[0] ?? null;
  const isRunning = items[0]?.status === "running";

  const start = () =>
    runJob(slug).then(
      (r) => {
        location.hash = `#/jobs/${slug}/runs/${r.run_id}`;
        runs.reload();
      },
      (e) => setError(errorText(e)),
    );

  return (
    <div className="stack">
      <a className="back-link" href="#/jobs">
        <ArrowLeft size={14} strokeWidth={1.5} aria-hidden /> Jobs
      </a>
      <PageHead title={j.name}>
        <a className="btn" href={`#/jobs/${slug}/edit`}>Edit</a>
        <button className="btn" onClick={() => void start()} disabled={isRunning}>
          <Play size={14} strokeWidth={1.5} aria-hidden /> Run now
        </button>
      </PageHead>
      <p className="job-meta muted">
        <span>{j.enabled ? scheduleText(j) : "Disabled"}</span>
        <span>{MODEL_TEXT[j.model]} model</span>
        <span>{originText(j)}</span>
        {j.enabled && j.next_run && <span title={clock(j.next_run)}>Next {until(j.next_run, now)}</span>}
      </p>
      {error && <p className="error">{error}</p>}
      {runs.data && items.length === 0 ? (
        <Panel>
          <Empty>No runs yet. The first comes on schedule, or start one with Run now.</Empty>
        </Panel>
      ) : (
        <div className="job-split">
          <Panel title="Runs" className="run-list">
            <ul>
              {items.map((r) => (
                <li key={r.id}>
                  <a className={`history-row${selected?.id === r.id ? " selected" : ""}`} href={`#/jobs/${slug}/runs/${r.id}`}>
                    <span className="history-top">
                      <Badge level={STATUS_LEVEL[r.status]}>
                        <span className="history-time" title={clock(r.started)}>{ago(r.started, now)}</span>
                      </Badge>
                      <span className="muted small run-trigger">{TRIGGER_TEXT[r.trigger]}</span>
                    </span>
                    <span className="run-numbers muted small">
                      {STATUS_TEXT[r.status]}
                      {r.duration_s != null && ` · ${seconds(r.duration_s)}`}
                      {tokenSum(r) > 0 && ` · ${tokenSum(r).toLocaleString()} tokens`}
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          </Panel>
          {selected ? <RunView run={selected} now={now} /> : runId !== null && runs.data ? <Panel><Empty>This run is not among the last 50.</Empty></Panel> : null}
        </div>
      )}
    </div>
  );
}

const tokenSum = (r: JobRun) => (r.prompt_tokens ?? 0) + (r.completion_tokens ?? 0);

function seconds(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

function RunView({ run, now }: { run: JobRun; now: number }) {
  return (
    <Panel
      className="run-view"
      title={<Badge level={STATUS_LEVEL[run.status]}>{STATUS_TEXT[run.status]}</Badge>}
      aside={
        <>
          {run.status !== "running" && <ExplainButton runId={run.id} />}
          <a href={logsHref(run)} title="This run's model and tool calls on the Logs page">
            LLM and tool calls
          </a>
        </>
      }
    >
      <dl className="run-facts">
        <dt>Started</dt>
        <dd>
          <span title={new Date(run.started * 1000).toLocaleString()}>{clock(run.started)}</span>
          <span className="muted"> · {ago(run.started, now)} · {TRIGGER_TEXT[run.trigger].toLowerCase()}</span>
        </dd>
        <dt>Duration</dt>
        <dd>{run.duration_s != null ? seconds(run.duration_s) : run.status === "running" ? "Still running" : "—"}</dd>
        <dt>Model</dt>
        <dd>
          <span className="mono">{run.model ?? "—"}</span>
          {run.steps != null && <span className="muted"> · {run.steps} step{run.steps === 1 ? "" : "s"}</span>}
          {tokenSum(run) > 0 && (
            <span className="muted">
              {" "}· {(run.prompt_tokens ?? 0).toLocaleString()} in, {(run.completion_tokens ?? 0).toLocaleString()} out
            </span>
          )}
        </dd>
        {run.mcp && (
          <>
            <dt>MCP</dt>
            <dd>
              {run.mcp.domains.length ? run.mcp.domains.join(", ") : "—"}
              {Object.entries(run.mcp.unreachable ?? {}).map(([server, why]) => (
                <span key={server} className="muted"> · server {server} unreachable: {why}</span>
              ))}
              {(run.mcp.unsorted ?? []).length > 0 && (
                <span className="muted"> · not sorted, not given: {(run.mcp.unsorted ?? []).join(", ")}</span>
              )}
            </dd>
          </>
        )}
      </dl>
      {run.error && <pre className="run-error">{run.error}</pre>}
      {run.status === "running" && <p className="muted run-wait">The model is working on it. This view updates when it ends.</p>}
      {run.summary && <Markdown text={run.summary} />}
      {run.findings.length > 0 && <Findings findings={run.findings} runId={run.id} />}
      {run.status !== "running" && !run.summary && !run.error && run.findings.length === 0 && <Empty>The run left no summary.</Empty>}
    </Panel>
  );
}

const SEVERITY_ORDER = { crit: 0, warn: 1, info: 2 } as const;

export function Findings({ findings, runId }: { findings: Finding[]; runId?: number }) {
  // (sorted for display; the Explain button names a finding by its stored index)
  const sorted = findings.map((f, index) => ({ f, index })).sort((a, b) => SEVERITY_ORDER[a.f.severity] - SEVERITY_ORDER[b.f.severity]);
  return (
    <section className="findings">
      <h4>Findings</h4>
      <ul>
        {sorted.map(({ f, index }) => (
          <li key={index}>
            <div className="finding-head">
              <Badge level={SEVERITY_LEVEL[f.severity]}>
                <span className="sr-only">{SEVERITY_TEXT[f.severity]}: </span>
                <strong>{f.title}</strong>
              </Badge>
              {runId !== undefined && <ExplainButton runId={runId} finding={index} />}
            </div>
            {f.detail && <p className="finding-detail">{f.detail}</p>}
            {(f.evidence ?? []).length > 0 && (
              <pre className="finding-evidence">{(f.evidence ?? []).join("\n")}</pre>
            )}
            {f.verified === false && <p className="finding-unverified">Not confirmed by command output</p>}
          </li>
        ))}
      </ul>
    </section>
  );
}
