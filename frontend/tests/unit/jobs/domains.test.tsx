// The "domains" field of the job form: the MCP domains a job's runs get. What the select offers, the chips, the count of
// tools the job can use, and "classify", which asks the worker model to pick the domains for the prompt.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AutomationProvider } from "../../../src/automation";
import { JobsPage } from "../../../src/pages/Jobs";
import { makeJob } from "../../mocks/fixtures/dashboard";
import { jobsList, mcpTools, type McpToolsAnswer } from "../../mocks/fixtures/jobs";
import { server } from "../../mocks/server";
import { installDialogBehaviour } from "../kit/part1-support";
import { installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();
let dialogs: ReturnType<typeof installDialogBehaviour>;
beforeEach(() => {
  dialogs = installDialogBehaviour();
  server.use(http.get("/api/jobs", () => HttpResponse.json(jobsList())));
  serveTools(mcpTools());
});
afterEach(() => {
  dialogs.remove();
  location.hash = "";
});

const serveTools = (page: McpToolsAnswer) => server.use(http.get("/api/mcp/tools", () => HttpResponse.json(page)));

async function openForm(params: string[], title: string) {
  render(
    <AutomationProvider>
      <JobsPage params={params} />
    </AutomationProvider>,
  );
  await screen.findByRole("heading", { level: 1, name: title });
  // the tools arrive after the form
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Add domain" })).toBeInTheDocument());
}

const select = () => screen.getByRole("combobox", { name: "Add domain" });
const options = () => within(select()).getAllByRole("option").map((o) => o.textContent);
const chips = () => screen.queryAllByRole("button", { name: /^Remove / }).map((b) => b.getAttribute("aria-label")!.replace(/^Remove /, ""));
const classifyButton = () => screen.getByRole("button", { name: /^classif/ });
const prompt = () => screen.getByRole("textbox", { name: /^prompt/ });
const domainsGroup = () => screen.getByRole("group", { name: /^domains/ });

/** Records what classify sends and answers with `domains`. */
function serveClassify(answer: () => Response | Promise<Response>) {
  const bodies: unknown[] = [];
  server.use(
    http.post("/api/jobs/classify-domains", async ({ request }) => {
      bodies.push(await request.json());
      return answer();
    }),
  );
  return bodies;
}

describe("the domain select", () => {
  it("offers the domains that have tools, with how many of them are on", async () => {
    await openForm(["new"], "New job");
    expect(options()).toEqual(["Add domain…", "docker (2 of 3 on)", "storage (1 of 1 on)", "off (0 of 1 on)"]);
    expect(select()).toBeEnabled();
    expect(screen.getByText("This job uses only the shell.")).toBeInTheDocument();
    expect(chips()).toEqual([]);
  });

  it("adds a domain as a chip, takes it out of the options and counts the tools the job can use", async () => {
    await openForm(["new"], "New job");
    await userEvent.setup().selectOptions(select(), "docker");
    expect(chips()).toEqual(["docker"]);
    expect(options()).toEqual(["Add domain…", "storage (1 of 1 on)", "off (0 of 1 on)"]);
    expect(select()).toHaveDisplayValue("Add domain…");
    expect(screen.getByText("This job can use 2 tools: 1 read, 1 write.")).toBeInTheDocument();
  });

  it("keeps the chips sorted by name, whatever the order they were added in", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.selectOptions(select(), "storage");
    await user.selectOptions(select(), "docker");
    expect(chips()).toEqual(["docker", "storage"]);
    expect(screen.getByText("This job can use 3 tools: 2 read, 1 write.")).toBeInTheDocument();
  });

  it("counts one tool in the singular, and a domain with no tool on as none", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.selectOptions(select(), "storage");
    expect(screen.getByText("This job can use 1 tool: 1 read, 0 write.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove storage" }));
    await user.selectOptions(select(), "off");
    expect(screen.getByText("This job can use 0 tools: 0 read, 0 write.")).toBeInTheDocument();
  });

  it("removes a chip and offers the domain again", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.selectOptions(select(), "docker");
    await user.selectOptions(select(), "storage");
    await user.click(screen.getByRole("button", { name: "Remove docker" }));
    expect(chips()).toEqual(["storage"]);
    expect(options()).toContain("docker (2 of 3 on)");
    await user.click(screen.getByRole("button", { name: "Remove storage" }));
    expect(chips()).toEqual([]);
    expect(screen.getByText("This job uses only the shell.")).toBeInTheDocument();
  });

  it("says All added and cannot be opened when every domain is chosen", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    for (const name of ["docker", "storage", "off"]) await user.selectOptions(select(), name);
    expect(options()).toEqual(["All added"]);
    expect(select()).toBeDisabled();
  });

  it("says No domains yet when no domain has a tool", async () => {
    const page = mcpTools();
    serveTools({ ...page, domains: page.domains.map((d) => ({ ...d, tools: 0 })), tools: [] });
    await openForm(["new"], "New job");
    expect(options()).toEqual(["No domains yet"]);
    expect(select()).toBeDisabled();
  });

  it("says No domains yet, and does not break, when the tools cannot be loaded", async () => {
    server.use(http.get("/api/mcp/tools", () => HttpResponse.json({ detail: "MCP is down" }, { status: 500 })));
    await openForm(["new"], "New job");
    await waitFor(() => expect(options()).toEqual(["No domains yet"]));
    expect(select()).toBeDisabled();
    expect(classifyButton()).toBeDisabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sends the chosen domains when the job is saved", async () => {
    const bodies: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/jobs", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        bodies.push(body);
        return HttpResponse.json(makeJob({ slug: String(body.slug), name: String(body.name) }), { status: 201 });
      }),
    );
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: /^name/ }), "Docker check");
    await user.type(prompt(), "Look at the containers.");
    await user.selectOptions(select(), "storage");
    await user.selectOptions(select(), "docker");
    await user.click(screen.getByRole("button", { name: "create job" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ domains: ["docker", "storage"] });
  });
});

describe("the domains of a job that is edited", () => {
  it("shows the stored domains as chips and counts their tools", async () => {
    server.use(http.get("/api/jobs/:slug", () => HttpResponse.json(makeJob({ slug: "docked", name: "Docked", domains: ["docker"] }))));
    await openForm(["docked", "edit"], "Edit Docked");
    await waitFor(() => expect(chips()).toEqual(["docker"]));
    expect(within(domainsGroup()).getByText("This job can use 2 tools: 1 read, 1 write.")).toBeInTheDocument();
    expect(options()).not.toContain("docker (2 of 3 on)");
  });

  it("keeps a domain that has no tools while it is chosen, and does not offer it afterwards", async () => {
    server.use(http.get("/api/jobs/:slug", () => HttpResponse.json(makeJob({ slug: "docked", name: "Docked", domains: ["unused"] }))));
    await openForm(["docked", "edit"], "Edit Docked");
    await waitFor(() => expect(chips()).toEqual(["unused"]));
    expect(options()).not.toContain("unused (0 of 0 on)");
    await userEvent.setup().click(screen.getByRole("button", { name: "Remove unused" }));
    expect(options().some((o) => o?.startsWith("unused"))).toBe(false);
  });

  it("keeps a domain that no longer exists as a chip, and counts no tool for it", async () => {
    server.use(http.get("/api/jobs/:slug", () => HttpResponse.json(makeJob({ slug: "docked", name: "Docked", domains: ["legacy"] }))));
    await openForm(["docked", "edit"], "Edit Docked");
    await waitFor(() => expect(chips()).toEqual(["legacy"]));
    expect(screen.getByText("This job can use 0 tools: 0 read, 0 write.")).toBeInTheDocument();
  });
});

describe("classify", () => {
  it("cannot be pressed before the prompt has ten characters", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    expect(classifyButton()).toBeDisabled();
    await user.type(prompt(), "123456789");
    expect(classifyButton()).toBeDisabled();
    await user.type(prompt(), "0");
    expect(classifyButton()).toBeEnabled();
    await user.clear(prompt());
    await user.type(prompt(), "          x          ");
    expect(classifyButton()).toBeDisabled();
  });

  it("cannot be pressed when no domain has a tool switched on, and says why", async () => {
    const page = mcpTools();
    serveTools({ ...page, tools: page.tools.map((t) => ({ ...t, enabled: false })) });
    await openForm(["new"], "New job");
    await userEvent.setup().type(prompt(), "Check the containers on the host.");
    expect(classifyButton()).toBeDisabled();
    expect(classifyButton()).toHaveAttribute("title", "No domain has a tool switched on");
  });

  it("asks the worker model with the prompt and takes its domains", async () => {
    const bodies = serveClassify(() => HttpResponse.json({ domains: ["docker", "storage"] }));
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.type(prompt(), "Check the containers and the disks.");
    expect(classifyButton()).toHaveAttribute("title", "The worker model picks the domains this prompt needs");
    await user.click(classifyButton());
    await waitFor(() => expect(chips()).toEqual(["docker", "storage"]));
    expect(bodies).toEqual([{ prompt: "Check the containers and the disks." }]);
    expect(screen.getByText("This job can use 3 tools: 2 read, 1 write.")).toBeInTheDocument();
    expect(classifyButton()).toHaveTextContent("classify");
    expect(classifyButton()).toBeEnabled();
  });

  it("works with the default answer of the shared handlers", async () => {
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.type(prompt(), "Check the containers and the disks.");
    await user.click(classifyButton());
    await waitFor(() => expect(chips()).toEqual(["docker"]));
  });

  it("replaces the domains that were chosen before", async () => {
    serveClassify(() => HttpResponse.json({ domains: ["storage"] }));
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.selectOptions(select(), "docker");
    await user.type(prompt(), "Check the disks of the host.");
    await user.click(classifyButton());
    await waitFor(() => expect(chips()).toEqual(["storage"]));
  });

  it("says so when the model picks no domain, and clears the chosen ones", async () => {
    serveClassify(() => HttpResponse.json({ domains: [] }));
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.selectOptions(select(), "docker");
    await user.type(prompt(), "Say hello to the people.");
    await user.click(classifyButton());
    expect(await screen.findByText("The model picked no domain for this prompt.")).toBeInTheDocument();
    expect(chips()).toEqual([]);
  });

  it("shows the error of the server and keeps the chosen domains", async () => {
    serveClassify(() => HttpResponse.json({ detail: "The worker model is not reachable" }, { status: 502 }));
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.selectOptions(select(), "docker");
    await user.type(prompt(), "Check the containers on the host.");
    await user.click(classifyButton());
    expect(await screen.findByText(/The worker model is not reachable/)).toBeInTheDocument();
    expect(chips()).toEqual(["docker"]);
    expect(classifyButton()).toBeEnabled();
  });

  it("shows classifying… and cannot be pressed again while the answer is on its way", async () => {
    let release = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    const bodies = serveClassify(async () => {
      await gate;
      return HttpResponse.json({ domains: ["docker"] });
    });
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.type(prompt(), "Check the containers on the host.");
    await user.click(classifyButton());
    expect(await screen.findByRole("button", { name: "classifying…" })).toBeDisabled();
    release();
    await waitFor(() => expect(chips()).toEqual(["docker"]));
    expect(bodies).toHaveLength(1);
    expect(screen.getByRole("button", { name: "classify" })).toBeEnabled();
  });

  it("forgets the note of the last answer when it is asked again", async () => {
    let domains: string[] = [];
    serveClassify(() => HttpResponse.json({ domains }));
    await openForm(["new"], "New job");
    const user = userEvent.setup();
    await user.type(prompt(), "Say hello to the people.");
    await user.click(classifyButton());
    expect(await screen.findByText("The model picked no domain for this prompt.")).toBeInTheDocument();
    domains = ["docker"];
    await user.click(classifyButton());
    await waitFor(() => expect(chips()).toEqual(["docker"]));
    expect(screen.queryByText("The model picked no domain for this prompt.")).not.toBeInTheDocument();
  });
});
