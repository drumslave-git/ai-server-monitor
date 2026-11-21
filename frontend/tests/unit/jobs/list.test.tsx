// The Jobs page's list (`#/jobs`): the summaries and the jobs from API data, switching one on or off, run now,
// what the last result and the next run say, and the error, loading and empty states.
// `/api/stream` is a fake EventSource (fake-event-source.ts).
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { AutomationProvider } from "../../../src/automation";
import { JobsPage } from "../../../src/pages/Jobs";
import { makeJob, nowSeconds, type JobsAnswer } from "../../mocks/fixtures/dashboard";
import { jobsList } from "../../mocks/fixtures/jobs";
import { server } from "../../mocks/server";
import { FakeEventSource, installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();
afterEach(() => {
  location.hash = "";
});

const renderList = () =>
  render(
    <AutomationProvider>
      <JobsPage params={[]} />
    </AutomationProvider>,
  );

/** Answers GET /api/jobs with the list (changed by `mutate`). */
function serve(mutate: (list: JobsAnswer) => JobsAnswer = (l) => l) {
  server.use(http.get("/api/jobs", () => HttpResponse.json(mutate(jobsList(nowSeconds())))));
}

/** The requests of a path the page made. */
function watch(method: "get" | "post" | "put", path: string, answer: () => Response = () => HttpResponse.json({})) {
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

const summariesTable = () => screen.getAllByRole("table")[0];
const jobsTable = () => screen.getAllByRole("table")[1];
const rowOf = (table: HTMLElement, name: string | RegExp) => within(table).getByRole("row", { name });
const loaded = () => screen.findAllByRole("table", {}, { timeout: 3000 }).then(() => undefined);

describe("header", () => {
  it("names the page and links to the two forms for new things", async () => {
    serve();
    renderList();
    await loaded();
    expect(screen.getByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "new summary" })).toHaveAttribute("href", "#/jobs/new/summary");
    expect(screen.getByRole("link", { name: "new job" })).toHaveAttribute("href", "#/jobs/new");
  });
});

describe("loading and errors", () => {
  it("says it is loading until the list is there", async () => {
    serve();
    renderList();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await loaded();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("shows the message of a failed load, and no list", async () => {
    server.use(http.get("/api/jobs", () => HttpResponse.json({ detail: "Database is locked" }, { status: 500 })));
    renderList();
    expect(await screen.findByRole("alert")).toHaveTextContent("Backend unreachable: Database is locked");
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("falls back to the status code when the answer has no text", async () => {
    server.use(http.get("/api/jobs", () => new HttpResponse(null, { status: 502 })));
    renderList();
    expect(await screen.findByRole("alert")).toHaveTextContent("Backend unreachable: HTTP 502");
  });
});

describe("empty states", () => {
  it("offers to create the first summary and the first job", async () => {
    serve(() => ({ items: [], retired: [] }));
    renderList();
    expect(await screen.findByText(/No summaries\./)).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "Create one" });
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["#/jobs/new/summary", "#/jobs/new"]);
    expect(screen.getByText(/No jobs\./)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows only the summaries table when there is no job", async () => {
    serve((l) => ({ ...l, items: l.items.filter((j) => j.kind === "summary") }));
    renderList();
    await loaded();
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(screen.getByText(/No jobs\./)).toBeInTheDocument();
    expect(screen.queryByText(/No summaries\./)).not.toBeInTheDocument();
  });
});

describe("summaries", () => {
  it("has the columns of a summary and a row with its name, schedule, model, jobs, notify, last result and next run", async () => {
    serve();
    renderList();
    await loaded();
    const heads = within(summariesTable()).getAllByRole("columnheader").map((h) => h.textContent);
    expect(heads).toEqual(["on", "summary", "schedule", "jobs", "notify", "last result", "next run", ""]);
    const row = rowOf(summariesTable(), /Example daily/);
    expect(within(row).getByRole("link", { name: "Example daily" })).toHaveAttribute("href", "#/jobs/daily");
    expect(row).toHaveTextContent("custom, made by a user");
    expect(row).toHaveTextContent("at 06:00");
    expect(within(row).getByRole("link", { name: "main model" })).toHaveAttribute("href", "#/settings/llm");
    expect(row).toHaveTextContent("2 jobs");
    expect(row).toHaveTextContent("when the result changes");
    expect(row).toHaveTextContent("warning");
    expect(row).toHaveTextContent("5m ago");
    expect(row).toHaveTextContent("in 3h");
  });

  it("previews its first two jobs as links, and says how many more there are", async () => {
    serve((l) => ({
      ...l,
      items: l.items.map((j) => (j.slug === "daily" ? { ...j, members: ["test:disk", "test:services", "test:solo", "test:fresh"] } : j)),
    }));
    renderList();
    await loaded();
    const row = rowOf(summariesTable(), /Example daily/);
    expect(row).toHaveTextContent("4 jobs");
    expect(within(row).getByRole("link", { name: "Disk usage" })).toHaveAttribute("href", "#/jobs/test%3Adisk");
    expect(within(row).getByRole("link", { name: "Services" })).toHaveAttribute("href", "#/jobs/test%3Aservices");
    expect(within(row).queryByRole("link", { name: "Solo check" })).not.toBeInTheDocument();
    expect(row).toHaveTextContent("and 2 more");
  });

  it("says 'none yet' for a summary without jobs, and counts one job in the singular", async () => {
    serve((l) => ({
      ...l,
      items: [
        ...l.items,
        makeJob({ slug: "empty", name: "Empty summary", kind: "summary", members: [] }),
        makeJob({ slug: "single", name: "Single summary", kind: "summary", members: ["test:solo"] }),
      ],
    }));
    renderList();
    await loaded();
    expect(rowOf(summariesTable(), /Empty summary/)).toHaveTextContent("0 jobsnone yet");
    const single = rowOf(summariesTable(), /Single summary/);
    expect(single).toHaveTextContent("1 job");
    expect(single).not.toHaveTextContent("1 jobs");
    expect(single).not.toHaveTextContent("and ");
  });

  it("falls back to the slug of a member the list does not know", async () => {
    serve((l) => ({ ...l, items: l.items.map((j) => (j.slug === "daily" ? { ...j, members: ["gone:job"] } : j)) }));
    renderList();
    await loaded();
    expect(within(rowOf(summariesTable(), /Example daily/)).getByRole("link", { name: "gone:job" })).toBeInTheDocument();
  });

  it("shows a summary that is off as off, with its switch off", async () => {
    serve((l) => ({ ...l, items: l.items.map((j) => (j.slug === "daily" ? { ...j, enabled: false } : j)) }));
    renderList();
    await loaded();
    const row = rowOf(summariesTable(), /Example daily/);
    expect(within(row).getByRole("switch", { name: "Example daily enabled" })).toHaveAttribute("aria-checked", "false");
    expect(within(row).getByRole("switch", { name: "Example daily enabled" })).toHaveAttribute("title", "Turn on");
    expect(row).toHaveTextContent("off");
    expect(row).not.toHaveTextContent("in 3h");
  });
});

describe("jobs", () => {
  it("has the columns of a job", async () => {
    serve();
    renderList();
    await loaded();
    const heads = within(jobsTable()).getAllByRole("columnheader").map((h) => h.textContent);
    expect(heads).toEqual(["on", "job", "runs", "notify", "last result", "next run", ""]);
  });

  it("groups them by the part of the slug before the colon: named groups in order, 'no prefix' last", async () => {
    serve();
    renderList();
    await loaded();
    const groupRows = within(jobsTable())
      .getAllByRole("row")
      .filter((r) => r.classList.contains("fd-group"))
      .map((r) => r.textContent);
    expect(groupRows).toEqual(["other · 1", "test · 6", "no prefix · 1"]);
    // the job sits in the group of its prefix
    const rows = within(jobsTable()).getAllByRole("row");
    const order = rows.map((r) => r.textContent ?? "");
    const at = (needle: string) => order.findIndex((t) => t.includes(needle));
    expect(at("Made by the model")).toBeGreaterThan(at("other · 1"));
    expect(at("Made by the model")).toBeLessThan(at("test · 6"));
    expect(at("Disk usage")).toBeGreaterThan(at("test · 6"));
    expect(at("Disk usage")).toBeLessThan(at("no prefix · 1"));
    expect(at("Loose check")).toBeGreaterThan(at("no prefix · 1"));
  });

  it("shows a job on its own schedule with its notify, model, origin and next run", async () => {
    serve();
    renderList();
    await loaded();
    const row = rowOf(jobsTable(), /Fresh check/);
    expect(within(row).getByRole("link", { name: "Fresh check" })).toHaveAttribute("href", "#/jobs/test:fresh");
    expect(row).toHaveTextContent("custom, made by a user");
    expect(row).toHaveTextContent("every 15 minutes");
    expect(within(row).getByRole("link", { name: "worker model" })).toHaveAttribute("href", "#/settings/worker");
    expect(row).toHaveTextContent("when the result changes");
    expect(row).toHaveTextContent("in 10m");
  });

  it("names builtin, edited and LLM-made jobs by their origin", async () => {
    serve();
    renderList();
    await loaded();
    expect(rowOf(jobsTable(), /Builtin check/)).toHaveTextContent("builtin");
    expect(rowOf(jobsTable(), /Builtin check/)).not.toHaveTextContent("edited");
    expect(rowOf(jobsTable(), /Edited builtin/)).toHaveTextContent("builtin, edited");
    expect(rowOf(jobsTable(), /Made by the model/)).toHaveTextContent("custom, made by the LLM");
  });

  it("reads a cron schedule in words, and a job with no schedule as manual", async () => {
    serve();
    renderList();
    await loaded();
    expect(rowOf(jobsTable(), /Made by the model/)).toHaveTextContent("at 07:30 AM, only on Monday".replace("07:30 AM", "07:30"));
    expect(rowOf(jobsTable(), /Loose check/)).toHaveTextContent("no schedule (manual)");
    // nothing to wait for: its next run is a dash
    expect(rowOf(jobsTable(), /Loose check/)).toHaveTextContent("—");
  });

  it("a job in a summary names its summary as a link, takes its notify from it and has no own next run", async () => {
    serve();
    renderList();
    await loaded();
    const row = rowOf(jobsTable(), /Disk usage/);
    expect(row).toHaveTextContent("in Example daily");
    expect(within(row).getAllByRole("link", { name: "Example daily" }).map((a) => a.getAttribute("href"))).toContain("#/jobs/daily");
    expect(row).toHaveTextContent("its summary reports");
    expect(row).not.toHaveTextContent("when the result changes");
  });

  it("shows when a job in a summary runs next: its own next run, or else the summary's, named", async () => {
    serve((l) => ({ ...l, items: l.items.map((j) => (j.slug === "test:disk" ? { ...j, next_run: null } : j)) }));
    renderList();
    await loaded();
    const row = rowOf(jobsTable(), /Disk usage/);
    // the summary's 06:00 is 3 h away
    expect(row).toHaveTextContent("in 3h");
    expect(row).toHaveTextContent("with Example daily");
    // a job that has its own next run does not name the summary
    expect(rowOf(jobsTable(), /Services/)).not.toHaveTextContent("with Example daily");
    expect(rowOf(jobsTable(), /Services/)).toHaveTextContent("in 30m");
  });

  it("uses the soonest of several summaries, and skips one that is off", async () => {
    const now = nowSeconds();
    serve((l) => ({
      ...l,
      items: [
        ...l.items.map((j) => (j.slug === "test:disk" ? { ...j, next_run: null, summaries: ["daily", "early", "off"] } : j)),
        makeJob({ slug: "early", name: "Early summary", kind: "summary", next_run: now + 2 * 3600, interval_s: 7200 }),
        makeJob({ slug: "off", name: "Off summary", kind: "summary", enabled: false, next_run: now + 600, interval_s: 600 }),
      ],
    }));
    renderList();
    await loaded();
    const row = rowOf(jobsTable(), /Disk usage/);
    expect(row).toHaveTextContent("in 2h");
    expect(row).toHaveTextContent("with Early summary");
    expect(row).not.toHaveTextContent("with Off summary");
  });

  it("shows a job that is off as off, with its switch off and nothing to wait for", async () => {
    serve();
    renderList();
    await loaded();
    const row = rowOf(jobsTable(), /Solo check/);
    expect(within(row).getByRole("switch", { name: "Solo check enabled" })).toHaveAttribute("aria-checked", "false");
    expect(row).toHaveTextContent("off");
    expect(rowOf(jobsTable(), /Fresh check/)).not.toHaveTextContent("off");
  });

  it("shows 'never run' for a job without a run, and the result and its age for one with a run", async () => {
    serve();
    renderList();
    await loaded();
    expect(rowOf(jobsTable(), /Fresh check/)).toHaveTextContent("never run");
    const disk = rowOf(jobsTable(), /Disk usage/);
    expect(disk).toHaveTextContent("warning");
    expect(disk).toHaveTextContent("20m ago");
    expect(rowOf(jobsTable(), /Services/)).toHaveTextContent("OK");
  });

  it("says 'paused' instead of the next run of an enabled job while automation is paused", async () => {
    serve();
    server.use(http.get("/api/automation", () => HttpResponse.json({ paused: true, since: nowSeconds() - 60, by: "ui", queued_actions: 0 })));
    renderList();
    await loaded();
    await waitFor(() => expect(rowOf(jobsTable(), /Fresh check/)).toHaveTextContent("paused"));
    expect(rowOf(jobsTable(), /Fresh check/)).not.toHaveTextContent("in 10m");
    expect(rowOf(summariesTable(), /Example daily/)).toHaveTextContent("paused");
    // a job that is off stays "off"
    expect(rowOf(jobsTable(), /Solo check/)).toHaveTextContent("off");
    expect(rowOf(jobsTable(), /Solo check/)).not.toHaveTextContent("paused");
  });
});

describe("switching on and off", () => {
  it("turns a job off with a PUT of enabled: false and reads the list again", async () => {
    serve();
    const puts = watch("put", "/api/jobs/:slug", () => HttpResponse.json(makeJob({ slug: "test:fresh", name: "Fresh check", enabled: false })));
    const lists = watch("get", "/api/jobs", () => HttpResponse.json(jobsList()));
    const user = userEvent.setup();
    renderList();
    await loaded();
    expect(lists).toHaveLength(1);
    const toggle = within(rowOf(jobsTable(), /Fresh check/)).getByRole("switch", { name: "Fresh check enabled" });
    expect(toggle).toHaveAttribute("title", "Turn off");
    await user.click(toggle);
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].url.pathname).toBe("/api/jobs/test%3Afresh");
    expect(puts[0].body).toEqual({ enabled: false });
    await waitFor(() => expect(lists).toHaveLength(2));
  });

  it("turns a job that is off on with enabled: true", async () => {
    serve();
    const puts = watch("put", "/api/jobs/:slug");
    const user = userEvent.setup();
    renderList();
    await loaded();
    await user.click(within(rowOf(jobsTable(), /Solo check/)).getByRole("switch", { name: "Solo check enabled" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].body).toEqual({ enabled: true });
  });

  it("switches a summary through the same call", async () => {
    serve();
    const puts = watch("put", "/api/jobs/:slug");
    const user = userEvent.setup();
    renderList();
    await loaded();
    await user.click(within(rowOf(summariesTable(), /Example daily/)).getByRole("switch", { name: "Example daily enabled" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].url.pathname).toBe("/api/jobs/daily");
    expect(puts[0].body).toEqual({ enabled: false });
  });

  it("shows the message of a failed switch and does not read the list again", async () => {
    serve();
    server.use(http.put("/api/jobs/:slug", () => HttpResponse.json({ detail: "A summary needs a prompt" }, { status: 422 })));
    const lists = watch("get", "/api/jobs", () => HttpResponse.json(jobsList()));
    const user = userEvent.setup();
    renderList();
    await loaded();
    await user.click(within(rowOf(jobsTable(), /Fresh check/)).getByRole("switch", { name: "Fresh check enabled" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A summary needs a prompt");
    expect(lists).toHaveLength(1);
  });
});

describe("run now", () => {
  it("starts a job on its own with POST run and names it in the button", async () => {
    serve();
    const posts = watch("post", "/api/jobs/:slug/run", () => HttpResponse.json({ run_id: 7 }));
    const user = userEvent.setup();
    renderList();
    await loaded();
    await user.click(within(rowOf(jobsTable(), /Fresh check/)).getByRole("button", { name: "Run Fresh check now" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url.pathname).toBe("/api/jobs/test%3Afresh/run");
    expect(posts[0].body).toBeUndefined();
  });

  it("a job in a summary runs alone: the button says so and names the summary", async () => {
    serve();
    renderList();
    await loaded();
    expect(within(rowOf(jobsTable(), /Disk usage/)).getByRole("button", { name: "Run Disk usage alone, without Example daily" })).toBeInTheDocument();
  });

  it("a summary runs its jobs and then itself: the button says how many jobs", async () => {
    serve();
    const posts = watch("post", "/api/jobs/:slug/run", () => HttpResponse.json({ run_id: 8 }));
    const user = userEvent.setup();
    renderList();
    await loaded();
    await user.click(within(rowOf(summariesTable(), /Example daily/)).getByRole("button", { name: "Run Example daily now: its 2 jobs, then the summary" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url.pathname).toBe("/api/jobs/daily/run");
  });

  it("shows the message when the run cannot be started", async () => {
    serve();
    server.use(http.post("/api/jobs/:slug/run", () => HttpResponse.json({ detail: "The model is not configured" }, { status: 409 })));
    const user = userEvent.setup();
    renderList();
    await loaded();
    await user.click(within(rowOf(jobsTable(), /Fresh check/)).getByRole("button", { name: "Run Fresh check now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The model is not configured");
  });

  it("the button of a job that is running is off, and the job reads 'running'", async () => {
    serve((l) => ({ ...l, items: l.items.map((j) => (j.slug === "test:fresh" ? { ...j, running_run_id: 12 } : j)) }));
    renderList();
    await loaded();
    const row = rowOf(jobsTable(), /Fresh check/);
    expect(within(row).getByRole("button", { name: "Run Fresh check now" })).toHaveAttribute("aria-disabled", "true");
    expect(row).toHaveTextContent("running");
    expect(row).not.toHaveTextContent("never run");
    expect(within(rowOf(jobsTable(), /Services/)).getByRole("button", { name: "Run Services alone, without Example daily" })).not.toHaveAttribute("aria-disabled");
  });

  it("does not start a run when the button of a running job is pressed", async () => {
    serve((l) => ({ ...l, items: l.items.map((j) => (j.slug === "test:fresh" ? { ...j, running_run_id: 12 } : j)) }));
    const posts = watch("post", "/api/jobs/:slug/run");
    const user = userEvent.setup();
    renderList();
    await loaded();
    await user.click(within(rowOf(jobsTable(), /Fresh check/)).getByRole("button", { name: "Run Fresh check now" }));
    expect(posts).toHaveLength(0);
  });
});

describe("live events", () => {
  it("shows a job as running from its job.started event and as done after job.finished", async () => {
    serve();
    renderList();
    await loaded();
    const row = () => rowOf(jobsTable(), /Services/);
    expect(row()).not.toHaveTextContent("running");
    act(() => FakeEventSource.current().emit("job.started", { slug: "test:services" }));
    await waitFor(() => expect(row()).toHaveTextContent("running"));
    expect(within(row()).getByRole("button", { name: /^Run Services/ })).toHaveAttribute("aria-disabled", "true");
    act(() => FakeEventSource.current().emit("job.finished", { slug: "test:services" }));
    await waitFor(() => expect(row()).not.toHaveTextContent("running"));
  });

  it("reads the list again when a job finishes", async () => {
    serve();
    const lists = watch("get", "/api/jobs", () => HttpResponse.json(jobsList()));
    renderList();
    await loaded();
    expect(lists).toHaveLength(1);
    act(() => FakeEventSource.current().emit("job.finished", { slug: "test:services", run_id: 5 }));
    await waitFor(() => expect(lists).toHaveLength(2));
  });

  it("shows the new result after the list was read again", async () => {
    serve();
    renderList();
    await loaded();
    expect(rowOf(jobsTable(), /Services/)).toHaveTextContent("OK");
    serve((l) => ({
      ...l,
      items: l.items.map((j) => (j.slug === "test:services" ? { ...j, last_run: makeJob({ slug: "x", name: "x" }) && { ...j.last_run!, id: 50, status: "crit" as const } } : j)),
    }));
    act(() => FakeEventSource.current().emit("job.finished", { slug: "test:services", run_id: 50 }));
    await waitFor(() => expect(rowOf(jobsTable(), /Services/)).toHaveTextContent("critical"));
  });
});
