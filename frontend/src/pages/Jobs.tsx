import { useEffect, useState, type CSSProperties, type FormEvent, type ReactNode } from "react";
import { useAutomation } from "../automation";
import { ExplainButton } from "../components/Explain";
import { Findings } from "../components/Findings";
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
  STATUS_LEVEL,
  STATUS_TEXT,
  TRIGGER_TEXT,
  UNITS,
  useRunning,
  type Job,
  type JobRun,
} from "../jobs";
import {
  Axis,
  Button,
  Code,
  Empty,
  ErrorText,
  Field,
  Input,
  KeyValue,
  LinkButton,
  List,
  ListItem,
  Meta,
  Meter,
  PageHeader,
  Panel,
  Row,
  Schedule,
  Segmented,
  Select,
  Split,
  Stack,
  Status,
  Switch,
  Table,
  Tag,
  Textarea,
  useFieldId,
  type Choice,
  type Tone,
} from "../kit";
import { useStream } from "../live";
import { Markdown, useNow, usePoll } from "../ui";

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

/** The texts of jobs.ts start with a capital; inside a line of lowercase words they do not.
 *  A name in capitals at the start (OK) keeps its case. */
const lower = (s: string) => (/^[A-Z][a-z]/.test(s) ? s.charAt(0).toLowerCase() + s.slice(1) : s);

// Placement of parts the kit has no layout component for.
const column: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--space-4)" };
const fieldRow: CSSProperties = { display: "flex", flexWrap: "wrap", gap: "var(--space-4) var(--space-5)" };
const controlRow: CSSProperties = { display: "flex", flexWrap: "wrap", alignItems: "center", gap: "var(--space-2) var(--space-3)" };

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
    <>
      <PageHeader
        title="Jobs"
        note="Each job is an LLM run on a schedule with a narrow topic. The summary job reads their latest results and sets the status on the Dashboard."
      >
        <LinkButton variant="primary" href="#/jobs/new">
          new job
        </LinkButton>
      </PageHeader>
      {error && <ErrorText>{error}</ErrorText>}
      {jobs.error && !jobs.data && <ErrorText>Backend unreachable: {jobs.error}</ErrorText>}
      {!jobs.data ? (
        !jobs.error && <Meta>Loading…</Meta>
      ) : items.length === 0 ? (
        <Panel>
          <Empty>No jobs.</Empty>
        </Panel>
      ) : (
        <Panel>
          <Table>
            <thead>
              <tr>
                <th>on</th>
                <th>job</th>
                <th>schedule</th>
                <th>last result</th>
                <th>next run</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((job) => {
                const run = job.last_run;
                const isRunning = running.has(job.slug);
                return (
                  <tr key={job.slug} className={job.enabled ? undefined : "fd-off"}>
                    <td>
                      <Switch checked={job.enabled} onChange={() => void toggle(job)} label={`${job.name} enabled`} title={job.enabled ? "Turn off" : "Turn on"} />
                    </td>
                    <td>
                      <Stack>
                        <a className="fd-title-link" href={`#/jobs/${job.slug}`}>
                          {job.name}
                        </a>
                        <Meta>{lower(originText(job))}</Meta>
                      </Stack>
                    </td>
                    <td>
                      <Stack>
                        <span className="data">{lower(scheduleText(job))}</span>
                        <Meta>{lower(MODEL_TEXT[job.model])} model</Meta>
                      </Stack>
                    </td>
                    <td>
                      <Stack>
                        {isRunning ? (
                          <Status tone="info" blink>
                            running
                          </Status>
                        ) : run ? (
                          <Status tone={STATUS_LEVEL[run.status]}>{lower(STATUS_TEXT[run.status])}</Status>
                        ) : (
                          <Status tone="off" hollow>
                            never run
                          </Status>
                        )}
                        {run && !isRunning && (
                          <Meta>
                            <span title={run.finished ? clock(run.finished) : undefined}>{ago(run.finished, now)}</span>
                          </Meta>
                        )}
                      </Stack>
                    </td>
                    <td className="fd-nowrap">
                      {!job.enabled ? (
                        <span className="data fd-dim">off</span>
                      ) : auto?.paused ? (
                        <span className="data fd-dim">paused</span>
                      ) : job.next_run ? (
                        <span className="data" title={clock(job.next_run)}>
                          {until(job.next_run, now)}
                        </span>
                      ) : (
                        <span className="data fd-dim">—</span>
                      )}
                    </td>
                    <td className="fd-right">
                      <Button onClick={() => void start(job)} disabled={isRunning} aria-label={`Run ${job.name} now`}>
                        run now
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </Panel>
      )}
    </>
  );
}

// ---------------------------------------------------------------- form

type Unit = (typeof UNITS)[number]["key"];
type Mode = "interval" | "cron";

const MODES: Choice<Mode>[] = [
  { value: "interval", label: "interval" },
  { value: "cron", label: "cron" },
];

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
  if (error) return <ErrorText>{error}</ErrorText>;
  if (!job) return <Meta>Loading…</Meta>;
  return <JobForm key={job.updated} job={job} onReset={setJob} />;
}

function JobForm({ job, onReset }: { job: Job | null; onReset?: (job: Job) => void }) {
  const creating = job === null;
  const formId = useFieldId();
  const [name, setName] = useState(job?.name ?? "");
  const [slug, setSlug] = useState(job?.slug ?? "");
  const [slugTouched, setSlugTouched] = useState(false);
  const [prompt, setPrompt] = useState(job?.prompt ?? "");
  const [mode, setMode] = useState<Mode>(job?.cron ? "cron" : "interval");
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
  const intervalS = Math.round(Number(n) * (UNITS.find((u) => u.key === unit)?.s ?? 60));

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
    if (mode === "interval") body.interval_s = intervalS;
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
    <>
      <PageHeader
        title={creating ? "New job" : `Edit ${job.name}`}
        back={{ label: creating ? "jobs" : job.name, href: back }}
        note={
          job?.builtin
            ? job.edited
              ? "A builtin job you changed: it keeps your version and no longer follows later releases. Reset to default to follow them again."
              : "A builtin job. As long as you do not change it, it follows the version of later releases. Turning it on or off does not count as a change."
            : undefined
        }
      >
        {job?.builtin && job.edited && (
          <Button onClick={reset} disabled={busy}>
            reset to default
          </Button>
        )}
        {job && !job.builtin && (
          <Button onClick={remove} disabled={busy}>
            delete
          </Button>
        )}
        <LinkButton href={back}>cancel</LinkButton>
        <Button variant="primary" type="submit" form={formId} disabled={busy}>
          {creating ? "create job" : "save"}
        </Button>
      </PageHeader>
      {error && <ErrorText>{error}</ErrorText>}
      <form id={formId} className="fd-split" style={{ alignItems: "stretch" }} onSubmit={submit}>
        <Panel pad flex="5 1 520px">
          <div style={column}>
            <div style={fieldRow}>
              <Field label="name" flex="2 1 260px">
                <Input id="job-name" value={name} onChange={(e) => onName(e.target.value)} required maxLength={80} />
              </Field>
              {creating ? (
                <Field label="slug" flex="1 1 180px" help="Its id in links, logs and metrics. Cannot be changed later.">
                  <Input
                    id="job-slug"
                    value={slug}
                    onChange={(e) => {
                      setSlug(e.target.value);
                      setSlugTouched(true);
                    }}
                    required
                    pattern="[a-z0-9][a-z0-9_-]*"
                    title="Lower-case letters, digits, - and _"
                  />
                </Field>
              ) : (
                <Field label="slug" group flex="1 1 180px" help="Its id in links, logs and metrics.">
                  <span className="data" style={{ display: "flex", alignItems: "center", minHeight: "var(--control)" }}>
                    {job.slug}
                  </span>
                </Field>
              )}
            </div>
            <Field label="prompt" help="What the model checks, with which commands, and when a result counts as warning or critical. Keep the topic narrow.">
              <Textarea id="job-prompt" value={prompt} onChange={(e) => setPrompt(e.target.value)} required rows={16} />
            </Field>
          </div>
        </Panel>

        <Panel pad flex="4 1 420px">
          <div style={column}>
            <Field label="schedule" group help="Cron times use the time zone set in Settings → General (the host's by default).">
              <div style={controlRow}>
                <div style={{ flex: "1 1 180px" }}>
                  <Segmented label="Schedule type" options={MODES} value={mode} onChange={setMode} />
                </div>
                {mode === "interval" ? (
                  <div style={{ flex: "1 1 200px", display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
                    <span className="data">every</span>
                    <Input type="number" min={1} step={1} value={n} onChange={(e) => setN(e.target.value)} required aria-label="Interval" style={{ width: "76px" }} />
                    <Select value={unit} onChange={(e) => setUnit(e.target.value as Unit)} aria-label="Unit" style={{ flex: "1 1 104px" }}>
                      {[...UNITS].reverse().map((u) => (
                        <option key={u.key} value={u.key}>
                          {Number(n) === 1 ? u.key.slice(0, -1) : u.key}
                        </option>
                      ))}
                    </Select>
                  </div>
                ) : (
                  <div style={{ flex: "1 1 200px" }}>
                    <Input value={cron} onChange={(e) => setCron(e.target.value)} required aria-label="Cron expression" placeholder="0 6 * * *" invalid={!cronWords} />
                  </div>
                )}
              </div>
              {mode === "cron" &&
                (cronWords ? <Meta>{cronWords}</Meta> : <Meta tone="crit">Not a cron expression this page can read: minute hour day month weekday.</Meta>)}
              {mode === "interval" && <IntervalPreview every={intervalS} />}
            </Field>

            <Field label="model" help="The worker model suits one narrow topic; the main model is the bigger one.">
              <Select id="job-model" value={model} onChange={(e) => setModel(e.target.value as Job["model"])}>
                <option value="worker">Worker</option>
                <option value="main">Main</option>
              </Select>
            </Field>

            <DomainsField prompt={prompt} value={domains} onChange={setDomains} />

            <div style={fieldRow}>
              <Field label="enabled" group flex="0 0 76px">
                <Switch checked={enabled} onChange={setEnabled} label="Enabled" />
              </Field>
              <Field label="time limit" flex="1 1 140px" help="Minutes. Empty: the default from Settings → Jobs.">
                <Input id="job-timeout" type="number" min={1} step="any" value={timeout} onChange={(e) => setTimeoutMin(e.target.value)} placeholder="Default" />
              </Field>
              <Field label="step limit" flex="1 1 140px" help="Most tool-use rounds per run. Empty: the default from Settings → Jobs.">
                <Input id="job-steps" type="number" min={1} max={60} step={1} value={steps} onChange={(e) => setSteps(e.target.value)} placeholder="Default" />
              </Field>
            </div>

            {job && (
              <Meta>
                {job.created_by === "builtin" ? "Added with the app" : job.created_by === "llm" ? "Made by the LLM" : "Made by a user"}{" "}
                <span title={clock(job.created)}>{ago(job.created)}</span>, changed <span title={clock(job.updated)}>{ago(job.updated)}</span>.
              </Meta>
            )}
          </div>
        </Panel>
      </form>
    </>
  );
}

/** The clock of the preview: half-hour cells over the next 24 hours. */
const PREVIEW_SLOT_S = 1800;
const PREVIEW_SLOTS = 48;

/** When an interval of `every` seconds fires in the next 24 hours, the first time one interval from now. */
function IntervalPreview({ every }: { every: number }) {
  const now = useNow();
  const valid = every > 0;
  const count = valid ? Math.floor((PREVIEW_SLOTS * PREVIEW_SLOT_S) / every) : 0;
  const runs = `${count} run${count === 1 ? "" : "s"}`;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
      <Schedule
        runs={[]}
        now={now}
        slot={PREVIEW_SLOT_S}
        past={0}
        future={PREVIEW_SLOTS}
        next={valid ? now + every : null}
        every={valid ? every : null}
        label={`When this schedule fires in the next 24 hours: ${runs}`}
      />
      <Axis labels={["now", "+6 h", "+12 h", "+18 h", "+24 h"]} />
      <Meta>{runs} in the next 24 h · one cell = 30 minutes</Meta>
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
    <Field label="domains" group help="MCP tools the runs get: exactly the enabled tools of these domains. Empty: the shell only." error={note ?? undefined}>
      {value.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "0 var(--space-3)" }}>
          {value.map((name) => (
            <span key={name} style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-1)" }}>
              <Tag>{name}</Tag>
              <Button variant="link" aria-label={`Remove ${name}`} onClick={() => onChange(value.filter((v) => v !== name))}>
                remove
              </Button>
            </span>
          ))}
        </div>
      )}
      <div style={controlRow}>
        <Select
          id="job-domains"
          aria-label="Add domain"
          value=""
          disabled={free.length === 0}
          onChange={(e) => e.target.value && onChange([...value, e.target.value].sort())}
          style={{ flex: "1 1 200px" }}
        >
          <option value="">{free.length ? "Add domain…" : known.length ? "All added" : "No domains yet"}</option>
          {free.map((d) => (
            <option key={d.id} value={d.name}>
              {d.name} ({onIn(d.name)} of {d.tools} on)
            </option>
          ))}
        </Select>
        <Button
          onClick={classify}
          disabled={busy || prompt.trim().length < 10 || !classifiable}
          title={classifiable ? "The worker model picks the domains this prompt needs" : "No domain has a tool switched on"}
        >
          {busy ? "classifying…" : "classify"}
        </Button>
      </div>
      <Meta>
        {value.length === 0
          ? "This job uses only the shell."
          : `This job can use ${tools.length} tool${tools.length === 1 ? "" : "s"}: ${tools.length - writes} read, ${writes} write.`}
      </Meta>
    </Field>
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

  if (job.error && !job.data) return <ErrorText>{job.error}</ErrorText>;
  if (!job.data) return <Meta>Loading…</Meta>;
  const j = job.data;
  const items = runs.data?.items ?? [];
  const selected = runId !== null ? items.find((r) => r.id === runId) ?? null : items[0] ?? null;
  const isRunning = items[0]?.status === "running";
  // the meters of the list share one maximum: the longest of the listed runs
  const longest = Math.max(0, ...items.map((r) => r.duration_s ?? 0));

  const start = () =>
    runJob(slug).then(
      (r) => {
        location.hash = `#/jobs/${slug}/runs/${r.run_id}`;
        runs.reload();
      },
      (e) => setError(errorText(e)),
    );

  return (
    <>
      <PageHeader
        title={j.name}
        back={{ label: "jobs", href: "#/jobs" }}
        meta={
          <>
            {j.enabled ? lower(scheduleText(j)) : "disabled"} · {lower(MODEL_TEXT[j.model])} model · {lower(originText(j))}
            {j.enabled && j.next_run && (
              <>
                {" · "}
                <span title={clock(j.next_run)}>next {until(j.next_run, now)}</span>
              </>
            )}
          </>
        }
      >
        <LinkButton href={`#/jobs/${slug}/edit`}>edit</LinkButton>
        <Button variant="primary" onClick={() => void start()} disabled={isRunning}>
          run now
        </Button>
      </PageHeader>
      {error && <ErrorText>{error}</ErrorText>}
      {runs.data && items.length === 0 ? (
        <Panel>
          <Empty>No runs yet. The first comes on schedule, or start one with run now.</Empty>
        </Panel>
      ) : (
        <Split>
          {selected ? (
            <RunView run={selected} now={now} />
          ) : runId !== null && runs.data ? (
            <Panel flex={RUN_FLEX}>
              <Empty>This run is not among the last 50.</Empty>
            </Panel>
          ) : null}
          <Panel title="Runs" meta="bar = how long the run took" flex="2 1 420px">
            <div style={{ maxHeight: "70vh", overflow: "auto" }}>
              <List>
                {items.map((r) => (
                  <ListItem
                    key={r.id}
                    href={`#/jobs/${slug}/runs/${r.id}`}
                    selected={selected?.id === r.id}
                    title={
                      <Status tone={STATUS_LEVEL[r.status]} blink={r.status === "running"}>
                        <span title={clock(r.started)}>{ago(r.started, now)}</span>
                      </Status>
                    }
                    meta={
                      <>
                        {lower(STATUS_TEXT[r.status])}
                        {r.duration_s != null && ` · ${seconds(r.duration_s)}`}
                        {tokenSum(r) > 0 && ` · ${tokenSum(r).toLocaleString()} tokens`}
                      </>
                    }
                    trailing={lower(TRIGGER_TEXT[r.trigger])}
                  >
                    <Meter value={r.duration_s ?? 0} max={longest} count={12} tone={cellTone(STATUS_LEVEL[r.status])} />
                  </ListItem>
                ))}
              </List>
            </div>
          </Panel>
        </Split>
      )}
    </>
  );
}

/** Where the opened run stands beside the list of runs. */
const RUN_FLEX = "3 1 520px";

/** A run's tone as the fill of a meter (a run is never "off"). */
const cellTone = (tone: Tone) => (tone === "off" ? "text" : tone);

const tokenSum = (r: JobRun) => (r.prompt_tokens ?? 0) + (r.completion_tokens ?? 0);

function seconds(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

function RunView({ run, now }: { run: JobRun; now: number }) {
  const running = run.status === "running";
  const facts: [ReactNode, ReactNode][] = [
    [
      "started",
      <>
        <span title={new Date(run.started * 1000).toLocaleString()}>{clock(run.started)}</span>
        <span className="fd-dim">
          {" "}· {ago(run.started, now)} · {lower(TRIGGER_TEXT[run.trigger])}
        </span>
      </>,
    ],
    ["duration", run.duration_s != null ? seconds(run.duration_s) : running ? "still running" : "—"],
    [
      "model",
      <>
        {run.model ?? "—"}
        {run.steps != null && (
          <span className="fd-dim">
            {" "}· {run.steps} step{run.steps === 1 ? "" : "s"}
          </span>
        )}
        {tokenSum(run) > 0 && (
          <span className="fd-dim">
            {" "}· {(run.prompt_tokens ?? 0).toLocaleString()} in, {(run.completion_tokens ?? 0).toLocaleString()} out
          </span>
        )}
      </>,
    ],
  ];
  if (run.mcp)
    facts.push([
      "MCP",
      <>
        {run.mcp.domains.length ? run.mcp.domains.join(", ") : "—"}
        {Object.entries(run.mcp.unreachable ?? {}).map(([server, why]) => (
          <span key={server} className="fd-dim">
            {" "}· server {server} unreachable: {why}
          </span>
        ))}
        {(run.mcp.unsorted ?? []).length > 0 && <span className="fd-dim"> · not sorted, not given: {(run.mcp.unsorted ?? []).join(", ")}</span>}
      </>,
    ]);
  return (
    <Panel
      flex={RUN_FLEX}
      title={
        <Status tone={STATUS_LEVEL[run.status]} blink={running}>
          {lower(STATUS_TEXT[run.status])}
        </Status>
      }
      aside={
        <>
          {!running && <ExplainButton runId={run.id} />}
          <LinkButton variant="link" href={logsHref(run)} title="This run's model and tool calls on the Logs page">
            LLM and tool calls
          </LinkButton>
        </>
      }
    >
      <Row>
        <div style={{ ...column, flex: "1 1 100%", minWidth: 0 }}>
          <KeyValue items={facts} />
          {run.error && <Code wrap>{run.error}</Code>}
          {running && <p className="fd-dim">The model is working on it. This view updates when it ends.</p>}
          {run.summary && <Markdown text={run.summary} />}
        </div>
      </Row>
      {run.findings.length > 0 && <Findings findings={run.findings} runId={run.id} />}
      {!running && !run.summary && !run.error && run.findings.length === 0 && <Empty>The run left no summary.</Empty>}
    </Panel>
  );
}
