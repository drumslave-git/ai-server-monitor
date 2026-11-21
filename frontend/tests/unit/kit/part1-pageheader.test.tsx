import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { PageHeader } from "../../../src/kit";

describe("PageHeader", () => {
  it("shows the title as the first-level heading inside a banner", () => {
    render(<PageHeader title="Jobs" />);
    expect(screen.getByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(screen.getByRole("banner")).toBeInTheDocument();
  });

  it("shows the note under the title only when given", () => {
    const { rerender } = render(<PageHeader title="Jobs" />);
    expect(screen.queryByText("Prompts that run on a schedule")).not.toBeInTheDocument();
    rerender(<PageHeader title="Jobs" note="Prompts that run on a schedule" />);
    expect(screen.getByText("Prompts that run on a schedule")).toBeInTheDocument();
  });

  it("shows a back link with an arrow, going to its href", () => {
    render(<PageHeader title="Run 7" back={{ label: "jobs", href: "#/jobs" }} />);
    const back = screen.getByRole("link", { name: /jobs/ });
    expect(back).toHaveAttribute("href", "#/jobs");
    expect(back).toHaveTextContent("← jobs");
  });

  it("shows meta beside the back link, or alone", () => {
    const { rerender } = render(<PageHeader title="Run 7" meta="finished 2 min ago" />);
    expect(screen.getByText("finished 2 min ago")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    rerender(<PageHeader title="Run 7" meta="finished 2 min ago" back={{ label: "jobs", href: "#/jobs" }} />);
    expect(screen.getByText("finished 2 min ago")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /jobs/ })).toBeInTheDocument();
  });

  it("shows the actions (children)", () => {
    render(
      <PageHeader title="Jobs">
        <button>new job</button>
      </PageHeader>,
    );
    expect(screen.getByRole("button", { name: "new job" })).toBeInTheDocument();
  });

  it("shows the tools (a filter row) with the title", () => {
    render(<PageHeader title="Logs" tools={<input aria-label="search logs" />} />);
    expect(screen.getByLabelText("search logs")).toBeInTheDocument();
  });

  it("has no fold button without a fold name, even with tools", () => {
    render(<PageHeader title="Logs" tools={<input aria-label="search logs" />} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("has no fold button when there are no tools, even with a fold name", () => {
    render(<PageHeader title="Logs" fold="filters" />);
    expect(screen.queryByRole("button", { name: "filters" })).not.toBeInTheDocument();
  });

  it("the fold button, named by fold, starts closed and toggles aria-expanded on each click", async () => {
    const user = userEvent.setup();
    render(<PageHeader title="Logs" tools={<input aria-label="search logs" />} fold="filters" />);
    const fold = screen.getByRole("button", { name: "filters" });
    expect(fold).toHaveAttribute("aria-expanded", "false");
    await user.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "true");
    await user.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "false");
  });

  it("the fold button controls the element that holds the tools", () => {
    render(<PageHeader title="Logs" tools={<input aria-label="search logs" />} fold="filters" />);
    const fold = screen.getByRole("button", { name: "filters" });
    const target = document.getElementById(fold.getAttribute("aria-controls")!);
    expect(target).not.toBeNull();
    expect(target).toContainElement(screen.getByLabelText("search logs"));
  });

  it("keeps the tools in the document whether folded or open", async () => {
    const user = userEvent.setup();
    render(<PageHeader title="Logs" tools={<input aria-label="search logs" />} fold="filters" />);
    expect(screen.getByLabelText("search logs")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "filters" }));
    expect(screen.getByLabelText("search logs")).toBeInTheDocument();
  });

  it("two headers on a page give their fold buttons different controlled ids", () => {
    render(
      <>
        <PageHeader title="A" tools={<span>t1</span>} fold="filters a" />
        <PageHeader title="B" tools={<span>t2</span>} fold="filters b" />
      </>,
    );
    const a = screen.getByRole("button", { name: "filters a" }).getAttribute("aria-controls");
    const b = screen.getByRole("button", { name: "filters b" }).getAttribute("aria-controls");
    expect(a).not.toBe(b);
  });
});
