import { useState } from "react";
import { api, ApiError, type ActionRecord } from "../api";
import { ago, clock, until } from "../fmt";
import { useStream } from "../live";
import { Badge, Empty, PageHead, Panel, useNow, usePoll, type Level } from "../ui";

type Listing = { items: ActionRecord[]; total: number };

const STATUSES = ["pending", "suggested", "executed", "failed", "rejected", "expired", "dry_run"];

const statusLevel = (s: string): Level =>
  s === "executed" ? "ok" : s === "failed" ? "crit" : s === "pending" ? "warn" : s === "dry_run" || s === "suggested" ? "info" : "off";

const label = (s: string) => s.replace(/_/g, " ");

function describe(a: ActionRecord): string {
  const result = a.result ?? {};
  if (a.status === "failed") return String(result.error ?? "failed");
  if (a.status === "rejected") return String(result.detail ?? "rejected");
  if (a.status === "dry_run") return `dry run: ${String(result.would ?? "nothing was sent")}`;
  return a.reason ?? "";
}

export function ApprovalsPage() {
  const now = useNow(30_000);
  const pending = usePoll(() => api.get<{ items: ActionRecord[] }>("/api/approvals"), 30_000);
  const [status, setStatus] = useState("");
  const [action, setAction] = useState("");
  const [limit, setLimit] = useState(25);
  const query = new URLSearchParams({ limit: String(limit) });
  if (status) query.set("status", status);
  if (action) query.set("action", action);
  const log = usePoll(() => api.get<Listing>(`/api/actions?${query}`), 30_000);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    pending.reload();
    log.reload();
  };
  useStream(["approval", "action"], refresh);

  const decide = async (id: number, decision: "approve" | "reject") => {
    setBusy(id);
    setError(null);
    try {
      await api.post(`/api/approvals/${id}`, { decision });
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : (e as Error).message);
    } finally {
      setBusy(null);
      refresh();
    }
  };

  const items = pending.data?.items ?? [];
  const actionNames = Array.from(new Set((log.data?.items ?? []).map((a) => a.action))).sort();

  return (
    <div className="stack">
      <PageHead title="Approvals and actions" />
      <Panel title="Waiting for you" aside={items.length ? <span className="muted small">{items.length}</span> : undefined}>
        {error && <p className="error">{error}</p>}
        {pending.data && items.length === 0 ? (
          <Empty>Nothing is waiting. Actions set to “approve” in Settings appear here.</Empty>
        ) : (
          <ul className="approvals">
            {items.map((a) => (
              <li key={a.id}>
                <div className="approval-body">
                  <p className="approval-title">
                    <span className="mono">{a.action}</span>
                    <span>{a.target}</span>
                  </p>
                  <p className="muted small">
                    from {a.source} · requested {ago(a.ts, now)} · expires {until(a.expires_at, now)}
                  </p>
                  {a.reason && <p className="approval-reason">{a.reason}</p>}
                  {a.args && <details className="small"><summary>Arguments</summary><pre>{JSON.stringify(a.args, null, 2)}</pre></details>}
                </div>
                <div className="approval-buttons">
                  <button className="btn" disabled={busy === a.id} onClick={() => decide(a.id, "reject")}>
                    Reject
                  </button>
                  <button className="btn btn-primary" disabled={busy === a.id} onClick={() => decide(a.id, "approve")}>
                    Approve
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel
        title="Audit log"
        aside={
          <div className="filters">
            <select aria-label="Status" value={status} onChange={(e) => { setStatus(e.target.value); setLimit(25); }}>
              <option value="">All statuses</option>
              {STATUSES.map((s) => <option key={s} value={s}>{label(s)}</option>)}
            </select>
            <select aria-label="Action" value={action} onChange={(e) => { setAction(e.target.value); setLimit(25); }}>
              <option value="">All actions</option>
              {actionNames.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        }
      >
        {log.data && log.data.items.length === 0 ? (
          <Empty>No actions recorded{status || action ? " for this filter" : ""}.</Empty>
        ) : (
          <table className="table log">
            <thead>
              <tr><th>Time</th><th>Action</th><th>Status</th><th className="hide-sm">Source</th><th className="hide-sm">Detail</th></tr>
            </thead>
            <tbody>
              {(log.data?.items ?? []).map((a) => (
                <tr key={a.id}>
                  <td className="nowrap">{clock(a.ts)}</td>
                  <td>
                    <span className="mono">{a.action}</span>
                    <span className="muted block small">{a.target}</span>
                  </td>
                  <td><Badge level={statusLevel(a.status)}>{label(a.status)}</Badge></td>
                  <td className="hide-sm muted">{a.source}{a.decided_by ? ` · ${a.decided_by}` : ""}</td>
                  <td className="hide-sm details">{describe(a)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {log.data && log.data.total > log.data.items.length && (
          <button className="btn btn-quiet more" onClick={() => setLimit(limit + 25)}>
            Show more ({log.data.total - log.data.items.length} older)
          </button>
        )}
      </Panel>
    </div>
  );
}
