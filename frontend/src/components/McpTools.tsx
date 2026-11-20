import { type ConnectionTestResult, type Json, type JsonObject } from "../api";
import { Actions, Button, Clamp, Inline, Meta, Pad, Panel, Select, Status, Switch, Table, Td, Tr } from "../kit";

/* Tool picker of one MCP server: shown under the server's Test result. The list comes from the
   test (what the server offers); the choice is stored in the server's `tool_filter`, the
   per-tool exceptions in `tool_overrides`. Tools are grouped by what they do (read / write,
   after the owner's override); long descriptions fold to a few lines. */

type ToolRow = {
  name: string;
  title?: string | null;
  description: string;
  read_only?: boolean | null;
};

const POLICIES = ["inherit", "suggest", "approve", "auto"];
const KINDS = ["auto", "read", "write"];

export function mcpTools(result: ConnectionTestResult | null): ToolRow[] | null {
  const tools = result?.ok ? (result.details?.tools as ToolRow[] | undefined) : undefined;
  return Array.isArray(tools) ? tools : null;
}

export function McpToolPicker({ tools, value, onChange }: {
  tools: ToolRow[]; value: JsonObject; onChange: (v: JsonObject) => void;
}) {
  const chosen = ((value.tool_filter as string[] | undefined) ?? []).filter((n) => typeof n === "string");
  const overrides = ((value.tool_overrides as JsonObject[] | undefined) ?? []).filter((o) => o && typeof o === "object");
  const overrideOf = (name: string) => overrides.find((o) => o.name === name);
  const kindOf = (t: ToolRow) => {
    const k = String(overrideOf(t.name)?.kind ?? "auto");
    return k === "read" || k === "write" ? k : t.read_only === true ? "read" : "write";
  };

  const setChosen = (names: string[], on: boolean) => {
    const rest = chosen.filter((n) => !names.includes(n));
    onChange({ ...value, tool_filter: on ? [...rest, ...names] : rest });
  };

  const setOverride = (name: string, field: "policy" | "kind", v: string) => {
    const current = overrideOf(name) ?? { name, policy: "inherit", kind: "auto" };
    const next = { ...current, [field]: v } as JsonObject;
    const rest = overrides.filter((o) => o.name !== name);
    const neutral = next.policy === "inherit" && next.kind === "auto";
    onChange({ ...value, tool_overrides: (neutral ? rest : [...rest, next]) as Json });
  };

  const bulk = (names: string[], label: string) => (
    <Actions>
      <Button disabled={names.every((n) => chosen.includes(n))} onClick={() => setChosen(names, true)} aria-label={`Select all ${label}`}>
        select all
      </Button>
      <Button disabled={!names.some((n) => chosen.includes(n))} onClick={() => setChosen(names, false)} aria-label={`Deselect all ${label}`}>
        deselect all
      </Button>
    </Actions>
  );

  const groups = (["read", "write"] as const).map((kind) => ({ kind, rows: tools.filter((t) => kindOf(t) === kind) }));
  const on = chosen.filter((n) => tools.some((t) => t.name === n)).length;
  return (
    <Panel inset>
      <Pad gap={2}>
        <Inline between gap={2}>
          <Status tone={on ? "ok" : "off"} hollow={!on}>
            {on} of {tools.length} tools on
          </Status>
          {bulk(tools.map((t) => t.name), "tools of this server")}
        </Inline>
        <Meta block>A tool is read only if the server says so (or you set it); anything else is a write and goes through the policy.</Meta>
      </Pad>
      <Table dense stack head={[{ label: "tool", minWidth: "240px" }, "treat as", "policy"]}>
        {groups.filter((g) => g.rows.length).map(({ kind, rows }) => (
          <tbody key={kind} aria-label={kind === "read" ? "Read tools" : "Write tools"}>
            <Tr group>
              <Td colSpan={3}>
                <Inline between gap={2}>
                  <Inline gap={2}>
                    <Status tone={kind === "read" ? "info" : "warn"}>{kind}</Status>
                    <Meta>
                      {rows.filter((t) => chosen.includes(t.name)).length} of {rows.length} on
                    </Meta>
                  </Inline>
                  {bulk(rows.map((t) => t.name), `${kind} tools`)}
                </Inline>
              </Td>
            </Tr>
            {rows.map((t) => {
              const used = chosen.includes(t.name);
              const o = overrideOf(t.name);
              return (
                <Tr key={t.name} off={!used}>
                  <Td>
                    <Switch checked={used} onChange={(v) => setChosen([t.name], v)} label={`Use ${t.name}`} text={t.name} />
                    <Clamp>{t.description}</Clamp>
                  </Td>
                  <Td>
                    <Select width="130px" aria-label={`Treat ${t.name} as`} value={String(o?.kind ?? "auto")} onChange={(e) => setOverride(t.name, "kind", e.target.value)}>
                      {KINDS.map((k) => <option key={k} value={k}>{k === "auto" ? `auto (${t.read_only === true ? "read" : "write"})` : k}</option>)}
                    </Select>
                  </Td>
                  <Td>
                    {kind === "write" && (
                      <Select width="160px" aria-label={`Policy of ${t.name}`} disabled={!used} value={String(o?.policy ?? "inherit")} onChange={(e) => setOverride(t.name, "policy", e.target.value)}>
                        {POLICIES.map((p) => <option key={p} value={p}>{p === "inherit" ? "policy: server's" : p}</option>)}
                      </Select>
                    )}
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        ))}
      </Table>
    </Panel>
  );
}
