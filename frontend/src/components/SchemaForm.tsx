import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { McpToolPicker, mcpTools } from "./McpTools";
import { api, type ConnectionTestResult, type Json, type JsonObject, type JsonSchema } from "../api";
import { Button, Card, Cards, Column, ErrorText, Field as KitField, FormGrid, IconButton, Input, Joined, Live, Meta, Pad, Panel, SectionHead, Segmented, Select, Status, Switch, TestLine, Text, Textarea } from "../kit";

/* Renders a form from the backend's pydantic JSON schema, so every setting the backend
   defines shows up here without UI changes. Field help comes from `description`.

   Which kit control a field gets is decided in one place: `kindOf` names the kind of a schema
   node and `Field` draws it.

     group (an object with properties)     Panel, inset when it sits inside another panel
     optional group (the same, or null)    that Panel with "configure" / "remove"
     list of objects                       a Card per entry (closed until opened, added or in error),
                                           under a heading with "add"
     boolean                               Switch
     boolean or "auto"                     Segmented: auto / on / off
     enum                                  Segmented up to SEGMENTED_MAX values, Select above
     number, number or "auto"              Input (number)
     secret                                Input (password) with "clear"
     duration, text                        Input
     multi-line text                       Textarea, the whole width
     list of texts or numbers              Input, comma-separated
     names with a text each                two Inputs per row
     any other object                      Textarea (JSON)

   Fields stand in the columns of a FormGrid; a group, an optional group and a list of objects
   are blocks of their own between those grids. */

export const SECRET_MASK = "********";
const ACRONYMS: Record<string, string> = {
  url: "URL", llm: "LLM", api: "API", id: "ID", ids: "IDs", gpu: "GPU", ttl: "TTL",
  df: "disk usage", n: "N", mcp: "MCP",
};
/** The most values an enum shows side by side (Segmented); one with more is a Select. */
const SEGMENTED_MAX = 5;
/** The height of a multi-line text box in lines: a few paragraphs without scrolling. */
const MULTILINE_ROWS = 10;

/** The title of a group and the name a control has for assistive tech. */
export function humanize(key: string): string {
  const words = key.split("_").map((w) => ACRONYMS[w] ?? w);
  const text = words.join(" ");
  // A word with its own capitalisation keeps it at the start too.
  const first = words[0];
  const mixedCase = first !== first.toLowerCase() && first[0] === first[0].toLowerCase();
  return mixedCase ? text : text.charAt(0).toUpperCase() + text.slice(1);
}

/** A field's visible label: lowercase like every label, with the case the schema gives a name. */
function fieldLabel(key: string): string {
  return key.split("_").map((w) => ACRONYMS[w]?.toLowerCase() ?? w).join(" ");
}

export type FormContext = {
  defs: Record<string, JsonSchema>;
  errors: Record<string, string>;
  suggestions: Record<string, string[]>;
  /** A line shown above the row at this path (e.g. what the model says about itself). */
  notes?: Record<string, string>;
  /** Paths of rows that do not apply and are left out. */
  hidden?: string[];
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

// ---- the mapping: schema node → kind → kit control ---------------------------

type Kind =
  | { kind: "group" | "switch" | "auto-switch" | "number" | "secret" | "duration" | "text" | "multiline" | "pairs" | "json"; s: JsonSchema }
  | { kind: "optional-group"; s: JsonSchema; inner: JsonSchema }
  | { kind: "objects"; s: JsonSchema; item: JsonSchema }
  | { kind: "auto-number"; s: JsonSchema; numeric: JsonSchema }
  | { kind: "list"; s: JsonSchema; numeric: boolean }
  | { kind: "choice"; s: JsonSchema; options: string[] };

function isNestedObject(s: JsonSchema): boolean {
  return s.type === "object" && !!s.properties;
}

const isNumber = (s: JsonSchema) => s.type === "number" || s.type === "integer";

/** What kind of field a schema node is; `s` is the node the field is drawn from. */
function kindOf(schema: JsonSchema, defs: Record<string, JsonSchema>): Kind {
  const s = resolve(schema, defs);
  if (isNestedObject(s)) return { kind: "group", s };

  if (s.anyOf) {
    const options = s.anyOf.map((x) => resolve(x, defs));
    const nonNull = options.filter((x) => x.type !== "null");
    const nullable = nonNull.length < options.length;
    const auto = nonNull.some((x) => x.const === "auto");
    if (nonNull.some((x) => x.type === "boolean") && auto) return { kind: "auto-switch", s };
    const numeric = nonNull.find(isNumber);
    // a number, or "auto" (= not sent) while the box is empty
    if (numeric && auto) return { kind: "auto-number", s, numeric };
    if (nullable && nonNull.length === 1 && isNestedObject(nonNull[0])) {
      return { kind: "optional-group", s, inner: { ...nonNull[0], description: s.description ?? nonNull[0].description } };
    }
    return kindOf({ ...nonNull[0], description: s.description }, defs);
  }

  if (s.type === "array" && s.items) {
    const item = resolve(s.items, defs);
    return isNestedObject(item) ? { kind: "objects", s, item } : { kind: "list", s, numeric: isNumber(item) };
  }

  if (s.type === "object") {
    const extra = s.additionalProperties;
    return extra && typeof extra === "object" && extra.type === "string" ? { kind: "pairs", s } : { kind: "json", s };
  }

  if (s.enum) return { kind: "choice", s, options: s.enum.map(String) };
  if (s.type === "boolean") return { kind: "switch", s };
  if (isNumber(s)) return { kind: "number", s };
  if (s.format === "password") return { kind: "secret", s };
  if (s.format === "duration") return { kind: "duration", s };
  if (s.format === "multiline") return { kind: "multiline", s };
  return { kind: "text", s };
}

/** A block stands on its own between the grids of fields. */
const isBlock = (k: Kind) => k.kind === "group" || k.kind === "optional-group" || k.kind === "objects";

type FieldProps = {
  name: string;
  schema: JsonSchema;
  value: Json;
  onChange: (value: Json) => void;
  path: string;
  ctx: FormContext;
  /** The field sits inside a panel: its groups are inset panels and its grids need no panel. */
  inset?: boolean;
};

/** All properties of an object schema: fields in columns, groups as blocks between them. */
export function ObjectFields({ schema, value, onChange, path, ctx, inset }: Omit<FieldProps, "name">) {
  const s = resolve(schema, ctx.defs);
  const obj = (value ?? {}) as JsonObject;
  const blocks: ReactNode[] = [];
  let cells: ReactNode[] = [];
  const closeGrid = () => {
    if (cells.length === 0) return;
    const key = `grid-${blocks.length}`;
    blocks.push(inset ? <FormGrid key={key}>{cells}</FormGrid> : <Panel key={key} pad><FormGrid>{cells}</FormGrid></Panel>);
    cells = [];
  };
  for (const [key, prop] of Object.entries(s.properties ?? {})) {
    const fieldPath = path ? `${path}.${key}` : key;
    if (ctx.hidden?.includes(fieldPath)) continue;
    const note = ctx.notes?.[fieldPath];
    const field = <Field key={key} name={key} schema={prop} value={obj[key] ?? null} onChange={(v) => onChange({ ...obj, [key]: v })} path={fieldPath} ctx={ctx} inset={inset} />;
    if (isBlock(kindOf(prop, ctx.defs))) {
      closeGrid();
      if (note) blocks.push(<Meta key={`${key}-note`} block>{note}</Meta>);
      blocks.push(field);
    } else {
      if (note) cells.push(<Meta key={`${key}-note`} block span="all">{note}</Meta>);
      cells.push(field);
    }
  }
  closeGrid();
  return <>{blocks}</>;
}

export function Field({ name, schema, value, onChange, path, ctx, inset }: FieldProps) {
  const k = kindOf(schema, ctx.defs);
  const s = k.s;
  const title = humanize(name);
  const cell = { name, description: s.description, path, ctx };

  switch (k.kind) {
    case "group":
      return (
        <Tested spec={s} value={value}>
          {(buttons, result) => (
            <Group title={title} description={s.description} error={ctx.errors[path]} actions={buttons} below={result} inset={inset}>
              <ObjectFields schema={s} value={value} onChange={onChange} path={path} ctx={ctx} inset />
            </Group>
          )}
        </Tested>
      );

    case "optional-group":
      return <OptionalGroup title={title} spec={s} inner={k.inner} value={value} onChange={onChange} path={path} ctx={ctx} inset={inset} />;

    case "objects":
      return <ListOfObjects title={title} spec={s} item={k.item} value={value} onChange={onChange} path={path} ctx={ctx} inset={inset} />;

    case "switch":
      return (
        <Cell {...cell} group>
          {() => <Switch checked={Boolean(value)} onChange={onChange} label={title} />}
        </Cell>
      );

    case "auto-switch":
      return (
        <Cell {...cell} group>
          {() => (
            <Segmented
              label={title}
              options={[{ value: "auto", label: "auto" }, { value: "on", label: "on" }, { value: "off", label: "off" }]}
              value={value === "auto" ? "auto" : value ? "on" : "off"}
              onChange={(v) => onChange(v === "auto" ? "auto" : v === "on")}
            />
          )}
        </Cell>
      );

    case "choice":
      if (k.options.length <= SEGMENTED_MAX) {
        return (
          <Cell {...cell} group>
            {() => <Segmented label={title} options={k.options.map((o) => ({ value: o, label: o }))} value={String(value ?? "")} onChange={onChange} />}
          </Cell>
        );
      }
      return (
        <Cell {...cell}>
          {(id, invalid) => (
            <Select id={id} invalid={invalid} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
              {k.options.map((o) => (
                <option key={o} value={o}>{o}</option>
              ))}
            </Select>
          )}
        </Cell>
      );

    case "number":
      return (
        <Cell {...cell}>
          {(id, invalid) => (
            <Input
              id={id}
              type="number"
              invalid={invalid}
              value={value === null || value === undefined ? "" : String(value)}
              min={s.minimum}
              max={s.maximum}
              step={s.type === "integer" ? 1 : "any"}
              onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
            />
          )}
        </Cell>
      );

    case "auto-number":
      return (
        <Cell {...cell}>
          {(id, invalid) => (
            <Input
              id={id}
              type="number"
              invalid={invalid}
              placeholder="auto"
              value={value === "auto" || value === null || value === undefined ? "" : String(value)}
              min={k.numeric.minimum}
              max={k.numeric.maximum}
              step={k.numeric.type === "integer" ? 1 : "any"}
              onChange={(e) => onChange(e.target.value === "" ? "auto" : Number(e.target.value))}
            />
          )}
        </Cell>
      );

    case "secret":
      return (
        <Cell {...cell}>
          {(id, invalid) => <SecretInput id={id} invalid={invalid} value={String(value ?? "")} onChange={onChange} />}
        </Cell>
      );

    case "duration": {
      const text = String(value ?? "");
      const valid = !s.pattern || new RegExp(s.pattern).test(text);
      return (
        <Cell {...cell}>
          {(id, invalid) => <Input id={id} invalid={invalid || !valid} value={text} placeholder="e.g. 30s, 5m, 6h, 30d" onChange={(e) => onChange(e.target.value)} />}
        </Cell>
      );
    }

    case "text": {
      const suggestions = ctx.suggestions[path];
      return (
        <Cell {...cell}>
          {(id, invalid) => (
            <>
              <Input id={id} invalid={invalid} value={String(value ?? "")} list={suggestions?.length ? `${id}-list` : undefined} onChange={(e) => onChange(e.target.value)} />
              {suggestions?.length ? (
                <datalist id={`${id}-list`}>
                  {suggestions.map((o) => <option key={o} value={o} />)}
                </datalist>
              ) : null}
            </>
          )}
        </Cell>
      );
    }

    case "multiline":
      return (
        <Cell {...cell} wide>
          {(id, invalid) => <Textarea id={id} rows={MULTILINE_ROWS} invalid={invalid} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />}
        </Cell>
      );

    case "list":
      return (
        <Cell {...cell}>
          {(id, invalid) => <ListInput id={id} invalid={invalid} value={(value as Json[]) ?? []} numeric={k.numeric} onChange={onChange} />}
        </Cell>
      );

    case "pairs":
      return (
        <Cell {...cell} wide group>
          {() => <KeyValueInput value={(value as Record<string, string>) ?? {}} onChange={onChange} />}
        </Cell>
      );

    case "json":
      return (
        <Cell {...cell} wide>
          {(id, invalid) => <JsonInput id={id} invalid={invalid} value={value ?? {}} onChange={onChange} />}
        </Cell>
      );
  }
}

/** One labelled field of a grid: its control, its help line and the server's error for it. */
function Cell({
  name,
  description,
  path,
  ctx,
  wide,
  group,
  children,
}: {
  name: string;
  description?: string;
  path: string;
  ctx: FormContext;
  /** Takes the whole width of the grid. */
  wide?: boolean;
  /** The control is not one input (a switch, a set of buttons, several inputs). */
  group?: boolean;
  children: (id: string, invalid: boolean) => ReactNode;
}) {
  const id = useId();
  const error = ctx.errors[path];
  return (
    <KitField label={fieldLabel(name)} help={description} error={error} group={group} htmlFor={group ? undefined : id} span={wide ? "all" : undefined}>
      {children(id, Boolean(error))}
    </KitField>
  );
}

function Group({ title, description, error, actions, below, inset, children }: {
  title: string; description?: string; error?: string; actions?: ReactNode; below?: ReactNode; inset?: boolean; children: ReactNode;
}) {
  return (
    <Panel inset={inset} title={title} aside={actions}>
      {below}
      <Pad>
        {description && <Text block dim>{description}</Text>}
        {error && <ErrorText>{error}</ErrorText>}
        {children}
      </Pad>
    </Panel>
  );
}

type BlockProps = Omit<FieldProps, "name" | "schema"> & {
  title: string;
  /** The schema node of the field itself: it says whether this is a connection that can be tested. */
  spec: JsonSchema;
};

function OptionalGroup({ title, spec, inner, value, onChange, path, ctx, inset }: BlockProps & { inner: JsonSchema }) {
  const enabled = value !== null && value !== undefined;
  return (
    <Tested spec={enabled ? spec : undefined} value={value}>
      {(buttons, result) => (
        <Group
          title={title}
          description={inner.description}
          error={ctx.errors[path]}
          below={result}
          inset={inset}
          actions={
            enabled ? (
              <>
                {buttons}
                <IconButton icon="remove" label="remove" onClick={() => onChange(null)} />
              </>
            ) : (
              <Button onClick={() => onChange(defaultFor({ ...inner, default: undefined }, ctx.defs))}>configure</Button>
            )
          }
        >
          {enabled ? <ObjectFields schema={inner} value={value} onChange={onChange} path={path} ctx={ctx} inset /> : <Text block dim>Not configured.</Text>}
        </Group>
      )}
    </Tested>
  );
}

function ListOfObjects({ title, spec, item, value, onChange, path, ctx, inset }: BlockProps & { item: JsonSchema }) {
  const list = (value as Json[]) ?? [];
  // the entry just added opens; the others stay as the owner left them
  const [added, setAdded] = useState<number | null>(null);
  const update = (i: number, v: Json) => onChange(list.map((x, j) => (j === i ? v : x)));
  const add = (
    <IconButton
      icon="add"
      label="add"
      onClick={() => {
        setAdded(list.length);
        onChange([...list, defaultFor({ ...item, default: undefined }, ctx.defs)]);
      }}
    />
  );
  const none = list.length === 0 && <Text block dim>None.</Text>;
  const entries = list.length > 0 && (
    <Cards>
      {list.map((entry, i) => {
        const fields = (entry ?? {}) as JsonObject;
        const at = `${path}.${i}`;
        const failed = Object.keys(ctx.errors).some((p) => p === at || p.startsWith(`${at}.`));
        return (
          <Tested key={i} spec={spec} value={entry} index={i} onEdit={(v) => update(i, v)}>
            {(buttons, result) => (
              <Card
                title={fields.name ? String(fields.name) : `#${i + 1}`}
                // an entry with an on/off switch shows it, and one with an address shows that, while closed
                status={typeof fields.enabled === "boolean" && <Status tone={fields.enabled ? "ok" : "off"}>{fields.enabled ? "on" : "off"}</Status>}
                meta={typeof fields.url === "string" && fields.url}
                reveal={added === i || failed}
                aside={
                  <>
                    {buttons}
                    <IconButton small icon="remove" label="remove" onClick={() => onChange(list.filter((_, j) => j !== i))} />
                  </>
                }
                below={result}
              >
                <ObjectFields schema={item} value={entry} onChange={(v) => update(i, v)} path={at} ctx={ctx} inset />
              </Card>
            )}
          </Tested>
        );
      })}
    </Cards>
  );
  // Inside a panel the list is a card of its own; on the page its entries are the cards.
  if (inset) {
    return (
      <Group title={title} description={spec.description} error={ctx.errors[path]} actions={add} inset>
        {none}
        {entries}
      </Group>
    );
  }
  return (
    <>
      <SectionHead title={title} note={spec.description}>
        {add}
      </SectionHead>
      {ctx.errors[path] && <ErrorText>{ctx.errors[path]}</ErrorText>}
      {none}
      {entries}
    </>
  );
}

/** A button's label as the schema writes it, starting lowercase like every button. */
const buttonLabel = (label: string) => label.charAt(0).toLowerCase() + label.slice(1);

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
    <Button key={mode ?? ""} disabled={running !== null} onClick={() => void run(mode)}>
      {running === (mode ?? "") ? "testing…" : label}
    </Button>
  );
  const buttons = (
    <>
      <IconButton small icon="test" label={running === "" ? "testing…" : "test"} disabled={running !== null} onClick={() => void run(null)} />
      {Object.entries(spec?.test_modes ?? {}).map(([mode, label]) => button(mode, buttonLabel(label)))}
    </>
  );
  const tools = kind === "mcp" && onEdit ? mcpTools(result) : null;
  const shown = (
    <Live>
      {running !== null && <TestLine running />}
      {result && <TestLine ok={result.ok}>{result.message}</TestLine>}
      {tools && onEdit && (
        <Pad>
          <McpToolPicker tools={tools} value={value as JsonObject} onChange={onEdit} />
        </Pad>
      )}
    </Live>
  );
  return <>{children(buttons, shown)}</>;
}

function SecretInput({ id, invalid, value, onChange }: { id: string; invalid: boolean; value: string; onChange: (v: Json) => void }) {
  const isStored = value === SECRET_MASK;
  return (
    <Joined>
      <Input
        id={id}
        type="password"
        autoComplete="new-password"
        invalid={invalid}
        value={isStored ? "" : value}
        placeholder={isStored ? "Saved — type to replace" : "Not set"}
        onChange={(e) => onChange(e.target.value === "" && isStored ? SECRET_MASK : e.target.value)}
      />
      {isStored && <Button onClick={() => onChange("")}>clear</Button>}
    </Joined>
  );
}

function ListInput({ id, invalid, value, numeric, onChange }: { id: string; invalid: boolean; value: Json[]; numeric: boolean; onChange: (v: Json) => void }) {
  const [text, setText] = useState(value.join(", "));
  useEffect(() => setText(value.join(", ")), [value.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  const commit = (t: string) => {
    const parts = t.split(/[\s,]+/).filter(Boolean);
    onChange(numeric ? parts.map(Number).filter((n) => !Number.isNaN(n)) : parts);
  };
  return <Input id={id} invalid={invalid} value={text} placeholder="Comma-separated" onChange={(e) => setText(e.target.value)} onBlur={(e) => commit(e.target.value)} />;
}

function KeyValueInput({ value, onChange }: { value: Record<string, string>; onChange: (v: Json) => void }) {
  const rows = Object.entries(value);
  const set = (next: [string, string][]) => onChange(Object.fromEntries(next));
  return (
    <Column gap={2} align="start">
      {rows.map(([k, v], i) => (
        <Joined key={i} width="100%">
          <Input value={k} placeholder="name" onChange={(e) => set(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          <Input value={v} placeholder="value" onChange={(e) => set(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
          <Button onClick={() => set(rows.filter((_, j) => j !== i))}>remove</Button>
        </Joined>
      ))}
      <Button onClick={() => set([...rows, [`query_${rows.length + 1}`, ""]])}>add</Button>
    </Column>
  );
}

function JsonInput({ id, invalid, value, onChange }: { id: string; invalid: boolean; value: Json; onChange: (v: Json) => void }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <Textarea
        id={id}
        rows={4}
        spellCheck={false}
        invalid={invalid || error !== null}
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
      {error && <Text tone="crit">{error}</Text>}
    </>
  );
}
