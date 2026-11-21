import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ConnectionTestResult, JsonObject } from "../../../src/api";
import { McpToolPicker, mcpTools } from "../../../src/components/McpTools";

type Tool = { name: string; title?: string | null; description: string; read_only?: boolean | null };

const TOOLS: Tool[] = [
  { name: "test_list", description: "Lists the things", read_only: true },
  { name: "test_peek", description: "Peeks at one thing", read_only: true },
  { name: "test_wipe", description: "Wipes the things", read_only: false },
  { name: "test_odd", description: "The server did not say", read_only: null },
];

/** The picker is controlled: this keeps the value like the form around it does, and tells the spy about each change. */
function renderPicker(value: JsonObject = {}, tools: Tool[] = TOOLS) {
  const onChange = vi.fn();
  function Harness() {
    const [v, setV] = useState<JsonObject>(value);
    return (
      <McpToolPicker
        tools={tools}
        value={v}
        onChange={(next) => {
          onChange(next);
          setV(next);
        }}
      />
    );
  }
  render(<Harness />);
  return { onChange, user: userEvent.setup() };
}

const group = (name: "Read tools" | "Write tools") => within(screen.getByRole("rowgroup", { name }));
const last = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls[fn.mock.calls.length - 1][0] as JsonObject;

describe("mcpTools", () => {
  const result = (over: Partial<ConnectionTestResult>): ConnectionTestResult => ({ ok: true, message: "ok", ...over });

  it("is null without a test result", () => {
    expect(mcpTools(null)).toBeNull();
  });

  it("is null when the test failed, even if it carries tools", () => {
    expect(mcpTools(result({ ok: false, details: { tools: TOOLS } }))).toBeNull();
  });

  it("is null without details or without a tools list", () => {
    expect(mcpTools(result({}))).toBeNull();
    expect(mcpTools(result({ details: {} }))).toBeNull();
    expect(mcpTools(result({ details: { tools: "none" } }))).toBeNull();
    expect(mcpTools(result({ details: { tools: { a: 1 } } }))).toBeNull();
  });

  it("gives the tools of a successful test, and an empty list as it is", () => {
    expect(mcpTools(result({ details: { tools: TOOLS } }))).toEqual(TOOLS);
    expect(mcpTools(result({ details: { tools: [] } }))).toEqual([]);
  });
});

describe("McpToolPicker: what it shows", () => {
  it("groups the tools by what the server says: read-only ones read, everything else write", () => {
    renderPicker();
    expect(group("Read tools").getByRole("switch", { name: "Use test_list" })).toBeInTheDocument();
    expect(group("Read tools").getByRole("switch", { name: "Use test_peek" })).toBeInTheDocument();
    expect(group("Write tools").getByRole("switch", { name: "Use test_wipe" })).toBeInTheDocument();
    expect(group("Write tools").getByRole("switch", { name: "Use test_odd" })).toBeInTheDocument();
    expect(screen.getAllByRole("switch")).toHaveLength(4);
  });

  it("shows each tool's description", () => {
    renderPicker();
    expect(screen.getByText("Lists the things")).toBeInTheDocument();
    expect(screen.getByText("Wipes the things")).toBeInTheDocument();
  });

  it("counts the tools on, overall and per group", () => {
    renderPicker({ tool_filter: ["test_list", "test_wipe", "test_odd"] });
    expect(screen.getByText("3 of 4 tools on")).toBeInTheDocument();
    expect(group("Read tools").getByText("1 of 2 on")).toBeInTheDocument();
    expect(group("Write tools").getByText("2 of 2 on")).toBeInTheDocument();
  });

  it("starts with nothing on, and shows the switches off", () => {
    renderPicker();
    expect(screen.getByText("0 of 4 tools on")).toBeInTheDocument();
    for (const sw of screen.getAllByRole("switch")) expect(sw).not.toBeChecked();
  });

  it("shows the chosen tools as on, and does not count a name the server no longer offers", () => {
    renderPicker({ tool_filter: ["test_peek", "test_gone"] });
    expect(screen.getByText("1 of 4 tools on")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Use test_peek" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Use test_list" })).not.toBeChecked();
  });

  it("only shows a group that has tools", () => {
    renderPicker({}, [TOOLS[0], TOOLS[1]]);
    expect(screen.getByRole("rowgroup", { name: "Read tools" })).toBeInTheDocument();
    expect(screen.queryByRole("rowgroup", { name: "Write tools" })).not.toBeInTheDocument();
  });

  it("shows no group and 0 of 0 for a server without tools", () => {
    renderPicker({}, []);
    expect(screen.getByText("0 of 0 tools on")).toBeInTheDocument();
    expect(screen.queryByRole("rowgroup", { name: "Read tools" })).not.toBeInTheDocument();
    expect(screen.queryByRole("rowgroup", { name: "Write tools" })).not.toBeInTheDocument();
    expect(screen.queryAllByRole("switch")).toHaveLength(0);
  });

  it("puts a tool where the owner's kind says, not where the server says", () => {
    renderPicker({
      tool_overrides: [
        { name: "test_peek", kind: "write", policy: "inherit" },
        { name: "test_wipe", kind: "read", policy: "inherit" },
      ],
    });
    expect(group("Read tools").getByRole("switch", { name: "Use test_wipe" })).toBeInTheDocument();
    expect(group("Write tools").getByRole("switch", { name: "Use test_peek" })).toBeInTheDocument();
    expect(group("Read tools").queryByRole("switch", { name: "Use test_peek" })).not.toBeInTheDocument();
  });

  it("falls back to what the server says for a kind it does not know, and for an override without a kind", () => {
    renderPicker({ tool_overrides: [{ name: "test_list", kind: "bogus" }, { name: "test_wipe", policy: "auto" }] });
    expect(group("Read tools").getByRole("switch", { name: "Use test_list" })).toBeInTheDocument();
    expect(group("Write tools").getByRole("switch", { name: "Use test_wipe" })).toBeInTheDocument();
  });

  it("ignores junk in the stored lists", () => {
    renderPicker({ tool_filter: ["test_list", 7, null] as unknown as string[], tool_overrides: [null, "x", { name: "test_odd", kind: "read" }] as unknown as JsonObject[] });
    expect(screen.getByText("1 of 4 tools on")).toBeInTheDocument();
    expect(group("Read tools").getByRole("switch", { name: "Use test_odd" })).toBeInTheDocument();
  });

  it("explains how a tool counts as read", () => {
    renderPicker();
    expect(screen.getByText(/A tool is read only if the server says so/)).toBeInTheDocument();
  });
});

describe("McpToolPicker: choosing tools", () => {
  it("turns a tool on with its switch, keeping what else the value holds", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_list"], note: "keep me" });
    await user.click(screen.getByRole("switch", { name: "Use test_wipe" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(last(onChange)).toEqual({ tool_filter: ["test_list", "test_wipe"], note: "keep me" });
    expect(screen.getByRole("switch", { name: "Use test_wipe" })).toBeChecked();
    expect(screen.getByText("2 of 4 tools on")).toBeInTheDocument();
  });

  it("turns a tool off with its switch", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_list", "test_wipe"] });
    await user.click(screen.getByRole("switch", { name: "Use test_list" }));
    expect(last(onChange)).toEqual({ tool_filter: ["test_wipe"] });
    expect(screen.getByRole("switch", { name: "Use test_list" })).not.toBeChecked();
  });

  it("selects every tool of the server with the top button, without listing a tool twice", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_peek"] });
    await user.click(screen.getByRole("button", { name: "Select all tools of this server" }));
    const filter = last(onChange).tool_filter as string[];
    expect([...filter].sort()).toEqual(["test_list", "test_odd", "test_peek", "test_wipe"]);
    expect(screen.getByText("4 of 4 tools on")).toBeInTheDocument();
  });

  it("deselects every tool of the server with the top button, and keeps names of tools that are not listed", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_peek", "test_wipe", "test_gone"] });
    await user.click(screen.getByRole("button", { name: "Deselect all tools of this server" }));
    expect(last(onChange)).toEqual({ tool_filter: ["test_gone"] });
  });

  it("selects only the tools of one group with the group's button", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_wipe"] });
    await user.click(group("Read tools").getByRole("button", { name: "Select all read tools" }));
    expect([...(last(onChange).tool_filter as string[])].sort()).toEqual(["test_list", "test_peek", "test_wipe"]);
    expect(group("Read tools").getByText("2 of 2 on")).toBeInTheDocument();
    expect(group("Write tools").getByText("1 of 2 on")).toBeInTheDocument();
  });

  it("deselects only the tools of one group with the group's button", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_list", "test_wipe", "test_odd"] });
    await user.click(group("Write tools").getByRole("button", { name: "Deselect all write tools" }));
    expect(last(onChange)).toEqual({ tool_filter: ["test_list"] });
  });

  it("keeps select all off when everything is already on, and deselect all off when nothing is", () => {
    renderPicker({ tool_filter: TOOLS.map((t) => t.name) });
    expect(screen.getByRole("button", { name: "Select all tools of this server" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Deselect all tools of this server" })).toBeEnabled();
    expect(group("Read tools").getByRole("button", { name: "Select all read tools" })).toBeDisabled();
  });

  it("keeps deselect all off when nothing is on", () => {
    renderPicker();
    expect(screen.getByRole("button", { name: "Deselect all tools of this server" })).toBeDisabled();
    expect(group("Read tools").getByRole("button", { name: "Deselect all read tools" })).toBeDisabled();
    expect(group("Write tools").getByRole("button", { name: "Select all write tools" })).toBeEnabled();
  });

  it("keeps a group's buttons live while only the other group is on", () => {
    renderPicker({ tool_filter: ["test_list", "test_peek"] });
    expect(group("Read tools").getByRole("button", { name: "Select all read tools" })).toBeDisabled();
    expect(group("Read tools").getByRole("button", { name: "Deselect all read tools" })).toBeEnabled();
    expect(group("Write tools").getByRole("button", { name: "Select all write tools" })).toBeEnabled();
    expect(group("Write tools").getByRole("button", { name: "Deselect all write tools" })).toBeDisabled();
  });
});

describe("McpToolPicker: the owner's exceptions", () => {
  const kindSelect = (name: string) => screen.getByRole("combobox", { name: `Treat ${name} as` });
  const policySelect = (name: string) => screen.getByRole("combobox", { name: `Policy of ${name}` });

  it("offers auto (with what the server says), read and write as the kind", () => {
    renderPicker();
    const options = (name: string) => [...kindSelect(name).querySelectorAll("option")].map((o) => o.textContent);
    expect(options("test_list")).toEqual(["auto (read)", "read", "write"]);
    expect(options("test_wipe")).toEqual(["auto (write)", "read", "write"]);
    expect(options("test_odd")).toEqual(["auto (write)", "read", "write"]);
    expect(kindSelect("test_list")).toHaveValue("auto");
  });

  it("stores a kind as an override of the tool", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_list"] });
    await user.selectOptions(kindSelect("test_list"), "write");
    expect(last(onChange)).toEqual({ tool_filter: ["test_list"], tool_overrides: [{ name: "test_list", policy: "inherit", kind: "write" }] });
    expect(kindSelect("test_list")).toHaveValue("write");
    expect(group("Write tools").getByRole("switch", { name: "Use test_list" })).toBeInTheDocument();
  });

  it("removes the override again when the kind goes back to auto and the policy is the server's", async () => {
    const { onChange, user } = renderPicker({ tool_overrides: [{ name: "test_list", policy: "inherit", kind: "write" }] });
    await user.selectOptions(kindSelect("test_list"), "auto");
    expect(last(onChange).tool_overrides).toEqual([]);
    expect(group("Read tools").getByRole("switch", { name: "Use test_list" })).toBeInTheDocument();
  });

  it("keeps the override when the kind goes back to auto but a policy is set", async () => {
    const { onChange, user } = renderPicker({ tool_overrides: [{ name: "test_wipe", policy: "approve", kind: "read" }] });
    await user.selectOptions(kindSelect("test_wipe"), "auto");
    expect(last(onChange).tool_overrides).toEqual([{ name: "test_wipe", policy: "approve", kind: "auto" }]);
  });

  it("leaves the overrides of other tools alone", async () => {
    const { onChange, user } = renderPicker({ tool_overrides: [{ name: "test_wipe", policy: "suggest", kind: "auto" }] });
    await user.selectOptions(kindSelect("test_list"), "write");
    const overrides = last(onChange).tool_overrides as JsonObject[];
    expect(overrides).toHaveLength(2);
    expect(overrides).toContainEqual({ name: "test_wipe", policy: "suggest", kind: "auto" });
    expect(overrides).toContainEqual({ name: "test_list", policy: "inherit", kind: "write" });
  });

  it("shows a stored kind in the select", () => {
    renderPicker({ tool_overrides: [{ name: "test_odd", policy: "inherit", kind: "read" }] });
    expect(kindSelect("test_odd")).toHaveValue("read");
  });

  it("has a policy select only for tools that are writes", () => {
    renderPicker({ tool_filter: TOOLS.map((t) => t.name) });
    expect(screen.queryByRole("combobox", { name: "Policy of test_list" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Policy of test_peek" })).not.toBeInTheDocument();
    expect(policySelect("test_wipe")).toBeInTheDocument();
    expect(policySelect("test_odd")).toBeInTheDocument();
  });

  it("offers the server's policy, suggest, approve and auto", () => {
    renderPicker({ tool_filter: ["test_wipe"] });
    expect([...policySelect("test_wipe").querySelectorAll("option")].map((o) => o.textContent)).toEqual(["policy: server's", "suggest", "approve", "auto"]);
    expect(policySelect("test_wipe")).toHaveValue("inherit");
  });

  it("keeps the policy off for a tool that is not used", async () => {
    const { user } = renderPicker({ tool_filter: [] });
    expect(policySelect("test_wipe")).toBeDisabled();
    await user.click(screen.getByRole("switch", { name: "Use test_wipe" }));
    expect(policySelect("test_wipe")).toBeEnabled();
    await user.click(screen.getByRole("switch", { name: "Use test_wipe" }));
    expect(policySelect("test_wipe")).toBeDisabled();
  });

  it("stores a policy as an override of the tool", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_wipe"] });
    await user.selectOptions(policySelect("test_wipe"), "approve");
    expect(last(onChange)).toEqual({ tool_filter: ["test_wipe"], tool_overrides: [{ name: "test_wipe", policy: "approve", kind: "auto" }] });
    expect(policySelect("test_wipe")).toHaveValue("approve");
  });

  it("keeps a stored kind when the policy changes", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_list"], tool_overrides: [{ name: "test_list", policy: "inherit", kind: "write" }] });
    await user.selectOptions(policySelect("test_list"), "auto");
    expect(last(onChange).tool_overrides).toEqual([{ name: "test_list", policy: "auto", kind: "write" }]);
  });

  it("removes the override when the policy goes back to the server's and the kind is auto", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_wipe"], tool_overrides: [{ name: "test_wipe", policy: "suggest", kind: "auto" }] });
    expect(policySelect("test_wipe")).toHaveValue("suggest");
    await user.selectOptions(policySelect("test_wipe"), "inherit");
    expect(last(onChange).tool_overrides).toEqual([]);
  });

  it("keeps the choice of tools when only an exception changes", async () => {
    const { onChange, user } = renderPicker({ tool_filter: ["test_wipe", "test_list"] });
    await user.selectOptions(kindSelect("test_odd"), "read");
    expect(last(onChange).tool_filter).toEqual(["test_wipe", "test_list"]);
  });
});
