import { useEffect, useState } from "react";
import { api, type Info } from "../api";
import { humanize } from "../components/SchemaForm";

function ago(ts: number | null): string {
  if (!ts) return "—";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function uptime(s: number): string {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

export function OverviewPage() {
  const [info, setInfo] = useState<Info | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () => api.get<Info>("/api/info").then((i) => { setInfo(i); setError(null); }, (e: Error) => setError(e.message));
    load();
    const timer = setInterval(load, 10_000);
    return () => clearInterval(timer);
  }, []);

  if (error && !info) return <p className="error">Backend unreachable: {error}</p>;
  if (!info) return <p className="muted">Loading…</p>;

  const llm = info.llm;
  return (
    <div className="overview">
      {!llm.configured && (
        <div className="notice">
          The LLM isn't configured yet, so no reports are written. <a href="#/settings/llm">Configure it</a>.
        </div>
      )}

      <div className="cards">
        <section className="card">
          <h3>Service</h3>
          <dl>
            <dt>Version</dt><dd>{info.version}</dd>
            <dt>Uptime</dt><dd>{uptime(info.uptime_s)}</dd>
            <dt>Host root</dt><dd className="mono">{info.host_root}</dd>
          </dl>
        </section>
        <section className="card">
          <h3>LLM</h3>
          <dl>
            <dt>Model</dt><dd>{llm.configured ? llm.model : "not configured"}</dd>
            <dt>Tool calling</dt><dd>{llm.tool_mode.effective ?? (llm.configured ? "detecting…" : "—")}</dd>
            <dt>Requests</dt><dd>{llm.stats.requests} ({llm.stats.errors} failed)</dd>
          </dl>
          {llm.configured && llm.tool_mode.last_error && <p className="error small">{llm.tool_mode.last_error}</p>}
        </section>
      </div>

      <section className="card">
        <h3>Collectors</h3>
        <table className="table">
          <thead>
            <tr><th>Collector</th><th>Status</th><th>Last run</th><th>Took</th><th>Details</th></tr>
          </thead>
          <tbody>
            {Object.entries(info.collectors).map(([name, c]) => {
              const status =
                c.state !== "active" ? c.state : c.ok === null ? "starting" : c.ok ? "ok" : "error";
              return (
                <tr key={name}>
                  <td><a href={`/api/collectors/${name}`} target="_blank" rel="noreferrer">{humanize(name)}</a></td>
                  <td><span className={`status status-${status}`}>{status}</span></td>
                  <td>{ago(c.last_ts)}</td>
                  <td>{c.duration_s !== null ? `${c.duration_s}s` : "—"}</td>
                  <td className="details">{c.error ?? c.reason ?? ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </div>
  );
}
