import { useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { type ConnectionTestResult, type Json, type JsonObject } from "../api";
import { Actions, Button, Meta, Panel, Select, Status, Switch, Table } from "../kit";

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

/** A folded description shows three lines; an open one keeps the line breaks it was written with. */
const FOLDED: CSSProperties = { display: "-webkit-box", WebkitBoxOrient: "vertical", WebkitLineClamp: 3, overflow: "hidden" };
const UNFOLDED: CSSProperties = { whiteSpace: "pre-line" };
const HEAD: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--space-2)", padding: "var(--space-4) var(--space-5)" };
const BETWEEN: CSSProperties = { display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "var(--space-2) var(--space-3)" };
const BESIDE: CSSProperties = { display: "flex", flexWrap: "wrap", alignItems: "center", gap: "var(--space-2) var(--space-3)" };

export function mcpTools(result: ConnectionTestResult | null): ToolRow[] | null {
  const tools = result?.ok ? (result.details?.tools as ToolRow[] | undefined) : undefined;
  return Array.isArray(tools) ? tools : null;
}

export function Description({ text }: { text: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [open, setOpen] = useState(false);
  const [long, setLong] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && !open) setLong(el.scrollHeight > el.clientHeight + 1);
  }, [text, open]);
  if (!text) return null;
  return (
    <>
      <p ref={ref} className="fd-dim fd-wrap" style={open ? UNFOLDED : FOLDED}>{text}</p>
      {(long || open) && (
        <Button variant="link" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "show less" : "show more"}
        </Button>
      )}
    </>
  );
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
      <div style={HEAD}>
        <div style={BETWEEN}>
          <Status tone={on ? "ok" : "off"} hollow={!on}>
            {on} of {tools.length} tools on
          </Status>
          {bulk(tools.map((t) => t.name), "tools of this server")}
        </div>
        <p className="fd-sub">A tool is read only if the server says so (or you set it); anything else is a write and goes through the policy.</p>
      </div>
      <Table dense>
        <thead>
          <tr>
            <th>tool</th>
            <th>treat as</th>
            <th>policy</th>
          </tr>
        </thead>
        {groups.filter((g) => g.rows.length).map(({ kind, rows }) => (
          <tbody key={kind} aria-label={kind === "read" ? "Read tools" : "Write tools"}>
            <tr className="fd-group">
              <td colSpan={3}>
                <div style={BETWEEN}>
                  <span style={BESIDE}>
                    <Status tone={kind === "read" ? "info" : "warn"}>{kind}</Status>
                    <Meta>
                      {rows.filter((t) => chosen.includes(t.name)).length} of {rows.length} on
                    </Meta>
                  </span>
                  {bulk(rows.map((t) => t.name), `${kind} tools`)}
                </div>
              </td>
            </tr>
            {rows.map((t) => {
              const used = chosen.includes(t.name);
              const o = overrideOf(t.name);
              return (
                <tr key={t.name} className={used ? undefined : "fd-off"}>
                  <td style={{ minWidth: "240px" }}>
                    <Switch checked={used} onChange={(v) => setChosen([t.name], v)} label={`Use ${t.name}`} text={t.name} />
                    <Description text={t.description} />
                  </td>
                  <td>
                    <Select style={{ width: "130px" }} aria-label={`Treat ${t.name} as`} value={String(o?.kind ?? "auto")} onChange={(e) => setOverride(t.name, "kind", e.target.value)}>
                      {KINDS.map((k) => <option key={k} value={k}>{k === "auto" ? `auto (${t.read_only === true ? "read" : "write"})` : k}</option>)}
                    </Select>
                  </td>
                  <td>
                    {kind === "write" && (
                      <Select style={{ width: "160px" }} aria-label={`Policy of ${t.name}`} disabled={!used} value={String(o?.policy ?? "inherit")} onChange={(e) => setOverride(t.name, "policy", e.target.value)}>
                        {POLICIES.map((p) => <option key={p} value={p}>{p === "inherit" ? "policy: server's" : p}</option>)}
                      </Select>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        ))}
      </Table>
    </Panel>
  );
}
