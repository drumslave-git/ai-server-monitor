import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { ApprovalBatch } from "../../../src/api";
import { ApprovalCards } from "../../../src/components/ApprovalCards";
import { action, batch, commandBatch, decidedBatch, multiBatch, queuedBatch, singleBatch } from "../../mocks/fixtures/approvals";
import { server } from "../../mocks/server";

type Decision = { id: number; body: unknown };

/** Answer `GET /api/approvals?of=…` with these approvals; record the queries. */
function answers(batches: ApprovalBatch[]) {
  const queries: string[] = [];
  server.use(
    http.get("/api/approvals", ({ request }) => {
      queries.push(new URL(request.url).search);
      return HttpResponse.json({ batches, items: [] });
    }),
  );
  return queries;
}

function recordDecisions(answer: () => Response = () => HttpResponse.json(multiBatch)) {
  const seen: Decision[] = [];
  server.use(
    http.post("/api/approvals/:id", async ({ request, params }) => {
      seen.push({ id: Number(params.id), body: await request.json() });
      return answer();
    }),
  );
  return seen;
}

describe("ApprovalCards", () => {
  it("asks for the approvals of the items a chat answer named", async () => {
    const queries = answers([multiBatch]);
    render(<ApprovalCards items={[101, 102, 103]} />);
    await screen.findByText(/Each with/);
    expect(queries).toEqual(["?of=101&of=102&of=103"]);
  });

  it("shows one card for an approval of several items: what it is, the shared arguments once, and what differs per item", async () => {
    answers([multiBatch]);
    render(<ApprovalCards items={[101, 102, 103]} />);
    expect(await screen.findByText("mcp_call")).toBeInTheDocument();
    expect(screen.getByText(/test-server · delete_item/)).toBeInTheDocument();
    expect(screen.getByText("service: example")).toBeInTheDocument();
    for (const id of ["a101", "a102", "a103"]) expect(screen.getByText(`id: ${id}`)).toBeInTheDocument();
    expect(screen.getAllByTitle("waiting for your approval")).toHaveLength(3);
    expect(screen.getByText(/3 waiting for your approval/)).toBeInTheDocument();
  });

  it("shows one card for each approval", async () => {
    answers([multiBatch, singleBatch]);
    render(<ApprovalCards items={[101, 104]} />);
    expect(await screen.findAllByRole("button", { name: /^deny/ })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "deny all 3" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "deny" })).toBeInTheDocument();
  });

  it("links to the approval in Approvals", async () => {
    answers([multiBatch, singleBatch]);
    render(<ApprovalCards items={[101, 104]} />);
    const links = await screen.findAllByRole("link", { name: "open in Approvals" });
    expect(links.map((l) => l.getAttribute("href"))).toEqual(["#/approvals/41", "#/approvals/42"]);
  });

  it("names the buttons of a single call without a count and says it waits", async () => {
    answers([singleBatch]);
    render(<ApprovalCards items={[104]} />);
    expect(await screen.findByRole("button", { name: "approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "deny" })).toBeEnabled();
    expect(screen.getByText(/waiting for your approval/)).toBeInTheDocument();
    expect(screen.queryByText(/Each with/)).not.toBeInTheDocument();
    expect(screen.queryByTitle("waiting for your approval")).not.toBeInTheDocument();
  });

  it("shows nothing for an answer with no approvals", async () => {
    const queries = answers([]);
    const { container } = render(<ApprovalCards items={[1]} />);
    await waitFor(() => expect(queries).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 20));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows why the approvals could not be loaded", async () => {
    server.use(http.get("/api/approvals", () => HttpResponse.json({ detail: "approvals are down" }, { status: 503 })));
    render(<ApprovalCards items={[101]} />);
    expect(await screen.findByText("Approval: approvals are down")).toBeInTheDocument();
  });

  describe("deciding", () => {
    it("approve sends the decision of the approval", async () => {
      const user = userEvent.setup();
      answers([multiBatch]);
      const decisions = recordDecisions();
      render(<ApprovalCards items={[101]} />);
      await user.click(await screen.findByRole("button", { name: "approve all 3" }));
      await waitFor(() => expect(decisions).toEqual([{ id: 41, body: { decision: "approve" } }]));
    });

    it("deny sends a rejection", async () => {
      const user = userEvent.setup();
      answers([multiBatch]);
      const decisions = recordDecisions();
      render(<ApprovalCards items={[101]} />);
      await user.click(await screen.findByRole("button", { name: "deny all 3" }));
      await waitFor(() => expect(decisions).toEqual([{ id: 41, body: { decision: "reject" } }]));
    });

    it("approve for this whole answer sends the rest flag, and only where later calls can be covered", async () => {
      const user = userEvent.setup();
      answers([multiBatch, singleBatch]);
      const decisions = recordDecisions();
      render(<ApprovalCards items={[101, 104]} />);
      const buttons = await screen.findAllByRole("button", { name: "approve for this whole answer" });
      expect(buttons).toHaveLength(1);
      await user.click(buttons[0]);
      await waitFor(() => expect(decisions).toEqual([{ id: 41, body: { decision: "approve", rest: true } }]));
    });

    it("loads the approvals again after a decision and shows the outcome without buttons", async () => {
      const user = userEvent.setup();
      let decided = false;
      const done = batch({ id: 42, title: singleBatch.title, items: [action({ id: 104, status: "executed", result: {}, args: singleBatch.items[0].args })] });
      server.use(
        http.get("/api/approvals", () => HttpResponse.json({ batches: [decided ? done : singleBatch], items: [] })),
        http.post("/api/approvals/:id", () => {
          decided = true;
          return HttpResponse.json(done);
        }),
      );
      render(<ApprovalCards items={[104]} />);
      await user.click(await screen.findByRole("button", { name: "approve" }));
      expect(await screen.findByText(/approved and run/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "approve" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "deny" })).not.toBeInTheDocument();
    });

    it("disables the buttons while the decision is on its way", async () => {
      const user = userEvent.setup();
      answers([multiBatch]);
      let release!: () => void;
      const gate = new Promise<void>((r) => (release = r));
      server.use(
        http.post("/api/approvals/:id", async () => {
          await gate;
          return HttpResponse.json(multiBatch);
        }),
      );
      render(<ApprovalCards items={[101]} />);
      await user.click(await screen.findByRole("button", { name: "approve all 3" }));
      await waitFor(() => expect(screen.getByRole("button", { name: "approve all 3" })).toBeDisabled());
      expect(screen.getByRole("button", { name: "deny all 3" })).toBeDisabled();
      release();
      await waitFor(() => expect(screen.getByRole("button", { name: "approve all 3" })).toBeEnabled());
    });

    it("shows the message of a refused decision in the card", async () => {
      const user = userEvent.setup();
      answers([multiBatch]);
      recordDecisions(() => HttpResponse.json({ detail: "This approval was already decided." }, { status: 409 }));
      render(<ApprovalCards items={[101]} />);
      await user.click(await screen.findByRole("button", { name: "deny all 3" }));
      expect(await screen.findByText("This approval was already decided.")).toBeInTheDocument();
      await waitFor(() => expect(screen.getByRole("button", { name: "deny all 3" })).toBeEnabled());
    });
  });

  describe("decided and queued approvals", () => {
    it("has no buttons once nothing waits, and says what became of the items", async () => {
      answers([decidedBatch]);
      render(<ApprovalCards items={[109]} />);
      expect(await screen.findByText(/1 executed, 1 rejected, 1 failed/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /approve|deny/ })).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: "open in Approvals" })).toHaveAttribute("href", "#/approvals/46");
    });

    it("says what an approval for the whole answer covers", async () => {
      answers([decidedBatch]);
      render(<ApprovalCards items={[109]} />);
      expect(await screen.findByText("Approved for this whole answer: later calls of test-server · delete_item run without asking and are listed here.")).toBeInTheDocument();
    });

    it("shows the reason of what did not happen, and puts it before the items that went well", async () => {
      answers([decidedBatch]);
      const { container } = render(<ApprovalCards items={[109]} />);
      expect(await screen.findByText("tool timed out")).toBeInTheDocument();
      expect(screen.getByText("Denied by the owner")).toBeInTheDocument();
      const text = container.textContent ?? "";
      expect(text.indexOf("tool timed out")).toBeLessThan(text.indexOf("id: a109"));
      expect(text.indexOf("Denied by the owner")).toBeLessThan(text.indexOf("id: a109"));
    });

    it("shows the state of an item that is neither waiting nor run", async () => {
      answers([queuedBatch]);
      render(<ApprovalCards items={[106]} />);
      expect(await screen.findAllByText("approved; queued")).not.toHaveLength(0);
      expect(screen.getByText(/2 queued/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /approve|deny/ })).not.toBeInTheDocument();
    });

    it("says an expired approval expired and offers no buttons", async () => {
      const expired = batch({ id: 45, title: "Remove the old cache", items: [action({ id: 108, status: "expired" })] });
      answers([expired]);
      render(<ApprovalCards items={[108]} />);
      expect(await screen.findByText(/expired/)).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    });
  });

  describe("a changing shell command", () => {
    it("shows the command to run as root and why, and deciding it sends the same decision", async () => {
      const user = userEvent.setup();
      answers([commandBatch]);
      const decisions = recordDecisions(() => HttpResponse.json(commandBatch));
      render(<ApprovalCards items={[105]} />);
      expect(await screen.findByText("Command to run as root on the host")).toBeInTheDocument();
      expect(screen.getByText("journalctl --vacuum-size=200M")).toBeInTheDocument();
      expect(screen.getByText("The journal is large")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "approve" }));
      await waitFor(() => expect(decisions).toEqual([{ id: 43, body: { decision: "approve" } }]));
    });

    it("counts the commands of an approval of several", async () => {
      const several = batch({
        id: 60,
        title: "Two commands",
        action: "host_command",
        items: [105, 106].map((id) => action({ id, action: "host_command", target: "host", args: { command: `echo ${id}` } })),
      });
      answers([several]);
      render(<ApprovalCards items={[105]} />);
      expect(await screen.findByText("2 commands to run as root on the host")).toBeInTheDocument();
      expect(screen.getByText("echo 105")).toBeInTheDocument();
      expect(screen.getByText("echo 106")).toBeInTheDocument();
    });
  });

  describe("long approvals", () => {
    const items = Array.from({ length: 7 }, (_, n) =>
      action({
        id: 400 + n,
        target: `item ${n}`,
        args: { server: "test-server", tool: "delete_item", arguments: { id: `x${n}` } },
        // the last one failed: it must not hide behind "show 2 more"
        ...(n === 6 ? { status: "failed", result: { error: "disk is full" } } : { status: "executed" }),
      }),
    );
    const long = batch({ id: 70, title: "Seven items", items });

    it("shows five items, failures first, and the rest on demand", async () => {
      const user = userEvent.setup();
      answers([long]);
      render(<ApprovalCards items={[400]} />);
      expect(await screen.findByText("disk is full")).toBeInTheDocument();
      expect(screen.getByText("id: x0")).toBeInTheDocument();
      expect(screen.getByText("id: x3")).toBeInTheDocument();
      expect(screen.queryByText("id: x4")).not.toBeInTheDocument();
      expect(screen.queryByText("id: x5")).not.toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "show 2 more" }));
      expect(screen.getByText("id: x5")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "show only the first 5" })).toHaveAttribute("aria-expanded", "true");
      await user.click(screen.getByRole("button", { name: "show only the first 5" }));
      expect(screen.queryByText("id: x5")).not.toBeInTheDocument();
    });

    it("has no 'show more' for an approval of five items", async () => {
      answers([batch({ id: 71, title: "Five", items: items.slice(0, 5) })]);
      render(<ApprovalCards items={[400]} />);
      await screen.findByText("id: x4");
      expect(screen.queryByRole("button", { name: /show/ })).not.toBeInTheDocument();
    });
  });

  it("keeps all the arguments of each call when they differ in nothing", async () => {
    const same = batch({
      id: 80,
      title: "Same twice",
      items: [901, 902].map((id) => action({ id, args: { server: "test-server", tool: "delete_item", arguments: { service: "example" } } })),
    });
    answers([same]);
    render(<ApprovalCards items={[901]} />);
    expect(await screen.findAllByText("service: example")).toHaveLength(2);
    expect(screen.queryByText(/Each with/)).not.toBeInTheDocument();
  });

  it("shows the target of a call that has no arguments", async () => {
    const bare = batch({ id: 81, title: "Bare", items: [action({ id: 903, target: "the target", args: { server: "test-server", tool: "ping" } })] });
    answers([bare]);
    render(<ApprovalCards items={[903]} />);
    expect(await screen.findByText("the target")).toBeInTheDocument();
    expect(screen.queryByText(/^service:/)).not.toBeInTheDocument();
  });
});
