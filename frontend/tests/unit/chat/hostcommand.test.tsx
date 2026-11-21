import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HostCommand } from "../../../src/components/HostCommand";

type Item = Parameters<typeof HostCommand>[0]["item"];

/** An action record as the approvals API returns it, with what a test does not care about filled in. */
const action = (item: { args?: Record<string, unknown> | null; reason?: string | null; status: string; result?: Record<string, unknown> | null }) =>
  ({ args: { command: "docker image prune -f" }, reason: null, result: null, ...item }) as unknown as Item;

describe("HostCommand", () => {
  it("shows the command and why the model wants to run it", () => {
    render(<HostCommand item={action({ status: "pending", args: { command: "systemctl restart test-main", reason: "the service is stuck" } })} />);
    expect(screen.getByText("systemctl restart test-main")).toBeInTheDocument();
    expect(screen.getByText("the service is stuck")).toBeInTheDocument();
    // not run yet: no exit code, no output, no note
    expect(screen.queryByText(/exit code/)).not.toBeInTheDocument();
    expect(screen.queryByText("No output.")).not.toBeInTheDocument();
  });

  it("falls back to the action's own reason when the arguments have none", () => {
    render(<HostCommand item={action({ status: "pending", reason: "cleaning up images" })} />);
    expect(screen.getByText("cleaning up images")).toBeInTheDocument();
  });

  it("the reason in the arguments wins over the action's reason", () => {
    render(<HostCommand item={action({ status: "pending", args: { command: "ls", reason: "from the model" }, reason: "from the record" })} />);
    expect(screen.getByText("from the model")).toBeInTheDocument();
    expect(screen.queryByText("from the record")).not.toBeInTheDocument();
  });

  it("says so when there is no command", () => {
    render(<HostCommand item={action({ status: "pending", args: null })} />);
    expect(screen.getByText("(no command)")).toBeInTheDocument();
  });

  describe("executed", () => {
    it("shows exit code 0, the duration and the output", () => {
      render(<HostCommand item={action({ status: "executed", result: { exit_code: 0, duration_s: 0.46, stopped: "exit", truncated: false, output: "Total reclaimed space: 1.2GB" } })} />);
      expect(screen.getByText("exit code 0")).toBeInTheDocument();
      expect(screen.getByText("· 0.5 s")).toBeInTheDocument();
      expect(screen.getByText("Total reclaimed space: 1.2GB")).toBeInTheDocument();
      // a normal end is not repeated, and the output is not shortened
      expect(screen.queryByText("· exit")).not.toBeInTheDocument();
      expect(screen.queryByText("· output shortened")).not.toBeInTheDocument();
    });

    it("shows a non-zero exit code too", () => {
      render(<HostCommand item={action({ status: "executed", result: { exit_code: 2, duration_s: 1, stopped: "exit", truncated: false, output: "failed" } })} />);
      expect(screen.getByText("exit code 2")).toBeInTheDocument();
    });

    it("says what stopped it when it did not end by itself, and that the output was shortened", () => {
      render(<HostCommand item={action({ status: "executed", result: { exit_code: 124, duration_s: 30, stopped: "timeout", truncated: true, output: "partial" } })} />);
      expect(screen.getByText("· timeout")).toBeInTheDocument();
      expect(screen.getByText("· output shortened")).toBeInTheDocument();
    });

    it("says there was no output", () => {
      render(<HostCommand item={action({ status: "executed", result: { exit_code: 0, output: "" } })} />);
      expect(screen.getByText("No output.")).toBeInTheDocument();
    });

    it("shows a question mark when the exit code is missing", () => {
      render(<HostCommand item={action({ status: "executed", result: { output: "x" } })} />);
      expect(screen.getByText("exit code ?")).toBeInTheDocument();
    });

    it("keeps a short output open", () => {
      render(<HostCommand item={action({ status: "executed", result: { exit_code: 0, output: "a\nb\nc" } })} />);
      expect(document.querySelector("details")).toBeNull();
      expect(screen.getByText(/a\s+b\s+c/)).toBeInTheDocument();
    });

    it("folds an output of more than 12 lines, with its line count", () => {
      const output = Array.from({ length: 13 }, (_, i) => `line ${i + 1}`).join("\n");
      render(<HostCommand item={action({ status: "executed", result: { exit_code: 0, output } })} />);
      const fold = screen.getByText("output (13 lines)").closest("details")!;
      expect(fold).not.toHaveAttribute("open");
      expect(fold).toHaveTextContent("line 13");
    });

    it("does not fold exactly 12 lines", () => {
      const output = Array.from({ length: 12 }, (_, i) => `line ${i + 1}`).join("\n");
      render(<HostCommand item={action({ status: "executed", result: { exit_code: 0, output } })} />);
      expect(screen.queryByText(/^output \(/)).not.toBeInTheDocument();
    });

    it("folds a long output of a single line", () => {
      render(<HostCommand item={action({ status: "executed", result: { exit_code: 0, output: "x".repeat(1300) } })} />);
      expect(screen.getByText("output (1 lines)")).toBeInTheDocument();
    });
  });

  describe("failed", () => {
    it("a command that ran and failed shows its exit code and output like an executed one", () => {
      render(<HostCommand item={action({ status: "failed", result: { exit_code: 1, duration_s: 0.2, stopped: "exit", output: "permission denied" } })} />);
      expect(screen.getByText("exit code 1")).toBeInTheDocument();
      expect(screen.getByText("permission denied")).toBeInTheDocument();
    });

    it("one that could not start shows the error instead", () => {
      render(<HostCommand item={action({ status: "failed", result: { error: "the host shell is not available" } })} />);
      expect(screen.getByText("the host shell is not available")).toBeInTheDocument();
      expect(screen.queryByText(/exit code/)).not.toBeInTheDocument();
    });

    it("says failed when there is no error text", () => {
      render(<HostCommand item={action({ status: "failed" })} />);
      expect(screen.getByText("failed")).toBeInTheDocument();
    });
  });

  describe("not run", () => {
    it("a denied command says it did not run, with the owner's detail", () => {
      render(<HostCommand item={action({ status: "rejected", result: { detail: "too risky" } })} />);
      expect(screen.getByText("Denied: too risky. The command did not run.")).toBeInTheDocument();
    });

    it("leaves out a detail that only repeats the word rejected", () => {
      render(<HostCommand item={action({ status: "rejected", result: { detail: "Rejected by the owner" } })} />);
      expect(screen.getByText("Denied. The command did not run.")).toBeInTheDocument();
    });

    it("a denied command without a detail says just that", () => {
      render(<HostCommand item={action({ status: "rejected" })} />);
      expect(screen.getByText("Denied. The command did not run.")).toBeInTheDocument();
    });

    it("an expired one says nobody decided", () => {
      render(<HostCommand item={action({ status: "expired" })} />);
      expect(screen.getByText("Expired without a decision. The command did not run.")).toBeInTheDocument();
      expect(screen.queryByText(/exit code/)).not.toBeInTheDocument();
    });
  });
});
