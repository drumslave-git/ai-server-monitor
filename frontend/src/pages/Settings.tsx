import { useEffect, useId, useMemo, useRef, useState } from "react";
import { api, ApiError, type FieldError, type JsonObject, type LlmTestResult, type ModelInfo, type SettingsDoc } from "../api";
import { ObjectFields, resolve, type FormContext } from "../components/SchemaForm";
import { Button, Count, ErrorText, IconButton, type IconName, List, ListItem, Live, Meta, Pad, PageHeader, Pane, Panes, Panel, SectionHead, setRootFontSize, Status, TestLine, Text } from "../kit";

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

/** What the selected model reports about itself (Ollama only), re-read when the endpoint or
    model changes. */
function useModelInfo(baseUrl: string, model: string, llm: JsonObject): ModelInfo | null {
  const [info, setInfo] = useState<ModelInfo | null>(null);
  const latest = useRef(llm);
  latest.current = llm;
  useEffect(() => {
    setInfo(null);
    if (!baseUrl.trim() || !model.trim()) return;
    let stale = false;
    const timer = setTimeout(() => {
      api.post<ModelInfo>("/api/settings/llm-model-info", latest.current).then(
        (r) => { if (!stale) setInfo(r); },
        () => undefined,
      );
    }, 500);
    return () => { stale = true; clearTimeout(timer); };
  }, [baseUrl, model]);
  return info;
}

export function SettingsPage() {
  const [doc, setDoc] = useState<SettingsDoc | null>(null);
  const [values, setValues] = useState<JsonObject | null>(null);
  const [section, setSection] = useState<string>(() => location.hash.split("/")[2] || "llm");
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

  const llm = (values?.llm ?? {}) as JsonObject;
  const modelInfo = useModelInfo(String(llm.base_url ?? ""), String(llm.model ?? ""), llm);

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
    notes: modelInfo?.available ? { "llm.temperature": modelDefaultsLine(modelInfo) } : {},
    // a model that does not report "thinking" has no reasoning to set
    hidden: modelInfo?.available && !modelInfo.capabilities.includes("thinking") ? ["llm.reasoning_effort"] : [],
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
        </Pane>

        <Pane
          bare
          label={doc.sections[section]}
          id={formId}
          resetOn={section}
          onSubmit={(e) => { e.preventDefault(); void save(); }}
        >
          <SectionHead title={doc.sections[section]} note={sectionSchema.description} />
          <ObjectFields
            schema={sectionSchema}
            value={values[section]}
            onChange={(v) => {
              setValues({ ...values, [section]: v });
              if (status) setStatus(null);
            }}
            path={section}
            ctx={ctx}
          />
          {(section === "llm" || section === "worker") && (
            <LlmTest key={section} profile={section === "llm" ? "main" : "worker"} llm={values[section] as JsonObject} onModels={setModels} />
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
