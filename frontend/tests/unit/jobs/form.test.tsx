// The Jobs page's forms (`#/jobs/new`, `#/jobs/new/summary`, `#/jobs/<slug>/edit`): defaults, the slug that follows the
// name, the schedule (interval / cron / none, with its words and preview), what Save sends, errors, the notes of builtin
// jobs, reset and delete with their confirmations, and the notice a save leaves on the detail.
// The members of a summary are in members.test.tsx, the domains field in domains.test.tsx.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AutomationProvider } from "../../../src/automation";
import { JobsPage } from "../../../src/pages/Jobs";
import { makeJob } from "../../mocks/fixtures/dashboard";
import { jobsList, mcpTools, type JobItem } from "../../mocks/fixtures/jobs";
import { server } from "../../mocks/server";
import { installDialogBehaviour } from "../kit/part1-support";
import { installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();
let dialogs: ReturnType<typeof installDialogBehaviour>;
beforeEach(() => {
  dialogs = installDialogBehaviour();
  server.use(
    http.get("/api/jobs", () => HttpResponse.json(jobsList())),
    http.get("/api/mcp/tools", () => HttpResponse.json(mcpTools())),
  );
});
afterEach(() => {
  dialogs.remove();
  location.hash = "";
});

const tree = (...params: string[]) => (
  <AutomationProvider>
    <JobsPage params={params} />
  </AutomationProvider>
);

/** Renders the page at `params` and waits for the form's title. */
async function openForm(params: string[], title: string) {
  const view = render(tree(...params));
  await screen.findByRole("heading", { level: 1, name: title });
  return view;
}

type Seen = { url: URL; body: unknown };
/** Answers a write to `path` and records what the page sent. */
function capture(method: "post" | "put" | "delete", path: string, answer: (body: Record<string, unknown>) => Response | Promise<Response>) {
  const seen: Seen[] = [];
  server.use(
    http[method](path, async ({ request }) => {
      const text = await request.text();
      const body = text ? (JSON.parse(text) as Record<string, unknown>) : undefined;
      seen.push({ url: new URL(request.url), body });
      return answer(body ?? {});
    }),
  );
  return seen;
}

const jobOf = (slug: string): JobItem => jobsList().items.find((j) => j.slug === slug)!;
const savedAs = (slug: string) => () => HttpResponse.json(jobOf(slug));
const created = (body: Record<string, unknown>) => HttpResponse.json(makeJob({ slug: String(body.slug), name: String(body.name) }), { status: 201 });

/** One job served by `GET /api/jobs/<slug>` instead of the default. */
const serveJob = (job: JobItem) => server.use(http.get("/api/jobs/:slug", ({ params }) => (decodeURIComponent(String(params.slug)) === job.slug ? HttpResponse.json(job) : HttpResponse.json({ detail: "Job not found" }, { status: 404 }))));

const field = (name: RegExp) => screen.getByRole("textbox", { name });
const submitButton = (name: string) => screen.getByRole("button", { name });
const radio = (name: string) => screen.getByRole("radio", { name });
const unitSelect = () => screen.getByRole("combobox", { name: "Unit" });
const intervalInput = () => screen.getByRole("spinbutton", { name: "Interval" });
const optionTexts = (select: HTMLElement) => within(select).getAllByRole("option").map((o) => o.textContent);

async function fillNew(user: ReturnType<typeof userEvent.setup>, name = "Disk check", prompt = "Check the free disk space.") {
  await user.type(field(/^name/), name);
  await user.type(field(/^prompt/), prompt);
}

describe("loading and errors", () => {
  it("says Loading… until the jobs are there, then shows the form", async () => {
    render(tree("new"));
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 1, name: "New job" })).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("shows the error when the list of jobs cannot be loaded for a new job", async () => {
    server.use(http.get("/api/jobs", () => HttpResponse.json({ detail: "Database is locked" }, { status: 500 })));
    render(tree("new"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Database is locked");
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
  });

  it("shows the error when the job to edit does not exist", async () => {
    render(tree("nowhere", "edit"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Job not found");
    expect(screen.queryByRole("button", { name: "save" })).not.toBeInTheDocument();
  });

  it("shows the error when the list of jobs cannot be loaded for an edit", async () => {
    server.use(http.get("/api/jobs", () => HttpResponse.json({ detail: "Database is locked" }, { status: 500 })));
    render(tree("test:disk", "edit"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Database is locked");
  });

  it("says Loading… while the job to edit is on its way", async () => {
    render(tree("test:disk", "edit"));
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await screen.findByRole("heading", { level: 1, name: "Edit Disk usage" });
  });
});

describe("a new job", () => {
  it("starts with the defaults of a topic job", async () => {
    await openForm(["new"], "New job");
    expect(screen.getByRole("link", { name: "← jobs" })).toHaveAttribute("href", "#/jobs");
    expect(field(/^name/)).toHaveValue("");
    expect(field(/^slug/)).toHaveValue("");
    expect(field(/^prompt/)).toHaveValue("");
    expect(radio("interval")).toBeChecked();
    expect(intervalInput()).toHaveValue(1);
    expect(unitSelect()).toHaveDisplayValue("hour");
    expect(screen.getByRole("combobox", { name: /^notify/ })).toHaveDisplayValue("on warning, critical or error");
    expect(screen.getByRole("combobox", { name: /^model/ })).toHaveDisplayValue("Worker");
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked();
    expect(screen.getByRole("spinbutton", { name: /^time limit/ })).toHaveValue(null);
    expect(screen.getByRole("spinbutton", { name: /^step limit/ })).toHaveValue(null);
    expect(screen.getByText("This job uses only the shell.")).toBeInTheDocument();
    expect(submitButton("create job")).toBeEnabled();
    expect(screen.getByRole("link", { name: "cancel" })).toHaveAttribute("href", "#/jobs");
    expect(screen.queryByRole("button", { name: "delete" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "reset to default" })).not.toBeInTheDocument();
    expect(screen.queryByText(/^Made by/)).not.toBeInTheDocument();
  });

  it("offers every summary to put the job into, with its schedule", async () => {
    await openForm(["new"], "New job");
    const group = screen.getByRole("group", { name: /^summaries/ });
    const boxes = within(group).getAllByRole("checkbox");
    expect(boxes).toHaveLength(1);
    expect(within(group).getByRole("checkbox", { name: /Example daily/ })).not.toBeChecked();
    expect(group).toHaveTextContent("Example daily · at 06:00");
  });

  it("says so when there is no summary yet", async () => {
    server.use(http.get("/api/jobs", () => HttpResponse.json({ items: jobsList().items.filter((j) => j.kind !== "summary"), retired: [] })));
    await openForm(["new"], "New job");
    expect(screen.getByText("There is no summary yet.")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("a new summary starts with the main model, notify on change, and no summaries to join", async () => {
    await openForm(["new", "summary"], "New summary");
    expect(screen.getByRole("combobox", { name: /^model/ })).toHaveDisplayValue("Main");
    expect(screen.getByRole("combobox", { name: /^notify/ })).toHaveDisplayValue("when the result changes");
    expect(screen.queryByRole("group", { name: /^summaries/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Jobs in this summary" })).toBeInTheDocument();
    expect(submitButton("create summary")).toBeInTheDocument();
    expect(screen.getByText(/How to sum up the results of its jobs/)).toBeInTheDocument();
  });

  it("opens a different form when the route changes from a job to a summary", async () => {
    const view = await openForm(["new"], "New job");
    await userEvent.setup().type(field(/^name/), "Typed text");
    view.rerender(tree("new", "summary"));
    expect(await screen.findByRole("heading", { level: 1, name: "New summary" })).toBeInTheDocument();
    expect(field(/^name/)).toHaveValue("");
  });
});

describe("the slug of a new job", () => {
  it("follows the name: lower case, runs of other characters become one dash, no dash at the ends", async () => {
    await openForm(["new"], "New job");
    await userEvent.setup().type(field(/^name/), "  Disk & Space: Check! ");
    expect(field(/^slug/)).toHaveValue("disk-space-check");
  });

  it("is cut at 40 characters", async () => {
    await openForm(["new"], "New job");
    await userEvent.setup().type(field(/^name/), "a".repeat(50));
    expect(field(/^slug/)).toHaveValue("a".repeat(40));
  });

  it("stops following the name once it was typed in by hand", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.type(field(/^name/), "Disk");
    expect(field(/^slug/)).toHaveValue("disk");
    await user.clear(field(/^slug/));
    await user.type(field(/^slug/), "my:own");
    await user.type(field(/^name/), " check");
    expect(field(/^slug/)).toHaveValue("my:own");
  });
});

describe("the schedule", () => {
  it("names the units in the singular for 1 and in the plural otherwise", async () => {
    await openForm(["new"], "New job");
    expect(optionTexts(unitSelect())).toEqual(["minute", "hour", "day"]);
    const user = userEvent.setup();
    await user.clear(intervalInput());
    await user.type(intervalInput(), "3");
    expect(optionTexts(unitSelect())).toEqual(["minutes", "hours", "days"]);
    expect(unitSelect()).toHaveDisplayValue("hours");
  });

  it("previews how often an interval fires in the next 24 hours", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    expect(screen.getByRole("img", { name: "When this schedule fires in the next 24 hours: 24 runs" })).toBeInTheDocument();
    expect(screen.getByText("24 runs in the next 24 h · one cell = 30 minutes")).toBeInTheDocument();

    await user.selectOptions(unitSelect(), "minutes");
    await user.clear(intervalInput());
    await user.type(intervalInput(), "30");
    expect(screen.getByRole("img", { name: "When this schedule fires in the next 24 hours: 48 runs" })).toBeInTheDocument();
    expect(screen.getByText("48 runs in the next 24 h · one cell = 30 minutes")).toBeInTheDocument();

    await user.selectOptions(unitSelect(), "days");
    await user.clear(intervalInput());
    await user.type(intervalInput(), "1");
    expect(screen.getByRole("img", { name: "When this schedule fires in the next 24 hours: 1 run" })).toBeInTheDocument();
    expect(screen.getByText("1 run in the next 24 h · one cell = 30 minutes")).toBeInTheDocument();
  });

  it("cron: shows the default expression in words and reads what is typed", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.click(radio("cron"));
    expect(radio("cron")).toBeChecked();
    expect(screen.getByRole("textbox", { name: "Cron expression" })).toHaveValue("0 6 * * *");
    expect(screen.getByText("At 06:00")).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton", { name: "Interval" })).not.toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /^When this schedule fires/ })).not.toBeInTheDocument();

    await user.clear(screen.getByRole("textbox", { name: "Cron expression" }));
    await user.type(screen.getByRole("textbox", { name: "Cron expression" }), "30 7 * * 1");
    expect(screen.getByText("At 07:30, only on Monday")).toBeInTheDocument();
  });

  it("cron: says so when the expression cannot be read", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.click(radio("cron"));
    await user.clear(screen.getByRole("textbox", { name: "Cron expression" }));
    await user.type(screen.getByRole("textbox", { name: "Cron expression" }), "every now and then");
    expect(screen.getByText("Not a cron expression this page can read: minute hour day month weekday.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Cron expression" })).toBeInvalid();
  });

  it("none: says it runs by hand only and drops the interval and cron fields", async () => {
    await openForm(["new"], "New job");
    await userEvent.setup().click(radio("none"));
    expect(screen.getByText("No schedule: manual.")).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton", { name: "Interval" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Cron expression" })).not.toBeInTheDocument();
  });

  it("the arrow keys move the choice between interval, cron and none", async () => {
    // (the focus move after the key runs in a frame callback that throws in the kit's Segmented: a known finding of the
    // kit tests, schemaform.test.tsx; the frame callbacks are dropped here)
    vi.stubGlobal("requestAnimationFrame", () => 0);
    try {
      await openForm(["new"], "New job");
      const user = userEvent.setup();
      radio("interval").focus();
      await user.keyboard("{ArrowRight}");
      expect(radio("cron")).toBeChecked();
      await user.keyboard("{ArrowRight}");
      expect(radio("none")).toBeChecked();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("saving a new job", () => {
  it("sends the interval job as typed and goes to its page", async () => {
    const seen = capture("post", "/api/jobs", created);
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await fillNew(user);
    await user.click(submitButton("create job"));
    await waitFor(() => expect(location.hash).toBe("#/jobs/disk-check"));
    expect(seen).toHaveLength(1);
    expect(seen[0].url.pathname).toBe("/api/jobs");
    expect(seen[0].body).toEqual({
      name: "Disk check",
      prompt: "Check the free disk space.",
      model: "worker",
      enabled: true,
      timeout_s: null,
      max_steps: null,
      domains: [],
      interval_s: 3600,
      cron: null,
      notify: "warn_crit",
      summaries: [],
      slug: "disk-check",
      kind: "topic",
    });
  });

  it("works against the default handlers of the shared setup too", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await fillNew(user, "Plain job");
    await user.click(submitButton("create job"));
    await waitFor(() => expect(location.hash).toBe("#/jobs/plain-job"));
  });

  it("sends the name trimmed and the chosen options: minutes, notify, model, enabled, limits", async () => {
    const seen = capture("post", "/api/jobs", created);
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await fillNew(user, "  Net check  ");
    await user.selectOptions(unitSelect(), "minutes");
    await user.clear(intervalInput());
    await user.type(intervalInput(), "15");
    await user.selectOptions(screen.getByRole("combobox", { name: /^notify/ }), "never");
    await user.selectOptions(screen.getByRole("combobox", { name: /^model/ }), "main");
    await user.click(screen.getByRole("switch", { name: "Enabled" }));
    await user.type(screen.getByRole("spinbutton", { name: /^time limit/ }), "2.5");
    await user.type(screen.getByRole("spinbutton", { name: /^step limit/ }), "12");
    await user.click(submitButton("create job"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({
      name: "Net check",
      model: "main",
      enabled: false,
      timeout_s: 150,
      max_steps: 12,
      interval_s: 900,
      cron: null,
      notify: "never",
      slug: "net-check",
    });
  });

  it("sends a cron job with the expression trimmed and no interval", async () => {
    const seen = capture("post", "/api/jobs", created);
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await fillNew(user);
    await user.click(radio("cron"));
    await user.clear(screen.getByRole("textbox", { name: "Cron expression" }));
    await user.type(screen.getByRole("textbox", { name: "Cron expression" }), "  0 3 * * *  ");
    await user.click(submitButton("create job"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ cron: "0 3 * * *", interval_s: null });
  });

  it("sends a job without a schedule with neither interval nor cron", async () => {
    const seen = capture("post", "/api/jobs", created);
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await fillNew(user);
    await user.click(radio("none"));
    await user.click(submitButton("create job"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ cron: null, interval_s: null });
  });

  it("a job put into a summary is sent without a schedule of its own and shows where it runs", async () => {
    const seen = capture("post", "/api/jobs", created);
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await fillNew(user);
    await user.click(screen.getByRole("checkbox", { name: /Example daily/ }));
    expect(screen.getByText("runs in Example daily")).toBeInTheDocument();
    expect(screen.getByText("nothing itself: its summary reports")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: "Schedule type" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /^notify/ })).not.toBeInTheDocument();
    await user.click(submitButton("create job"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ summaries: ["daily"], slug: "disk-check" });
    expect(seen[0].body).not.toHaveProperty("interval_s");
    expect(seen[0].body).not.toHaveProperty("cron");
    expect(seen[0].body).not.toHaveProperty("notify");
  });

  it("a new job has no warning about losing a schedule: it has none yet", async () => {
    await openForm(["new"], "New job");
    await userEvent.setup().click(screen.getByRole("checkbox", { name: /Example daily/ }));
    expect(screen.queryByText("Its own schedule will be removed")).not.toBeInTheDocument();
  });

  it("a new summary is sent with its (empty) list of jobs, its kind and no summaries", async () => {
    const seen = capture("post", "/api/jobs", created);
    await openForm(["new", "summary"], "New summary");
    const user = userEvent.setup();
    await fillNew(user, "Weekly", "Sum up the week for me.");
    await user.click(submitButton("create summary"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ kind: "summary", slug: "weekly", members: [], model: "main", notify: "changed", interval_s: 3600 });
    expect(seen[0].body).not.toHaveProperty("summaries");
    await waitFor(() => expect(location.hash).toBe("#/jobs/weekly"));
  });

  it("does not send a form without a name", async () => {
    const seen = capture("post", "/api/jobs", created);
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.type(field(/^prompt/), "Check the free disk space.");
    await user.click(submitButton("create job"));
    await new Promise((r) => setTimeout(r, 50));
    expect(seen).toHaveLength(0);
    expect(location.hash).toBe("");
  });

  it("shows the server's error, keeps what was typed and lets the form be sent again", async () => {
    let fail = true;
    const seen = capture("post", "/api/jobs", (body) => (fail ? HttpResponse.json({ detail: "A job with this slug exists" }, { status: 409 }) : created(body)));
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await fillNew(user);
    await user.click(submitButton("create job"));
    expect(await screen.findByRole("alert")).toHaveTextContent("A job with this slug exists");
    expect(location.hash).toBe("");
    expect(field(/^name/)).toHaveValue("Disk check");
    expect(submitButton("create job")).toBeEnabled();

    fail = false;
    await user.click(submitButton("create job"));
    await waitFor(() => expect(location.hash).toBe("#/jobs/disk-check"));
    expect(seen).toHaveLength(2);
  });

  it("shows each problem of a validation error as 'Field: message.'", async () => {
    capture("post", "/api/jobs", () =>
      HttpResponse.json({ detail: [{ loc: ["body", "name"], msg: "Too long", type: "x" }, { loc: ["body", "cron"], msg: "Bad cron", type: "x" }] }, { status: 422 }),
    );
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await fillNew(user);
    await user.click(submitButton("create job"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Name: Too long. Cron: Bad cron");
  });

  it("disables the buttons while the save is on its way", async () => {
    let release = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    capture("post", "/api/jobs", async (body) => {
      await gate;
      return created(body);
    });
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await fillNew(user);
    await user.click(submitButton("create job"));
    await waitFor(() => expect(submitButton("create job")).toBeDisabled());
    release();
    await waitFor(() => expect(location.hash).toBe("#/jobs/disk-check"));
  });
});

describe("editing a job", () => {
  it("shows the job as stored", async () => {
    await openForm(["test:disk", "edit"], "Edit Disk usage");
    expect(screen.getByRole("link", { name: "← Disk usage" })).toHaveAttribute("href", "#/jobs/test:disk");
    expect(screen.getByRole("link", { name: "cancel" })).toHaveAttribute("href", "#/jobs/test:disk");
    expect(field(/^name/)).toHaveValue("Disk usage");
    expect(field(/^prompt/)).toHaveValue("Check the thing.");
    expect(screen.getByRole("group", { name: /^slug/ })).toHaveTextContent("test:disk");
    expect(screen.queryByRole("textbox", { name: /^slug/ })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Example daily/ })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked();
    expect(submitButton("save")).toBeInTheDocument();
    expect(screen.getByText(/^Made by a user/)).toBeInTheDocument();
  });

  it("a job in a summary shows where it runs instead of a schedule and notify", async () => {
    await openForm(["test:disk", "edit"], "Edit Disk usage");
    expect(screen.getByRole("group", { name: /^schedule/ })).toHaveTextContent("runs in Example daily");
    expect(screen.getByRole("group", { name: /^schedule/ })).toHaveTextContent("Take it out of every summary to give it a schedule of its own.");
    expect(screen.getByRole("group", { name: /^notify/ })).toHaveTextContent("nothing itself: its summary reports");
  });

  it("sends the stored job back as it is, without schedule and notify, to its own address", async () => {
    const seen = capture("put", "/api/jobs/:slug", savedAs("test:disk"));
    await openForm(["test:disk", "edit"], "Edit Disk usage");
    await userEvent.setup().click(submitButton("save"));
    await waitFor(() => expect(location.hash).toBe("#/jobs/test:disk"));
    expect(seen).toHaveLength(1);
    expect(seen[0].url.pathname).toBe("/api/jobs/test%3Adisk");
    expect(seen[0].body).toEqual({
      name: "Disk usage",
      prompt: "Check the thing.",
      model: "worker",
      enabled: true,
      timeout_s: null,
      max_steps: null,
      domains: [],
      summaries: ["daily"],
    });
  });

  it("taking the job out of its summaries brings its schedule back, as it was stored", async () => {
    const seen = capture("put", "/api/jobs/:slug", savedAs("test:disk"));
    await openForm(["test:disk", "edit"], "Edit Disk usage");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: /Example daily/ }));
    expect(radio("interval")).toBeChecked();
    expect(intervalInput()).toHaveValue(30);
    expect(unitSelect()).toHaveDisplayValue("minutes");
    expect(screen.getByRole("combobox", { name: /^notify/ })).toHaveDisplayValue("when the result changes");
    await user.click(submitButton("save"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ interval_s: 1800, cron: null, notify: "changed", summaries: [] });
  });

  it("changes are sent: name, prompt, the new schedule", async () => {
    const seen = capture("put", "/api/jobs/:slug", savedAs("test:fresh"));
    await openForm(["test:fresh", "edit"], "Edit Fresh check");
    const user = userEvent.setup();
    expect(unitSelect()).toHaveDisplayValue("minutes");
    expect(intervalInput()).toHaveValue(15);
    await user.clear(field(/^name/));
    await user.type(field(/^name/), "Fresher check");
    await user.clear(field(/^prompt/));
    await user.type(field(/^prompt/), "Look at it again.");
    await user.selectOptions(unitSelect(), "hours");
    await user.clear(intervalInput());
    await user.type(intervalInput(), "6");
    await user.click(submitButton("save"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ name: "Fresher check", prompt: "Look at it again.", interval_s: 21600, cron: null });
    expect(seen[0].body).not.toHaveProperty("slug");
    expect(seen[0].body).not.toHaveProperty("kind");
  });

  it("the name does not change the slug of an existing job", async () => {
    await openForm(["test:fresh", "edit"], "Edit Fresh check");
    await userEvent.setup().type(field(/^name/), " again");
    expect(screen.getByRole("group", { name: /^slug/ })).toHaveTextContent("test:fresh");
  });

  it("shows an interval in the biggest unit that holds it", async () => {
    await openForm(["test:solo", "edit"], "Edit Solo check");
    expect(intervalInput()).toHaveValue(2);
    expect(unitSelect()).toHaveDisplayValue("hours");
    expect(screen.getByRole("switch", { name: "Enabled" })).not.toBeChecked();
  });

  it("an interval that is not whole minutes is shown in minutes, rounded", async () => {
    serveJob(makeJob({ slug: "odd", name: "Odd", interval_s: 100 }));
    await openForm(["odd", "edit"], "Edit Odd");
    expect(intervalInput()).toHaveValue(2);
    expect(unitSelect()).toHaveDisplayValue("minutes");
  });

  it("a cron job opens in cron mode with its expression in words", async () => {
    await openForm(["other:llm", "edit"], "Edit Made by the model");
    expect(radio("cron")).toBeChecked();
    expect(screen.getByRole("textbox", { name: "Cron expression" })).toHaveValue("30 7 * * 1");
    expect(screen.getByText("At 07:30, only on Monday")).toBeInTheDocument();
    expect(screen.getByText(/^Made by the LLM/)).toBeInTheDocument();
  });

  it("a job without a schedule opens with none and sends no schedule", async () => {
    const seen = capture("put", "/api/jobs/:slug", savedAs("loose"));
    await openForm(["loose", "edit"], "Edit Loose check");
    expect(radio("none")).toBeChecked();
    expect(screen.getByText("No schedule: manual.")).toBeInTheDocument();
    await userEvent.setup().click(submitButton("save"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ interval_s: null, cron: null });
  });

  it("shows the limits in minutes and steps, and sends them back in seconds", async () => {
    const job = makeJob({ slug: "limited", name: "Limited", interval_s: 3600, timeout_s: 90, max_steps: 12 });
    serveJob(job);
    const seen = capture("put", "/api/jobs/:slug", () => HttpResponse.json(job));
    await openForm(["limited", "edit"], "Edit Limited");
    expect(screen.getByRole("spinbutton", { name: /^time limit/ })).toHaveValue(1.5);
    expect(screen.getByRole("spinbutton", { name: /^step limit/ })).toHaveValue(12);
    const user = userEvent.setup();
    await user.clear(screen.getByRole("spinbutton", { name: /^time limit/ }));
    await user.type(screen.getByRole("spinbutton", { name: /^time limit/ }), "3");
    await user.clear(screen.getByRole("spinbutton", { name: /^step limit/ }));
    await user.click(submitButton("save"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ timeout_s: 180, max_steps: null });
  });

  it("the switch turns the job on", async () => {
    const seen = capture("put", "/api/jobs/:slug", savedAs("test:solo"));
    await openForm(["test:solo", "edit"], "Edit Solo check");
    const user = userEvent.setup();
    await user.click(screen.getByRole("switch", { name: "Enabled" }));
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked();
    await user.click(submitButton("save"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ enabled: true, interval_s: 7200 });
  });

  it("shows the server's error and stays on the form", async () => {
    capture("put", "/api/jobs/:slug", () => HttpResponse.json({ detail: "Prompt must not be empty" }, { status: 422 }));
    await openForm(["test:fresh", "edit"], "Edit Fresh check");
    await userEvent.setup().click(submitButton("save"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Prompt must not be empty");
    expect(location.hash).toBe("");
    expect(submitButton("save")).toBeEnabled();
  });
});

describe("a job that joins a summary", () => {
  it("warns that its own schedule is removed, and the warning goes with the tick", async () => {
    await openForm(["test:solo", "edit"], "Edit Solo check");
    const user = userEvent.setup();
    expect(screen.queryByText("Its own schedule will be removed")).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /Example daily/ }));
    expect(screen.getByText("Its own schedule will be removed")).toBeInTheDocument();
    expect(screen.getByText("Saving puts Solo check into Example daily. It then runs there, and its own schedule (every 2 hours) is removed.")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /Example daily/ }));
    expect(screen.queryByText("Its own schedule will be removed")).not.toBeInTheDocument();
  });

  it("does not warn when the job had no schedule", async () => {
    await openForm(["loose", "edit"], "Edit Loose check");
    await userEvent.setup().click(screen.getByRole("checkbox", { name: /Example daily/ }));
    expect(screen.queryByText("Its own schedule will be removed")).not.toBeInTheDocument();
  });

  it("does not warn about a job that is in the summary already", async () => {
    await openForm(["test:disk", "edit"], "Edit Disk usage");
    expect(screen.queryByText("Its own schedule will be removed")).not.toBeInTheDocument();
  });

  it("sends the summary it joins", async () => {
    const seen = capture("put", "/api/jobs/:slug", savedAs("test:solo"));
    await openForm(["test:solo", "edit"], "Edit Solo check");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: /Example daily/ }));
    await user.click(submitButton("save"));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].body).toMatchObject({ summaries: ["daily"] });
    expect(seen[0].body).not.toHaveProperty("interval_s");
  });
});

describe("the notice a save leaves", () => {
  it("is shown once on the detail the save leads to, and can be dismissed", async () => {
    capture("put", "/api/jobs/:slug", () => HttpResponse.json({ ...jobOf("test:solo"), notice: "Its own schedule was removed." }));
    const view = await openForm(["test:solo", "edit"], "Edit Solo check");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: /Example daily/ }));
    await user.click(submitButton("save"));
    await waitFor(() => expect(location.hash).toBe("#/jobs/test:solo"));

    view.rerender(tree("test:solo"));
    expect(await screen.findByRole("heading", { level: 1, name: "Solo check" })).toBeInTheDocument();
    expect(await screen.findByText("Its own schedule was removed.")).toBeInTheDocument();
    expect(screen.getByText("Saved")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "dismiss" }));
    expect(screen.queryByText("Its own schedule was removed.")).not.toBeInTheDocument();
  });

  it("does not show up on the detail of another job", async () => {
    capture("put", "/api/jobs/:slug", () => HttpResponse.json({ ...jobOf("test:solo"), notice: "Its own schedule was removed." }));
    const view = await openForm(["test:solo", "edit"], "Edit Solo check");
    await userEvent.setup().click(submitButton("save"));
    await waitFor(() => expect(location.hash).toBe("#/jobs/test:solo"));
    view.rerender(tree("test:fresh"));
    expect(await screen.findByRole("heading", { level: 1, name: "Fresh check" })).toBeInTheDocument();
    expect(screen.queryByText("Its own schedule was removed.")).not.toBeInTheDocument();
  });

  it("a save without a notice shows none", async () => {
    capture("put", "/api/jobs/:slug", savedAs("test:solo"));
    const view = await openForm(["test:solo", "edit"], "Edit Solo check");
    await userEvent.setup().click(submitButton("save"));
    await waitFor(() => expect(location.hash).toBe("#/jobs/test:solo"));
    view.rerender(tree("test:solo"));
    expect(await screen.findByRole("heading", { level: 1, name: "Solo check" })).toBeInTheDocument();
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });
});

describe("builtin jobs", () => {
  it("an unchanged builtin explains that it follows the releases, and cannot be reset or deleted", async () => {
    await openForm(["test:builtin", "edit"], "Edit Builtin check");
    expect(screen.getByText("A builtin job. As long as you do not change it, it follows the version of later releases. Turning it on or off does not count as a change.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "reset to default" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "delete" })).not.toBeInTheDocument();
    expect(screen.getByText(/^Added with the app/)).toBeInTheDocument();
  });

  it("a changed builtin explains that it keeps the version, and can be reset but not deleted", async () => {
    await openForm(["test:edited", "edit"], "Edit Edited builtin");
    expect(screen.getByText("A builtin job you changed: it keeps your version and no longer follows later releases. Reset to default to follow them again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "reset to default" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "delete" })).not.toBeInTheDocument();
  });

  it("a job of the user can be deleted but not reset, and has no builtin note", async () => {
    await openForm(["test:fresh", "edit"], "Edit Fresh check");
    expect(screen.getByRole("button", { name: "delete" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "reset to default" })).not.toBeInTheDocument();
    expect(screen.queryByText(/^A builtin/)).not.toBeInTheDocument();
  });

  it("reset asks first; cancel changes nothing", async () => {
    const seen = capture("post", "/api/jobs/:slug/reset", savedAs("test:edited"));
    await openForm(["test:edited", "edit"], "Edit Edited builtin");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "reset to default" }));
    const dialog = screen.getByRole("dialog", { name: "Reset Edited builtin to the version of this release?" });
    expect(dialog).toHaveTextContent("Your changes to it are lost. Whether it is on, and when it notifies, stay as they are.");
    await user.click(within(dialog).getByRole("button", { name: "cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(seen).toHaveLength(0);
    expect(screen.getByRole("button", { name: "reset to default" })).toBeInTheDocument();
  });

  it("reset posts without a body, shows the notice and the note of an unchanged builtin", async () => {
    const seen = capture("post", "/api/jobs/:slug/reset", () => HttpResponse.json({ ...jobOf("test:edited"), edited: false, notice: "Back to the version of this release." }));
    await openForm(["test:edited", "edit"], "Edit Edited builtin");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "reset to default" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "reset" }));
    expect(await screen.findByText("Reset to default")).toBeInTheDocument();
    expect(screen.getByText("Back to the version of this release.")).toBeInTheDocument();
    expect(seen).toHaveLength(1);
    expect(seen[0].url.pathname).toBe("/api/jobs/test%3Aedited/reset");
    expect(seen[0].body).toBeUndefined();
    expect(screen.getByText(/^A builtin job\. As long as you do not change it/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "reset to default" })).not.toBeInTheDocument();
  });

  it("reset loads the form with the job as the release has it", async () => {
    const stored = jobOf("test:edited");
    capture("post", "/api/jobs/:slug/reset", () => HttpResponse.json({ ...stored, name: "Edited builtin, release version", edited: false, updated: stored.updated + 60, notice: "Back." }));
    await openForm(["test:edited", "edit"], "Edit Edited builtin");
    const user = userEvent.setup();
    await user.type(field(/^name/), " plus my change");
    await user.click(screen.getByRole("button", { name: "reset to default" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "reset" }));
    await screen.findByText("Back.");
    expect(field(/^name/)).toHaveValue("Edited builtin, release version");
  });

  it("a failed reset shows the error and keeps the form", async () => {
    capture("post", "/api/jobs/:slug/reset", () => HttpResponse.json({ detail: "No release version of this job" }, { status: 409 }));
    await openForm(["test:edited", "edit"], "Edit Edited builtin");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "reset to default" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "reset" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No release version of this job");
    expect(screen.queryByText("Reset to default")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "reset to default" })).toBeEnabled();
  });
});

describe("deleting a job", () => {
  it("asks first and says what goes with it; cancel changes nothing", async () => {
    const seen = capture("delete", "/api/jobs/:slug", () => new HttpResponse(null, { status: 204 }));
    await openForm(["test:fresh", "edit"], "Edit Fresh check");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete the job Fresh check?" });
    expect(dialog).toHaveTextContent("Its runs are deleted too. This cannot be undone.");
    await user.click(within(dialog).getByRole("button", { name: "cancel" }));
    expect(seen).toHaveLength(0);
    expect(location.hash).toBe("");
  });

  it("deletes after the confirmation and goes back to the list", async () => {
    const seen = capture("delete", "/api/jobs/:slug", () => new HttpResponse(null, { status: 204 }));
    await openForm(["test:fresh", "edit"], "Edit Fresh check");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "delete" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "delete" }));
    await waitFor(() => expect(location.hash).toBe("#/jobs"));
    expect(seen).toHaveLength(1);
    expect(seen[0].url.pathname).toBe("/api/jobs/test%3Afresh");
  });

  it("names the summaries a job is taken out of", async () => {
    await openForm(["test:disk", "edit"], "Edit Disk usage");
    await userEvent.setup().click(screen.getByRole("button", { name: "delete" }));
    expect(screen.getByRole("dialog", { name: "Delete the job Disk usage?" })).toHaveTextContent("Its runs are deleted too, and it is taken out of Example daily. This cannot be undone.");
  });

  it("a summary says how many jobs stay and what happens to them", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    await userEvent.setup().click(screen.getByRole("button", { name: "delete" }));
    expect(screen.getByRole("dialog", { name: "Delete the summary Example daily?" })).toHaveTextContent(
      "Its runs are deleted too. Its 2 jobs stay; those in no other summary are left without a schedule and run only when started by hand. This cannot be undone.",
    );
  });

  it("a failed delete shows the error and keeps the form", async () => {
    capture("delete", "/api/jobs/:slug", () => HttpResponse.json({ detail: "The job is running" }, { status: 409 }));
    await openForm(["test:fresh", "edit"], "Edit Fresh check");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "delete" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "delete" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The job is running");
    expect(location.hash).toBe("");
    expect(submitButton("save")).toBeEnabled();
  });
});
