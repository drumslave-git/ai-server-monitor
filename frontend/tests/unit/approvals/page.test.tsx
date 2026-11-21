import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { ApprovalBatch } from "../../../src/api";
import { AutomationProvider } from "../../../src/automation";
import { ApprovalsPage } from "../../../src/pages/Approvals";
import { action, batch, CHAT_ID, decidedBatch, expiredBatch, multiBatch, pendingBatches, queuedBatch, singleBatch } from "../../mocks/fixtures/approvals";
import { server } from "../../mocks/server";

function renderPage(params?: string[]) {
  return render(
    <AutomationProvider>
      <ApprovalsPage params={params} />
    </AutomationProvider>,
  );
}

/** The card of one approval, by its title. */
const card = (title: string) => within(screen.getByRole("article", { name: title }));

type Decision = { id: number; body: unknown };

/** Record every decision the page sends; answer with the approval as it was. */
function recordDecisions(answer: (id: number) => Response = () => HttpResponse.json(multiBatch)) {
  const seen: Decision[] = [];
  server.use(
    http.post("/api/approvals/:id", async ({ request, params }) => {
      seen.push({ id: Number(params.id), body: await request.json() });
      return answer(Number(params.id));
    }),
  );
  return seen;
}

/** The paths of every GET the page makes to `path` (with the query). */
function recordGets(path: string) {
  const seen: string[] = [];
  server.use(
    http.get(path, ({ request }) => {
      const url = new URL(request.url);
      seen.push(url.pathname + url.search);
      return undefined;
    }),
  );
  return seen;
}

describe("ApprovalsPage: waiting approvals", () => {
  it("shows one card per waiting approval and how many approvals and items wait", async () => {
    renderPage();
    for (const b of pendingBatches) expect(await screen.findByRole("article", { name: b.title })).toBeInTheDocument();
    expect(screen.getByText("4 approvals · 5 items")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Delete three items" })).toBeInTheDocument();
  });

  it("says a counter of one approval in the singular", async () => {
    server.use(http.get("/api/approvals", () => HttpResponse.json({ batches: [singleBatch], items: [] })));
    renderPage();
    await screen.findByRole("article", { name: singleBatch.title });
    expect(screen.getByText("1 approval · 1 item")).toBeInTheDocument();
  });

  it("shows when an approval was requested and when it expires", async () => {
    renderPage();
    expect(await screen.findByRole("article", { name: multiBatch.title })).toHaveTextContent("#41 · from chat abc12345 · requested 10m ago · expires in 2h");
    expect(screen.getByRole("article", { name: singleBatch.title })).toHaveTextContent("expires in 45m");
  });

  it("links the source of an approval to its chat or job", async () => {
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    expect(card(multiBatch.title).getByRole("link", { name: "chat abc12345" })).toHaveAttribute("href", `#/chat/${CHAT_ID}`);
    expect(card(singleBatch.title).getByRole("link", { name: "example-job" })).toHaveAttribute("href", "#/jobs/example-job");
  });

  it("names the server of each call as a link to its card in Settings", async () => {
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    const links = card(multiBatch.title).getAllByRole("link", { name: "test-server" });
    expect(links).toHaveLength(3);
    expect(links[0]).toHaveAttribute("href", "#/settings/mcp/test-server");
  });

  it("shows the arguments and the reason of a waiting call", async () => {
    renderPage();
    await screen.findByRole("article", { name: singleBatch.title });
    const single = card(singleBatch.title);
    expect(single.getByText("name: host-a")).toBeInTheDocument();
    expect(single.getByText("It stopped answering")).toBeInTheDocument();
    expect(card(multiBatch.title).getByText("service: example · id: a101")).toBeInTheDocument();
  });

  it("shows a changing shell command as the command and why", async () => {
    renderPage();
    await screen.findByRole("article", { name: "Free disk space" });
    const command = card("Free disk space");
    expect(command.getByText("journalctl --vacuum-size=200M")).toBeInTheDocument();
    expect(command.getByText("The journal is large")).toBeInTheDocument();
  });

  it("shows an approval whose items were approved and wait for the hourly limit with a way to cancel, and no approve buttons", async () => {
    renderPage();
    await screen.findByRole("article", { name: queuedBatch.title });
    const queued = card(queuedBatch.title);
    expect(queued.getByText("2 queued")).toBeInTheDocument();
    expect(queued.getByText("the next one runs in 12m")).toBeInTheDocument();
    expect(queued.getByRole("button", { name: "cancel the 2 queued" })).toBeEnabled();
    expect(queued.queryByRole("button", { name: /^approve/ })).not.toBeInTheDocument();
    expect(queued.queryByRole("button", { name: /^reject/ })).not.toBeInTheDocument();
    // nothing waits there, so no expiry is announced
    expect(screen.getByRole("article", { name: queuedBatch.title })).not.toHaveTextContent("expires");
  });

  it("says the queued items wait for the resume while automation is paused", async () => {
    server.use(http.get("/api/automation", () => HttpResponse.json({ paused: true, since: 1_800_000_000, by: "ui", queued_actions: 2 })));
    renderPage();
    await screen.findByRole("article", { name: queuedBatch.title });
    await waitFor(() => expect(card(queuedBatch.title).getByText("approved; waiting because automation is paused, it runs after the resume")).toBeInTheDocument());
    expect(card(queuedBatch.title).queryByText("the next one runs in 12m")).not.toBeInTheDocument();
  });

  it("offers 'approve for the whole run' only for an approval that can cover later requests", async () => {
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    expect(card(multiBatch.title).getByRole("button", { name: "approve for the whole run" })).toBeEnabled();
    expect(card(singleBatch.title).queryByRole("button", { name: "approve for the whole run" })).not.toBeInTheDocument();
  });

  it("counts the waiting items in the names of the buttons of a batch, and not for a single item", async () => {
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    expect(card(multiBatch.title).getByRole("button", { name: "approve all (3)" })).toBeInTheDocument();
    expect(card(multiBatch.title).getByRole("button", { name: "reject all (3)" })).toBeInTheDocument();
    expect(card(singleBatch.title).getByRole("button", { name: "approve all" })).toBeInTheDocument();
    expect(card(singleBatch.title).getByRole("button", { name: "reject all" })).toBeInTheDocument();
  });

  it("shows the empty state when nothing waits", async () => {
    server.use(http.get("/api/approvals", () => HttpResponse.json({ batches: [], items: [] })));
    renderPage();
    expect(await screen.findByText(/Nothing is waiting\./)).toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.getByText("0")).toBeInTheDocument();
  });

  it("shows a dash instead of a count until the list has loaded, and does not claim that nothing waits when it cannot be loaded", async () => {
    server.use(http.get("/api/approvals", () => HttpResponse.json({ detail: "database is locked" }, { status: 500 })));
    renderPage();
    expect(screen.getByText("—")).toBeInTheDocument();
    // the audit log of the same page does load: wait for it, so the failed answer has been handled
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing is waiting\./)).not.toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("shows the message of the failed answer, where the waiting approvals would be", async () => {
    server.use(http.get("/api/approvals", () => HttpResponse.json({ detail: "database is locked" }, { status: 500 })));
    renderPage();
    expect(await screen.findByText("database is locked")).toBeInTheDocument();
  });
});

describe("ApprovalsPage: deciding", () => {
  it("approve all sends the approval and refreshes the list", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions();
    const gets = recordGets("/api/approvals");
    renderPage();
    await user.click(await within(await screen.findByRole("article", { name: multiBatch.title })).findByRole("button", { name: "approve all (3)" }));
    await waitFor(() => expect(decisions).toEqual([{ id: 41, body: { decision: "approve" } }]));
    await waitFor(() => expect(gets.length).toBeGreaterThan(1));
  });

  it("reject all sends a rejection", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions();
    renderPage();
    await user.click(await within(await screen.findByRole("article", { name: multiBatch.title })).findByRole("button", { name: "reject all (3)" }));
    await waitFor(() => expect(decisions).toEqual([{ id: 41, body: { decision: "reject" } }]));
  });

  it("approve for the whole run sends the rest flag", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions();
    renderPage();
    await user.click(await within(await screen.findByRole("article", { name: multiBatch.title })).findByRole("button", { name: "approve for the whole run" }));
    await waitFor(() => expect(decisions).toEqual([{ id: 41, body: { decision: "approve", rest: true } }]));
  });

  it("decides the approval whose button was pressed", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions((id) => HttpResponse.json(pendingBatches.find((b) => b.id === id)));
    renderPage();
    await screen.findByRole("article", { name: singleBatch.title });
    await user.click(card(singleBatch.title).getByRole("button", { name: "reject all" }));
    await waitFor(() => expect(decisions).toEqual([{ id: 42, body: { decision: "reject" } }]));
  });

  it("cancelling queued items sends a rejection of the approval", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions((id) => HttpResponse.json(pendingBatches.find((b) => b.id === id)));
    renderPage();
    await screen.findByRole("article", { name: queuedBatch.title });
    await user.click(card(queuedBatch.title).getByRole("button", { name: "cancel the 2 queued" }));
    await waitFor(() => expect(decisions).toEqual([{ id: 44, body: { decision: "reject" } }]));
  });

  it("removes a decided approval from the list once the server no longer lists it", async () => {
    const user = userEvent.setup();
    let decided = false;
    server.use(
      http.get("/api/approvals", () => HttpResponse.json({ batches: pendingBatches.filter((b) => !(decided && b.id === 41)), items: [] })),
      http.post("/api/approvals/:id", () => {
        decided = true;
        return HttpResponse.json(multiBatch);
      }),
    );
    renderPage();
    await user.click(await within(await screen.findByRole("article", { name: multiBatch.title })).findByRole("button", { name: "approve all (3)" }));
    await waitFor(() => expect(screen.queryByRole("article", { name: multiBatch.title })).not.toBeInTheDocument());
    expect(screen.getByRole("article", { name: singleBatch.title })).toBeInTheDocument();
    expect(screen.getByText("3 approvals · 2 items")).toBeInTheDocument();
  });

  it("disables the buttons of the approval while its decision is on its way", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    server.use(
      http.post("/api/approvals/:id", async () => {
        await gate;
        return HttpResponse.json(multiBatch);
      }),
    );
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    await user.click(card(multiBatch.title).getByRole("button", { name: "approve all (3)" }));
    await waitFor(() => expect(card(multiBatch.title).getByRole("button", { name: "approve all (3)" })).toBeDisabled());
    expect(card(multiBatch.title).getByRole("button", { name: "reject all (3)" })).toBeDisabled();
    expect(card(multiBatch.title).getByRole("button", { name: "approve for the whole run" })).toBeDisabled();
    // the other approval is not busy
    expect(card(singleBatch.title).getByRole("button", { name: "approve all" })).toBeEnabled();
    release();
    await waitFor(() => expect(card(multiBatch.title).getByRole("button", { name: "approve all (3)" })).toBeEnabled());
  });

  it("shows the message of the server when the decision is refused, and lets the owner try again", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions(() => HttpResponse.json({ detail: "This approval was already decided." }, { status: 409 }));
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    await user.click(card(multiBatch.title).getByRole("button", { name: "approve all (3)" }));
    expect(await screen.findByText("This approval was already decided.")).toBeInTheDocument();
    await waitFor(() => expect(card(multiBatch.title).getByRole("button", { name: "approve all (3)" })).toBeEnabled());
    await user.click(card(multiBatch.title).getByRole("button", { name: "reject all (3)" }));
    await waitFor(() => expect(decisions).toHaveLength(2));
    // the second refusal shows its message too
    expect(await screen.findByText("This approval was already decided.")).toBeInTheDocument();
  });

  it("shows the message of a server error", async () => {
    const user = userEvent.setup();
    recordDecisions(() => HttpResponse.json({ detail: "The server could not run the tool." }, { status: 500 }));
    renderPage();
    await screen.findByRole("article", { name: singleBatch.title });
    await user.click(card(singleBatch.title).getByRole("button", { name: "approve all" }));
    expect(await screen.findByText("The server could not run the tool.")).toBeInTheDocument();
  });

  it("reads a validation error (a list of entries) as 'field: message', not as [object Object]", async () => {
    const user = userEvent.setup();
    recordDecisions(() => HttpResponse.json({ detail: [{ loc: ["body", "items", 0], msg: "Input should be a valid integer", type: "int_parsing" }] }, { status: 422 }));
    renderPage();
    await screen.findByRole("article", { name: singleBatch.title });
    await user.click(card(singleBatch.title).getByRole("button", { name: "approve all" }));
    expect(await screen.findByText("items.0: Input should be a valid integer")).toBeInTheDocument();
    expect(screen.queryByText(/object Object/)).not.toBeInTheDocument();
  });

  it("clears the message of an earlier failure when the next decision goes through", async () => {
    const user = userEvent.setup();
    let fail = true;
    server.use(
      http.post("/api/approvals/:id", () => (fail ? HttpResponse.json({ detail: "Try later." }, { status: 503 }) : HttpResponse.json(multiBatch))),
    );
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    await user.click(card(multiBatch.title).getByRole("button", { name: "approve all (3)" }));
    expect(await screen.findByText("Try later.")).toBeInTheDocument();
    fail = false;
    await user.click(card(multiBatch.title).getByRole("button", { name: "approve all (3)" }));
    await waitFor(() => expect(screen.queryByText("Try later.")).not.toBeInTheDocument());
  });
});

describe("ApprovalsPage: choosing items of an approval", () => {
  it("offers a choice only where more than one item waits", async () => {
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    expect(card(multiBatch.title).getAllByRole("checkbox")).toHaveLength(4); // three items and select all
    expect(card(singleBatch.title).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(card(singleBatch.title).queryByRole("button", { name: "approve selected" })).not.toBeInTheDocument();
  });

  it("keeps approve selected and reject selected disabled until an item is ticked", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    const multi = card(multiBatch.title);
    expect(multi.getByRole("button", { name: "approve selected" })).toBeDisabled();
    expect(multi.getByRole("button", { name: "reject selected" })).toBeDisabled();
    expect(multi.getByRole("checkbox", { name: "select all 3" })).not.toBeChecked();
    await user.click(multi.getByRole("checkbox", { name: "Select item alpha" }));
    expect(multi.getByRole("button", { name: "approve selected" })).toBeEnabled();
    expect(multi.getByRole("button", { name: "reject selected" })).toBeEnabled();
    expect(multi.getByRole("checkbox", { name: "1 of 3 selected" })).toBePartiallyChecked();
  });

  it("approve selected sends only the ticked items, then clears the ticks", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions();
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    const multi = card(multiBatch.title);
    await user.click(multi.getByRole("checkbox", { name: "Select item alpha" }));
    await user.click(multi.getByRole("checkbox", { name: "Select item gamma" }));
    expect(multi.getByRole("checkbox", { name: "2 of 3 selected" })).toBePartiallyChecked();
    await user.click(multi.getByRole("button", { name: "approve selected" }));
    await waitFor(() => expect(decisions).toEqual([{ id: 41, body: { decision: "approve", items: [101, 103] } }]));
    await waitFor(() => expect(multi.getByRole("checkbox", { name: "select all 3" })).not.toBeChecked());
    expect(multi.getByRole("checkbox", { name: "Select item alpha" })).not.toBeChecked();
  });

  it("reject selected sends a rejection of the ticked items", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions();
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    await user.click(card(multiBatch.title).getByRole("checkbox", { name: "Select item beta" }));
    await user.click(card(multiBatch.title).getByRole("button", { name: "reject selected" }));
    await waitFor(() => expect(decisions).toEqual([{ id: 41, body: { decision: "reject", items: [102] } }]));
  });

  it("select all ticks every waiting item, and ticking it again clears them", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions();
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    const multi = card(multiBatch.title);
    await user.click(multi.getByRole("checkbox", { name: "select all 3" }));
    expect(multi.getByRole("checkbox", { name: "3 of 3 selected" })).toBeChecked();
    for (const name of ["Select item alpha", "Select item beta", "Select item gamma"]) expect(multi.getByRole("checkbox", { name })).toBeChecked();
    await user.click(multi.getByRole("checkbox", { name: "3 of 3 selected" }));
    expect(multi.getByRole("checkbox", { name: "select all 3" })).not.toBeChecked();
    await user.click(multi.getByRole("checkbox", { name: "select all 3" }));
    await user.click(multi.getByRole("button", { name: "approve selected" }));
    await waitFor(() => expect(decisions).toEqual([{ id: 41, body: { decision: "approve", items: [101, 102, 103] } }]));
  });
});

describe("ApprovalsPage: long approvals", () => {
  const long: ApprovalBatch = batch({
    id: 50,
    title: "Delete twelve items",
    items: Array.from({ length: 12 }, (_, n) => action({ id: 200 + n, target: `item ${n + 1}` })),
  });

  it("shows the first ten items and opens the rest on demand; select all covers the hidden ones too", async () => {
    const user = userEvent.setup();
    const decisions = recordDecisions(() => HttpResponse.json(long));
    server.use(http.get("/api/approvals", () => HttpResponse.json({ batches: [long], items: [] })));
    renderPage();
    await screen.findByRole("article", { name: long.title });
    const it_ = card(long.title);
    expect(it_.getAllByRole("checkbox", { name: /^Select item / })).toHaveLength(10);
    expect(it_.queryByRole("checkbox", { name: "Select item 11" })).not.toBeInTheDocument();

    await user.click(it_.getByRole("checkbox", { name: "select all 12" }));
    expect(it_.getByRole("checkbox", { name: "12 of 12 selected" })).toBeChecked();

    await user.click(it_.getByRole("button", { name: "show 2 more" }));
    expect(it_.getByRole("checkbox", { name: "Select item 11" })).toBeChecked();
    expect(it_.getByRole("button", { name: "show only the first 10" })).toHaveAttribute("aria-expanded", "true");

    await user.click(it_.getByRole("button", { name: "approve selected" }));
    await waitFor(() => expect(decisions).toHaveLength(1));
    expect(decisions[0]).toEqual({ id: 50, body: { decision: "approve", items: Array.from({ length: 12 }, (_, n) => 200 + n) } });
  });

  it("folds the list back", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/approvals", () => HttpResponse.json({ batches: [long], items: [] })));
    renderPage();
    await screen.findByRole("article", { name: long.title });
    const it_ = card(long.title);
    await user.click(it_.getByRole("button", { name: "show 2 more" }));
    expect(it_.getAllByRole("checkbox", { name: /^Select item / })).toHaveLength(12);
    await user.click(it_.getByRole("button", { name: "show only the first 10" }));
    expect(it_.getAllByRole("checkbox", { name: /^Select item / })).toHaveLength(10);
  });
});

describe("ApprovalsPage: opening one approval by its id", () => {
  it("asks for that approval and marks its card as the current one", async () => {
    const gets = recordGets("/api/approvals/:id");
    renderPage(["41"]);
    const multi = await screen.findByRole("article", { name: multiBatch.title });
    await waitFor(() => expect(multi).toHaveAttribute("aria-current", "true"));
    expect(screen.getByRole("article", { name: singleBatch.title })).not.toHaveAttribute("aria-current");
    expect(gets).toContain("/api/approvals/41");
  });

  it("does not show a waiting approval twice", async () => {
    renderPage(["41"]);
    await screen.findByRole("article", { name: multiBatch.title });
    await waitFor(() => expect(screen.getByRole("article", { name: multiBatch.title })).toHaveAttribute("aria-current", "true"));
    expect(screen.getAllByRole("article", { name: multiBatch.title })).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "Approval #41" })).not.toBeInTheDocument();
  });

  it("shows an expired approval on top with no buttons to decide it, and a link that closes it", async () => {
    renderPage(["45"]);
    const heading = await screen.findByRole("heading", { name: "Approval #45" });
    const panel = within(heading.closest("section")!);
    const opened = panel.getByRole("article", { name: expiredBatch.title });
    expect(within(opened).getByText("1 expired")).toBeInTheDocument();
    expect(opened).not.toHaveTextContent("expires");
    expect(within(opened).queryByRole("button", { name: /approve|reject|cancel/ })).not.toBeInTheDocument();
    expect(within(opened).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(panel.getByRole("link", { name: "close" })).toHaveAttribute("href", "#/approvals");
    // the waiting approvals are still below
    expect(await screen.findByRole("article", { name: multiBatch.title })).toBeInTheDocument();
  });

  it("shows a decided approval with the outcome of every item, and says what it covers", async () => {
    renderPage(["46"]);
    const heading = await screen.findByRole("heading", { name: "Approval #46" });
    const opened = within(within(heading.closest("section")!).getByRole("article", { name: decidedBatch.title }));
    expect(opened.getByText("1 executed")).toBeInTheDocument();
    expect(opened.getByText("1 rejected")).toBeInTheDocument();
    expect(opened.getByText("1 failed")).toBeInTheDocument();
    expect(opened.getByText("tool timed out")).toBeInTheDocument();
    expect(opened.getByText("Denied by the owner")).toBeInTheDocument();
    expect(opened.getByText(/Approved for the whole run: later requests of test-server · delete_item run without asking/)).toBeInTheDocument();
    expect(opened.queryByRole("button", { name: /^(approve|reject)/ })).not.toBeInTheDocument();
    expect(opened.getByRole("button", { name: "show in the audit log" })).toBeInTheDocument();
  });

  it("does not ask for an approval when the address carries no number", async () => {
    const gets = recordGets("/api/approvals/:id");
    renderPage(["abc"]);
    await screen.findByRole("article", { name: multiBatch.title });
    await screen.findByRole("table");
    expect(gets).toEqual([]);
    expect(screen.queryByRole("heading", { name: /^Approval #/ })).not.toBeInTheDocument();
  });

  it("shows only the waiting approvals when the one asked for does not exist", async () => {
    renderPage(["999"]);
    await screen.findByRole("article", { name: multiBatch.title });
    await screen.findByRole("table");
    expect(screen.queryByRole("heading", { name: /^Approval #/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(pendingBatches.length);
  });

  it("loads another approval when a link changes the address while the page is open", async () => {
    const { rerender } = renderPage(["45"]);
    await screen.findByRole("heading", { name: "Approval #45" });
    rerender(
      <AutomationProvider>
        <ApprovalsPage params={["46"]} />
      </AutomationProvider>,
    );
    expect(await screen.findByRole("heading", { name: "Approval #46" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Approval #45" })).not.toBeInTheDocument();
  });
});

describe("ApprovalsPage: change shell notice", () => {
  const shellStatus = (shell: { policy: "off" | "approve" | "auto"; available: boolean; reason: string | null }) =>
    http.get("/api/status", () =>
      HttpResponse.json({
        automation: { paused: false },
        jobs: [],
        llm_configured: true,
        main_model: "test-main",
        worker_model: "test-worker",
        now: 1_800_000_000,
        pending_approvals: 0,
        uptime_s: 7200,
        version: "9.9.9",
        change_shell: { ...shell, offered: true },
      }),
    );

  it("tells the owner when shell commands that change things cannot be reached, with the reason and where to turn them off", async () => {
    server.use(shellStatus({ policy: "approve", available: false, reason: "sudo is not installed" }));
    renderPage();
    expect(await screen.findByText("Changing shell commands are not available.")).toBeInTheDocument();
    expect(screen.getByText("sudo is not installed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings → Approvals" })).toHaveAttribute("href", "#/settings/approvals");
  });

  it("gives a default reason when the host did not say why", async () => {
    server.use(shellStatus({ policy: "auto", available: false, reason: null }));
    renderPage();
    expect(await screen.findByText("The host shell could not be reached.")).toBeInTheDocument();
  });

  it.each([
    ["they are turned off", { policy: "off" as const, available: false, reason: "no" }],
    ["they are available", { policy: "approve" as const, available: true, reason: null }],
  ])("says nothing when %s", async (_name, shell) => {
    server.use(shellStatus(shell));
    renderPage();
    await screen.findByRole("article", { name: multiBatch.title });
    // let the status answer arrive
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("Changing shell commands are not available.")).not.toBeInTheDocument();
  });
});
