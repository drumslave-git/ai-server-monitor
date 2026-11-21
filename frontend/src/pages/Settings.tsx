import { useEffect, useId, useMemo, useRef, useState } from "react";
import { api, ApiError, type FieldError, type Json, type JsonObject, type JsonSchema, type LlmTestResult, type ModelInfo, type SettingsDoc } from "../api";
import { ObjectFields, resolve, type FieldGroup, type FormContext } from "../components/SchemaForm";
import { Button, Count, ErrorText, IconButton, type IconName, List, ListItem, ListPicker, Live, Meta, Pad, PageHeader, Pane, Panes, Panel, SectionHead, setRootFontSize, Status, TestLine, Text } from "../kit";

/** What each settings section is, as an icon (the sections that are also pages use those pages' icons). */
const SECTION_ICONS: Record<string, IconName> = {
  general: "general",
  llm: "model",
  worker: "worker",
  chat: "chat",
  jobs: "jobs",
  storage: "storage",
  logs: "logs",
  approvals: "approvals",
  outputs: "outputs",
  mcp: "mcp",
};

function modelDefaultsLine(info: ModelInfo): string {
  const p = info.parameters;
  const sampling = ["temperature", "top_k", "top_p"].filter((k) => p[k] !== undefined).map((k) => `${k.replace("_", " ")} ${p[k]}`);
  const parts = [`Model default: ${sampling.length ? sampling.join(", ") : "no sampling values set"}`];
  if (info.capabilities.length) parts.push(info.capabilities.filter((c) => c !== "completion").join(", "));
  if (info.context_length) parts.push(`context ${info.context_length}`);
  return parts.filter(Boolean).join(" · ");
}

/** What the model of a profile reports about itself (Ollama only), re-read when its endpoint or
    model changes. The worker is asked about as the agents use it: with the main model's endpoint
    when its own is empty, and the main model when it has none. */
function useModelInfo(profile: "main" | "worker", model: JsonObject): ModelInfo | null {
  const [info, setInfo] = useState<ModelInfo | null>(null);
  const latest = useRef(model);
  latest.current = model;
  const baseUrl = String(model.base_url ?? "");
  const name = String(model.model ?? "");
  useEffect(() => {
    setInfo(null);
    if (profile === "main" && (!baseUrl.trim() || !name.trim())) return;
    let stale = false;
    const timer = setTimeout(() => {
      api.post<ModelInfo>(`/api/settings/llm-model-info?profile=${profile}`, latest.current).then(
        (r) => { if (!stale) setInfo(r); },
        () => undefined,
      );
    }, 500);
    return () => { stale = true; clearTimeout(timer); };
  }, [profile, baseUrl, name]);
  return info;
}

/** The groups of a model's fields, the same for the main and the worker model. */
function modelGroups(profile: "main" | "worker", info: ModelInfo | null): FieldGroup[] {
  const inherits = profile === "worker" ? ["Empty base URL and API key = the main model's. Empty model = the main model does the sub-tasks too."] : [];
  return [
    { title: "Connection", fields: ["base_url", "api_key", "model"], notes: [...inherits, ...(info?.available ? [modelDefaultsLine(info)] : [])] },
    { title: "Output", fields: ["max_output_tokens", "reasoning_effort"] },
    { title: "Sampling", fields: ["temperature", "top_p", "top_k"] },
    { title: "Requests", fields: ["timeout", "max_retries"] },
    { title: "Advanced", fields: ["structured_output", "extra_body"], wide: ["extra_body"] },
  ];
}

/** The groups of the other sections that have more than a handful of fields. */
const GROUPS: Record<string, FieldGroup[]> = {
  general: [
    { title: "Service", fields: ["log_level", "timezone", "docker_url"] },
    { title: "Language and display", fields: ["language", "root_font_size"] },
    { title: "Instructions", fields: ["instructions"], wide: ["instructions"] },
  ],
  chat: [
    { title: "Steps", fields: ["max_steps", "worker_max_steps"] },
    { title: "What the model sees", fields: ["history_messages", "max_tool_result_chars"] },
    { title: "Time limits", fields: ["command_timeout", "tool_timeout"] },
  ],
  logs: [
    { title: "Recording", fields: ["llm_trace", "store_bodies", "keep_app_log"] },
    { title: "Retention", fields: ["retention", "max_total_mb"] },
  ],
};

/** The form of a model: the main one and the worker one are the same groups of the same fields;
    the worker only has its empty values mean "the main model's". */
function ModelForm({ profile, schema, value, onChange, ctx, onModels }: {
  profile: "main" | "worker";
  schema: JsonSchema;
  value: JsonObject;
  onChange: (v: Json) => void;
  ctx: FormContext;
  onModels: (m: string[]) => void;
}) {
  const section = profile === "main" ? "llm" : "worker";
  const info = useModelInfo(profile, value);
  const form: FormContext = {
    ...ctx,
    groups: { ...ctx.groups, [section]: modelGroups(profile, info) },
    placeholders: profile === "worker" ? { "worker.base_url": "main model's endpoint" } : undefined,
    // a model that does not report "thinking" has no reasoning to set
    hidden: info?.available && !info.capabilities.includes("thinking") ? [`${section}.reasoning_effort`] : [],
  };
  return (
    <>
      <ObjectFields schema={schema} value={value} onChange={onChange} path={section} ctx={form} />
      <LlmTest key={section} profile={profile} llm={value} onModels={onModels} />
    </>
  );
}

export function SettingsPage() {
  const [doc, setDoc] = useState<SettingsDoc | null>(null);
  const [values, setValues] = useState<JsonObject | null>(null);
  const [section, setSection] = useState<string>(() => location.hash.split("/")[2] || "llm");
  // #/settings/mcp/<server>: a link to one entry of the section; its card opens (the address then drops to the section)
  const [focusEntry, setFocusEntry] = useState<string | undefined>(() => {
    const name = location.hash.split("/")[3];
    return name ? decodeURIComponent(name) : undefined;
  });
  const linkedSection = useRef(section);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const formId = useId();

  useEffect(() => {
    api.get<SettingsDoc>("/api/settings").then(
      (d) => {
        setDoc(d);
        setValues(d.values);
      },
      (e: Error) => setLoadError(e.message),
    );
  }, []);

  useEffect(() => {
    history.replaceState(null, "", `#/settings/${section}`);
    if (section !== linkedSection.current) setFocusEntry(undefined);
    window.scrollTo({ top: 0 });
  }, [section]);

  useEffect(() => {
    if (status?.kind !== "ok") return;
    const timer = setTimeout(() => setStatus(null), 4000);
    return () => clearTimeout(timer);
  }, [status]);

  const dirty = useMemo(() => !!doc && JSON.stringify(values) !== JSON.stringify(doc.values), [doc, values]);

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  if (loadError) {
    return (
      <>
        <PageHeader title="Settings" />
        <ErrorText>Could not load settings: {loadError}</ErrorText>
      </>
    );
  }
  if (!doc || !values) {
    return (
      <>
        <PageHeader title="Settings" />
        <Meta>Loading…</Meta>
      </>
    );
  }

  const defs = doc.schema.$defs ?? {};
  const sectionSchema = resolve(doc.schema.properties![section], defs);
  const errorCount = (key: string) => Object.keys(errors).filter((p) => p === key || p.startsWith(`${key}.`)).length;
  const ctx: FormContext = {
    defs,
    errors,
    suggestions: { "llm.model": models, "worker.model": models },
    groups: GROUPS,
    focusEntry,
  };

  const update = (key: string, v: Json) => {
    setValues({ ...values, [key]: v });
    if (status) setStatus(null);
  };

  const save = async () => {
    setSaving(true);
    setStatus(null);
    try {
      const result = await api.put<{ values: JsonObject }>("/api/settings", values);
      setDoc({ ...doc, values: result.values });
      setValues(result.values);
      setRootFontSize(Number((result.values.general as JsonObject).root_font_size));
      setErrors({});
      setStatus({ kind: "ok", text: "Saved and applied." });
    } catch (e) {
      if (e instanceof ApiError && e.status === 422 && Array.isArray(e.detail)) {
        const map: Record<string, string> = {};
        for (const err of e.detail as FieldError[]) map[err.loc.join(".")] = err.msg;
        setErrors(map);
        const first = (e.detail as FieldError[])[0]?.loc[0];
        if (first && first in doc.sections) setSection(first);
        setStatus({ kind: "error", text: "Some values are invalid — see the highlighted fields." });
      } else {
        setStatus({ kind: "error", text: `Save failed: ${(e as Error).message}` });
      }
    } finally {
      setSaving(false);
    }
  };


  /** Discard and save, once, in the page's head (it stays in view), with what they did or what waits beside them. */
  const buttons = (
    <>
      {status ? <Status tone={status.kind === "ok" ? "ok" : "crit"}>{status.text}</Status> : dirty && <Status tone="warn">unsaved changes</Status>}
      <IconButton icon="discard" label="discard" disabled={!dirty || saving} onClick={() => { setValues(doc.values); setErrors({}); setStatus(null); }} />
      <Button type="submit" form={formId} variant="primary" icon="save" disabled={!dirty || saving}>
        {saving ? "saving…" : "save"}
      </Button>
    </>
  );

  return (
    <>
      <PageHeader title="Settings">{buttons}</PageHeader>
      <Panes>
        <Pane side label="Settings sections">
          <ListPicker
            label="section"
            value={section}
            options={Object.entries(doc.sections).map(([key, title]) => ({ value: key, label: errorCount(key) > 0 ? `${title.toLowerCase()} (${errorCount(key)} to fix)` : title.toLowerCase() }))}
            onChange={setSection}
          >
            <List label="Settings sections">
              {Object.entries(doc.sections).map(([key, title]) => (
                <ListItem
                  key={key}
                  mono
                  icon={SECTION_ICONS[key]}
                  title={title.toLowerCase()}
                  href={`#/settings/${key}`}
                  selected={key === section}
                  // the page stays, the address follows (see the effect above)
                  onClick={(e) => {
                    e.preventDefault();
                    setSection(key);
                  }}
                >
                  {errorCount(key) > 0 && <Count tone="crit">{errorCount(key)}</Count>}
                </ListItem>
              ))}
            </List>
          </ListPicker>
        </Pane>

        <Pane
          bare
          narrow
          label={doc.sections[section]}
          id={formId}
          resetOn={section}
          onSubmit={(e) => { e.preventDefault(); void save(); }}
        >
          <SectionHead title={doc.sections[section]} note={sectionSchema.description} />
          {section === "llm" || section === "worker" ? (
            <ModelForm
              key={section}
              profile={section === "llm" ? "main" : "worker"}
              schema={sectionSchema}
              value={values[section] as JsonObject}
              onChange={(v) => update(section, v)}
              ctx={ctx}
              onModels={setModels}
            />
          ) : (
            <ObjectFields schema={sectionSchema} value={values[section]} onChange={(v) => update(section, v)} path={section} ctx={ctx} />
          )}
        </Pane>
      </Panes>
    </>
  );
}

/** Same look and behaviour as the connection tests in SchemaForm: a "test" button in the
    panel's head, an ok / failed line under it, and a result that goes away as soon as the
    values it was run with are edited. */
function LlmTest({ profile, llm, onModels }: { profile: string; llm: JsonObject; onModels: (m: string[]) => void }) {
  type Line = { ok: boolean; text: string };
  const [result, setResult] = useState<(Line & { structured?: Line | null }) | null>(null);
  const [running, setRunning] = useState(false);
  const requestId = useRef(0);
  const snapshot = JSON.stringify(llm);
  useEffect(() => {
    requestId.current++; // an edit makes an older answer (or a test still running) stale
    setResult(null);
    setRunning(false);
  }, [snapshot]);
  const run = async () => {
    const mine = ++requestId.current;
    setRunning(true);
    setResult(null);
    let outcome: Line & { structured?: Line | null };
    try {
      const r = await api.post<LlmTestResult>(`/api/settings/test-llm?profile=${profile}`, llm);
      if (mine !== requestId.current) return;
      if (r.models?.length) onModels(r.models);
      outcome = r.ok
        ? {
            ok: true,
            text:
              `Model replied “${r.reply || "(empty)"}” in ${r.latency_s}s.` +
              (r.tool_call ? " Tool call OK." : "") +
              (r.model_listed === false ? " Note: the endpoint's model list doesn't include this model name." : "") +
              (r.models?.length ? ` ${r.models.length} models available — suggestions added to the Model field.` : ""),
            structured: r.structured_output,
          }
        : { ok: false, text: r.error ?? "The test failed." };
    } catch (e) {
      outcome = { ok: false, text: `The test could not run: ${(e as Error).message}` };
    }
    if (mine !== requestId.current) return;
    setResult(outcome);
    setRunning(false);
  };
  return (
    <Panel
      title="Connection test"
      aside={
        <IconButton icon="test" label={running ? "testing…" : "test"} disabled={running} onClick={() => void run()} />
      }
    >
      <Pad>
        <Text block dim>
          Checks the endpoint and model, then runs one tool call and one structured answer with the values above (saved or not).
        </Text>
      </Pad>
      <Live>
        {running && <TestLine running />}
        {result && <TestLine ok={result.ok}>{result.text}</TestLine>}
        {result?.structured && <TestLine ok={result.structured.ok}>{result.structured.text}</TestLine>}
      </Live>
    </Panel>
  );
}
