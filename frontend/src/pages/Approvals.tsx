import { useEffect, useRef, useState } from "react";
import { api, ApiError, type ActionRecord, type ApprovalBatch, type StatusResponse } from "../api";
import { callArguments } from "../components/ApprovalCards";
import { HostCommand } from "../components/HostCommand";
import { ago, clock, until } from "../fmt";
import { useAutomation } from "../automation";
import { useStream } from "../live";
import { Badge, Empty, PageHead, Panel, useNow, usePoll, type Level } from "../ui";

type Listing = { items: ActionRecord[]; total: number };

const STATUSES = ["pending", "queued", "suggested", "executed", "failed", "rejected", "expired", "dry_run"];
const SHOWN = 10;

const statusLevel = (s: string): Level =>
  s === "executed"
    ? "ok"
    : s === "failed"
      ? "crit"
      : s === "pending" || s === "queued" || s === "deferred"
        ? "warn"
        : s === "dry_run" || s === "suggested" || s === "approved"
          ? "info"
          : "off";

const label = (s: string) => s.replace(/_/g, " ");

function describe(a: ActionRecord): string {
  const result = a.result ?? {};
  if (a.status === "failed") return String(result.error ?? "failed");
  if (a.status === "rejected" || a.status === "deferred") return String(result.detail ?? a.status);
  if (a.status === "queued") return "approved; waiting for the hourly limit, it runs by itself";
  if (a.status === "dry_run") return `dry run: ${String(result.would ?? "nothing was sent")}`;
  if (a.action === "host_command" && typeof result.exit_code === "number") return `exit code ${result.exit_code}${a.reason ? ` · ${a.reason}` : ""}`;
  return a.reason ?? "";
}

type CardProps = {
  batch: ApprovalBatch;
  now: number;
  focused: boolean;
  busy: boolean;
  onDecide: (batch: ApprovalBatch, decision: "approve" | "reject", items?: number[], rest?: boolean) => void;
  onAudit: (batch: ApprovalBatch) => void;
};

function BatchCard({ batch, now, focused, busy, onDecide, onAudit }: CardProps) {
  const paused = Boolean(useAutomation().state?.paused);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [all, setAll] = useState(false);
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [focused]);

  const waiting = batch.items.filter((i) => i.status === "pending");
  const waitingIds = new Set(waiting.map((i) => i.id));
  const chosen = [...selected].filter((id) => waitingIds.has(id));
  const toggle = (id: number) =>
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const done = Object.entries(batch.counts).filter(([k]) => k !== "pending");
  // Long batches show the first items; the rest opens in place. Select all always covers every
  // pending item, shown or not.
  const visible = all ? batch.items : batch.items.slice(0, SHOWN);
  const hidden = batch.items.length - visible.length;

  return (
    <article ref={ref} className={`batch ${focused ? "focused" : ""}`} aria-label={batch.title}>
      <header className="batch-head">
        <div className="batch-title">
          <h4>{batch.title}</h4>
          <p className="muted small">
            #{batch.id} · from {batch.source} · requested {ago(batch.ts, now)}
            {waiting.length > 0 && <> · expires {until(batch.expires_at, now)}</>}
          </p>
          {batch.covered.length > 0 && (
            <p className="muted small">
              Approved for the whole run: later requests of {batch.covered.join(", ")} run without asking and are listed here.
            </p>
          )}
          {done.length > 0 && (
            <p className="batch-outcome">
              {done.map(([status, n]) => (
                <Badge key={status} level={statusLevel(status)}>
                  {n} {label(status)}
                </Badge>
              ))}
              {batch.queued > 0 && <span className="muted small">{paused ? "approved; waiting because automation is paused, it runs after the resume" : batch.queue_note.replace(/^, /, "")}</span>}
            </p>
          )}
        </div>
        {waiting.length === 0 && batch.queued > 0 && (
          <div className="approval-buttons">
            <button className="btn" disabled={busy} onClick={() => onDecide(batch, "reject")}>
              Cancel the {batch.queued} queued
            </button>
          </div>
        )}
        {waiting.length > 0 && (
          <div className="approval-buttons">
            <button className="btn" disabled={busy} onClick={() => onDecide(batch, "reject")}>
              Reject all{waiting.length > 1 ? ` (${waiting.length})` : ""}
            </button>
            {batch.rest.length > 0 && (
              <button
                className="btn"
                disabled={busy}
                title={`Approves everything waiting here, and every later request of ${batch.rest.join(", ")} from the same run (a chat answer until it is finished). Each one is still recorded in the audit log.`}
                onClick={() => onDecide(batch, "approve", undefined, true)}
              >
                Approve for the whole run
              </button>
            )}
            <button className="btn btn-primary" disabled={busy} onClick={() => onDecide(batch, "approve")}>
              Approve all{waiting.length > 1 ? ` (${waiting.length})` : ""}
            </button>
          </div>
        )}
      </header>

      {waiting.length > 1 && (
        <div className="batch-bar">
          <label className="check">
            <input
              type="checkbox"
              checked={chosen.length === waiting.length}
              ref={(el) => {
                if (el) el.indeterminate = chosen.length > 0 && chosen.length < waiting.length;
              }}
              onChange={(e) => setSelected(e.target.checked ? new Set(waitingIds) : new Set())}
            />
            <span>{chosen.length ? `${chosen.length} of ${waiting.length} selected` : `Select all ${waiting.length}`}</span>
          </label>
          <div className="batch-bar-buttons">
            <button
              className="btn"
              disabled={busy || chosen.length === 0}
              onClick={() => {
                onDecide(batch, "reject", chosen);
                setSelected(new Set());
              }}
            >
              Reject selected
            </button>
            <button
              className="btn"
              disabled={busy || chosen.length === 0}
              onClick={() => {
                onDecide(batch, "approve", chosen);
                setSelected(new Set());
              }}
            >
              Approve selected
            </button>
          </div>
        </div>
      )}

      <ul className={`batch-items ${all ? "scroll" : ""}`}>
        {visible.map((item) => (
          <li key={item.id} className={item.status === "pending" ? undefined : "settled"}>
            {waiting.length > 1 && item.status === "pending" ? (
              <input
                type="checkbox"
                aria-label={`Select ${item.target ?? item.id}`}
                checked={selected.has(item.id)}
                onChange={() => toggle(item.id)}
              />
            ) : (
              <span className="check-gap" aria-hidden />
            )}
            <div className="item-body">
              {item.action === "host_command" ? (
                <HostCommand item={item} />
              ) : (
                <>
                  <span className="item-target">{item.target}</span>
                  {callArguments(item) && <span className="mono small block">{callArguments(item)}</span>}
                  {item.reason && item.status === "pending" && <span className="muted small block">{item.reason}</span>}
                  {item.status !== "pending" && <span className="muted small block">{describe(item)}</span>}
                </>
              )}
            </div>
            {item.status !== "pending" && <Badge level={statusLevel(item.status)}>{label(item.status)}</Badge>}
          </li>
        ))}
      </ul>
      <footer className="batch-foot">
        {batch.items.length > SHOWN && (
          <button className="btn btn-quiet" aria-expanded={all} onClick={() => setAll(!all)}>
            {all ? `Show only the first ${SHOWN}` : `Show ${hidden} more`}
          </button>
        )}
        <button className="btn btn-quiet" onClick={() => onAudit(batch)}>
          Show in the audit log
        </button>
      </footer>
    </article>
  );
}

export function ApprovalsPage({ params = [] }: { params?: string[] }) {
  const now = useNow(30_000);
  const focusId = /^\d+$/.test(params[0] ?? "") ? Number(params[0]) : null;
  const pending = usePoll(() => api.get<{ batches: ApprovalBatch[] }>("/api/approvals"), 30_000);
  const opened = usePoll(
    () => (focusId ? api.get<ApprovalBatch>(`/api/approvals/${focusId}`) : Promise.resolve(null)),
    30_000,
  );
  const [status, setStatus] = useState("");
  const [action, setAction] = useState("");
  const [batch, setBatch] = useState<number | null>(null);
  const [limit, setLimit] = useState(25);
  const query = new URLSearchParams({ limit: String(limit) });
  if (status) query.set("status", status);
  if (action) query.set("action", action);
  if (batch) query.set("batch", String(batch));
  const log = usePoll(() => api.get<Listing>(`/api/actions?${query}`), 30_000);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  // a link to another approval while the page is open: load that one now
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => opened.reload(), [focusId]);
  const [shell, setShell] = useState<StatusResponse["change_shell"]>(null);
  useEffect(() => {
    api.get<StatusResponse>("/api/status").then((s) => setShell(s.change_shell ?? null), () => undefined);
  }, []);

  const refresh = () => {
    pending.reload();
    opened.reload();
    log.reload();
  };
  useStream(["approval", "action"], refresh);

  const decide = async (b: ApprovalBatch, decision: "approve" | "reject", items?: number[], rest?: boolean) => {
    setBusy(b.id);
    setError(null);
    try {
      await api.post(`/api/approvals/${b.id}`, { decision, ...(items ? { items } : {}), ...(rest ? { rest } : {}) });
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : (e as Error).message);
    } finally {
      setBusy(null);
      refresh();
    }
  };
  const showAudit = (b: ApprovalBatch) => {
    setBatch(b.id);
    setStatus("");
    setAction("");
    setLimit(100);
    document.getElementById("audit-log")?.scrollIntoView({ behavior: "smooth" });
  };

  const batches = pending.data?.batches ?? [];
  const waitingItems = batches.reduce((n, b) => n + b.pending, 0);
  // A batch opened by link that is no longer waiting (decided, expired) is shown on top.
  const extra = opened.data && !batches.some((b) => b.id === opened.data?.id) ? opened.data : null;
  const actionNames = Array.from(new Set((log.data?.items ?? []).map((a) => a.action))).sort();

  return (
    <div className="stack">
      <PageHead title="Approvals and actions" />
      {shell && shell.policy !== "off" && !shell.available && (
        <div className="notice">
          <strong>Changing shell commands are not available.</strong> The chat cannot ask to run them until this is fixed:
          <p className="muted small">{shell.reason ?? "The host shell could not be reached."}</p>
          <p className="muted small">
            Turn them off in <a href="#/settings/approvals">Settings → Approvals</a> to hide this note.
          </p>
        </div>
      )}
      {extra && (
        <Panel title={`Approval #${extra.id}`} aside={<a href="#/approvals" className="small">Close</a>}>
          <BatchCard batch={extra} now={now} focused={false} busy={busy === extra.id} onDecide={decide} onAudit={showAudit} />
        </Panel>
      )}
      <Panel
        title="Waiting for you"
        aside={
          batches.length ? (
            <span className="muted small">
              {batches.length} approval{batches.length === 1 ? "" : "s"} · {waitingItems} item{waitingItems === 1 ? "" : "s"}
            </span>
          ) : undefined
        }
      >
        {error && <p className="error">{error}</p>}
        {pending.data && batches.length === 0 ? (
          <Empty>Nothing is waiting. Actions set to “approve” in Settings appear here.</Empty>
        ) : (
          <div className="batches">
            {batches.map((b) => (
              <BatchCard
                key={b.id}
                batch={b}
                now={now}
                focused={b.id === focusId}
                busy={busy === b.id}
                onDecide={decide}
                onAudit={showAudit}
              />
            ))}
          </div>
        )}
      </Panel>

      <div id="audit-log">
        <Panel
          title="Audit log"
          aside={
            <div className="filters">
              {batch && (
                <button className="btn btn-quiet" onClick={() => setBatch(null)} title="Show all actions again">
                  Approval #{batch} ✕
                </button>
              )}
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
            <Empty>No actions recorded{status || action || batch ? " for this filter" : ""}.</Empty>
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
                    <td className="hide-sm muted">
                      {a.source}
                      {a.decided_by ? ` · ${a.decided_by}` : ""}
                      {a.batch_id && !batch ? (
                        <button className="link-btn" onClick={() => { setBatch(a.batch_id); setLimit(100); }} title="Only this approval">
                          {" "}#{a.batch_id}
                        </button>
                      ) : null}
                    </td>
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
    </div>
  );
}
