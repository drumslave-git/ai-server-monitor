// The jobs of a summary on the form (`#/jobs/<summary>/edit`, `#/jobs/new/summary`): the ordered list with its up, down and
// remove buttons, the picker that adds a job, what is said about a job that gives up its own schedule, and what Save sends.
// Dragging a row with the pointer is not tested: the kit's sortable list owns it (tests/unit/kit/part1-sortable.test.tsx).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { AutomationProvider } from "../../../src/automation";
import { JobsPage } from "../../../src/pages/Jobs";
import { makeJob } from "../../mocks/fixtures/dashboard";
import { jobsList, mcpTools, type JobItem } from "../../mocks/fixtures/jobs";
import { server } from "../../mocks/server";
import { installDialogBehaviour } from "../kit/part1-support";
import { installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();
// happy-dom has no ARIA reflection (element.ariaDisabled); the focus hand-over after a move reads it, as a browser has it.
beforeAll(() => {
  Object.defineProperty(Element.prototype, "ariaDisabled", { configurable: true, get: function (this: Element) { return this.getAttribute("aria-disabled"); } });
});
afterAll(() => {
  delete (Element.prototype as unknown as Record<string, unknown>).ariaDisabled;
});
let dialogs: ReturnType<typeof installDialogBehaviour>;
beforeEach(() => {
  dialogs = installDialogBehaviour();
  serve(jobsList().items);
  server.use(http.get("/api/mcp/tools", () => HttpResponse.json(mcpTools())));
});
afterEach(() => {
  dialogs.remove();
  location.hash = "";
});

/** `GET /api/jobs` answers these jobs, and `GET /api/jobs/<slug>` the same ones by slug. */
function serve(items: JobItem[]) {
  server.use(
    http.get("/api/jobs", () => HttpResponse.json({ items, retired: [] })),
    http.get("/api/jobs/:slug", ({ params }) => {
      const job = items.find((j) => j.slug === decodeURIComponent(String(params.slug)));
      return job ? HttpResponse.json(job) : HttpResponse.json({ detail: "Job not found" }, { status: 404 });
    }),
  );
}

async function openForm(params: string[], title: string) {
  render(
    <AutomationProvider>
      <JobsPage params={params} />
    </AutomationProvider>,
  );
  await screen.findByRole("heading", { level: 1, name: title });
}

/** Records what Save sends (PUT of an edit, POST of a new summary) and answers with the summary. */
function captureSave(method: "put" | "post", path: string) {
  const bodies: Record<string, unknown>[] = [];
  server.use(
    http[method](path, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      bodies.push(body);
      return HttpResponse.json(makeJob({ slug: String(body.slug ?? "daily"), name: String(body.name), kind: "summary" }), { status: method === "post" ? 201 : 200 });
    }),
  );
  return bodies;
}

const list = (name: string) => screen.getByRole("list", { name });
const DAILY_LIST = "Jobs of Example daily, in the order they run";
/** The names of the rows, in order (from the handles: "Move <name>, 1 of 2"). */
const rowNames = () => screen.getAllByRole("button", { name: /^Move .*, \d+ of \d+$/ }).map((b) => b.getAttribute("aria-label")!.replace(/^Move (.*), \d+ of \d+$/, "$1"));
const addJob = async (user: ReturnType<typeof userEvent.setup>, ...names: string[]) => {
  for (const name of names) {
    await user.click(screen.getByRole("button", { name: "add job" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: new RegExp(name) }));
  }
};

describe("the list of jobs in a summary", () => {
  it("shows the jobs in the order they run, with their slugs, and how to move them", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    const panel = screen.getByRole("heading", { level: 2, name: "Jobs in this summary" }).closest("section")!;
    expect(panel).toHaveTextContent("2 · they run in this order, then the summary sums them up");
    const rows = within(list(DAILY_LIST)).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Disk usage");
    expect(rows[0]).toHaveTextContent("test:disk");
    expect(rows[1]).toHaveTextContent("Services");
    expect(rows[1]).toHaveTextContent("test:services");
    expect(rows[0]).not.toHaveTextContent("not saved yet");
    expect(screen.getByText(/^Drag a row by its number/)).toBeInTheDocument();
  });

  it("is not on the form of a job", async () => {
    await openForm(["test:fresh", "edit"], "Edit Fresh check");
    expect(screen.queryByRole("heading", { level: 2, name: "Jobs in this summary" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "add job" })).not.toBeInTheDocument();
  });

  it("moves a job down and up, and sends the new order", async () => {
    const bodies = captureSave("put", "/api/jobs/:slug");
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "move Disk usage down" }));
    expect(rowNames()).toEqual(["Services", "Disk usage"]);
    await user.click(screen.getByRole("button", { name: "move Disk usage up" }));
    expect(rowNames()).toEqual(["Disk usage", "Services"]);
    await user.click(screen.getByRole("button", { name: "move Services up" }));
    expect(rowNames()).toEqual(["Services", "Disk usage"]);
    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ members: ["test:services", "test:disk"] });
  });

  it("the edit of a summary sends its schedule, notify and model but no summaries", async () => {
    const bodies = captureSave("put", "/api/jobs/:slug");
    await openForm(["daily", "edit"], "Edit Example daily");
    await userEvent.setup().click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ members: ["test:disk", "test:services"], model: "main", interval_s: null, cron: "0 6 * * *", notify: "changed" });
    expect(bodies[0]).not.toHaveProperty("summaries");
  });

  it("removes a job from the list and counts the rest", async () => {
    const bodies = captureSave("put", "/api/jobs/:slug");
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "remove Services" }));
    expect(rowNames()).toEqual(["Disk usage"]);
    expect(screen.getByText("1 · they run in this order, then the summary sums them up")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ members: ["test:disk"] });
  });

  it("says so when the last job is removed, and the hint on dragging goes", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "remove Disk usage" }));
    await user.click(screen.getByRole("button", { name: "remove Services" }));
    expect(screen.getByText("No jobs yet. Add the jobs this summary runs.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: DAILY_LIST })).not.toBeInTheDocument();
    expect(screen.queryByText(/^Drag a row by its number/)).not.toBeInTheDocument();
  });

  it("a member that is not among the jobs is shown by its slug", async () => {
    serve(jobsList().items.map((j) => (j.slug === "daily" ? { ...j, members: ["test:disk", "ghost"] } : j)));
    await openForm(["daily", "edit"], "Edit Example daily");
    expect(rowNames()).toEqual(["Disk usage", "ghost"]);
    expect(screen.getByRole("button", { name: "remove ghost" })).toBeInTheDocument();
  });

  it("names the other summaries a job is in", async () => {
    const items = jobsList().items.map((j) => (j.slug === "test:disk" ? { ...j, summaries: ["daily", "weekly"] } : j));
    serve([...items, makeJob({ slug: "weekly", name: "Weekly review", kind: "summary", model: "main", interval_s: null, members: ["test:disk"] })]);
    await openForm(["daily", "edit"], "Edit Example daily");
    const first = within(list(DAILY_LIST)).getAllByRole("listitem")[0];
    expect(first).toHaveTextContent("test:disk · also in Weekly review");
    expect(within(list(DAILY_LIST)).getAllByRole("listitem")[1]).not.toHaveTextContent("also in");
  });
});

describe("adding a job to a summary", () => {
  it("offers the jobs that are not in the summary yet, in groups, with where they run", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    await userEvent.setup().click(screen.getByRole("button", { name: "add job" }));
    const dialog = screen.getByRole("dialog", { name: "Add a job to Example daily" });
    const group = (label: string) => within(within(dialog).getByRole("region", { name: label }));
    expect(within(dialog).getAllByRole("region").map((r) => r.getAttribute("aria-label"))).toEqual(["other", "test", "no prefix"]);
    expect(group("test").getAllByRole("button").map((b) => b.textContent)).toEqual([
      expect.stringContaining("Solo check"),
      expect.stringContaining("Fresh check"),
      expect.stringContaining("Builtin check"),
      expect.stringContaining("Edited builtin"),
    ]);
    expect(group("test").getByRole("button", { name: /Solo check/ })).toHaveTextContent("test:solo · every 2 hours (removed when it joins)");
    expect(group("no prefix").getByRole("button", { name: /Loose check/ })).toHaveTextContent("loose · no schedule (manual)");
    expect(group("no prefix").getByRole("button", { name: /Loose check/ })).not.toHaveTextContent("removed when it joins");
    expect(within(dialog).queryByRole("button", { name: /Disk usage/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /Services/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /Example daily/ })).not.toBeInTheDocument();
  });

  it("finds a job by its name or its slug", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "add job" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByRole("searchbox", { name: "filter" }), "fresh");
    expect(within(dialog).getAllByRole("button").filter((b) => b.textContent !== "close")).toHaveLength(1);
    expect(within(dialog).getByRole("button", { name: /Fresh check/ })).toBeInTheDocument();
    await user.clear(within(dialog).getByRole("searchbox", { name: "filter" }));
    await user.type(within(dialog).getByRole("searchbox", { name: "filter" }), "other:llm");
    expect(within(dialog).getByRole("button", { name: /Made by the model/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /Fresh check/ })).not.toBeInTheDocument();
  });

  it("a picked job is added at the end, closes the picker and is sent on save", async () => {
    const bodies = captureSave("put", "/api/jobs/:slug");
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await addJob(user, "Loose check");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(rowNames()).toEqual(["Disk usage", "Services", "Loose check"]);
    expect(within(list(DAILY_LIST)).getAllByRole("listitem")[2]).toHaveTextContent("loose · added, not saved yet");
    expect(screen.getByText("3 · they run in this order, then the summary sums them up")).toBeInTheDocument();
    expect(screen.queryByText("Its own schedule will be removed")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ members: ["test:disk", "test:services", "loose"] });
  });

  it("a picked job is no longer offered", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await addJob(user, "Loose check");
    await user.click(screen.getByRole("button", { name: "add job" }));
    expect(within(screen.getByRole("dialog")).queryByRole("button", { name: /Loose check/ })).not.toBeInTheDocument();
  });

  it("a removed job is offered again", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "remove Services" }));
    await user.click(screen.getByRole("button", { name: "add job" }));
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: /Services/ })).toBeInTheDocument();
  });

  it("says when every job is in the summary already", async () => {
    const items = jobsList().items.filter((j) => j.slug === "daily" || j.slug.startsWith("test:d") || j.slug === "test:services");
    serve(items);
    await openForm(["daily", "edit"], "Edit Example daily");
    await userEvent.setup().click(screen.getByRole("button", { name: "add job" }));
    expect(within(screen.getByRole("dialog")).getByText("Every job is in this summary already.")).toBeInTheDocument();
  });

  it("closing the picker adds nothing", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "add job" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(rowNames()).toEqual(["Disk usage", "Services"]);
  });

  it("shows where a job that is in another summary runs, without a schedule note", async () => {
    const items = jobsList().items.map((j) => (j.slug === "test:solo" ? { ...j, interval_s: null, summaries: ["weekly"] } : j));
    serve([...items, makeJob({ slug: "weekly", name: "Weekly review", kind: "summary", model: "main", interval_s: null, members: ["test:solo"] })]);
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "add job" }));
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: /Solo check/ })).toHaveTextContent("test:solo · in Weekly review");
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: /Solo check/ })).not.toHaveTextContent("removed when it joins");
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: /Solo check/ }));
    expect(within(list(DAILY_LIST)).getAllByRole("listitem")[2]).toHaveTextContent("test:solo · also in Weekly review · added, not saved yet");
  });
});

describe("a job that gives up its own schedule by joining", () => {
  it("is announced with its schedule, in the singular", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await addJob(user, "Solo check");
    expect(screen.getByText("Its own schedule will be removed")).toBeInTheDocument();
    expect(screen.getByText("Saving adds Solo check (every 2 hours) to Example daily. It then runs here, and its own schedule is removed.")).toBeInTheDocument();
    expect(within(list(DAILY_LIST)).getAllByRole("listitem")[2]).toHaveTextContent("test:solo · its schedule (every 2 hours) is removed on save");
  });

  it("is announced for every such job, in the plural", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await addJob(user, "Solo check", "Fresh check");
    expect(screen.getByText("Their own schedules will be removed")).toBeInTheDocument();
    expect(screen.getByText("Saving adds Solo check (every 2 hours), Fresh check (every 15 minutes) to Example daily. They then run here, and their own schedule is removed.")).toBeInTheDocument();
  });

  it("is withdrawn when the job is taken out again", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    const user = userEvent.setup();
    await addJob(user, "Solo check");
    await user.click(screen.getByRole("button", { name: "remove Solo check" }));
    expect(screen.queryByText("Its own schedule will be removed")).not.toBeInTheDocument();
  });

  it("does not concern the jobs that are members already", async () => {
    await openForm(["daily", "edit"], "Edit Example daily");
    await userEvent.setup().click(screen.getByRole("button", { name: "move Disk usage down" }));
    expect(screen.queryByText("Its own schedule will be removed")).not.toBeInTheDocument();
  });
});

describe("a new summary", () => {
  it("starts with no jobs and says how to add them", async () => {
    await openForm(["new", "summary"], "New summary");
    expect(screen.getByText("No jobs yet. Add the jobs this summary runs.")).toBeInTheDocument();
    expect(screen.getByText("0 · they run in this order, then the summary sums them up")).toBeInTheDocument();
  });

  it("calls itself 'this summary' until it has a name, then uses the name", async () => {
    await openForm(["new", "summary"], "New summary");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "add job" }));
    expect(screen.getByRole("dialog", { name: "Add a job to this summary" })).toBeInTheDocument();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "close" }));
    await addJob(user, "Solo check");
    expect(screen.getByRole("list", { name: "Jobs of this summary, in the order they run" })).toBeInTheDocument();
    expect(screen.getByText(/^Saving adds Solo check \(every 2 hours\) to this summary\./)).toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: /^name/ }), "Weekly");
    expect(screen.getByRole("list", { name: "Jobs of Weekly, in the order they run" })).toBeInTheDocument();
    expect(screen.getByText(/^Saving adds Solo check \(every 2 hours\) to Weekly\./)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "add job" }));
    expect(screen.getByRole("dialog", { name: "Add a job to Weekly" })).toBeInTheDocument();
  });

  it("is sent with the jobs in the order they were added", async () => {
    const bodies = captureSave("post", "/api/jobs");
    await openForm(["new", "summary"], "New summary");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: /^name/ }), "Weekly");
    await user.type(screen.getByRole("textbox", { name: /^prompt/ }), "Sum up the week for me.");
    await addJob(user, "Loose check", "Fresh check");
    await user.click(screen.getByRole("button", { name: "move Fresh check up" }));
    await user.click(screen.getByRole("button", { name: "create summary" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ kind: "summary", slug: "weekly", members: ["test:fresh", "loose"] });
    await waitFor(() => expect(location.hash).toBe("#/jobs/weekly"));
  });
});
