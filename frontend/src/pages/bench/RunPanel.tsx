import { useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import { Panel, useNow } from "../../ui";
import { elapsed } from "./format";
import type { BenchStatus, Catalog } from "./types";

/** Start / cancel a run and follow its progress. */
export function RunPanel({ status, catalog, reload }: { status: BenchStatus | null; catalog: Catalog | null; reload: () => void }) {
  const [repeats, setRepeats] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const now = useNow(1000);
  useEffect(() => {
    if (repeats === null && status) setRepeats(status.repeats);
  }, [status, repeats]);

  const start = async (group?: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.post<{ run_id: number }>("/api/bench/runs", { group, repeats: repeats ?? undefined });
      reload();
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : (e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => {
    setBusy(true);
    try {
      await api.post("/api/bench/cancel", {});
    } finally {
      setBusy(false);
      reload();
    }
  };

  const llm = status?.llm;
  const p = status?.progress ?? null;
  const running = Boolean(status?.running);
  const perGroup = (key: string) => catalog?.scenarios.filter((s) => s.group === key).length ?? 0;
  const total = catalog?.scenarios.length ?? 0;
  const attempts = (n: number) => n * (repeats ?? status?.repeats ?? 3);

  return (
    <Panel title="Run a benchmark" aside={<a href="#/settings/bench">Benchmark settings</a>}>
      {llm && !llm.configured && (
        <div className="notice">
          The LLM is not configured, so there is nothing to benchmark yet. Set it in <a href="#/settings/llm">Settings → LLM</a>.
        </div>
      )}
      {llm?.configured && (
        <p className="bench-under-test">
          Model under test: <strong className="mono">{llm.model}</strong> <span className="muted">at {llm.base_url.replace(/^https?:\/\//, "")}</span>
          <span className="help block">
            Runs use the LLM from <a href="#/settings/llm">Settings → LLM</a>. To compare models, change it there, run again, and tick the runs below.
          </span>
        </p>
      )}

      {!running && (
        <div className="bench-controls">
          <label className="bench-repeats">
            <span className="muted small">Attempts per scenario</span>
            <input type="number" min={1} max={20} value={repeats ?? 3} onChange={(e) => setRepeats(Math.max(1, Math.min(20, Number(e.target.value) || 1)))} />
          </label>
          <div className="bench-run-buttons">
            <button className="btn btn-primary" disabled={busy || !llm?.configured || total === 0} onClick={() => void start()}>
              Run all · {total} scenarios · {attempts(total)} attempts
            </button>
            {catalog?.groups.map((g) => (
              <button key={g.key} className="btn" disabled={busy || !llm?.configured || perGroup(g.key) === 0} onClick={() => void start(g.key)}>
                {g.title} · {perGroup(g.key)}
              </button>
            ))}
          </div>
        </div>
      )}
      {error && <p className="error">{error}</p>}

      {running && p && (
        <div className="bench-progress" role="status" data-now={Math.round(now)}>
          <div className="bench-progress-head">
            <strong>{p.stage === "preparing" ? "Preparing…" : `Run #${p.run_id}`}</strong>
            <span className="muted small">
              {p.done} of {p.total} attempts · {elapsed(p.started, null)}
            </span>
            <button className="btn" disabled={busy} onClick={() => void cancel()}>Cancel</button>
          </div>
          <progress max={p.total || 1} value={p.done} aria-label="Benchmark progress" />
          <p className="muted small">
            {p.scenario ? (
              <>
                Now: <span className="mono">{p.scenario}</span> · attempt {p.attempt}
                {p.last_score !== undefined && <> · last attempt scored {Math.round(p.last_score * 100)}%</>}
              </>
            ) : (
              "Checking the backend and the tool mode…"
            )}
          </p>
        </div>
      )}
      <p className="help">
        Reports and other jobs keep running normally, so timings can be slower while a report is being written; each attempt records whether one overlapped.
        {status?.report_running && <strong className="warn-text"> A report is being written right now.</strong>}
      </p>
    </Panel>
  );
}
