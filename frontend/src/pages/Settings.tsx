import { useEffect, useMemo, useState } from "react";
import { api, ApiError, type FieldError, type JsonObject, type LlmTestResult, type SettingsDoc } from "../api";
import { PromAuto } from "../components/PromAuto";
import { ObjectFields, resolve, type FormContext } from "../components/SchemaForm";

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

  if (loadError) return <p className="error">Could not load settings: {loadError}</p>;
  if (!doc || !values) return <p className="muted">Loading…</p>;

  const defs = doc.schema.$defs ?? {};
  const sectionSchema = resolve(doc.schema.properties![section], defs);
  const errorCount = (key: string) => Object.keys(errors).filter((p) => p === key || p.startsWith(`${key}.`)).length;
  const ctx: FormContext = { defs, errors, suggestions: { "llm.model": models } };

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
        {section === "llm" && <LlmTest llm={values.llm as JsonObject} onModels={setModels} />}
        {section === "collectors" && <PromAuto />}

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

function LlmTest({ llm, onModels }: { llm: JsonObject; onModels: (m: string[]) => void }) {
  const [result, setResult] = useState<LlmTestResult | null>(null);
  const [running, setRunning] = useState(false);
  const run = async () => {
    setRunning(true);
    setResult(null);
    try {
      const r = await api.post<LlmTestResult>("/api/settings/test-llm", llm);
      setResult(r);
      if (r.models?.length) onModels(r.models);
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setRunning(false);
    }
  };
  return (
    <section className="group">
      <header className="group-head">
        <div>
          <h3>Connection test</h3>
          <p className="help">Sends a one-line prompt with the values above (saved or not).</p>
        </div>
        <button type="button" className="btn" disabled={running} onClick={() => void run()}>
          {running ? "Testing…" : "Test connection"}
        </button>
      </header>
      {result && (
        <div className="group-body">
          {result.ok ? (
            <p className="ok">
              Model replied “{result.reply || "(empty)"}” in {result.latency_s}s.
              {result.model_listed === false && " Note: the endpoint's model list doesn't include this model name."}
              {result.models?.length ? ` ${result.models.length} models available — suggestions added to the Model field.` : ""}
            </p>
          ) : (
            <p className="error">{result.error}</p>
          )}
        </div>
      )}
    </section>
  );
}
