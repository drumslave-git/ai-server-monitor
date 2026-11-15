import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { McpToolPicker, mcpTools } from "./McpTools";
import { api, type ConnectionTestResult, type Json, type JsonObject, type JsonSchema } from "../api";

/* Renders a form from the backend's pydantic JSON schema, so every setting the backend
   defines shows up here without UI changes. Field help comes from `description`. */

export const SECRET_MASK = "********";
const ACRONYMS: Record<string, string> = {
  url: "URL", llm: "LLM", api: "API", id: "ID", ids: "IDs", gpu: "GPU", ttl: "TTL",
  df: "disk usage", n: "N", mcp: "MCP", promql: "PromQL", qbittorrent: "qBittorrent",
};

export function humanize(key: string): string {
  const words = key.split("_").map((w) => ACRONYMS[w] ?? w);
  const text = words.join(" ");
  // A word with its own capitalisation (qBittorrent, PromQL) keeps it at the start too.
  const first = words[0];
  const mixedCase = first !== first.toLowerCase() && first[0] === first[0].toLowerCase();
  return mixedCase ? text : text.charAt(0).toUpperCase() + text.slice(1);
}

export type FormContext = {
  defs: Record<string, JsonSchema>;
  errors: Record<string, string>;
  suggestions: Record<string, string[]>;
};

export function resolve(schema: JsonSchema, defs: Record<string, JsonSchema>): JsonSchema {
  if (!schema.$ref) return schema;
  const name = schema.$ref.split("/").pop()!;
  const { $ref: _ignored, ...siblings } = schema;
  return { ...resolve(defs[name] ?? {}, defs), ...siblings };
}

export function defaultFor(schema: JsonSchema, defs: Record<string, JsonSchema>): Json {
  const s = resolve(schema, defs);
  if (s.default !== undefined) return structuredClone(s.default);
  if (s.anyOf) return defaultFor(s.anyOf.find((x) => x.type !== "null") ?? {}, defs);
  if (s.type === "object" && s.properties) {
    const out: JsonObject = {};
    for (const [key, prop] of Object.entries(s.properties)) out[key] = defaultFor(prop, defs);
    return out;
  }
  if (s.type === "object") return {};
  if (s.type === "array") return [];
  if (s.type === "boolean") return false;
  if (s.type === "integer" || s.type === "number") return 0;
  return "";
}

type FieldProps = {
  name: string;
  schema: JsonSchema;
  value: Json;
  onChange: (value: Json) => void;
  path: string;
  ctx: FormContext;
};

/** All properties of an object schema, as rows / nested groups. */
export function ObjectFields({ schema, value, onChange, path, ctx }: Omit<FieldProps, "name">) {
  const s = resolve(schema, ctx.defs);
  const obj = (value ?? {}) as JsonObject;
  return (
    <>
      {Object.entries(s.properties ?? {}).map(([key, prop]) => (
        <Field
          key={key}
          name={key}
          schema={prop}
          value={obj[key] ?? null}
          onChange={(v) => onChange({ ...obj, [key]: v })}
          path={path ? `${path}.${key}` : key}
          ctx={ctx}
        />
      ))}
    </>
  );
}

function isNestedObject(s: JsonSchema): boolean {
  return s.type === "object" && !!s.properties;
}

export function Field(props: FieldProps) {
  const { name, schema, value, onChange, path, ctx } = props;
  const s = resolve(schema, ctx.defs);
  const label = humanize(name);

  // Nested settings group (e.g. one collector) → titled group.
  if (isNestedObject(s)) {
    return (
      <Tested spec={s} value={value}>
        {(buttons, result) => (
          <Group title={label} description={s.description} error={ctx.errors[path]} actions={buttons} below={result}>
            <ObjectFields schema={s} value={value} onChange={onChange} path={path} ctx={ctx} />
          </Group>
        )}
      </Tested>
    );
  }

  if (s.anyOf) {
    const options = s.anyOf.map((x) => resolve(x, ctx.defs));
    const nonNull = options.filter((x) => x.type !== "null");
    const nullable = nonNull.length < options.length;
    if (nonNull.some((x) => x.type === "boolean") && nonNull.some((x) => x.const === "auto")) {
      return (
        <Row {...props} label={label} description={s.description}>
          {(id) => (
            <select
              id={id}
              value={value === "auto" ? "auto" : value ? "on" : "off"}
              onChange={(e) => onChange(e.target.value === "auto" ? "auto" : e.target.value === "on")}
            >
              <option value="auto">Auto</option>
              <option value="on">On</option>
              <option value="off">Off</option>
            </select>
          )}
        </Row>
      );
    }
    if (nullable && nonNull.length === 1 && isNestedObject(nonNull[0])) {
      return <OptionalGroup {...props} label={label} inner={{ ...nonNull[0], description: s.description ?? nonNull[0].description }} />;
    }
    return <Field {...props} schema={{ ...nonNull[0], description: s.description }} />;
  }

  if (s.type === "array" && s.items) {
    const item = resolve(s.items, ctx.defs);
    if (isNestedObject(item)) return <ListOfObjects {...props} label={label} item={item} />;
    return (
      <Row {...props} label={label} description={s.description}>
        {(id) => <ListInput id={id} value={(value as Json[]) ?? []} numeric={item.type === "integer" || item.type === "number"} onChange={onChange} />}
      </Row>
    );
  }

  if (s.type === "object") {
    const extra = s.additionalProperties;
    if (extra && typeof extra === "object" && extra.type === "string") {
      return (
        <Row {...props} label={label} description={s.description} wide>
          {() => <KeyValueInput value={(value as Record<string, string>) ?? {}} onChange={onChange} />}
        </Row>
      );
    }
    return (
      <Row {...props} label={label} description={s.description} wide>
        {(id) => <JsonInput id={id} value={value ?? {}} onChange={onChange} />}
      </Row>
    );
  }

  return (
    <Row {...props} label={label} description={s.description}>
      {(id) => <Scalar id={id} schema={s} value={value} onChange={onChange} suggestions={ctx.suggestions[path]} />}
    </Row>
  );
}

function Row({
  label,
  description,
  path,
  ctx,
  wide,
  children,
}: FieldProps & { label: string; description?: string; wide?: boolean; children: (id: string) => ReactNode }) {
  const id = useId();
  const error = ctx.errors[path];
  return (
    <div className={`field${wide ? " field-wide" : ""}${error ? " has-error" : ""}`}>
      <div className="field-label">
        <label htmlFor={id}>{label}</label>
        {description && <p className="help">{description}</p>}
      </div>
      <div className="field-control">
        {children(id)}
        {error && <p className="error">{error}</p>}
      </div>
    </div>
  );
}

function Group({ title, description, error, actions, below, children }: {
  title: string; description?: string; error?: string; actions?: ReactNode; below?: ReactNode; children: ReactNode;
}) {
  return (
    <section className="group">
      <header className="group-head">
        <div>
          <h3>{title}</h3>
          {description && <p className="help">{description}</p>}
        </div>
        {actions}
      </header>
      {below}
      {error && <p className="error">{error}</p>}
      <div className="group-body">{children}</div>
    </section>
  );
}

function OptionalGroup(props: FieldProps & { label: string; inner: JsonSchema }) {
  const { label, inner, value, onChange, path, ctx } = props;
  const enabled = value !== null && value !== undefined;
  return (
    <Tested spec={enabled ? resolve(props.schema, ctx.defs) : undefined} value={value}>
      {(buttons, result) => (
        <Group
          title={label}
          description={inner.description}
          error={ctx.errors[path]}
          below={result}
          actions={
            enabled ? (
              <div className="head-actions">
                {buttons}
                <button type="button" className="btn btn-quiet" onClick={() => onChange(null)}>Remove</button>
              </div>
            ) : (
              <button type="button" className="btn" onClick={() => onChange(defaultFor({ ...inner, default: undefined }, ctx.defs))}>
                Configure
              </button>
            )
          }
        >
          {enabled ? <ObjectFields schema={inner} value={value} onChange={onChange} path={path} ctx={ctx} /> : <p className="muted">Not configured.</p>}
        </Group>
      )}
    </Tested>
  );
}

function ListOfObjects(props: FieldProps & { label: string; item: JsonSchema }) {
  const { label, item, value, onChange, path, ctx } = props;
  const s = resolve(props.schema, ctx.defs);
  const list = (value as Json[]) ?? [];
  const update = (i: number, v: Json) => onChange(list.map((x, j) => (j === i ? v : x)));
  return (
    <Group
      title={label}
      description={s.description}
      error={ctx.errors[path]}
      actions={
        <button type="button" className="btn" onClick={() => onChange([...list, defaultFor({ ...item, default: undefined }, ctx.defs)])}>
          Add
        </button>
      }
    >
      {list.length === 0 && <p className="muted">None.</p>}
      {list.map((entry, i) => (
        <Tested key={i} spec={s} value={entry} index={i} onEdit={(v) => update(i, v)}>
          {(buttons, result) => (
            <div className="list-item">
              <div className="list-item-head">
                <strong>{(entry as JsonObject)?.name ? String((entry as JsonObject).name) : `#${i + 1}`}</strong>
                <div className="head-actions">
                  {buttons}
                  <button type="button" className="btn btn-quiet" onClick={() => onChange(list.filter((_, j) => j !== i))}>
                    Remove
                  </button>
                </div>
              </div>
              {result}
              <ObjectFields schema={item} value={entry} onChange={(v) => update(i, v)} path={`${path}.${i}`} ctx={ctx} />
            </div>
          )}
        </Tested>
      ))}
    </Group>
  );
}

/** Test buttons + inline result for a connection. A node is a connection when its schema has
    a `test` extra; any other node gets nothing extra. The values tested are the ones in the
    form right now (saved or not); secrets that are still masked are filled in by the server
    from the stored ones. */
function Tested({ spec, value, index, onEdit, children }: {
  spec?: JsonSchema; value: Json; index?: number; onEdit?: (v: Json) => void; children: (buttons: ReactNode, result: ReactNode) => ReactNode;
}) {
  const kind = spec?.test;
  const [running, setRunning] = useState<string | null>(null);
  const [result, setResult] = useState<ConnectionTestResult | null>(null);
  // Choosing tools of an MCP server must not discard the test result that lists them.
  const tested = kind === "mcp" && value && typeof value === "object" && !Array.isArray(value)
    ? { ...value, tool_filter: undefined, tool_overrides: undefined }
    : value;
  const snapshot = JSON.stringify(tested);
  const requestId = useRef(0);
  useEffect(() => {
    requestId.current++; // an edit makes an older answer (or a test still running) stale
    setResult(null);
    setRunning(null);
  }, [snapshot]);
  if (!kind) return <>{children(null, null)}</>;

  const run = async (mode: string | null) => {
    const mine = ++requestId.current;
    setRunning(mode ?? "");
    setResult(null);
    const query = new URLSearchParams();
    if (mode) query.set("mode", mode);
    if (index !== undefined) query.set("index", String(index));
    let outcome: ConnectionTestResult;
    try {
      const qs = query.toString();
      outcome = await api.post<ConnectionTestResult>(`/api/settings/test/${kind}${qs ? `?${qs}` : ""}`, value);
    } catch (e) {
      outcome = { ok: false, message: `The test could not run: ${(e as Error).message}`, details: {} };
    }
    if (mine !== requestId.current) return;
    setResult(outcome);
    setRunning(null);
  };
  const button = (mode: string | null, label: string) => (
    <button type="button" className="btn btn-test" disabled={running !== null} onClick={() => void run(mode)}>
      {running === (mode ?? "") && <span className="spinner" aria-hidden="true" />}
      {running === (mode ?? "") ? "Testing…" : label}
    </button>
  );
  const buttons = (
    <div className="test-buttons">
      {button(null, "Test")}
      {Object.entries(spec?.test_modes ?? {}).map(([mode, label]) => (
        <span key={mode}>{button(mode, label)}</span>
      ))}
    </div>
  );
  const shown = (
    <div className="test-result" role="status" aria-live="polite">
      {result && (
        <p className={result.ok ? "ok" : "error"}>
          <strong>{result.ok ? "✓ " : "✕ "}</strong>
          {result.message}
        </p>
      )}
      {kind === "mcp" && onEdit && mcpTools(result) && (
        <McpToolPicker tools={mcpTools(result)!} value={value as JsonObject} onChange={onEdit} />
      )}
    </div>
  );
  return <>{children(buttons, shown)}</>;
}

function Scalar({ id, schema, value, onChange, suggestions }: {
  id: string; schema: JsonSchema; value: Json; onChange: (v: Json) => void; suggestions?: string[];
}) {
  const listId = useId();
  if (schema.enum) {
    return (
      <select id={id} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
        {schema.enum.map((opt) => (
          <option key={String(opt)} value={String(opt)}>{String(opt)}</option>
        ))}
      </select>
    );
  }
  if (schema.type === "boolean") {
    return (
      <label className="switch">
        <input id={id} type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        <span aria-hidden="true" />
      </label>
    );
  }
  if (schema.type === "integer" || schema.type === "number") {
    return (
      <input
        id={id}
        type="number"
        className="input-short"
        value={value === null || value === undefined ? "" : String(value)}
        min={schema.minimum}
        max={schema.maximum}
        step={schema.type === "integer" ? 1 : "any"}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    );
  }
  if (schema.format === "password") return <SecretInput id={id} value={String(value ?? "")} onChange={onChange} />;
  if (schema.format === "duration") {
    const text = String(value ?? "");
    const valid = !schema.pattern || new RegExp(schema.pattern).test(text);
    return (
      <input
        id={id}
        className={`input-short${valid ? "" : " invalid"}`}
        value={text}
        placeholder="e.g. 30s, 5m, 6h, 30d"
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <>
      <input id={id} value={String(value ?? "")} list={suggestions?.length ? listId : undefined} onChange={(e) => onChange(e.target.value)} />
      {suggestions?.length ? (
        <datalist id={listId}>
          {suggestions.map((s) => <option key={s} value={s} />)}
        </datalist>
      ) : null}
    </>
  );
}

function SecretInput({ id, value, onChange }: { id: string; value: string; onChange: (v: Json) => void }) {
  const isStored = value === SECRET_MASK;
  return (
    <div className="secret">
      <input
        id={id}
        type="password"
        autoComplete="new-password"
        value={isStored ? "" : value}
        placeholder={isStored ? "Saved — type to replace" : "Not set"}
        onChange={(e) => onChange(e.target.value === "" && isStored ? SECRET_MASK : e.target.value)}
      />
      {isStored && (
        <button type="button" className="btn btn-quiet" onClick={() => onChange("")}>Clear</button>
      )}
    </div>
  );
}

function ListInput({ id, value, numeric, onChange }: { id: string; value: Json[]; numeric: boolean; onChange: (v: Json) => void }) {
  const [text, setText] = useState(value.join(", "));
  useEffect(() => setText(value.join(", ")), [value.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  const commit = (t: string) => {
    const parts = t.split(/[\s,]+/).filter(Boolean);
    onChange(numeric ? parts.map(Number).filter((n) => !Number.isNaN(n)) : parts);
  };
  return <input id={id} value={text} placeholder="Comma-separated" onChange={(e) => setText(e.target.value)} onBlur={(e) => commit(e.target.value)} />;
}

function KeyValueInput({ value, onChange }: { value: Record<string, string>; onChange: (v: Json) => void }) {
  const rows = Object.entries(value);
  const set = (next: [string, string][]) => onChange(Object.fromEntries(next));
  return (
    <div className="kv">
      {rows.map(([k, v], i) => (
        <div className="kv-row" key={i}>
          <input value={k} placeholder="name" onChange={(e) => set(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          <input value={v} placeholder="value" onChange={(e) => set(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
          <button type="button" className="btn btn-quiet" onClick={() => set(rows.filter((_, j) => j !== i))}>Remove</button>
        </div>
      ))}
      <button type="button" className="btn" onClick={() => set([...rows, [`query_${rows.length + 1}`, ""]])}>Add</button>
    </div>
  );
}

function JsonInput({ id, value, onChange }: { id: string; value: Json; onChange: (v: Json) => void }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <textarea
        id={id}
        rows={4}
        spellCheck={false}
        className={`mono${error ? " invalid" : ""}`}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(JSON.parse(e.target.value || "{}"));
            setError(null);
          } catch {
            setError("Not valid JSON yet");
          }
        }}
      />
      {error && <p className="error">{error}</p>}
    </>
  );
}
