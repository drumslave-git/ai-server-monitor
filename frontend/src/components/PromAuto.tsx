import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";

/* Settings → Collectors: the Prometheus queries the model chose. They are data, not
   settings, so changes here apply at once and are not part of the Save button. */

type LastValue = { total: number; series: { labels: Record<string, string>; value: number | string }[] };

type PromQuery = {
  id: number;
  name: string;
  query: string;
  description: string;
  unit: string;
  kind: string;
  warn_if: string | null;
  crit_if: string | null;
  source: string;
  job: string;
  enabled: boolean;
  pinned: boolean;
  edited: boolean;
  fail_count: number;
  disabled_reason: string | null;
  last_ok_ts: number | null;
  last_error: string | null;
  last_error_ts: number | null;
  last_value: LastValue | null;
};

type Plan = {
  running: boolean;
  progress: string;
  last: {
    ts: number;
    status: string;
    message: string;
    model: string | null;
    source: string | null;
    duration_s: number | null;
    counts: Record<string, number>;
    rejected: { job: string; name: string; reason: string }[];
  } | null;
};

type Overview = {
  configured: boolean;
  auto_queries: boolean;
  max_auto_queries: number;
  llm_configured: boolean;
  queries: PromQuery[];
  plan: Plan;
};

function ago(ts: number | null): string {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function fmt(value: number | string, unit: string): string {
  if (typeof value !== "number") return String(value);
  const text = Math.abs(value) >= 1000 ? value.toFixed(0) : Number(value.toPrecision(4)).toString();
  return unit ? `${text} ${unit}` : text;
}

function lastValue(q: PromQuery): string {
  const lv = q.last_value;
  if (!lv) return "—";
  if (lv.total === 0 || lv.series.length === 0) return q.kind === "alert" ? "nothing to report" : "no data";
  const shown = lv.series.slice(0, 2).map((s) => {
    const tag = Object.entries(s.labels)
      .filter(([k]) => k !== "job" && k !== "__name__")
      .map(([, v]) => v)
      .join(" ");
    return `${tag ? `${tag}: ` : ""}${fmt(s.value, q.unit)}`;
  });
  return shown.join(", ") + (lv.total > shown.length ? ` (+${lv.total - shown.length})` : "");
}

function limits(q: PromQuery): string {
  return [q.warn_if && `warn ${q.warn_if}`, q.crit_if && `crit ${q.crit_if}`].filter(Boolean).join(", ");
}

export function PromAuto() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.get<Overview>("/api/prometheus/queries"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const running = data?.plan.running ?? false;
  useEffect(() => {
    const timer = setInterval(() => void load(), running ? 2000 : 30000);
    return () => clearInterval(timer);
  }, [load, running]);

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof ApiError && typeof e.detail === "string" ? e.detail : (e as Error).message);
    }
    await load();
  };

  const replan = async () => {
    setBusy(true);
    await act(() => api.post("/api/prometheus/replan", {}));
    setBusy(false);
  };

  const patch = (q: PromQuery, body: Record<string, unknown>) =>
    act(() => api.patch(`/api/prometheus/queries/${q.id}`, body));

  if (!data) return error ? <p className="error">Could not load the automatic queries: {error}</p> : null;

  const plan = data.plan;
  const unusable = !data.configured
    ? "Set the Prometheus URL above and save to let the model choose queries."
    : !data.auto_queries
      ? "Automatic queries are switched off above."
      : null;

  return (
    <section className="group promauto" aria-labelledby="promauto-title">
      <header className="group-head">
        <div>
          <h3 id="promauto-title">Automatic queries</h3>
          <p className="help">
            The model reads what your Prometheus scrapes and picks queries that add to what the monitor already
            sees. Each one is run against Prometheus before it is kept; one that keeps failing is switched off.
            Pinned queries and the ones you edited survive a re-plan.
          </p>
        </div>
        <button type="button" className="btn btn-test" disabled={busy || running || !!unusable} onClick={() => void replan()}>
          {running && <span className="spinner" aria-hidden="true" />}
          {running ? "Planning…" : "Re-plan now"}
        </button>
      </header>
      <div className="group-body">
        {unusable && <p className="muted">{unusable}</p>}
        {!unusable && !data.llm_configured && (
          <p className="muted">No LLM is configured, so only the built-in checks run. Set one under LLM to get chosen queries.</p>
        )}
        {running && <p className="muted" role="status">{plan.progress || "Working…"}</p>}
        {!running && plan.last && (
          <p className={plan.last.status === "failed" ? "error" : "help"} role="status">
            Last plan {ago(plan.last.ts)}: {plan.last.message}
            {plan.last.model && plan.last.source === "llm" ? ` (${plan.last.model}` + (plan.last.duration_s ? `, ${plan.last.duration_s}s)` : ")") : ""}
            {plan.last.rejected.length > 0 && ` · ${plan.last.rejected.length} proposed queries were rejected`}
          </p>
        )}
        {error && <p className="error">{error}</p>}
        {data.queries.length === 0 ? (
          !unusable && !running && <p className="muted">No automatic queries yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="table promauto-table">
              <thead>
                <tr>
                  <th>Query</th>
                  <th>Last value</th>
                  <th>On</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {data.queries.map((q) => (
                  <tr key={q.id} className={q.enabled ? "" : "disabled-row"}>
                    <td>
                      <strong>{q.name}</strong>
                      {q.source === "fallback" && <span className="tag"> built-in</span>}
                      {q.job && <span className="tag"> {q.job}</span>}
                      {q.pinned && <span className="tag"> pinned</span>}
                      {q.edited && <span className="tag"> edited</span>}
                      {q.description && <div className="details">{q.description}</div>}
                      <code className="promql">{q.query}</code>
                      {limits(q) && <div className="details">{limits(q)}</div>}
                    </td>
                    <td className="details" data-label="Last value">
                      {q.disabled_reason ? (
                        <span className="error">Switched off: {q.disabled_reason}</span>
                      ) : q.last_error ? (
                        <span className="error">{q.last_error}</span>
                      ) : (
                        lastValue(q)
                      )}
                      {q.last_ok_ts && !q.last_error && !q.disabled_reason && <div className="muted">{ago(q.last_ok_ts)}</div>}
                    </td>
                    <td>
                      <label className="switch">
                        <input
                          type="checkbox"
                          aria-label={`Run ${q.name}`}
                          checked={q.enabled}
                          onChange={(e) => void patch(q, { enabled: e.target.checked })}
                        />
                        <span aria-hidden="true" />
                      </label>
                    </td>
                    <td className="row-actions">
                      <button
                        type="button"
                        className="btn btn-quiet"
                        aria-pressed={q.pinned}
                        title="Keep this query when the model chooses again"
                        onClick={() => void patch(q, { pinned: !q.pinned })}
                      >
                        {q.pinned ? "Unpin" : "Pin"}
                      </button>
                      {confirm === q.id ? (
                        <>
                          <button
                            type="button"
                            className="btn"
                            onClick={() => {
                              setConfirm(null);
                              void act(() => api.del(`/api/prometheus/queries/${q.id}`));
                            }}
                          >
                            Delete
                          </button>
                          <button type="button" className="btn btn-quiet" onClick={() => setConfirm(null)}>
                            Keep
                          </button>
                        </>
                      ) : (
                        <button type="button" className="btn btn-quiet" onClick={() => setConfirm(q.id)}>
                          Delete…
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
