// The dashboard page from API data: summary cards, the lanes with their run strips, the findings,
// run now, and the error, loading and empty states. `/api/stream` is a fake EventSource (fake-event-source.ts).
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { AutomationProvider } from "../../../src/automation";
import { takeExplain } from "../../../src/components/Explain";
import { DashboardPage } from "../../../src/pages/Dashboard";
import { dashboardJobs, dashboardStatus, makeJob, makeRun, nowSeconds, type JobItem, type JobsAnswer, type StatusAnswer } from "../../mocks/fixtures/dashboard";
import { server } from "../../mocks/server";
import { FakeEventSource, installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();
afterEach(() => {
  location.hash = "";
});

/** The dashboard as the app shows it: inside the automation provider. */
function renderDashboard() {
  return render(
    <AutomationProvider>
      <DashboardPage />
    </AutomationProvider>,
  );
}

/** Answers /api/status with the summaries' state (the shell's default status has no `overall`) and /api/jobs with `jobs`. */
function serve(opts: { status?: (s: StatusAnswer) => StatusAnswer; jobs?: (j: JobsAnswer) => JobsAnswer } = {}) {
  const now = nowSeconds();
  server.use(
    http.get("/api/status", () => HttpResponse.json((opts.status ?? ((s) => s))(dashboardStatus(now)))),
    http.get("/api/jobs", () => HttpResponse.json((opts.jobs ?? ((j) => j))(dashboardJobs(now)))),
  );
}

/** Changes one job of the list. */
const changeJob = (slug: string, over: Partial<JobItem>) => (list: JobsAnswer): JobsAnswer => ({
  ...list,
  items: list.items.map((j) => (j.slug === slug ? { ...j, ...over } : j)),
});

/** The requests of a path the page made. */
function watch(method: "get" | "post", path: string, answer: () => Response = () => HttpResponse.json({})) {
  const seen: { url: URL; body: string }[] = [];
  server.use(
    http[method](path, async ({ request }) => {
      seen.push({ url: new URL(request.url), body: await request.text() });
      return answer();
    }),
  );
  return seen;
}

const card = async () => (await screen.findByRole("heading", { level: 2, name: "Disk is filling up on host-a" })).closest("section")!;
const laneOf = (name: string) => screen.getByRole("link", { name }).closest(".fd-lane") as HTMLElement;
const laneNames = () => screen.getAllByRole("link").filter((a) => a.classList.contains("fd-lane-name")).map((a) => a.textContent);

describe("summary card", () => {
  it("shows the headline, the status word, the counts and when it ran and runs next", async () => {
    serve();
    renderDashboard();
    const section = await card();
    expect(within(section).getByText("Warning")).toBeInTheDocument();
    expect(section).toHaveTextContent("1critical1warning1info");
    expect(section).toHaveTextContent("Example daily · at 06:00 · ran 5m ago · next in 3h · history");
  });

  it("links to the history of the summary", async () => {
    serve();
    renderDashboard();
    await card();
    expect(screen.getByRole("link", { name: "History of Example daily" })).toHaveAttribute("href", "#/jobs/daily");
  });

  it("falls back to the first line of the summary text when the status has no headline", async () => {
    serve({ status: (s) => ({ ...s, overall: { status: "warn", summaries: [{ ...s.overall!.summaries[0], headline: "" }] } }) });
    renderDashboard();
    // "## Disk is filling up on host-a" without its heading mark
    expect(await screen.findByRole("heading", { level: 2, name: "Disk is filling up on host-a" })).toBeInTheDocument();
  });

  it("says a summary has not run when it has no run and no state", async () => {
    serve({
      status: (s) => ({ ...s, overall: null }),
      jobs: changeJob("daily", { last_run: null, next_run: null }),
    });
    renderDashboard();
    const heading = await screen.findByRole("heading", { level: 2, name: "Example daily has not run yet" });
    const section = heading.closest("section")!;
    expect(section).not.toHaveTextContent("critical");
    expect(section).not.toHaveTextContent("ran ");
    expect(within(section).getByRole("button", { name: /Run Example daily now/ })).toBeEnabled();
    expect(within(section).queryByRole("button", { name: "explain" })).not.toBeInTheDocument();
  });

  it("shows the error text of a failed run under the headline", async () => {
    serve({
      status: (s) => ({ ...s, overall: { status: "error", summaries: [{ ...s.overall!.summaries[0], status: "error", headline: "The summary failed" }] } }),
      jobs: changeJob("daily", { last_run: makeRun({ id: 42, job_slug: "daily", status: "error", error: "Model unreachable: connection refused" }) }),
    });
    renderDashboard();
    const heading = await screen.findByRole("heading", { level: 2, name: "The summary failed" });
    const section = heading.closest("section")!;
    expect(within(section).getByText("Error")).toBeInTheDocument();
    expect(within(section).getByText("Model unreachable: connection refused")).toBeInTheDocument();
  });

  it("does not repeat an error that is the headline already", async () => {
    serve({
      status: (s) => ({ ...s, overall: { status: "error", summaries: [{ ...s.overall!.summaries[0], status: "error", headline: "Model unreachable" }] } }),
      jobs: changeJob("daily", { last_run: makeRun({ id: 42, job_slug: "daily", status: "error", error: "Model unreachable" }) }),
    });
    renderDashboard();
    const heading = await screen.findByRole("heading", { level: 2, name: "Model unreachable" });
    // the card shows it once (the lane of the summary quotes it too, that is another place)
    expect(within(heading.closest("section")!).getAllByText("Model unreachable")).toHaveLength(1);
  });

  it("shows one card per enabled summary and none for a disabled one", async () => {
    serve({
      jobs: (list) => ({
        ...list,
        items: [
          ...list.items,
          makeJob({ slug: "weekly", name: "Example weekly", kind: "summary", interval_s: 86400 * 7, members: ["test:services"] }),
          makeJob({ slug: "retired-sum", name: "Example off", kind: "summary", enabled: false }),
        ],
      }),
    });
    renderDashboard();
    await card();
    expect(screen.getByRole("heading", { level: 2, name: "Example weekly has not run yet" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Example off/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Run .* now/ })).toHaveLength(2);
  });
});

describe("run now", () => {
  it("starts the run of the summary and keeps the button off until the server has answered", async () => {
    serve();
    let answer!: () => void;
    const seen: { url: URL; body: string }[] = [];
    server.use(
      http.post("/api/jobs/:slug/run", async ({ request }) => {
        seen.push({ url: new URL(request.url), body: await request.text() });
        await new Promise<void>((resolve) => (answer = resolve));
        return HttpResponse.json({ run_id: 7 });
      }),
    );
    const user = userEvent.setup();
    renderDashboard();
    const section = await card();
    const button = within(section).getByRole("button", { name: "Run Example daily now: its 2 jobs, then the summary" });
    expect(button).toHaveTextContent("run now");
    await user.click(button);

    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].url.pathname).toBe("/api/jobs/daily/run");
    expect(seen[0].body).toBe("");
    expect(button).toBeDisabled();
    await act(async () => answer());
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("shows the server's message when the run cannot start, and the button works again", async () => {
    serve();
    server.use(http.post("/api/jobs/:slug/run", () => HttpResponse.json({ detail: "Automation is paused" }, { status: 409 })));
    const user = userEvent.setup();
    renderDashboard();
    const section = await card();
    const button = within(section).getByRole("button", { name: /Run Example daily now/ });
    await user.click(button);
    expect(await within(section).findByRole("alert")).toHaveTextContent("Automation is paused");
    expect(button).toBeEnabled();
  });

  it("shows a running summary: its status line, a disabled button, and the lamp of its lane", async () => {
    serve({ jobs: changeJob("daily", { running_run_id: 50 }) });
    renderDashboard();
    const section = await card();
    expect(within(section).getByText("running: its 2 jobs, then the summary")).toBeInTheDocument();
    const button = within(section).getByRole("button", { name: /Run Example daily now/ });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("running…");
    expect(laneOf("Example daily")).toHaveTextContent("running now");
  });
});

describe("jobs on the clock", () => {
  it("lists the topics first, then the summaries, each linking to its job", async () => {
    serve();
    renderDashboard();
    await card();
    expect(laneNames()).toEqual(["Disk usage", "Services", "Solo check", "Fresh check", "Example daily"]);
    expect(screen.getByRole("link", { name: "Disk usage" })).toHaveAttribute("href", "#/jobs/test:disk");
    expect(screen.getByRole("link", { name: "Example daily" })).toHaveAttribute("href", "#/jobs/daily");
  });

  it("says in each lane what the job last said, what the summary quoted, and when it runs next", async () => {
    serve();
    renderDashboard();
    await card();
    // the clause the summary wrote about the topic wins over the topic's own text
    expect(laneOf("Disk usage")).toHaveTextContent("disk at 91 %");
    expect(laneOf("Disk usage")).toHaveTextContent("ran 20m ago · next in 10m");
    expect(laneOf("Services")).toHaveTextContent("all services up");
    // a topic outside any summary shows its own text; a disabled one says so and shows no next run
    expect(laneOf("Solo check")).toHaveTextContent("turned off");
    expect(laneOf("Solo check")).not.toHaveTextContent("next");
    expect(laneOf("Fresh check")).toHaveTextContent("waiting for its first run");
    expect(laneOf("Fresh check")).toHaveTextContent("next in 10m");
    expect(laneOf("Example daily")).toHaveTextContent("Disk is filling up on host-a");
  });

  it("shows the error of a failed run in its lane", async () => {
    serve({ jobs: changeJob("test:services", { last_run: makeRun({ id: 60, job_slug: "test:services", status: "error", error: "Tool call timed out" }) }) });
    renderDashboard();
    await card();
    expect(laneOf("Services")).toHaveTextContent("Tool call timed out");
  });

  it("asks for the runs of every job and marks them on the strips", async () => {
    serve();
    const asked: string[] = [];
    server.use(
      http.get("/api/jobs/:slug/runs", ({ request, params }) => {
        const url = new URL(request.url);
        asked.push(`${decodeURIComponent(String(params.slug))}?${url.searchParams.get("limit")}`);
        const now = nowSeconds();
        // a warning 20 minutes ago and an ok run 5 hours ago, only for test:disk
        const items =
          params.slug === "test%3Adisk" || params.slug === "test:disk"
            ? [makeRun({ id: 1, job_slug: "test:disk", status: "warn", started: now - 1200 }), makeRun({ id: 2, job_slug: "test:disk", status: "ok", started: now - 5 * 3600 })]
            : [];
        return HttpResponse.json({ items });
      }),
    );
    renderDashboard();
    await card();
    await waitFor(() => expect(laneOf("Disk usage").querySelectorAll(".fd-cell-warn")).toHaveLength(1));
    // (a request of the test before may land here late: compare the set)
    expect([...new Set(asked)].sort()).toEqual(["daily?25", "test:disk?25", "test:fresh?25", "test:services?25", "test:solo?25"]);
    const strip = laneOf("Disk usage");
    expect(strip.querySelectorAll(".fd-cell-ok")).toHaveLength(1);
    expect(strip.querySelectorAll(".fd-cell-now")).toHaveLength(1);
    expect(laneOf("Services").querySelectorAll(".fd-cell-ok, .fd-cell-warn, .fd-cell-crit")).toHaveLength(0);
  });

  it("marks the planned runs of an interval job on its strip, none for a job that is turned off", async () => {
    serve();
    renderDashboard();
    await card();
    // next in 10 min, then every 30 min, until 3 h ahead: the six slots of the future
    expect(laneOf("Disk usage").querySelectorAll(".fd-cell-next")).toHaveLength(6);
    expect(laneOf("Solo check").querySelectorAll(".fd-cell-next")).toHaveLength(0);
  });

  it("shows 'schedule paused' instead of the next runs while automation is paused", async () => {
    serve();
    server.use(http.get("/api/automation", () => HttpResponse.json({ paused: true, since: nowSeconds() - 60, by: "ui", queued_actions: 0 })));
    renderDashboard();
    const section = await card();
    await waitFor(() => expect(section).toHaveTextContent("schedule paused"));
    expect(section).not.toHaveTextContent("next in");
    expect(laneOf("Disk usage")).toHaveTextContent("ran 20m ago · schedule paused");
    expect(laneOf("Disk usage").querySelectorAll(".fd-cell-next")).toHaveLength(0);
    // a job that is turned off has no schedule to pause
    expect(laneOf("Solo check")).not.toHaveTextContent("schedule paused");
  });
});

describe("findings", () => {
  it("lists the findings of the summary worst first, with detail, evidence and the unconfirmed note", async () => {
    serve();
    renderDashboard();
    await card();
    const panel = screen.getByRole("heading", { level: 2, name: "Findings" }).closest("section")!;
    expect(panel).toHaveTextContent("3 from Example daily");
    expect(within(panel).getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["Disk almost full", "Backup is old", "Kernel update waiting"]);
    expect(within(panel).getByText("The data disk of host-a is at 91 %.")).toBeInTheDocument();
    expect(within(panel).getByText("/dev/sda1 91% /data")).toBeInTheDocument();
    expect(within(panel).getByText("The last backup finished two days ago. Not confirmed by command output.")).toBeInTheDocument();
  });

  it("names the summary of each finding when several summaries have findings", async () => {
    const now = nowSeconds();
    serve({
      jobs: (list) => ({
        ...list,
        items: [
          ...list.items,
          makeJob({
            slug: "weekly",
            name: "Example weekly",
            kind: "summary",
            interval_s: 86400 * 7,
            last_run: makeRun({ id: 77, job_slug: "weekly", status: "warn", started: now - 100, finished: now - 90, findings: [{ severity: "warn", title: "Certificate expires", detail: "In ten days.", evidence: [] }] }, now),
          }),
        ],
      }),
    });
    renderDashboard();
    await card();
    const panel = screen.getByRole("heading", { level: 2, name: "Findings" }).closest("section")!;
    expect(panel).toHaveTextContent("4 from 2 summaries");
    expect(within(panel).getByRole("link", { name: "Example weekly" })).toHaveAttribute("href", "#/jobs/weekly/runs/77");
    // each of the three findings of "Example daily" names it
    const daily = within(panel).getAllByRole("link", { name: "Example daily" });
    expect(daily).toHaveLength(3);
    for (const link of daily) expect(link).toHaveAttribute("href", "#/jobs/daily/runs/41");
  });

  it("opens a new chat that explains the finding, named by its stored index", async () => {
    serve();
    const user = userEvent.setup();
    renderDashboard();
    await card();
    const finding = screen.getByRole("heading", { level: 3, name: "Disk almost full" }).closest("article")!;
    await user.click(within(finding).getByRole("button", { name: "explain" }));
    expect(location.hash).toBe("#/chat/new");
    // the crit finding is the second one as stored by the server
    expect(takeExplain()).toEqual({ runId: 41, finding: 1 });
  });

  it("opens a new chat that explains the whole run from the card", async () => {
    serve();
    const user = userEvent.setup();
    renderDashboard();
    const section = await card();
    await user.click(within(section).getByRole("button", { name: "explain" }));
    expect(location.hash).toBe("#/chat/new");
    expect(takeExplain()).toEqual({ runId: 41, finding: null });
  });

  it("shows no findings panel when no summary has findings", async () => {
    serve({ jobs: changeJob("daily", { last_run: makeRun({ id: 41, job_slug: "daily", status: "ok", findings: [] }) }) });
    renderDashboard();
    await card();
    expect(screen.queryByRole("heading", { level: 2, name: "Findings" })).not.toBeInTheDocument();
  });
});

describe("notices, empty and error states", () => {
  it("tells the owner when no model is configured, with a link to the model settings", async () => {
    serve({ status: (s) => ({ ...s, llm_configured: false }) });
    renderDashboard();
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("No model is configured, so no job can run.");
    expect(within(notice).getByRole("link", { name: "Open the model settings" })).toHaveAttribute("href", "#/settings/llm");
  });

  it("has no notice when a model is configured", async () => {
    serve();
    renderDashboard();
    await card();
    expect(screen.queryByText(/No model is configured/)).not.toBeInTheDocument();
  });

  it("asks to create a summary when there is none, with a link to the Jobs page", async () => {
    serve({ jobs: (list) => ({ ...list, items: list.items.filter((j) => j.kind !== "summary") }) });
    renderDashboard();
    expect(await screen.findByRole("heading", { level: 1, name: "Nothing sums up the jobs yet" })).toBeInTheDocument();
    expect(screen.getByText(/Create one on the Jobs page\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "open jobs" })).toHaveAttribute("href", "#/jobs");
    // the lanes of the topics are still there
    expect(laneNames()).toContain("Disk usage");
  });

  it("asks to turn a summary on when all of them are off", async () => {
    serve({ jobs: changeJob("daily", { enabled: false }) });
    renderDashboard();
    expect(await screen.findByRole("heading", { level: 1, name: "Nothing sums up the jobs yet" })).toBeInTheDocument();
    expect(screen.getByText(/Turn one on, or create one, on the Jobs page\./)).toBeInTheDocument();
  });

  it("shows no lanes but a link to create a job when there are no jobs", async () => {
    serve({ jobs: () => ({ items: [], retired: [] }) });
    renderDashboard();
    await screen.findByRole("heading", { level: 1, name: "Nothing sums up the jobs yet" });
    expect(screen.getByText(/No jobs yet\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create one" })).toHaveAttribute("href", "#/jobs/new");
    expect(laneNames()).toEqual([]);
  });

  it("shows Loading… until the status and the jobs have come", async () => {
    serve();
    renderDashboard();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await card();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("shows the server's message when the status cannot be read", async () => {
    serve();
    server.use(http.get("/api/status", () => HttpResponse.json({ detail: "database is locked" }, { status: 500 })));
    renderDashboard();
    expect(await screen.findByRole("alert")).toHaveTextContent("Backend unreachable: database is locked");
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("shows the server's message when the jobs cannot be read", async () => {
    serve();
    server.use(http.get("/api/jobs", () => HttpResponse.json({ detail: "jobs are down" }, { status: 503 })));
    renderDashboard();
    expect(await screen.findByRole("alert")).toHaveTextContent("Backend unreachable: jobs are down");
  });

  it("falls back to 'HTTP <status>' when the error has no text", async () => {
    serve();
    server.use(http.get("/api/status", () => new HttpResponse(null, { status: 502 })));
    renderDashboard();
    expect(await screen.findByRole("alert")).toHaveTextContent("Backend unreachable: HTTP 502");
  });

  it("still draws the lanes when the runs of the strips cannot be read", async () => {
    serve();
    server.use(http.get("/api/jobs/:slug/runs", () => HttpResponse.json({ detail: "no runs" }, { status: 500 })));
    renderDashboard();
    await card();
    expect(laneNames()).toContain("Disk usage");
    expect(laneOf("Disk usage").querySelectorAll(".fd-cell-ok, .fd-cell-warn")).toHaveLength(0);
  });
});

describe("live updates", () => {
  it("reads everything again when a job finishes", async () => {
    serve();
    const statusAsked = watch("get", "/api/status", () => HttpResponse.json(dashboardStatus()));
    const jobsAsked = watch("get", "/api/jobs", () => HttpResponse.json(dashboardJobs()));
    renderDashboard();
    await card();
    expect(statusAsked).toHaveLength(1);
    expect(jobsAsked).toHaveLength(1);

    // the next answer carries the new result
    server.use(
      http.get("/api/status", () =>
        HttpResponse.json({ ...dashboardStatus(), overall: { status: "ok", summaries: [{ slug: "daily", name: "Example daily", status: "ok", headline: "Everything is fine", finished: nowSeconds(), run_id: 43 }] } }),
      ),
    );
    act(() => FakeEventSource.current().emit("job.finished", { slug: "daily", run_id: 43 }));
    expect(await screen.findByRole("heading", { level: 2, name: "Everything is fine" })).toBeInTheDocument();
    expect(screen.getByText("OK", { selector: ".fd-hero-word" })).toBeInTheDocument();
  });

  it("shows a job as running from its job.started event and as done after job.finished", async () => {
    serve();
    renderDashboard();
    await card();
    expect(laneOf("Services")).not.toHaveTextContent("running now");
    act(() => FakeEventSource.current().emit("job.started", { slug: "test:services" }));
    await waitFor(() => expect(laneOf("Services")).toHaveTextContent("running now"));
    act(() => FakeEventSource.current().emit("job.finished", { slug: "test:services" }));
    await waitFor(() => expect(laneOf("Services")).not.toHaveTextContent("running now"));
  });

  it("reads again when automation is paused or resumed", async () => {
    serve();
    const jobsAsked = watch("get", "/api/jobs", () => HttpResponse.json(dashboardJobs()));
    renderDashboard();
    await card();
    expect(jobsAsked).toHaveLength(1);
    act(() => FakeEventSource.current().emit("automation.changed", { paused: true }));
    await waitFor(() => expect(jobsAsked).toHaveLength(2));
  });
});
