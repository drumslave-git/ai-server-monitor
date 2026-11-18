import { useLayoutEffect, useRef, useState } from "react";
import { type ConnectionTestResult, type Json, type JsonObject } from "../api";

/* Tool picker of one MCP server: shown under the server's Test result. The list comes from the
   test (what the server offers); the choice is stored in the server's `tool_filter`, the
   per-tool exceptions in `tool_overrides`. Tools are grouped by what they do (Read / Write,
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
      <p ref={ref} className={`mcp-desc${open ? "" : " clamp"}`}>{text}</p>
      {(long || open) && (
        <button type="button" className="link-btn small" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show more"}
        </button>
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
    <span className="mcp-bulk">
      <button type="button" className="btn btn-sm btn-quiet" disabled={names.every((n) => chosen.includes(n))}
        onClick={() => setChosen(names, true)} aria-label={`Select all ${label}`}>Select all</button>
      <button type="button" className="btn btn-sm btn-quiet" disabled={!names.some((n) => chosen.includes(n))}
        onClick={() => setChosen(names, false)} aria-label={`Deselect all ${label}`}>Deselect all</button>
    </span>
  );

  const groups = (["read", "write"] as const).map((kind) => ({ kind, rows: tools.filter((t) => kindOf(t) === kind) }));
  return (
    <div className="mcp-tools">
      <div className="mcp-head">
        <p className="help">
          {chosen.filter((n) => tools.some((t) => t.name === n)).length} of {tools.length} tools on. A tool is read
          only if the server says so (or you set it); anything else is a write and goes through the policy.
        </p>
        {bulk(tools.map((t) => t.name), "tools of this server")}
      </div>
      {groups.filter((g) => g.rows.length).map(({ kind, rows }) => (
        <section key={kind} className="mcp-group" aria-label={kind === "read" ? "Read tools" : "Write tools"}>
          <header className="mcp-group-head">
            <h4>
              {kind === "read" ? "Read" : "Write"}
              <span className="muted"> · {rows.filter((t) => chosen.includes(t.name)).length} of {rows.length} on</span>
            </h4>
            {bulk(rows.map((t) => t.name), `${kind} tools`)}
          </header>
          <div className="mcp-row mcp-cols" aria-hidden>
            <span />
            <span>Tool</span>
            <span className="mcp-controls">
              <span>Treat as</span>
              {kind === "write" && <span>Policy</span>}
            </span>
          </div>
          {rows.map((t) => {
            const on = chosen.includes(t.name);
            const o = overrideOf(t.name);
            return (
              <div key={t.name} className={`mcp-row${on ? "" : " mcp-off"}`}>
                <input type="checkbox" aria-label={`Use ${t.name}`} checked={on} onChange={(e) => setChosen([t.name], e.target.checked)} />
                <div className="mcp-main">
                  <span className="mcp-name">{t.name}</span>
                  <Description text={t.description} />
                </div>
                <div className="mcp-controls">
                  <select aria-label={`Treat ${t.name} as`} value={String(o?.kind ?? "auto")} onChange={(e) => setOverride(t.name, "kind", e.target.value)}>
                      {KINDS.map((k) => <option key={k} value={k}>{k === "auto" ? `auto (${t.read_only === true ? "read" : "write"})` : k}</option>)}
                  </select>
                  {kind === "write" && (
                    <select aria-label={`Policy of ${t.name}`} disabled={!on} value={String(o?.policy ?? "inherit")} onChange={(e) => setOverride(t.name, "policy", e.target.value)}>
                      {POLICIES.map((p) => <option key={p} value={p}>{p === "inherit" ? "policy: server's" : p}</option>)}
                    </select>
                  )}
                </div>
              </div>
            );
          })}
        </section>
      ))}
    </div>
  );
}
