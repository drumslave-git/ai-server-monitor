import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Rail, BottomBar, type NavItem } from "../../../src/kit";

const items: NavItem[] = [
  { key: "dashboard", label: "dashboard", href: "#/", icon: "dashboard" },
  { key: "approvals", label: "approvals", href: "#/approvals", icon: "approvals", count: 3 },
  { key: "jobs", label: "jobs", href: "#/jobs", icon: "jobs", count: 0 },
  { key: "logs", label: "logs", href: "#/logs" },
];

describe("Rail", () => {
  it("renders the brand, a version and the live state", () => {
    render(<Rail items={items} active="dashboard" live version="0.9.1" />);
    expect(screen.getByText("ai server monitor")).toBeInTheDocument();
    expect(screen.getByText("0.9.1 · live")).toBeInTheDocument();
    expect(screen.getAllByTitle("Live updates on").length).toBeGreaterThan(0);
  });

  it("says it is reconnecting when live updates are off", () => {
    render(<Rail items={items} active="dashboard" live={false} version="0.9.1" />);
    expect(screen.getByText("0.9.1 · reconnecting")).toBeInTheDocument();
    expect(screen.getByTitle("Reconnecting")).toBeInTheDocument();
    expect(screen.queryByTitle("Live updates on")).not.toBeInTheDocument();
  });

  it("leaves out the version when there is none", () => {
    render(<Rail items={items} active="dashboard" live version={null} />);
    expect(screen.getByText("live")).toBeInTheDocument();
  });

  it("has two navigations named Main: the rail's list and the phone's bottom bar", () => {
    render(<Rail items={items} active="dashboard" live />);
    expect(screen.getAllByRole("navigation", { name: "Main" })).toHaveLength(2);
  });

  it.each([0, 1])("navigation %i links each page to its #/.. address", (index) => {
    render(<Rail items={items} active="dashboard" live />);
    const nav = screen.getAllByRole("navigation", { name: "Main" })[index];
    expect(within(nav).getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["#/", "#/approvals", "#/jobs", "#/logs"]);
    expect(within(nav).getByRole("link", { name: /jobs/ })).toHaveAttribute("href", "#/jobs");
  });

  it.each([0, 1])("navigation %i marks only the active page with aria-current=page", (index) => {
    render(<Rail items={items} active="jobs" live />);
    const nav = screen.getAllByRole("navigation", { name: "Main" })[index];
    const current = within(nav).getAllByRole("link").filter((a) => a.getAttribute("aria-current") === "page");
    expect(current).toHaveLength(1);
    expect(current[0]).toHaveAccessibleName(/jobs/);
  });

  it("marks no page when active matches none", () => {
    render(<Rail items={items} active="nowhere" live />);
    expect(document.querySelector("[aria-current]")).toBeNull();
  });

  it.each([0, 1])("navigation %i shows a count badge only when the count is above zero", (index) => {
    render(<Rail items={items} active="dashboard" live />);
    const nav = screen.getAllByRole("navigation", { name: "Main" })[index];
    expect(within(nav).getByRole("link", { name: /approvals/ })).toHaveTextContent("3");
    expect(within(nav).getByRole("link", { name: /jobs/ })).not.toHaveTextContent("0");
    expect(within(nav).getByRole("link", { name: /dashboard/ })).toHaveTextContent(/^dashboard$/);
  });

  it("draws an icon (hidden from assistive tech) before the word only for items that have one", () => {
    render(<Rail items={items} active="dashboard" live />);
    const nav = screen.getAllByRole("navigation", { name: "Main" })[0];
    expect(within(nav).getByRole("link", { name: /dashboard/ }).querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(within(nav).getByRole("link", { name: /logs/ }).querySelector("svg")).toBeNull();
  });

  it("shows meters and foot children only when given", () => {
    const { rerender } = render(<Rail items={items} active="dashboard" live />);
    expect(screen.queryByText("cpu gauge")).not.toBeInTheDocument();
    expect(screen.queryByText("automation box")).not.toBeInTheDocument();
    rerender(
      <Rail items={items} active="dashboard" live meters={<span>cpu gauge</span>}>
        <span>automation box</span>
      </Rail>,
    );
    expect(screen.getByText("cpu gauge")).toBeInTheDocument();
    expect(screen.getByText("automation box")).toBeInTheDocument();
  });

  it("updates the current page when active changes", () => {
    const { rerender } = render(<Rail items={items} active="dashboard" live />);
    rerender(<Rail items={items} active="approvals" live />);
    const nav = screen.getAllByRole("navigation", { name: "Main" })[0];
    expect(within(nav).getByRole("link", { name: /approvals/ })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: /dashboard/ })).not.toHaveAttribute("aria-current");
  });
});

describe("BottomBar", () => {
  it("is a navigation named Main with a link per page", () => {
    render(<BottomBar items={items} active="approvals" />);
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getAllByRole("link")).toHaveLength(4);
    expect(within(nav).getByRole("link", { name: /approvals/ })).toHaveAttribute("href", "#/approvals");
    expect(within(nav).getByRole("link", { name: /approvals/ })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: /approvals/ })).toHaveTextContent("3");
    expect(within(nav).getByRole("link", { name: /dashboard/ })).not.toHaveAttribute("aria-current");
  });
});
