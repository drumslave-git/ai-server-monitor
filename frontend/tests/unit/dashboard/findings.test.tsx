import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { takeExplain } from "../../../src/components/Explain";
import { Findings } from "../../../src/components/Findings";
import type { Finding } from "../../../src/jobs";

const finding = (over: Partial<Finding> & Pick<Finding, "title" | "severity">): Finding => ({ detail: "", evidence: [], ...over });

afterEach(() => {
  location.hash = "";
});

describe("Findings", () => {
  it("shows the worst first and keeps the stored order among equals", () => {
    render(
      <Findings
        findings={[
          finding({ severity: "info", title: "Info A" }),
          finding({ severity: "warn", title: "Warn A" }),
          finding({ severity: "crit", title: "Crit A" }),
          finding({ severity: "warn", title: "Warn B" }),
          finding({ severity: "crit", title: "Crit B" }),
        ]}
      />,
    );
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["Crit A", "Crit B", "Warn A", "Warn B", "Info A"]);
  });

  it("names the severity of each finding in words", () => {
    render(
      <Findings
        findings={[finding({ severity: "info", title: "Info A" }), finding({ severity: "warn", title: "Warn A" }), finding({ severity: "crit", title: "Crit A" })]}
      />,
    );
    const word = (title: string) => screen.getByRole("heading", { name: title }).closest("article")!.querySelector(".fd-status")!.textContent;
    expect(word("Crit A")).toBe("critical");
    expect(word("Warn A")).toBe("warning");
    expect(word("Info A")).toBe("info");
  });

  it("shows the detail and the evidence lines in one block", () => {
    render(<Findings findings={[finding({ severity: "crit", title: "Disk full", detail: "No space left.", evidence: ["line one", "line two"] })]} />);
    const article = screen.getByRole("heading", { name: "Disk full" }).closest("article")!;
    expect(within(article).getByText("No space left.")).toBeInTheDocument();
    expect(article.querySelector("pre")).toHaveTextContent("line one line two");
    expect(article.querySelector("pre")!.textContent).toBe("line one\nline two");
  });

  it("shows no evidence block and no detail when there are none", () => {
    render(<Findings findings={[finding({ severity: "info", title: "Plain" })]} />);
    const article = screen.getByRole("heading", { name: "Plain" }).closest("article")!;
    expect(article.querySelector("pre")).toBeNull();
    expect(article.querySelector(".fd-dim")).toBeNull();
  });

  it("adds the unconfirmed note only when the finding says it was not verified", () => {
    render(
      <Findings
        findings={[
          finding({ severity: "warn", title: "Unverified", detail: "Maybe.", verified: false }),
          finding({ severity: "warn", title: "Verified", detail: "Sure.", verified: true }),
          finding({ severity: "warn", title: "Unknown", detail: "Unsaid.", verified: null }),
          finding({ severity: "warn", title: "Only note", verified: false }),
        ]}
      />,
    );
    expect(screen.getByText("Maybe. Not confirmed by command output.")).toBeInTheDocument();
    expect(screen.getByText("Sure.")).toBeInTheDocument();
    expect(screen.getByText("Unsaid.")).toBeInTheDocument();
    expect(screen.getByText("Not confirmed by command output.")).toBeInTheDocument();
    expect(screen.getAllByText(/Not confirmed/)).toHaveLength(2);
  });

  it("shows the source beside the severity of every finding", () => {
    render(<Findings findings={[finding({ severity: "warn", title: "One" })]} source={<a href="#/jobs/x">Example job</a>} />);
    expect(screen.getByRole("link", { name: "Example job" })).toHaveAttribute("href", "#/jobs/x");
  });

  it("has no Explain button without a run id", () => {
    render(<Findings findings={[finding({ severity: "warn", title: "One" })]} />);
    expect(screen.queryByRole("button", { name: "explain" })).not.toBeInTheDocument();
  });

  it("gives each finding an Explain button that names it by its stored index, not its place on the screen", async () => {
    const user = userEvent.setup();
    render(<Findings runId={5} findings={[finding({ severity: "info", title: "Stored first" }), finding({ severity: "crit", title: "Stored second" })]} />);
    // on screen the critical one comes first, the Explain button still means index 1
    const first = screen.getAllByRole("heading", { level: 3 })[0].closest("article")!;
    expect(first).toHaveTextContent("Stored second");
    await user.click(within(first).getByRole("button", { name: "explain" }));
    expect(location.hash).toBe("#/chat/new");
    expect(takeExplain()).toEqual({ runId: 5, finding: 1 });
    expect(takeExplain()).toBeNull();
  });

  it("shows nothing for an empty list", () => {
    const { container } = render(<Findings findings={[]} runId={1} />);
    expect(container).toBeEmptyDOMElement();
  });
});
