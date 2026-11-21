import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { ActionRecord } from "../../../src/api";
import { AutomationProvider } from "../../../src/automation";
import { ApprovalsPage } from "../../../src/pages/Approvals";
import { action, auditLog, CHAT_ID } from "../../mocks/fixtures/approvals";
import { server } from "../../mocks/server";

function renderPage(params?: string[]) {
  return render(
    <AutomationProvider>
      <ApprovalsPage params={params} />
    </AutomationProvider>,
  );
}

/** Record the query of every audit log request (the default handler still answers). */
function recordLog() {
  const seen: URLSearchParams[] = [];
  server.use(
    http.get("/api/actions", ({ request }) => {
      seen.push(new URL(request.url).searchParams);
      return undefined;
    }),
  );
  return seen;
}

/** The body rows of the audit log table. */
const logRows = () => within(screen.getByRole("table")).getAllByRole("row").slice(1);
/** The row of the audit log with all of these texts in it. */
const logRow = (...texts: string[]) => {
  const row = logRows().find((r) => texts.every((t) => r.textContent?.includes(t)));
  if (!row) throw new Error(`No audit log row with ${texts.join(" and ")}`);
  return row;
};

const answer = (items: ActionRecord[], total = items.length) => http.get("/api/actions", () => HttpResponse.json({ items, total }));

describe("ApprovalsPage: audit log", () => {
  it("lists the actions newest first, 25 at most to begin with", async () => {
    const seen = recordLog();
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows()).toHaveLength(auditLog.length));
    expect(seen[0].get("limit")).toBe("25");
    expect(seen[0].has("status")).toBe(false);
    expect(logRows()[0]).toHaveTextContent("notify");
  });

  it("shows the columns of the table", async () => {
    renderPage();
    await screen.findByRole("table");
    expect(within(screen.getByRole("table")).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["time", "action", "status", "source", "detail"]);
  });

  it("shows what an action did to which target, with its server as a link", async () => {
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows().length).toBeGreaterThan(0));
    const row = within(logRow("approval #46", "executed"));
    expect(row.getByText("mcp_call")).toBeInTheDocument();
    expect(row.getByRole("link", { name: "test-server" })).toHaveAttribute("href", "#/settings/mcp/test-server");
    expect(row.getByText("executed")).toBeInTheDocument();
    expect(row.getByText(/delete_item/)).toBeInTheDocument();
  });

  it("links the source to its chat or job, names who decided, and links the approval", async () => {
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows().length).toBeGreaterThan(0));
    const executed = logRow("approval #46", "executed");
    expect(within(executed).getByRole("link", { name: "chat abc12345" })).toHaveAttribute("href", `#/chat/${CHAT_ID}`);
    expect(executed).toHaveTextContent("· user");
    expect(within(executed).getByRole("link", { name: "approval #46" })).toHaveAttribute("href", "#/approvals/46");
    const restart = within(logRow("approval #42"));
    expect(restart.getByRole("link", { name: "example-job" })).toHaveAttribute("href", "#/jobs/example-job");
    expect(restart.getByRole("link", { name: "approval #42" })).toHaveAttribute("href", "#/approvals/42");
  });

  it("shows a source that is no entity as text, and no approval link for an action without one", async () => {
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows().length).toBeGreaterThan(0));
    const notify = logRows()[0];
    expect(notify).toHaveTextContent("scheduler");
    expect(within(notify).queryByRole("link")).not.toBeInTheDocument();
  });

  it("says in the detail why an action did not happen or what it did", async () => {
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows().length).toBeGreaterThan(0));
    expect(logRow("approval #46", "failed")).toHaveTextContent("tool timed out");
    expect(logRow("approval #46", "rejected")).toHaveTextContent("Denied by the owner");
    expect(logRow("approval #44", "queued")).toHaveTextContent("approved; waiting for the hourly limit, it runs by itself");
    expect(logRow("telegram", "dry run")).toHaveTextContent("dry run: send a message");
    expect(logRow("approval #46", "executed")).toHaveTextContent("done");
  });

  it("says a failure without a reason is a failure, and a dry run without a note sent nothing", async () => {
    server.use(
      answer([
        action({ id: 301, status: "failed", target: "item one", args: null, result: {} }),
        action({ id: 302, status: "dry_run", target: "item two", args: null, result: {} }),
        action({ id: 303, status: "rejected", target: "item three", args: null, result: {} }),
      ]),
    );
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows()).toHaveLength(3));
    expect(logRow("item one")).toHaveTextContent("failed");
    expect(logRow("item two")).toHaveTextContent("dry run: nothing was sent");
    expect(logRow("item three")).toHaveTextContent("rejected");
  });

  it("shows the exit code of a shell command with the reason", async () => {
    server.use(
      answer([
        action({
          id: 310,
          action: "host_command",
          target: "host",
          args: { command: "true" },
          status: "executed",
          reason: "tidy up",
          result: { exit_code: 3 },
        }),
      ]),
    );
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows()).toHaveLength(1));
    expect(logRows()[0]).toHaveTextContent("exit code 3 · tidy up");
  });

  it("shows the empty state of an empty log", async () => {
    server.use(answer([]));
    renderPage();
    expect(await screen.findByText("No actions recorded.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

// FINDING (it.fails below): the audit log asks for its rows again only when `usePoll` is reloaded (an event, the
// 30 s timer with the query of the last reload). Changing a filter, "show more" or "show in the audit log" changes the
// query but `usePoll`'s effect depends on [tick, intervalMs] only, so no request goes out and the table keeps the
// old rows. These tests state the expected behaviour (a request with the new query); they pass with a usePoll that
// loads again when the query changes. Turn `it.fails` into `it` when that is fixed.
describe("ApprovalsPage: audit log filters", () => {
  it.fails("filters by status: asks for it and shows the rows the server sends", async () => {
    const user = userEvent.setup();
    const seen = recordLog();
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows()).toHaveLength(auditLog.length));
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "failed");
    await waitFor(() => expect(seen.at(-1)?.get("status")).toBe("failed"));
    await waitFor(() => expect(logRows()).toHaveLength(1));
    expect(logRows()[0]).toHaveTextContent("tool timed out");
  });

  it("offers every status, with the underscore of dry_run left out", async () => {
    renderPage();
    await screen.findByRole("table");
    const select = screen.getByRole("combobox", { name: "Status" });
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "all statuses",
      "pending",
      "queued",
      "suggested",
      "executed",
      "failed",
      "rejected",
      "expired",
      "dry run",
    ]);
    expect(within(select).getByRole("option", { name: "dry run" })).toHaveValue("dry_run");
  });

  it.fails("offers the actions found in the log, each once and in order, and filters by one", async () => {
    const user = userEvent.setup();
    const seen = recordLog();
    renderPage();
    await screen.findByRole("table");
    const select = screen.getByRole("combobox", { name: "Action" });
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(4));
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["all actions", "host_command", "mcp_call", "notify"]);
    await user.selectOptions(select, "notify");
    await waitFor(() => expect(seen.at(-1)?.get("action")).toBe("notify"));
    await waitFor(() => expect(logRows()).toHaveLength(1));
    expect(logRows()[0]).toHaveTextContent("telegram");
  });

  it.fails("combines the two filters, and 'all' removes one again", async () => {
    const user = userEvent.setup();
    const seen = recordLog();
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows()).toHaveLength(auditLog.length));
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "expired");
    await waitFor(() => expect(seen.at(-1)?.get("status")).toBe("expired"));
    await waitFor(() => expect(within(screen.getByRole("combobox", { name: "Action" })).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(screen.getByRole("combobox", { name: "Action" }), "mcp_call");
    await waitFor(() => expect(seen.at(-1)?.get("action")).toBe("mcp_call"));
    expect(seen.at(-1)?.get("status")).toBe("expired");
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "all statuses");
    await waitFor(() => expect(seen.at(-1)?.has("status")).toBe(false));
    expect(seen.at(-1)?.get("action")).toBe("mcp_call");
  });

  it.fails("says the log is empty for this filter, and the filter can be changed back", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("table");
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "suggested");
    expect(await screen.findByText("No actions recorded for this filter.")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "all statuses");
    await waitFor(() => expect(logRows()).toHaveLength(auditLog.length));
  });
});

describe("ApprovalsPage: older actions", () => {
  const many = Array.from({ length: 60 }, (_, n) => action({ id: 1000 - n, status: "executed", target: `item ${1000 - n}` }));
  /** Answer like the server: `limit` rows of the 60. */
  const paged = http.get("/api/actions", ({ request }) => {
    const limit = Number(new URL(request.url).searchParams.get("limit"));
    return HttpResponse.json({ items: many.slice(0, limit), total: many.length });
  });

  it.fails("offers the older actions when the log holds more than it shows, 25 more each time", async () => {
    const user = userEvent.setup();
    server.use(paged);
    const seen = recordLog();
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows()).toHaveLength(25));
    await user.click(screen.getByRole("button", { name: "show more (35 older)" }));
    await waitFor(() => expect(logRows()).toHaveLength(50));
    expect(seen.map((q) => q.get("limit"))).toContain("50");
    await user.click(screen.getByRole("button", { name: "show more (10 older)" }));
    await waitFor(() => expect(logRows()).toHaveLength(60));
    expect(screen.queryByRole("button", { name: /show more/ })).not.toBeInTheDocument();
  });

  it("offers nothing more when everything is shown", async () => {
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows()).toHaveLength(auditLog.length));
    expect(screen.queryByRole("button", { name: /show more/ })).not.toBeInTheDocument();
  });

  it.fails("starts again at 25 when a filter changes", async () => {
    const user = userEvent.setup();
    server.use(paged);
    const seen = recordLog();
    renderPage();
    await screen.findByRole("table");
    await waitFor(() => expect(logRows()).toHaveLength(25));
    await user.click(screen.getByRole("button", { name: "show more (35 older)" }));
    await waitFor(() => expect(logRows()).toHaveLength(50));
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "executed");
    await waitFor(() => expect(seen.at(-1)?.get("status")).toBe("executed"));
    expect(seen.at(-1)?.get("limit")).toBe("25");
  });
});

describe("ApprovalsPage: show in the audit log", () => {
  it.fails("narrows the log to the approval, shows up to 100 of its actions, and clears again", async () => {
    const user = userEvent.setup();
    const seen = recordLog();
    renderPage();
    const multi = await screen.findByRole("article", { name: "Delete three items" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "executed");
    await user.click(within(multi).getByRole("button", { name: "show in the audit log" }));
    await waitFor(() => expect(seen.at(-1)?.get("batch")).toBe("41"));
    expect(seen.at(-1)?.get("limit")).toBe("100");
    // the other filters are cleared so the approval's actions all show
    expect(seen.at(-1)?.has("status")).toBe(false);
    expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("");
    await waitFor(() => expect(logRows()).toHaveLength(3));
    for (const row of logRows()) expect(within(row).getByRole("link", { name: "approval #41" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "approval #41 · clear" }));
    await waitFor(() => expect(seen.at(-1)?.has("batch")).toBe(false));
    await waitFor(() => expect(logRows()).toHaveLength(auditLog.length));
    expect(screen.queryByRole("button", { name: /· clear$/ })).not.toBeInTheDocument();
  });

  it.fails("says the log is empty for this approval", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/actions", ({ request }) => {
        const batch = new URL(request.url).searchParams.get("batch");
        return HttpResponse.json(batch ? { items: [], total: 0 } : { items: auditLog, total: auditLog.length });
      }),
    );
    renderPage();
    const single = await screen.findByRole("article", { name: "Restart the example container" });
    await user.click(within(single).getByRole("button", { name: "show in the audit log" }));
    expect(await screen.findByText("No actions recorded for this filter.")).toBeInTheDocument();
  });
});
