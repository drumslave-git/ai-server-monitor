import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type FieldError, type JsonObject, type LlmTestResult, type ModelInfo, type SettingsDoc } from "../api";
import { ObjectFields, resolve, type FormContext } from "../components/SchemaForm";

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

  if (loadError) return <p className="error">Could not load settings: {loadError}</p>;
  if (!doc || !values) return <p className="muted">Loading…</p>;

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

  return (
    <div className="settings">
      <nav className="tabs" aria-label="Settings sections">
        {Object.entries(doc.sections).map(([key, title]) => (
          <button
            key={key}
            type="button"
            className={key === section ? "tab active" : "tab"}
            aria-current={key === section ? "page" : undefined}
            onClick={() => setSection(key)}
          >
            {title}
            {errorCount(key) > 0 && <span className="badge-error">{errorCount(key)}</span>}
          </button>
        ))}
      </nav>

      <form className="settings-body" onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <h2>{doc.sections[section]}</h2>
        {sectionSchema.description && <p className="help">{sectionSchema.description}</p>}
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

        <div className={`savebar${dirty || status ? " visible" : ""}`}>
          {status && <span className={status.kind === "ok" ? "ok" : "error"}>{status.text}</span>}
          {dirty && !status && <span className="muted">Unsaved changes</span>}
          <span className="spacer" />
          <button type="button" className="btn btn-quiet" disabled={!dirty || saving} onClick={() => { setValues(doc.values); setErrors({}); setStatus(null); }}>
            Discard
          </button>
          <button type="submit" className="btn btn-primary" disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}

/** Same look and behaviour as the connection tests in SchemaForm: a "Test" button with a
    spinner in the group head, a ✓/✕ line under it, and a result that goes away as soon as
    the values it was run with are edited. */
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
    <section className="group">
      <header className="group-head">
        <div>
          <h3>Connection test</h3>
          <p className="help">Checks the endpoint and model, then runs one tool call and one structured answer with the values above (saved or not).</p>
        </div>
        <div className="test-buttons">
          <button type="button" className="btn btn-test" disabled={running} onClick={() => void run()}>
            {running && <span className="spinner" aria-hidden="true" />}
            {running ? "Testing…" : "Test"}
          </button>
        </div>
      </header>
      <div className="test-result" role="status" aria-live="polite">
        {result && (
          <p className={result.ok ? "ok" : "error"}>
            <strong>{result.ok ? "✓ " : "✕ "}</strong>
            {result.text}
          </p>
        )}
        {result?.structured && (
          <p className={result.structured.ok ? "ok" : "error"}>
            <strong>{result.structured.ok ? "✓ " : "✕ "}</strong>
            {result.structured.text}
          </p>
        )}
      </div>
    </section>
  );
}
