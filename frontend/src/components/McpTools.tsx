import { type ConnectionTestResult, type Json, type JsonObject } from "../api";

/* Tool picker of one MCP server: shown under the server's Test result. The list comes from the
   test (what the server offers); the choice is stored in the server's `tool_filter`, the
   per-tool exceptions in `tool_overrides`. Small models cope badly with many tools, so nothing
   is chosen by default. */

type ToolRow = {
  name: string;
  title?: string | null;
  description: string;
  read_only?: boolean | null;
  kind: "read" | "write";
  schema_tokens: number;
};

const POLICIES = ["inherit", "off", "suggest", "approve", "auto"];
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

  const toggle = (name: string, on: boolean) =>
    onChange({ ...value, tool_filter: on ? [...chosen, name] : chosen.filter((n) => n !== name) });

  const setOverride = (name: string, field: "policy" | "kind", v: string) => {
    const current = overrideOf(name) ?? { name, policy: "inherit", kind: "auto" };
    const next = { ...current, [field]: v } as JsonObject;
    const rest = overrides.filter((o) => o.name !== name);
    const neutral = next.policy === "inherit" && next.kind === "auto";
    onChange({ ...value, tool_overrides: (neutral ? rest : [...rest, next]) as Json });
  };

  const tokens = tools.filter((t) => chosen.includes(t.name)).reduce((sum, t) => sum + t.schema_tokens, 0);
  return (
    <div className="mcp-tools">
      <p className="help">
        Tick the tools the chat may use ({chosen.length} of {tools.length} chosen, about {tokens} tokens of tool
        definitions). Above the "Max inline tool tokens" setting the chat gets a generic list/call pair instead.
        A tool counts as read-only only if the server says so; anything else is a write and goes through the policy.
      </p>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr><th>Use</th><th>Tool</th><th>Policy</th><th>Treat as</th><th>Tokens</th></tr>
          </thead>
          <tbody>
            {tools.map((t) => {
              const on = chosen.includes(t.name);
              const o = overrideOf(t.name);
              return (
                <tr key={t.name} className={on ? "" : "mcp-off"}>
                  <td>
                    <input type="checkbox" aria-label={`Use ${t.name}`} checked={on} onChange={(e) => toggle(t.name, e.target.checked)} />
                  </td>
                  <td>
                    <strong>{t.name}</strong>{" "}
                    <span className={t.read_only ? "tag tag-read" : "tag tag-write"}>
                      {t.read_only ? "read-only" : t.read_only === false ? "writes" : "may write"}
                    </span>
                    {t.description && <div className="details">{t.description}</div>}
                  </td>
                  <td>
                    <select aria-label={`Policy of ${t.name}`} disabled={!on} value={String(o?.policy ?? "inherit")} onChange={(e) => setOverride(t.name, "policy", e.target.value)}>
                      {POLICIES.map((p) => <option key={p} value={p}>{p}</option>)}
                    </select>
                  </td>
                  <td>
                    <select aria-label={`Treat ${t.name} as`} disabled={!on} value={String(o?.kind ?? "auto")} onChange={(e) => setOverride(t.name, "kind", e.target.value)}>
                      {KINDS.map((k) => <option key={k} value={k}>{k === "auto" ? `auto (${t.read_only ? "read" : "write"})` : k}</option>)}
                    </select>
                  </td>
                  <td>{t.schema_tokens}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
