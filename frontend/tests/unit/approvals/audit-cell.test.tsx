// The "action" cell of the audit log names a call as "server · tool": the server is a link (cut with an ellipsis
// and a tooltip when it is wider than its room), the tool text stays whole. The cut itself needs layout and is
// checked in Chromium (tests/e2e/layout.spec.ts); here: what the cell is made of for a very long server name.
import { render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { AutomationProvider } from "../../../src/automation";
import { ItemTarget } from "../../../src/components/EntityRefs";
import { ApprovalsPage } from "../../../src/pages/Approvals";
import { action } from "../../mocks/fixtures/approvals";
import { server } from "../../mocks/server";

const LONG_SERVER = "example-infrastructure-monitoring-and-inventory-gateway-server-number-one";

describe("the action cell of the audit log with a very long server name", () => {
  it("ItemTarget: the server is one link with its whole name, the tool follows as plain text", () => {
    const item = action({ id: 1, args: { server: LONG_SERVER, tool: "list_everything", arguments: {} } });
    const { container } = render(<ItemTarget item={item} />);
    const link = screen.getByRole("link", { name: LONG_SERVER });
    expect(link).toHaveAttribute("href", `#/settings/mcp/${LONG_SERVER}`);
    // the tool text is outside the link, so the cut of the link never takes a letter of it
    expect(link).not.toHaveTextContent("list_everything");
    expect(container).toHaveTextContent(`${LONG_SERVER} · list_everything`);
    const tail = container.querySelector(".fd-entity-tail-text");
    expect(tail).toHaveTextContent("· list_everything");
    expect(tail?.contains(link)).toBe(false);
  });

  it("the audit log table shows that cell in the action column", async () => {
    server.use(
      http.get("/api/actions", () =>
        HttpResponse.json({
          items: [action({ id: 7, status: "executed", args: { server: LONG_SERVER, tool: "list_everything", arguments: {} } })],
          total: 1,
        }),
      ),
    );
    render(
      <AutomationProvider>
        <ApprovalsPage />
      </AutomationProvider>,
    );
    const table = await screen.findByRole("table");
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(2));
    const row = within(table).getAllByRole("row")[1];
    expect(within(row).getByRole("link", { name: LONG_SERVER })).toBeInTheDocument();
    expect(row).toHaveTextContent("· list_everything");
  });

  it("the detail of an action stays on one line: its cell is the cut one, the whole text is in it for the tooltip", async () => {
    const reason = "This is a very long reason that the audit log must cut with an ellipsis instead of wrapping over several lines of the table";
    server.use(
      http.get("/api/actions", () =>
        HttpResponse.json({
          items: [action({ id: 8, status: "rejected", result: { detail: reason }, args: { server: "example", tool: "list_things", arguments: {} } })],
          total: 1,
        }),
      ),
    );
    render(
      <AutomationProvider>
        <ApprovalsPage />
      </AutomationProvider>,
    );
    const table = await screen.findByRole("table");
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(2));
    const cell = within(table.querySelectorAll("tbody tr")[0] as HTMLElement).getByText(reason).closest("td");
    expect(cell).toHaveClass("fd-cut");
    expect(cell).toHaveTextContent(reason);
  });
});
