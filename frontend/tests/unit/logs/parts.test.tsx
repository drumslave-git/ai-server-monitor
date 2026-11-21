// The shared pieces of the log views: the page head with its tabs and filters, and the status of a call.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { CallStatus, LogsHead } from "../../../src/pages/logs/parts";

describe("LogsHead", () => {
  it("is the Logs heading with a tab for each log, the open one selected", () => {
    render(<LogsHead tab="app" tools={<span>the filters</span>} />);
    expect(screen.getByRole("heading", { level: 1, name: "Logs" })).toBeInTheDocument();
    const tabs = screen.getByRole("tablist", { name: "Log type" });
    expect(tabs).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "LLM calls" })).toHaveAttribute("href", "#/logs/llm");
    expect(screen.getByRole("tab", { name: "LLM calls" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "App log" })).toHaveAttribute("href", "#/logs/app");
    expect(screen.getByRole("tab", { name: "App log" })).toHaveAttribute("aria-selected", "true");
  });

  it("shows the filters it is given", () => {
    render(<LogsHead tab="llm" tools={<span>the filters</span>} />);
    expect(screen.getByText("the filters")).toBeInTheDocument();
  });

  it("has no note when the list is not limited to one owner", () => {
    render(<LogsHead tab="llm" tools={null} />);
    expect(screen.queryByText(/Calls belonging to/)).not.toBeInTheDocument();
  });

  it("says whose calls the list shows and links the owner and 'show all'", () => {
    render(<LogsHead tab="llm" link={{ kind: "job", id: "example-job" }} tools={null} />);
    expect(screen.getByText(/Calls belonging to/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "job example-job" })).toHaveAttribute("href", "#/jobs/example-job");
    expect(screen.getByRole("link", { name: "show all" })).toHaveAttribute("href", "#/logs/llm");
  });

  it("folds the filters behind a button that opens them", async () => {
    const user = userEvent.setup();
    render(<LogsHead tab="llm" tools={<span>the filters</span>} />);
    const button = screen.getByRole("button", { name: "filters" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "false");
  });
});

describe("CallStatus", () => {
  it("says OK for a call that went well", () => {
    render(<CallStatus status="ok" />);
    expect(screen.getByText("OK")).toBeInTheDocument();
  });

  it("says cancelled for a cancelled call", () => {
    render(<CallStatus status="cancelled" />);
    expect(screen.getByText("cancelled")).toBeInTheDocument();
  });

  it("says failed for an error and for any state it does not know", () => {
    render(
      <>
        <CallStatus status="error" />
        <CallStatus status="" />
      </>,
    );
    expect(screen.getAllByText("failed")).toHaveLength(2);
  });
});
