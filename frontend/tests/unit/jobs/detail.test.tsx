// The Jobs page's detail (`#/jobs/<slug>` and `#/jobs/<slug>/runs/<id>`): the header, run now, the run view with its facts,
// findings and queue, the list of runs, empty and error states, and what the live stream changes.
// `/api/stream` is a fake EventSource (fake-event-source.ts). The summary text is shown through <Markdown>; whether it is
// sanitised is not asserted here (DOMPurify does not sanitise under happy-dom).
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AutomationProvider } from "../../../src/automation";
import { JobsPage } from "../../../src/pages/Jobs";
import { makeJob, makeRun, nowSeconds } from "../../mocks/fixtures/dashboard";
import { detailedRun, jobsList, type RunItem } from "../../mocks/fixtures/jobs";
import { server } from "../../mocks/server";
import { FakeEventSource, installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();
afterEach(() => {
  location.hash = "";
});

const renderDetail = (...params: string[]) =>
  render(
    <AutomationProvider>
      <JobsPage params={params} />
    </AutomationProvider>,
  );

/** The requests of a path the page made. */
function watch(method: "get" | "post", path: string, answer: () => Response = () => HttpResponse.json({})) {
  const seen: { url: URL; body: unknown }[] = [];
  server.use(
    http[method](path, async ({ request }) => {
      const text = await request.text();
      seen.push({ url: new URL(request.url), body: text ? JSON.parse(text) : undefined });
      return answer();
    }),
  );
  return seen;
}

/** The runs of every job (`GET /api/jobs/:slug/runs`). */
const serveRuns = (items: RunItem[]) => server.use(http.get("/api/jobs/:slug/runs", () => HttpResponse.json({ items })));

const runRegion = () => screen.getByRole("region", { name: "Run" });
const runsPane = () => screen.getByRole("heading", { name: "Runs" }).closest("section")!;
const hrefs = (root: HTMLElement) => within(root).getAllByRole("link").map((a) => a.getAttribute("href"));

/** A finished summary run of "daily": warning, with findings and a summary. The runs below are its history. */
function dailyRuns(now = nowSeconds()): RunItem[] {
  return [
    detailedRun(
      {
        id: 41,
        job_slug: "daily",
        status: "warn",
        started: now - 400,
        finished: now - 300,
        duration_s: 100,
        summary: "The data disk of host-a is almost full.",
        findings: [
          { severity: "info", title: "Kernel update waiting", detail: "A newer kernel is installed.", evidence: [] },
          { severity: "crit", title: "Disk almost full", detail: "The data disk is at 91 %.", evidence: ["/dev/sda1  91%  /data"], verified: true },
          { severity: "warn", title: "Backup is old", detail: "Two days old.", evidence: [], verified: false },
        ],
      },
      now,
    ),
    detailedRun({ id: 31, job_slug: "daily", status: "error", started: now - 5 * 3600, finished: now - 5 * 3600 + 20, duration_s: 20, trigger: "manual", error: "Model unreachable\nsecond line", prompt_tokens: 0, completion_tokens: 0 }, now),
  ];
}

/** Waits until the page shows the job, not "Loading…". */
const named = (name: string) => screen.findByRole("heading", { level: 1, name }, { timeout: 3000 });

describe("loading and errors", () => {
  it("says it is loading, then shows the name of the job", async () => {
    renderDetail("test:fresh");
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await named("Fresh check")).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("shows the message when the job is not found", async () => {
    renderDetail("nope");
    expect(await screen.findByRole("alert")).toHaveTextContent("Job not found");
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
  });

  it("asks for a slug with a colon as it is: one encoding, so the job is found", async () => {
    const seen = watch("get", "/api/jobs/:slug", () => HttpResponse.json(jobsList().items.find((j) => j.slug === "test:disk")));
    renderDetail("test:disk");
    await named("Disk usage");
    expect(seen[0].url.pathname).toBe("/api/jobs/test%3Adisk");
  });
});

describe("header", () => {
  it("has the back link, the edit link and a run button that names the jobs of a summary", async () => {
    renderDetail("daily");
    await named("Example daily");
    expect(screen.getByRole("link", { name: "← jobs" })).toHaveAttribute("href", "#/jobs");
    expect(screen.getByRole("link", { name: "edit" })).toHaveAttribute("href", "#/jobs/daily/edit");
    const run = screen.getByRole("button", { name: "Run Example daily now: its 2 jobs, then the summary" });
    expect(run).toHaveTextContent("run now");
  });

  it("tells the schedule, the model, who reports, where it comes from and when it runs next", async () => {
    renderDetail("daily");
    await named("Example daily");
    const meta = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(meta).toHaveTextContent("at 06:00");
    expect(within(meta).getByRole("link", { name: "main model" })).toHaveAttribute("href", "#/settings/llm");
    expect(meta).toHaveTextContent("notify when the result changes");
    expect(meta).toHaveTextContent("custom, made by a user");
    expect(meta).toHaveTextContent("next in 3h");
  });

  it("a job in a summary says its summary reports, names the summary as a link and offers to run alone", async () => {
    renderDetail("test:disk");
    await named("Disk usage");
    const meta = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(meta).toHaveTextContent("its summary reports");
    expect(meta).not.toHaveTextContent("notify when");
    expect(within(meta).getByRole("link", { name: "Example daily" })).toHaveAttribute("href", "#/jobs/daily");
    expect(meta).toHaveTextContent("in Example daily");
    expect(meta).not.toHaveTextContent("every 30 minutes");
    expect(meta).toHaveTextContent("next in 10m");
    const run = screen.getByRole("button", { name: "Run Disk usage alone, without Example daily" });
    expect(run).toHaveTextContent("run alone");
  });

  it("a job that is off says off, and no next run", async () => {
    renderDetail("test:solo");
    await named("Solo check");
    const meta = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(meta).toHaveTextContent("off");
    expect(meta).not.toHaveTextContent("next");
    expect(meta).not.toHaveTextContent("every 2 hours");
  });

  it("names a builtin job's origin and its notify setting", async () => {
    renderDetail("test:builtin");
    await named("Builtin check");
    const meta = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(meta).toHaveTextContent("builtin");
    expect(meta).toHaveTextContent("notify when the result changes");
  });
});

describe("run now", () => {
  it("starts the run, then shows it by address", async () => {
    const posts = watch("post", "/api/jobs/:slug/run", () => HttpResponse.json({ run_id: 99 }));
    const user = userEvent.setup();
    renderDetail("test:fresh");
    await named("Fresh check");
    await user.click(screen.getByRole("button", { name: "Run Fresh check now" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url.pathname).toBe("/api/jobs/test%3Afresh/run");
    await waitFor(() => expect(location.hash).toBe("#/jobs/test:fresh/runs/99"));
  });

  it("reads the runs again after the start", async () => {
    const runs = watch("get", "/api/jobs/:slug/runs", () => HttpResponse.json({ items: [] }));
    const user = userEvent.setup();
    renderDetail("test:fresh");
    await named("Fresh check");
    await waitFor(() => expect(runs).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Run Fresh check now" }));
    await waitFor(() => expect(runs).toHaveLength(2));
    expect(runs[1].url.search).toBe("?limit=50");
  });

  it("shows the message when the start fails, and stays where it is", async () => {
    server.use(http.post("/api/jobs/:slug/run", () => HttpResponse.json({ detail: "The model is not configured" }, { status: 409 })));
    const user = userEvent.setup();
    renderDetail("test:fresh");
    await named("Fresh check");
    await user.click(screen.getByRole("button", { name: "Run Fresh check now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The model is not configured");
    expect(location.hash).toBe("");
  });

  it("is off while the newest run is running, and starts nothing when pressed", async () => {
    serveRuns([detailedRun({ id: 50, job_slug: "test:fresh", status: "running", finished: null, duration_s: null })]);
    const posts = watch("post", "/api/jobs/:slug/run");
    const user = userEvent.setup();
    renderDetail("test:fresh");
    await named("Fresh check");
    const button = await screen.findByRole("button", { name: "Run Fresh check now" });
    await waitFor(() => expect(button).toBeDisabled());
    await user.click(button);
    expect(posts).toHaveLength(0);
  });
});

describe("empty states", () => {
  // (the default answer of the dashboard handlers has runs for jobs that ran: no run at all for every job here)
  beforeEach(() => serveRuns([]));

  it("a plain job has not run: the first comes on schedule or with run now", async () => {
    renderDetail("test:fresh");
    expect(await screen.findByText("No runs yet. The first comes on schedule, or start one with run now.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Runs" })).not.toBeInTheDocument();
  });

  it("a job in a summary says it runs with its summary or alone", async () => {
    renderDetail("test:disk");
    expect(await screen.findByText("No runs yet. It runs with its summary, or alone with run alone.")).toBeInTheDocument();
  });

  it("a summary says its jobs run one after another, then the summary", async () => {
    renderDetail("daily");
    expect(await screen.findByText("No runs yet. The first comes on schedule, or start one with run now: its jobs run one after another, then the summary.")).toBeInTheDocument();
  });
});

describe("runs", () => {
  it("lists every run as a link to its own address, newest first, with when, how it ended, how long and how many tokens", async () => {
    serveRuns(dailyRuns());
    renderDetail("daily");
    await named("Example daily");
    const pane = await waitFor(() => runsPane());
    expect(hrefs(pane)).toEqual(["#/jobs/daily/runs/41", "#/jobs/daily/runs/31"]);
    const [first, second] = within(pane).getAllByRole("link");
    expect(first).toHaveTextContent("7m ago");
    expect(first).toHaveTextContent("warning · 1m 40s · 1,540 tokens");
    expect(first).toHaveTextContent("scheduled");
    expect(second).toHaveTextContent("5h ago");
    expect(second).toHaveTextContent("error · 20s");
    expect(second).not.toHaveTextContent("tokens");
    expect(second).toHaveTextContent("manual");
  });

  it("marks the shown run in the list", async () => {
    serveRuns(dailyRuns());
    renderDetail("daily");
    await named("Example daily");
    const pane = await waitFor(() => runsPane());
    const [first, second] = within(pane).getAllByRole("link");
    expect(first).toHaveAttribute("aria-current", "true");
    expect(second).not.toHaveAttribute("aria-current");
  });

  it("shows the newest run when no run is named, and the named one when it is", async () => {
    serveRuns(dailyRuns());
    const { unmount } = renderDetail("daily");
    await named("Example daily");
    await waitFor(() => expect(runRegion()).toHaveTextContent("Disk almost full"));
    unmount();
    renderDetail("daily", "runs", "31");
    await named("Example daily");
    await waitFor(() => expect(runRegion()).toHaveTextContent("Model unreachable"));
    expect(runRegion()).not.toHaveTextContent("Disk almost full");
    const [first, second] = within(runsPane()).getAllByRole("link");
    expect(second).toHaveAttribute("aria-current", "true");
    expect(first).not.toHaveAttribute("aria-current");
  });

  it("says a run is not among the last 50 when its id is not in the list, and still lists the runs", async () => {
    serveRuns(dailyRuns());
    renderDetail("daily", "runs", "999");
    expect(await screen.findByText("This run is not among the last 50.")).toBeInTheDocument();
    expect(hrefs(runsPane())).toEqual(["#/jobs/daily/runs/41", "#/jobs/daily/runs/31"]);
  });

  it("takes a run id that is not a number for no run: the newest is shown", async () => {
    serveRuns(dailyRuns());
    renderDetail("daily", "runs", "abc");
    await waitFor(() => expect(runRegion()).toHaveTextContent("Disk almost full"));
    expect(screen.queryByText("This run is not among the last 50.")).not.toBeInTheDocument();
  });

  it("says a run in a queue is in a queue instead of how it was started", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", status: "warn", queue_run_id: 41, summary: "Disk at 91 %." })]);
    renderDetail("test:disk");
    await named("Disk usage");
    const pane = await waitFor(() => runsPane());
    expect(within(pane).getByRole("link")).toHaveTextContent("in a queue");
    expect(within(pane).getByRole("link")).not.toHaveTextContent("scheduled");
  });

  it("asks for the last 50 runs of the job by its slug", async () => {
    const seen = watch("get", "/api/jobs/:slug/runs", () => HttpResponse.json({ items: [] }));
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].url.pathname).toBe("/api/jobs/test%3Adisk/runs");
    expect(seen[0].url.search).toBe("?limit=50");
  });
});

describe("the run", () => {
  it("tells how it ended, when it started and how, how long it took, which model and which tokens", async () => {
    serveRuns([
      detailedRun({ id: 31, job_slug: "test:disk", status: "warn", started: nowSeconds() - 1300, finished: nowSeconds() - 1200, duration_s: 95, trigger: "llm", summary: "Disk at 91 % on host-a." }),
    ]);
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(runRegion()).toHaveTextContent("Disk at 91 % on host-a."));
    const region = runRegion();
    expect(within(region).getByText("warning")).toBeInTheDocument();
    expect(region).toHaveTextContent("started");
    expect(region).toHaveTextContent("22m ago · by the LLM");
    expect(region).toHaveTextContent("duration1m 35s");
    expect(region).toHaveTextContent("modeltest-worker · 4 steps · 1,200 in, 340 out");
    expect(region).toHaveTextContent("MCPdocker");
  });

  it("counts one step in the singular and leaves the tokens out when there are none", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", steps: 1, prompt_tokens: 0, completion_tokens: 0, summary: "Fine." })]);
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(runRegion()).toHaveTextContent("1 step"));
    expect(runRegion()).not.toHaveTextContent("1 steps");
    expect(runRegion()).not.toHaveTextContent(" in, ");
  });

  it("shows a dash for a model and a duration the run does not have, and no MCP row without MCP data", async () => {
    serveRuns([makeRun({ id: 31, job_slug: "test:disk", summary: "Fine." })]);
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(runRegion()).toHaveTextContent("Fine."));
    expect(runRegion()).toHaveTextContent("duration—");
    expect(runRegion()).toHaveTextContent("model—");
    expect(runRegion()).not.toHaveTextContent("MCP");
  });

  it("names an MCP server that was unreachable as a link, and the tools that were not sorted", async () => {
    serveRuns([
      detailedRun({
        id: 31,
        job_slug: "test:disk",
        summary: "Fine.",
        mcp: { domains: [], unreachable: { "fake-host": "connection refused" }, unsorted: ["fake_tool_a", "fake_tool_b"] },
      }),
    ]);
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(runRegion()).toHaveTextContent("MCP—"));
    expect(within(runRegion()).getByRole("link", { name: "fake-host" })).toHaveAttribute("href", "#/settings/mcp/fake-host");
    expect(runRegion()).toHaveTextContent("unreachable: connection refused");
    expect(runRegion()).toHaveTextContent("not sorted, not given: fake_tool_a, fake_tool_b");
  });

  it("shows the error of a run that failed, as it is, and no summary text", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", status: "error", error: "Model unreachable: connection refused", summary: null })]);
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(runRegion()).toHaveTextContent("Model unreachable: connection refused"));
    expect(within(runRegion()).getByText("error")).toBeInTheDocument();
    expect(runRegion()).not.toHaveTextContent("The run left no summary.");
  });

  it("says a finished run left no summary when it has no summary, no error and no findings", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", summary: null })]);
    renderDetail("test:disk");
    await named("Disk usage");
    expect(await screen.findByText("The run left no summary.")).toBeInTheDocument();
  });

  it("shows the findings, the worst first, with the evidence and a note when the command output did not confirm one", async () => {
    serveRuns(dailyRuns());
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(runRegion()).toHaveTextContent("Disk almost full"));
    const text = runRegion().textContent ?? "";
    expect(text.indexOf("Disk almost full")).toBeLessThan(text.indexOf("Backup is old"));
    expect(text.indexOf("Backup is old")).toBeLessThan(text.indexOf("Kernel update waiting"));
    expect(runRegion()).toHaveTextContent("/dev/sda1 91% /data");
    expect(runRegion()).toHaveTextContent("Two days old. Not confirmed by command output.");
    expect(runRegion()).not.toHaveTextContent("The data disk is at 91 %. Not confirmed");
    expect(runRegion()).not.toHaveTextContent("The run left no summary.");
  });

  it("shows the summary as markdown: paragraphs and no raw markup", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", summary: "## Disk is filling up\n\nThe data disk is at 91 %." })]);
    renderDetail("test:disk");
    await named("Disk usage");
    expect(await within(runRegion()).findByText("The data disk is at 91 %.")).toBeInTheDocument();
    // (the heading's tag is not asserted: DOMPurify under happy-dom drops some tags, see the note at the top)
    expect(within(runRegion()).getByText("Disk is filling up", { exact: false })).toBeInTheDocument();
    expect(within(runRegion()).getByText("The data disk is at 91 %.").tagName).toBe("P");
    expect(runRegion()).not.toHaveTextContent("##");
  });

  it("has no explain button on a run that is still running, and says what it is doing", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", status: "running", finished: null, duration_s: null })]);
    renderDetail("test:disk");
    await named("Disk usage");
    expect(await screen.findByText("The model is working on it. This view updates when it ends.")).toBeInTheDocument();
    expect(within(runRegion()).queryByRole("button", { name: "explain" })).not.toBeInTheDocument();
    expect(runRegion()).toHaveTextContent("duration still running".replace(" ", ""));
    expect(runRegion()).not.toHaveTextContent("The run left no summary.");
  });

  it("a summary that is running says its jobs run one after another", async () => {
    serveRuns([detailedRun({ id: 41, job_slug: "daily", status: "running", finished: null, duration_s: null })]);
    renderDetail("daily");
    await named("Example daily");
    expect(await screen.findByText("Its jobs run one after another, then the summary. This view updates as each ends.")).toBeInTheDocument();
    expect(screen.queryByText("The model is working on it. This view updates when it ends.")).not.toBeInTheDocument();
  });

  it("the explain buttons of a finished run lead to a new chat", async () => {
    serveRuns(dailyRuns());
    const user = userEvent.setup();
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(runRegion()).toHaveTextContent("Disk almost full"));
    // one for the run, one for each of the three findings
    const buttons = within(runRegion()).getAllByRole("button", { name: "explain" });
    expect(buttons).toHaveLength(4);
    await user.click(buttons[0]);
    expect(location.hash).toBe("#/chat/new");
  });

  it("links to the model and tool calls of the run on the Logs page", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", summary: "Fine.", trace_turn: "turn-7" })]);
    renderDetail("test:disk");
    await named("Disk usage");
    const link = await within(await waitFor(() => runRegion())).findByRole("link", { name: "LLM and tool calls" });
    expect(link).toHaveAttribute("href", "#/logs/llm/for/job/test%3Adisk/turn-7");
  });

  it("links to the calls of the job as a whole when the run has no trace turn", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", summary: "Fine." })]);
    renderDetail("test:disk");
    await named("Disk usage");
    const link = await within(await waitFor(() => runRegion())).findByRole("link", { name: "LLM and tool calls" });
    expect(link).toHaveAttribute("href", "#/logs/llm/for/job/test%3Adisk");
  });

  it("a run that ran in a queue names the queue run it belongs to, as a link", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", queue_run_id: 41, summary: "Disk at 91 %." })]);
    renderDetail("test:disk");
    await named("Disk usage");
    const link = await within(await waitFor(() => runRegion())).findByRole("link", { name: "a queue run of Example daily" });
    expect(link).toHaveAttribute("href", "#/jobs/daily/runs/41");
    expect(runRegion()).toHaveTextContent("part of");
  });

  it("says only 'a queue run' while the queue run is not known", async () => {
    serveRuns([detailedRun({ id: 31, job_slug: "test:disk", queue_run_id: 777, summary: "Disk at 91 %." })]);
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(runRegion()).toHaveTextContent("part ofa queue run"));
    expect(within(runRegion()).queryByRole("link", { name: /a queue run/ })).not.toBeInTheDocument();
  });
});

describe("the queue of a summary run", () => {
  it("lists the job runs of the run in the order they ran, each as a link with its name, result and how long ago", async () => {
    serveRuns(dailyRuns());
    renderDetail("daily");
    await named("Example daily");
    const list = await screen.findByRole("navigation", { name: "Job runs of this summary run" });
    expect(hrefs(list)).toEqual(["#/jobs/test:disk/runs/31", "#/jobs/test:services/runs/32"]);
    const [first, second] = within(list).getAllByRole("link");
    expect(first).toHaveTextContent("01");
    expect(first).toHaveTextContent("Disk usage");
    expect(first).toHaveTextContent("warning");
    expect(first).toHaveTextContent("22m ago");
    expect(second).toHaveTextContent("02");
    expect(second).toHaveTextContent("Services");
    expect(second).toHaveTextContent("OK");
  });

  it("counts the jobs in the title and in the run facts, and fetches the run by id", async () => {
    serveRuns(dailyRuns());
    const seen = watch("get", "/api/jobs/runs/:id", () =>
      HttpResponse.json({
        ...detailedRun({ id: 41, job_slug: "daily" }),
        queue: [
          detailedRun({ id: 31, job_slug: "test:disk", queue_run_id: 41 }),
          detailedRun({ id: 32, job_slug: "test:services", queue_run_id: 41, status: "error", error: "Timed out\nsecond line" }),
        ],
      }),
    );
    renderDetail("daily");
    await named("Example daily");
    await screen.findByRole("navigation", { name: "Job runs of this summary run" });
    expect(seen[0].url.pathname).toBe("/api/jobs/runs/41");
    expect(screen.getByText("2 jobs in the order they ran · 1 failed")).toBeInTheDocument();
    expect(runRegion()).toHaveTextContent("queue2 jobs · 1 failed");
    // only the first line of an error
    const list = screen.getByRole("navigation", { name: "Job runs of this summary run" });
    expect(list).toHaveTextContent("error · 1m 35s · Timed out".replace("1m 35s", "1m 35s"));
    expect(list).not.toHaveTextContent("second line");
  });

  it("says the queue is loading until it is there", async () => {
    serveRuns(dailyRuns());
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.get("/api/jobs/runs/:id", async () => {
        await gate;
        return HttpResponse.json({ ...detailedRun({ id: 41, job_slug: "daily" }), queue: [detailedRun({ id: 31, job_slug: "test:disk", queue_run_id: 41 })] });
      }),
    );
    renderDetail("daily");
    await named("Example daily");
    expect(await screen.findByText("loading…")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Job runs of this summary run" })).not.toBeInTheDocument();
    release();
    expect(await screen.findByRole("navigation", { name: "Job runs of this summary run" })).toBeInTheDocument();
    expect(screen.queryByText("loading…")).not.toBeInTheDocument();
    expect(screen.getByText("1 job in the order they ran")).toBeInTheDocument();
  });

  it("says a finished run without job runs ran before the queue existed or had no jobs", async () => {
    serveRuns(dailyRuns());
    server.use(http.get("/api/jobs/runs/:id", () => HttpResponse.json({ ...detailedRun({ id: 41, job_slug: "daily" }), queue: [] })));
    renderDetail("daily");
    await named("Example daily");
    expect(await screen.findByText("This run has no job runs (it ran before 0.10, or its summary had no jobs).")).toBeInTheDocument();
    expect(screen.getByText("0 jobs in the order they ran")).toBeInTheDocument();
  });

  it("says no job of a running run has ended yet", async () => {
    serveRuns([detailedRun({ id: 41, job_slug: "daily", status: "running", finished: null, duration_s: null })]);
    server.use(http.get("/api/jobs/runs/:id", () => HttpResponse.json({ ...detailedRun({ id: 41, job_slug: "daily", status: "running", finished: null }), queue: [] })));
    renderDetail("daily");
    await named("Example daily");
    expect(await screen.findByText("No job of this run has ended yet.")).toBeInTheDocument();
  });

  it("shows the message when the queue cannot be loaded, and not the list", async () => {
    serveRuns(dailyRuns());
    server.use(http.get("/api/jobs/runs/:id", () => HttpResponse.json({ detail: "Database is locked" }, { status: 500 })));
    renderDetail("daily");
    await named("Example daily");
    expect(await screen.findByText("The queue of this run could not be loaded: Database is locked")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Job runs of this summary run" })).not.toBeInTheDocument();
  });

  it("a job has no queue panel", async () => {
    serveRuns(dailyRuns().map((r) => ({ ...r, job_slug: "test:disk" })));
    renderDetail("test:disk");
    await named("Disk usage");
    await waitFor(() => expect(runRegion()).toHaveTextContent("Disk almost full"));
    expect(screen.queryByRole("heading", { name: "Queue" })).not.toBeInTheDocument();
  });

  it("the queue shown for a run of another id is the queue of that run", async () => {
    serveRuns(dailyRuns());
    const seen = watch("get", "/api/jobs/runs/:id", () => HttpResponse.json({ ...detailedRun({ id: 31, job_slug: "daily" }), queue: [] }));
    renderDetail("daily", "runs", "31");
    await named("Example daily");
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    expect(seen[0].url.pathname).toBe("/api/jobs/runs/31");
  });
});

describe("live events", () => {
  it("reads the runs and the job again when a run of this job finishes", async () => {
    const runs = watch("get", "/api/jobs/:slug/runs", () => HttpResponse.json({ items: [] }));
    const jobs = watch("get", "/api/jobs/:slug", () => HttpResponse.json(makeJob({ slug: "test:fresh", name: "Fresh check" })));
    renderDetail("test:fresh");
    await named("Fresh check");
    await waitFor(() => expect(runs).toHaveLength(1));
    expect(jobs).toHaveLength(1);
    act(() => FakeEventSource.current().emit("job.finished", { slug: "test:fresh", run_id: 5 }));
    await waitFor(() => expect(runs).toHaveLength(2));
    await waitFor(() => expect(jobs).toHaveLength(2));
  });

  it("reads only the runs when a run of this job starts", async () => {
    const runs = watch("get", "/api/jobs/:slug/runs", () => HttpResponse.json({ items: [] }));
    const jobs = watch("get", "/api/jobs/:slug", () => HttpResponse.json(makeJob({ slug: "test:fresh", name: "Fresh check" })));
    renderDetail("test:fresh");
    await named("Fresh check");
    await waitFor(() => expect(runs).toHaveLength(1));
    act(() => FakeEventSource.current().emit("job.started", { slug: "test:fresh", run_id: 6 }));
    await waitFor(() => expect(runs).toHaveLength(2));
    expect(jobs).toHaveLength(1);
  });

  it("ignores the events of other jobs for its runs", async () => {
    const runs = watch("get", "/api/jobs/:slug/runs", () => HttpResponse.json({ items: [] }));
    renderDetail("test:fresh");
    await named("Fresh check");
    await waitFor(() => expect(runs).toHaveLength(1));
    act(() => FakeEventSource.current().emit("job.finished", { slug: "test:disk", run_id: 5 }));
    // give a wrong reload time to happen
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 80)));
    expect(runs).toHaveLength(1);
  });

  it("shows the new run after the runs were read again", async () => {
    serveRuns([]);
    renderDetail("test:fresh");
    expect(await screen.findByText("No runs yet. The first comes on schedule, or start one with run now.")).toBeInTheDocument();
    serveRuns([detailedRun({ id: 60, job_slug: "test:fresh", status: "crit", summary: "Everything is down." })]);
    act(() => FakeEventSource.current().emit("job.finished", { slug: "test:fresh", run_id: 60 }));
    await waitFor(() => expect(runRegion()).toHaveTextContent("Everything is down."));
    expect(within(runRegion()).getByText("critical")).toBeInTheDocument();
    expect(screen.queryByText(/No runs yet/)).not.toBeInTheDocument();
  });

  it("reads the queue of a summary run again when a job of it finishes", async () => {
    serveRuns(dailyRuns());
    const seen = watch("get", "/api/jobs/runs/:id", () => HttpResponse.json({ ...detailedRun({ id: 41, job_slug: "daily" }), queue: [] }));
    renderDetail("daily");
    await named("Example daily");
    await waitFor(() => expect(seen).toHaveLength(1));
    act(() => FakeEventSource.current().emit("job.finished", { slug: "test:services", run_id: 32 }));
    await waitFor(() => expect(seen.length).toBeGreaterThan(1));
  });
});

