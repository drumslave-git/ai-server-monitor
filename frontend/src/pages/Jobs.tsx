import { Fragment, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useAutomation } from "../automation";
import { JobRef, ModelRef, ServerRef, runHref } from "../components/EntityRefs";
import { ExplainButton } from "../components/Explain";
import { Findings } from "../components/Findings";
import { api } from "../api";
import type { components } from "../api-types";
import { ago, clock, until } from "../fmt";
import {
  cronText,
  errorText,
  firstLine,
  jobGroups,
  logsHref,
  NOTIFY_CHOICES,
  NOTIFY_WORD,
  originWord,
  runJob,
  scheduleWord,
  STATUS_LEVEL,
  STATUS_WORD,
  TRIGGER_WORD,
  UNITS,
  useRunning,
  whereWord,
  type Job,
  type JobRun,
} from "../jobs";
import {
  Axis,
  Button,
  Checkbox,
  Chip,
  Code,
  Column,
  Confirm,
  Empty,
  EntityLink,
  ErrorText,
  Field,
  FieldValue,
  IconButton,
  IconLink,
  Inline,
  Input,
  KeyValue,
  LinkButton,
  List,
  ListItem,
  Meta,
  Meter,
  Notice,
  PageHeader,
  Pad,
  Pane,
  Panes,
  Panel,
  Picker,
  Row,
  Schedule,
  Segmented,
  Select,
  SortableList,
  Split,
  Stack,
  Status,
  Switch,
  Table,
  Td,
  Text,
  Textarea,
  TitleLink,
  Tr,
  useFieldId,
  type Choice,
  type PickerGroup,
  type Tone,
} from "../kit";
import { useStream } from "../live";
import { Markdown, useNow, usePoll } from "../ui";

type JobListResponse = components["schemas"]["JobList"];
type Kind = Job["kind"];

/** Routes (params after `#/jobs`):
 *    (none)               list
 *    new                  create a job
 *    new/summary          create a summary
 *    <slug>               a job or summary: its runs, the newest one opened
 *    <slug>/runs/<id>     the same with that run opened
 *    <slug>/edit          edit form
 */
export function JobsPage({ params }: { params: string[] }) {
  const [slug, sub, id] = params;
  if (slug === "new") return <JobNew kind={sub === "summary" ? "summary" : "topic"} />;
  if (slug && sub === "edit") return <JobEdit slug={slug} />;
  if (slug) return <JobDetail slug={slug} runId={sub === "runs" && /^\d+$/.test(id ?? "") ? Number(id) : null} />;
  return <JobList />;
}

/** Summary slug → name, for "in Health, Daily". */
const summaryNames = (jobs: Job[]) => Object.fromEntries(jobs.filter((j) => j.kind === "summary").map((j) => [j.slug, j.name]));

const hasSchedule = (job: Pick<Job, "interval_s" | "cron"> | undefined) => Boolean(job && (job.interval_s || job.cron));

/** The API's notice of the last save ("its own schedule was removed"), shown on the page the save
 *  leads to. Cleared when that page closes or the notice is dismissed. */
let savedNotice: { slug: string; text: string } | null = null;

// ---------------------------------------------------------------- list

function JobList() {
  const jobs = usePoll(() => api.get<JobListResponse>("/api/jobs"), 30_000);
  const items = jobs.data?.items ?? [];
  const running = useRunning(jobs.data?.items);
  const { state: auto } = useAutomation();
  const now = useNow();
  const [error, setError] = useState<string | null>(null);
  useStream(["job.finished"], () => jobs.reload());

  const toggle = (job: Job) =>
    api.put<Job>(`/api/jobs/${encodeURIComponent(job.slug)}`, { enabled: !job.enabled }).then(
      () => jobs.reload(),
      (e) => setError(errorText(e)),
    );
  const start = (job: Job) => runJob(job.slug).catch((e) => setError(errorText(e)));

  const summaries = items.filter((j) => j.kind === "summary");
  const topics = items.filter((j) => j.kind === "topic");
  const names = summaryNames(items);
  const bySlug = Object.fromEntries(items.map((j) => [j.slug, j]));
  const paused = Boolean(auto?.paused);

  return (
    <>
      <PageHeader
        title="Jobs"
        note="A job is an LLM run with one narrow topic. A summary runs its jobs one after another, sums up their results and sets its status on the Dashboard. A job in a summary has no schedule of its own: the summary runs it."
      >
        <IconLink icon="newSummary" label="new summary" href="#/jobs/new/summary" />
        <LinkButton variant="primary" icon="add" href="#/jobs/new">
          new job
        </LinkButton>
      </PageHeader>
      {error && <ErrorText>{error}</ErrorText>}
      {jobs.error && !jobs.data && <ErrorText>Backend unreachable: {jobs.error}</ErrorText>}
      {!jobs.data ? (
        !jobs.error && <Meta>Loading…</Meta>
      ) : (
        <Panes>
          <Pane bare label="Summaries and jobs">
          <Panel title="Summaries" meta={`${summaries.length} · each runs its jobs in order, then sums them up`}>
            {summaries.length === 0 ? (
              <Empty>
                No summaries. <a href="#/jobs/new/summary">Create one</a>
              </Empty>
            ) : (
              <Table stack stickyHead head={["on", "summary", "schedule", "jobs", "notify", "last result", "next run", ""]}>
                <tbody>
                  {summaries.map((s) => (
                    <Tr key={s.slug} off={!s.enabled}>
                      <Td>
                        <Switch checked={s.enabled} onChange={() => void toggle(s)} label={`${s.name} enabled`} title={s.enabled ? "Turn off" : "Turn on"} />
                      </Td>
                      <Td>
                        <Stack>
                          <TitleLink href={`#/jobs/${s.slug}`}>{s.name}</TitleLink>
                          <Meta>{originWord(s)}</Meta>
                        </Stack>
                      </Td>
                      <Td>
                        <Stack>
                          <Text kind="data">
                            <Where job={s} names={names} />
                          </Text>
                          <Meta>
                            <ModelRef model={s.model} />
                          </Meta>
                        </Stack>
                      </Td>
                      <Td>
                        <Stack>
                          <Text kind="data">
                            {s.members.length} job{s.members.length === 1 ? "" : "s"}
                          </Text>
                          <Meta>
                            <MemberPreview members={s.members} bySlug={bySlug} />
                          </Meta>
                        </Stack>
                      </Td>
                      <Td>
                        <Text kind="data">{NOTIFY_WORD[s.notify]}</Text>
                      </Td>
                      <Td>
                        <LastResult job={s} running={running.has(s.slug)} now={now} />
                      </Td>
                      <Td nowrap>
                        <NextRun job={s} paused={paused} now={now} />
                      </Td>
                      <Td right>
                        <RunButton job={s} names={names} disabled={running.has(s.slug)} onRun={() => void start(s)} />
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Panel>

          <Panel title="Jobs" meta="grouped by the part of the slug before the colon">
            {topics.length === 0 ? (
              <Empty>
                No jobs. <a href="#/jobs/new">Create one</a>
              </Empty>
            ) : (
              <Table stack stickyHead head={["on", "job", "runs", "notify", "last result", "next run", ""]}>
                {jobGroups(topics).map((g) => (
                  <tbody key={g.label}>
                    <Tr group>
                      <Td colSpan={7}>
                        {g.label} <Meta>· {g.jobs.length}</Meta>
                      </Td>
                    </Tr>
                    {g.jobs.map((job) => {
                      const member = job.summaries.length > 0;
                      return (
                        <Tr key={job.slug} off={!job.enabled}>
                          <Td>
                            <Switch checked={job.enabled} onChange={() => void toggle(job)} label={`${job.name} enabled`} title={job.enabled ? "Turn off" : "Turn on"} />
                          </Td>
                          <Td>
                            <Stack>
                              <TitleLink href={`#/jobs/${job.slug}`}>{job.name}</TitleLink>
                              <Meta>{originWord(job)}</Meta>
                            </Stack>
                          </Td>
                          <Td>
                            <Stack>
                              <Text kind="data">
                                <Where job={job} names={names} />
                              </Text>
                              <Meta>
                                <ModelRef model={job.model} />
                              </Meta>
                            </Stack>
                          </Td>
                          <Td>
                            {member ? (
                              <Text kind="data" dim>
                                its summary reports
                              </Text>
                            ) : (
                              <Text kind="data">{NOTIFY_WORD[job.notify]}</Text>
                            )}
                          </Td>
                          <Td>
                            <LastResult job={job} running={running.has(job.slug)} now={now} />
                          </Td>
                          <Td nowrap>
                            <NextRun job={job} paused={paused} now={now} via={job.summaries.map((s) => bySlug[s]).filter(Boolean)} />
                          </Td>
                          <Td right>
                            <RunButton job={job} names={names} disabled={running.has(job.slug)} onRun={() => void start(job)} />
                          </Td>
                        </Tr>
                      );
                    })}
                  </tbody>
                ))}
              </Table>
            )}
          </Panel>
          </Pane>
        </Panes>
      )}
    </>
  );
}

/** The first members of a summary by name, each a link: "Containers, Disks and 7 more". */
function MemberPreview({ members, bySlug }: { members: string[]; bySlug: Record<string, Job> }) {
  if (members.length === 0) return <>none yet</>;
  const shown = members.slice(0, 2);
  const rest = members.length - shown.length;
  return (
    <>
      {shown.map((m, i) => (
        <Fragment key={m}>
          <JobRef slug={m} name={bySlug[m]?.name} />
          {i < shown.length - 1 && ", "}
        </Fragment>
      ))}
      {rest > 0 && ` and ${rest} more`}
    </>
  );
}

/** What whereWord says, with the summaries a job is in as links. */
function Where({ job, names }: { job: Pick<Job, "kind" | "summaries" | "interval_s" | "cron">; names: Record<string, string> }) {
  if (job.kind === "topic" && job.summaries.length)
    return (
      <>
        in{" "}
        {job.summaries.map((slug, i) => (
          <Fragment key={slug}>
            <JobRef summary slug={slug} name={names[slug]} />
            {i < job.summaries.length - 1 && ", "}
          </Fragment>
        ))}
      </>
    );
  return <>{whereWord(job, names)}</>;
}

function LastResult({ job, running, now }: { job: Job; running: boolean; now: number }) {
  const run = job.last_run;
  return (
    <Stack>
      {running ? (
        <Status tone="info" blink>
          running
        </Status>
      ) : run ? (
        <Status tone={STATUS_LEVEL[run.status]}>{STATUS_WORD[run.status]}</Status>
      ) : (
        <Status tone="off" hollow>
          never run
        </Status>
      )}
      {run && !running && <Meta title={run.finished ? clock(run.finished) : undefined}>{ago(run.finished, now)}</Meta>}
    </Stack>
  );
}

/** When it runs next: its own schedule, or (a job in summaries) the soonest of its summaries. */
function NextRun({ job, paused, now, via = [] }: { job: Job; paused: boolean; now: number; via?: Job[] }) {
  if (!job.enabled)
    return (
      <Text kind="data" dim>
        off
      </Text>
    );
  if (paused)
    return (
      <Text kind="data" dim>
        paused
      </Text>
    );
  const soonest = via.filter((s) => s.enabled && s.next_run).sort((a, b) => (a.next_run ?? 0) - (b.next_run ?? 0))[0];
  const at = job.next_run ?? soonest?.next_run ?? null;
  if (!at)
    return (
      <Text kind="data" dim>
        —
      </Text>
    );
  return (
    <Stack>
      <Text kind="data" title={clock(at)}>
        {until(at, now)}
      </Text>
      {!job.next_run && soonest && (
        <Meta>
          with <JobRef summary slug={soonest.slug} name={soonest.name} />
        </Meta>
      )}
    </Stack>
  );
}

/** "run now" for a job on its own; "run alone" for a job in a summary, which runs it without the summary.
 *  In a row an icon with its hint; main: the page's main action, icon and word. */
function RunButton({ job, names, disabled, onRun, main }: { job: Job; names: Record<string, string>; disabled: boolean; onRun: () => void; main?: boolean }) {
  const where = job.summaries.map((s) => names[s] ?? s).join(", ");
  const { word, label, hint } =
    job.kind === "summary"
      ? { word: "run now", label: `Run ${job.name} now: its ${job.members.length} jobs, then the summary`, hint: "Run the summary now" }
      : job.summaries.length === 0
        ? { word: "run now", label: `Run ${job.name} now`, hint: undefined }
        : { word: "run alone", label: `Run ${job.name} alone, without ${where}`, hint: "Run only this job" };
  if (!main) return <IconButton small icon="run" label={label} hint={hint} disabled={disabled} onClick={onRun} />;
  return (
    <Button variant="primary" icon="run" onClick={onRun} disabled={disabled} title={hint} aria-label={label}>
      {word}
    </Button>
  );
}

// ---------------------------------------------------------------- form

type Unit = (typeof UNITS)[number]["key"];
type Mode = "interval" | "cron" | "none";

const MODES: Choice<Mode>[] = [
  { value: "interval", label: "interval" },
  { value: "cron", label: "cron" },
  { value: "none", label: "none" },
];

function splitInterval(seconds: number | null | undefined): { n: string; unit: Unit } {
  if (!seconds) return { n: "1", unit: "hours" };
  for (const u of UNITS) if (seconds % u.s === 0) return { n: String(seconds / u.s), unit: u.key };
  return { n: String(Math.max(1, Math.round(seconds / 60))), unit: "minutes" };
}

/** Every job and summary, for the summaries of a job and the members of a summary. */
function useAllJobs() {
  const [all, setAll] = useState<Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    api.get<JobListResponse>("/api/jobs").then(
      (r) => setAll(r.items),
      (e) => setError(errorText(e)),
    );
  }, [tick]);
  return { all, error, reload: () => setTick((t) => t + 1) };
}

function JobNew({ kind }: { kind: Kind }) {
  const { all, error } = useAllJobs();
  if (error) return <ErrorText>{error}</ErrorText>;
  if (!all) return <Meta>Loading…</Meta>;
  return <JobForm key={kind} job={null} kind={kind} all={all} />;
}

function JobEdit({ slug }: { slug: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const others = useAllJobs();
  useEffect(() => {
    api.get<Job>(`/api/jobs/${encodeURIComponent(slug)}`).then(setJob, (e) => setError(errorText(e)));
  }, [slug]);
  if (error || others.error) return <ErrorText>{error ?? others.error}</ErrorText>;
  if (!job || !others.all) return <Meta>Loading…</Meta>;
  return (
    <JobForm
      key={job.updated}
      job={job}
      kind={job.kind}
      all={others.all}
      notice={notice}
      onReset={(fresh) => {
        setJob(fresh);
        setNotice(fresh.notice ?? null);
        others.reload();
      }}
    />
  );
}

function JobForm({ job, kind, all, notice, onReset }: { job: Job | null; kind: Kind; all: Job[]; notice?: string | null; onReset?: (job: Job) => void }) {
  const creating = job === null;
  const isSummary = kind === "summary";
  const noun = isSummary ? "summary" : "job";
  const formId = useFieldId();
  const [name, setName] = useState(job?.name ?? "");
  const [slug, setSlug] = useState(job?.slug ?? "");
  const [slugTouched, setSlugTouched] = useState(false);
  const [prompt, setPrompt] = useState(job?.prompt ?? "");
  const [mode, setMode] = useState<Mode>(job?.cron ? "cron" : job?.interval_s || creating ? "interval" : "none");
  const initial = splitInterval(job?.interval_s);
  const [n, setN] = useState(initial.n);
  const [unit, setUnit] = useState<Unit>(initial.unit);
  const [cron, setCron] = useState(job?.cron ?? "0 6 * * *");
  const [model, setModel] = useState<Job["model"]>(job?.model ?? (isSummary ? "main" : "worker"));
  const [notify, setNotify] = useState<Job["notify"]>(job?.notify ?? (isSummary ? "changed" : "warn_crit"));
  const [enabled, setEnabled] = useState(job?.enabled ?? true);
  const [timeout, setTimeoutMin] = useState(job?.timeout_s ? String(job.timeout_s / 60) : "");
  const [steps, setSteps] = useState(job?.max_steps ? String(job.max_steps) : "");
  const [domains, setDomains] = useState<string[]>(job?.domains ?? []);
  const [members, setMembers] = useState<string[]>(job?.members ?? []);
  const [summaries, setSummaries] = useState<string[]>(job?.summaries ?? []);
  const [picking, setPicking] = useState(false);
  const [asking, setAsking] = useState<"reset" | "delete" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const bySlug = Object.fromEntries(all.map((j) => [j.slug, j]));
  const names = summaryNames(all);
  const allSummaries = all.filter((j) => j.kind === "summary");
  const inSummary = !isSummary && summaries.length > 0;
  const cronWords = mode === "cron" ? cronText(cron) : null;
  const back = creating ? "#/jobs" : `#/jobs/${job.slug}`;
  const intervalS = Math.round(Number(n) * (UNITS.find((u) => u.key === unit)?.s ?? 60));
  // the jobs whose own schedule this save removes: they join a summary
  const losing: Job[] = isSummary
    ? members.filter((m) => !job?.members.includes(m)).map((m) => bySlug[m]).filter((j): j is Job => hasSchedule(j))
    : job && hasSchedule(job) && summaries.some((s) => !job.summaries.includes(s))
      ? [job]
      : [];

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
    if (!inSummary) {
      body.interval_s = mode === "interval" ? intervalS : null;
      body.cron = mode === "cron" ? cron.trim() : null;
      body.notify = notify;
    }
    if (isSummary) body.members = members;
    else body.summaries = summaries;
    if (creating) {
      body.slug = slug;
      body.kind = kind;
    }
    setBusy(true);
    setError(null);
    const req = creating ? api.post<Job>("/api/jobs", body) : api.put<Job>(`/api/jobs/${encodeURIComponent(job.slug)}`, body);
    req.then(
      (saved) => {
        savedNotice = saved.notice ? { slug: saved.slug, text: saved.notice } : null;
        location.hash = `#/jobs/${saved.slug}`;
      },
      (err) => {
        setBusy(false);
        setError(errorText(err));
      },
    );
  };

  const reset = () => {
    if (!job) return;
    setBusy(true);
    api.post<Job>(`/api/jobs/${encodeURIComponent(job.slug)}/reset`, undefined).then(
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
    if (!job) return;
    setBusy(true);
    api.del(`/api/jobs/${encodeURIComponent(job.slug)}`).then(
      () => {
        location.hash = "#/jobs";
      },
      (err) => {
        setBusy(false);
        setError(errorText(err));
      },
    );
  };

  const move = (from: number, to: number) => {
    const next = [...members];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    setMembers(next);
  };

  const candidates: PickerGroup[] = jobGroups(all.filter((j) => j.kind === "topic" && !members.includes(j.slug))).map((g) => ({
    label: g.label,
    items: g.jobs.map((j) => ({
      value: j.slug,
      title: j.name,
      meta: `${j.slug} · ${whereWord(j, names)}${hasSchedule(j) ? " (removed when it joins)" : ""}`,
      text: `${j.name} ${j.slug}`,
    })),
  }));

  return (
    <>
      <PageHeader
        title={creating ? (isSummary ? "New summary" : "New job") : `Edit ${job.name}`}
        back={{ label: creating ? "jobs" : job.name, href: back }}
        note={
          job?.builtin
            ? job.edited
              ? `A builtin ${noun} you changed: it keeps your version and no longer follows later releases. Reset to default to follow them again.`
              : `A builtin ${noun}. As long as you do not change it, it follows the version of later releases. Turning it on or off does not count as a change.`
            : undefined
        }
      >
        {job?.builtin && job.edited && (
          <IconButton icon="reset" label="reset to default" onClick={() => setAsking("reset")} disabled={busy} />
        )}
        {job && !job.builtin && (
          <IconButton icon="remove" variant="danger" label="delete" onClick={() => setAsking("delete")} disabled={busy} />
        )}
        <LinkButton href={back}>cancel</LinkButton>
        <Button variant="primary" icon={creating ? "add" : "save"} type="submit" form={formId} disabled={busy}>
          {creating ? `create ${noun}` : "save"}
        </Button>
      </PageHeader>
      {error && <ErrorText>{error}</ErrorText>}
      {notice && <Notice title="Reset to default">{notice}</Notice>}
      {losing.length > 0 && <LosingNotice losing={losing} joins={isSummary ? [name || "this summary"] : summaries.filter((s) => !job?.summaries.includes(s)).map((s) => names[s] ?? s)} isSummary={isSummary} />}
      <Split stretch id={formId} onSubmit={submit}>
        <Panel pad flex="5 1 520px">
          <Inline align="start" gap={4}>
            <Field label="name" flex="2 1 260px">
              <Input id="job-name" value={name} onChange={(e) => onName(e.target.value)} required maxLength={80} />
            </Field>
            {creating ? (
              <Field label="slug" flex="1 1 180px" help="Its id in links and logs. Cannot be changed later.">
                <Input
                  id="job-slug"
                  value={slug}
                  onChange={(e) => {
                    setSlug(e.target.value);
                    setSlugTouched(true);
                  }}
                  required
                  pattern="[a-z0-9][a-z0-9_\-]*(:[a-z0-9][a-z0-9_\-]*)*"
                  title="Lower-case letters, digits, - and _; parts joined by a colon"
                />
              </Field>
            ) : (
              <Field label="slug" group flex="1 1 180px" help="Its id in links and logs.">
                <FieldValue>{job.slug}</FieldValue>
              </Field>
            )}
          </Inline>
          <Field
            label="prompt"
            help={
              isSummary
                ? "How to sum up the results of its jobs: what matters most, what the headline says."
                : "What the model checks, with which commands, and when a result counts as warning or critical. Keep the topic narrow."
            }
          >
            <Textarea id="job-prompt" value={prompt} onChange={(e) => setPrompt(e.target.value)} required rows={16} />
          </Field>
        </Panel>

        <Panel pad flex="4 1 420px">
          {!isSummary && (
            <Field label="summaries" group help="A job in a summary runs there, in the summary's order, and has no schedule of its own. With none ticked it runs on its own schedule.">
              {allSummaries.length === 0 ? (
                <Meta>There is no summary yet.</Meta>
              ) : (
                <Stack gap={2}>
                  {allSummaries.map((s) => (
                    <Checkbox
                      key={s.slug}
                      checked={summaries.includes(s.slug)}
                      onChange={(on) => setSummaries(on ? [...summaries, s.slug] : summaries.filter((x) => x !== s.slug))}
                      label={
                        <>
                          {s.name} <Meta>· {s.enabled ? scheduleWord(s) : "off"}</Meta>
                        </>
                      }
                    />
                  ))}
                </Stack>
              )}
            </Field>
          )}

          {inSummary ? (
            <>
              <Field label="schedule" group help="Take it out of every summary to give it a schedule of its own.">
                <FieldValue>runs {whereWord({ kind, summaries, interval_s: null, cron: null }, names)}</FieldValue>
              </Field>
              <Field label="notify" group>
                <FieldValue>nothing itself: its summary reports</FieldValue>
              </Field>
            </>
          ) : (
            <>
              <Field label="schedule" group help="Cron times use the time zone set in Settings → General (the host's by default). None: it runs only when started by hand or by the LLM.">
                <Inline>
                  <Segmented flex="1 1 200px" label="Schedule type" options={MODES} value={mode} onChange={setMode} />
                  {mode === "interval" ? (
                    <Inline nowrap gap={2} flex="1 1 200px">
                      <Text kind="data">every</Text>
                      <Input type="number" min={1} step={1} value={n} onChange={(e) => setN(e.target.value)} required aria-label="Interval" width="76px" />
                      <Select value={unit} onChange={(e) => setUnit(e.target.value as Unit)} aria-label="Unit" flex="1 1 104px">
                        {[...UNITS].reverse().map((u) => (
                          <option key={u.key} value={u.key}>
                            {Number(n) === 1 ? u.key.slice(0, -1) : u.key}
                          </option>
                        ))}
                      </Select>
                    </Inline>
                  ) : mode === "cron" ? (
                    <Input flex="1 1 200px" value={cron} onChange={(e) => setCron(e.target.value)} required aria-label="Cron expression" placeholder="0 6 * * *" invalid={!cronWords} />
                  ) : null}
                </Inline>
                {mode === "cron" &&
                  (cronWords ? <Meta>{cronWords}</Meta> : <Meta tone="crit">Not a cron expression this page can read: minute hour day month weekday.</Meta>)}
                {mode === "interval" && <IntervalPreview every={intervalS} />}
                {mode === "none" && <Meta>No schedule: manual.</Meta>}
              </Field>
              <Field
                label="notify"
                help="When the result is sent to Telegram. “When the result changes”: the status or the list of warnings differs from the last run."
              >
                <Select id="job-notify" value={notify} onChange={(e) => setNotify(e.target.value as Job["notify"])}>
                  {NOTIFY_CHOICES.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </Select>
              </Field>
            </>
          )}

          <Field
            label="model"
            help={isSummary ? "The main model is the bigger one; a summary reads many results." : "The worker model suits one narrow topic; the main model is the bigger one."}
          >
            <Select id="job-model" value={model} onChange={(e) => setModel(e.target.value as Job["model"])}>
              <option value="worker">Worker</option>
              <option value="main">Main</option>
            </Select>
          </Field>

          <DomainsField prompt={prompt} value={domains} onChange={setDomains} />

          <Inline align="start" gap={4}>
            <Field label="enabled" group flex="0 0 76px">
              <Switch checked={enabled} onChange={setEnabled} label="Enabled" />
            </Field>
            <Field label="time limit" flex="1 1 140px" help="Minutes. Empty: the default from Settings → Jobs.">
              <Input id="job-timeout" type="number" min={1} step="any" value={timeout} onChange={(e) => setTimeoutMin(e.target.value)} placeholder="Default" />
            </Field>
            <Field label="step limit" flex="1 1 140px" help="Most tool-use rounds per run. Empty: the default from Settings → Jobs.">
              <Input id="job-steps" type="number" min={1} max={60} step={1} value={steps} onChange={(e) => setSteps(e.target.value)} placeholder="Default" />
            </Field>
          </Inline>

          {job && (
            <Meta>
              {job.created_by === "builtin" ? "Added with the app" : job.created_by === "llm" ? "Made by the LLM" : "Made by a user"}{" "}
              <span title={clock(job.created)}>{ago(job.created)}</span>, changed <span title={clock(job.updated)}>{ago(job.updated)}</span>.
            </Meta>
          )}
        </Panel>
      </Split>

      {isSummary && (
        <Panel
          title="Jobs in this summary"
          meta={`${members.length} · they run in this order, then the summary sums them up`}
          aside={
            <IconButton icon="add" label="add job" onClick={() => setPicking(true)} disabled={busy} />
          }
        >
          {members.length === 0 ? (
            <Empty>No jobs yet. Add the jobs this summary runs.</Empty>
          ) : (
            <SortableList
              label={`Jobs of ${name || "this summary"}, in the order they run`}
              items={members.map((m) => {
                const member = bySlug[m];
                const elsewhere = (member?.summaries ?? []).filter((s) => s !== job?.slug).map((s) => names[s] ?? s);
                const joins = !job?.members.includes(m);
                return {
                  id: m,
                  name: member?.name ?? m,
                  title: member?.name ?? m,
                  meta: [
                    m,
                    elsewhere.length ? `also in ${elsewhere.join(", ")}` : null,
                    joins && hasSchedule(member) ? `its schedule (${scheduleWord(member)}) is removed on save` : joins ? "added, not saved yet" : null,
                  ]
                    .filter(Boolean)
                    .join(" · "),
                };
              })}
              onMove={move}
              onRemove={(id) => setMembers(members.filter((m) => m !== id))}
            />
          )}
          {members.length > 0 && (
            <Pad>
              <Meta>Drag a row by its number, or focus the number and press space, the arrow keys and space again; or use up and down.</Meta>
            </Pad>
          )}
        </Panel>
      )}

      {picking && (
        <Picker
          title={`Add a job to ${name || "this summary"}`}
          groups={candidates}
          onPick={(slug) => setMembers([...members, slug])}
          onClose={() => setPicking(false)}
          empty="Every job is in this summary already."
        />
      )}
      {asking === "reset" && job && (
        <Confirm title={`Reset ${job.name} to the version of this release?`} confirm="reset" variant="warn" onConfirm={reset} onClose={() => setAsking(null)}>
          <Text block>
            Your changes to it are lost{isSummary ? ", its list of jobs included" : ""}. Whether it is on, and when it notifies, stay as they are.
          </Text>
        </Confirm>
      )}
      {asking === "delete" && job && (
        <Confirm title={`Delete the ${noun} ${job.name}?`} confirm="delete" onConfirm={remove} onClose={() => setAsking(null)}>
          <Text block>
            {isSummary
              ? `Its runs are deleted too. Its ${job.members.length} jobs stay; those in no other summary are left without a schedule and run only when started by hand.`
              : `Its runs are deleted too${job.summaries.length ? `, and it is taken out of ${job.summaries.map((s) => names[s] ?? s).join(", ")}` : ""}.`}{" "}
            This cannot be undone.
          </Text>
        </Confirm>
      )}
    </>
  );
}

/** Before a save that puts jobs with a schedule into a summary: what happens to their schedules. */
function LosingNotice({ losing, joins, isSummary }: { losing: Job[]; joins: string[]; isSummary: boolean }) {
  const one = losing.length === 1;
  const list = losing.map((j) => `${j.name} (${scheduleWord(j)})`).join(", ");
  return (
    <Notice tone="warn" title={one ? "Its own schedule will be removed" : "Their own schedules will be removed"}>
      {isSummary
        ? `Saving adds ${list} to ${joins[0]}. ${one ? "It then runs" : "They then run"} here, and ${one ? "its" : "their"} own schedule is removed.`
        : `Saving puts ${losing[0].name} into ${joins.join(", ")}. It then runs there, and its own schedule (${scheduleWord(losing[0])}) is removed.`}
    </Notice>
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
    <Column gap={1}>
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
    </Column>
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
        <Inline gap={2}>
          {value.map((name) => (
            <Chip key={name} label={`Remove ${name}`} onRemove={() => onChange(value.filter((v) => v !== name))}>
              {name}
            </Chip>
          ))}
        </Inline>
      )}
      <Inline>
        <Select
          id="job-domains"
          aria-label="Add domain"
          value=""
          disabled={free.length === 0}
          onChange={(e) => e.target.value && onChange([...value, e.target.value].sort())}
          flex="1 1 200px"
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
      </Inline>
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
  const others = useAllJobs();
  const now = useNow();
  const [error, setError] = useState<string | null>(null);
  // the save's notice ("its own schedule was removed") is shown once, on the page the save led to
  const [notice, setNotice] = useState(() => (savedNotice?.slug === slug ? savedNotice.text : null));
  useEffect(
    () => () => {
      savedNotice = null;
    },
    [],
  );
  // a summary's queue grows while it runs: count the job.finished events to fetch it again
  const [finishedTick, setFinishedTick] = useState(0);
  useStream(["job"], (e) => {
    if (e.type === "job.finished") setFinishedTick((t) => t + 1);
    if (e.data.slug !== slug) return;
    runs.reload();
    if (e.type === "job.finished") job.reload();
  });

  const items = runs.data?.items ?? [];
  const selected = runId !== null ? (items.find((r) => r.id === runId) ?? null) : (items[0] ?? null);
  const full = useRunWithQueue(job.data?.kind === "summary" ? (selected?.id ?? null) : null, `${selected?.status}:${finishedTick}`);
  const parent = useRunWithQueue(selected?.queue_run_id ?? null, "");

  if (job.error && !job.data) return <ErrorText>{job.error}</ErrorText>;
  if (!job.data) return <Meta>Loading…</Meta>;
  const j = job.data;
  const isSummary = j.kind === "summary";
  const isRunning = items[0]?.status === "running";
  const all = others.all ?? [];
  const names = summaryNames(all);
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

  const shown = selected && full.run?.id === selected.id ? { ...selected, queue: full.run.queue } : selected;

  return (
    <>
      <PageHeader
        title={j.name}
        back={{ label: "jobs", href: "#/jobs" }}
        meta={
          <>
            {j.enabled ? <Where job={j} names={names} /> : "off"} · <ModelRef model={j.model} /> ·{" "}
            {j.kind === "topic" && j.summaries.length ? "its summary reports" : `notify ${NOTIFY_WORD[j.notify]}`} · {originWord(j)}
            {j.enabled && j.next_run && (
              <>
                {" · "}
                <span title={clock(j.next_run)}>next {until(j.next_run, now)}</span>
              </>
            )}
          </>
        }
      >
        <IconLink icon="rename" label="edit" href={`#/jobs/${slug}/edit`} />
        <RunButton job={j} names={names} main disabled={isRunning} onRun={() => void start()} />
      </PageHeader>
      {error && <ErrorText>{error}</ErrorText>}
      {notice && (
        <Notice
          title="Saved"
          action={
            <IconButton small icon="close" label="dismiss" hint="Dismiss" onClick={() => setNotice(null)} />
          }
        >
          {notice}
        </Notice>
      )}
      {runs.data && items.length === 0 ? (
        <Panel>
          <Empty>
            {isSummary
              ? "No runs yet. The first comes on schedule, or start one with run now: its jobs run one after another, then the summary."
              : j.summaries.length
                ? "No runs yet. It runs with its summary, or alone with run alone."
                : "No runs yet. The first comes on schedule, or start one with run now."}
          </Empty>
        </Panel>
      ) : (
        <Panes>
          {shown ? (
            <Pane bare label="Run" resetOn={shown.id}>
              <RunView
                run={shown}
                now={now}
                isSummary={isSummary}
                queueError={isSummary ? full.error : null}
                parent={parent.run}
                names={Object.fromEntries(all.map((x) => [x.slug, x.name]))}
              />
              {isSummary && <QueuePanel run={shown} loading={!full.run && !full.error} names={Object.fromEntries(all.map((x) => [x.slug, x.name]))} now={now} />}
            </Pane>
          ) : runId !== null && runs.data ? (
            <Pane bare label="Run">
              <Panel>
                <Empty>This run is not among the last 50.</Empty>
              </Panel>
            </Pane>
          ) : null}
          <Pane side="wide" title="Runs" meta="bar = how long the run took">
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
                        {STATUS_WORD[r.status]}
                        {r.duration_s != null && ` · ${seconds(r.duration_s)}`}
                        {tokenSum(r) > 0 && ` · ${tokenSum(r).toLocaleString()} tokens`}
                      </>
                    }
                    trailing={r.queue_run_id ? "in a queue" : TRIGGER_WORD[r.trigger]}
                  >
                    <Meter value={r.duration_s ?? 0} max={longest} count={12} tone={cellTone(STATUS_LEVEL[r.status])} />
                  </ListItem>
                ))}
              </List>
          </Pane>
        </Panes>
      )}
    </>
  );
}

/** One run fetched by id, with its queue (the list of runs leaves the queue out).
 *  `again` changes when it should be fetched again (a member finished). */
function useRunWithQueue(id: number | null, again: string) {
  const [run, setRun] = useState<JobRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (id === null) {
      setRun(null);
      setError(null);
      return;
    }
    let live = true;
    api.get<JobRun>(`/api/jobs/runs/${id}`).then(
      (r) => {
        if (!live) return;
        setRun(r);
        setError(null);
      },
      (e) => {
        if (live) setError(errorText(e));
      },
    );
    return () => {
      live = false;
    };
  }, [id, again]);
  return { run: run?.id === id ? run : null, error };
}

/** A run's tone as the fill of a meter (a run is never "off"). */
const cellTone = (tone: Tone) => (tone === "off" ? "text" : tone);

const tokenSum = (r: JobRun) => (r.prompt_tokens ?? 0) + (r.completion_tokens ?? 0);

function seconds(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

const failed = (r: JobRun) => r.status === "error";

function RunView({
  run,
  now,
  isSummary,
  queueError,
  parent,
  names,
}: {
  run: JobRun;
  now: number;
  isSummary: boolean;
  queueError: string | null;
  parent: JobRun | null;
  names: Record<string, string>;
}) {
  const running = run.status === "running";
  const facts: [ReactNode, ReactNode][] = [
    [
      "started",
      <>
        <span title={new Date(run.started * 1000).toLocaleString()}>{clock(run.started)}</span>
        <Text dim>
          {" "}· {ago(run.started, now)} · {TRIGGER_WORD[run.trigger]}
        </Text>
      </>,
    ],
    ["duration", run.duration_s != null ? seconds(run.duration_s) : running ? "still running" : "—"],
  ];
  if (isSummary && run.queue.length > 0) {
    const bad = run.queue.filter((r) => r.status === "error").length;
    facts.push([
      "queue",
      <>
        {run.queue.length} job{run.queue.length === 1 ? "" : "s"}
        {bad > 0 && <Text tone="crit"> · {bad} failed</Text>}
      </>,
    ]);
  }
  if (run.queue_run_id)
    facts.push([
      "part of",
      parent ? (
        <EntityLink kind="run" href={runHref(parent.job_slug, parent.id)}>
          a queue run of {names[parent.job_slug] ?? parent.job_slug}
        </EntityLink>
      ) : (
        "a queue run"
      ),
    ]);
  facts.push([
    "model",
    <>
      {run.model ?? "—"}
      {run.steps != null && (
        <Text dim>
          {" "}· {run.steps} step{run.steps === 1 ? "" : "s"}
        </Text>
      )}
      {tokenSum(run) > 0 && (
        <Text dim>
          {" "}· {(run.prompt_tokens ?? 0).toLocaleString()} in, {(run.completion_tokens ?? 0).toLocaleString()} out
        </Text>
      )}
    </>,
  ]);
  if (run.mcp)
    facts.push([
      "MCP",
      <>
        {run.mcp.domains.length ? run.mcp.domains.join(", ") : "—"}
        {Object.entries(run.mcp.unreachable ?? {}).map(([server, why]) => (
          <Text key={server} dim>
            {" "}· server <ServerRef name={server} /> unreachable: {why}
          </Text>
        ))}
        {(run.mcp.unsorted ?? []).length > 0 && <Text dim> · not sorted, not given: {(run.mcp.unsorted ?? []).join(", ")}</Text>}
      </>,
    ]);
  return (
    <Panel
      title={
        <Status tone={STATUS_LEVEL[run.status]} blink={running}>
          {STATUS_WORD[run.status]}
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
      <Row column gap={4}>
        <KeyValue items={facts} />
        {queueError && <ErrorText>The queue of this run could not be loaded: {queueError}</ErrorText>}
        {run.error && <Code wrap>{run.error}</Code>}
        {running && (
          <Text block dim>
            {isSummary ? "Its jobs run one after another, then the summary. This view updates as each ends." : "The model is working on it. This view updates when it ends."}
          </Text>
        )}
        {run.summary && <Markdown text={run.summary} />}
      </Row>
      {run.findings.length > 0 && <Findings findings={run.findings} runId={run.id} />}
      {!running && !run.summary && !run.error && run.findings.length === 0 && <Empty>The run left no summary.</Empty>}
    </Panel>
  );
}

/** The member runs of one summary run, in the order they ran, each linking to its own run. */
function QueuePanel({ run, loading, names, now }: { run: JobRun; loading: boolean; names: Record<string, string>; now: number }) {
  const bad = run.queue.filter(failed).length;
  return (
    <Panel title="Queue" meta={loading ? "loading…" : `${run.queue.length} job${run.queue.length === 1 ? "" : "s"} in the order they ran${bad ? ` · ${bad} failed` : ""}`}>
      {run.queue.length === 0 ? (
        !loading && <Empty>{run.status === "running" ? "No job of this run has ended yet." : "This run has no job runs (it ran before 0.10, or its summary had no jobs)."}</Empty>
      ) : (
        <List label="Job runs of this summary run">
          {run.queue.map((r, i) => (
            <ListItem
              key={r.id}
              index={String(i + 1).padStart(2, "0")}
              href={`#/jobs/${r.job_slug}/runs/${r.id}`}
              title={
                <Status tone={STATUS_LEVEL[r.status]} blink={r.status === "running"}>
                  {names[r.job_slug] ?? r.job_slug}
                </Status>
              }
              meta={
                <>
                  {STATUS_WORD[r.status]}
                  {r.duration_s != null && ` · ${seconds(r.duration_s)}`}
                  {r.error && ` · ${firstLine(r.error)}`}
                </>
              }
              trailing={<span title={clock(r.started)}>{ago(r.started, now)}</span>}
            />
          ))}
        </List>
      )}
    </Panel>
  );
}
