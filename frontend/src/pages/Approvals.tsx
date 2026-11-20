import { useEffect, useRef, useState } from "react";
import { api, ApiError, type ActionRecord, type ApprovalBatch, type StatusResponse } from "../api";
import { callArguments } from "../components/ApprovalCards";
import { HostCommand } from "../components/HostCommand";
import { ago, clock, until } from "../fmt";
import { useAutomation } from "../automation";
import {
  Actions,
  Button,
  Checkbox,
  Counter,
  Empty,
  ErrorText,
  Field,
  LinkButton,
  Meta,
  Notice,
  PageHeader,
  Panel,
  Row,
  Select,
  Stack,
  Status,
  Table,
  Toolbar,
  type Tone,
} from "../kit";
import { useStream } from "../live";
import { useNow, usePoll } from "../ui";

type Listing = { items: ActionRecord[]; total: number };

const STATUSES = ["pending", "queued", "suggested", "executed", "failed", "rejected", "expired", "dry_run"];
const SHOWN = 10;

const statusLevel = (s: string): Tone =>
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

/** The status of an action as lamp and word; an approved one is running. */
function ActionStatus({ status, count }: { status: string; count?: number }) {
  return (
    <Status tone={statusLevel(status)} blink={status === "approved"}>
      {count === undefined ? "" : `${count} `}
      {label(status)}
    </Status>
  );
}

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
  const selectable = waiting.length > 1;

  return (
    <Row current={focused}>
      <article ref={ref} aria-label={batch.title} style={{ flex: "1 1 100%", minWidth: 0, display: "flex", flexDirection: "column", gap: 12 }}>
        <Meta>
          #{batch.id} · from {batch.source} · requested {ago(batch.ts, now)}
          {waiting.length > 0 && <> · expires {until(batch.expires_at, now)}</>}
        </Meta>
        <h3 className="heading fd-wrap">{batch.title}</h3>
        {batch.covered.length > 0 && (
          <p className="fd-dim">
            Approved for the whole run: later requests of {batch.covered.join(", ")} run without asking and are listed here.
          </p>
        )}
        {done.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px 16px" }}>
            {done.map(([status, n]) => (
              <ActionStatus key={status} status={status} count={n} />
            ))}
            {batch.queued > 0 && <Meta>{paused ? "approved; waiting because automation is paused, it runs after the resume" : batch.queue_note.replace(/^, /, "")}</Meta>}
          </div>
        )}

        {selectable && (
          <Toolbar>
            <Checkbox
              checked={chosen.length === waiting.length}
              onChange={(checked) => setSelected(checked ? new Set(waitingIds) : new Set())}
              label={chosen.length ? `${chosen.length} of ${waiting.length} selected` : `select all ${waiting.length}`}
            />
            <Actions>
              <Button
                disabled={busy || chosen.length === 0}
                onClick={() => {
                  onDecide(batch, "approve", chosen);
                  setSelected(new Set());
                }}
              >
                approve selected
              </Button>
              <Button
                disabled={busy || chosen.length === 0}
                onClick={() => {
                  onDecide(batch, "reject", chosen);
                  setSelected(new Set());
                }}
              >
                reject selected
              </Button>
            </Actions>
          </Toolbar>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 8, ...(all ? { maxHeight: "60vh", overflow: "auto" } : {}) }}>
          {visible.map((item) => (
            <div
              key={item.id}
              className={item.status === "pending" ? undefined : "fd-dim"}
              style={{ display: "flex", alignItems: item.status === "pending" ? "center" : "flex-start", gap: 12 }}
            >
              {selectable &&
                (item.status === "pending" ? (
                  <Checkbox hideLabel label={`Select ${item.target ?? item.id}`} checked={selected.has(item.id)} onChange={() => toggle(item.id)} />
                ) : (
                  <span aria-hidden style={{ flex: "none", width: 18 }} />
                ))}
              <div className="fd-wrap" style={{ flex: 1, minWidth: 0 }}>
                {item.action === "host_command" ? (
                  <HostCommand item={item} />
                ) : (
                  <Stack>
                    <span className="label">{item.target}</span>
                    {callArguments(item) && <span className="data">{callArguments(item)}</span>}
                    {item.reason && item.status === "pending" && <span className="fd-dim">{item.reason}</span>}
                    {item.status !== "pending" && <span className="fd-dim">{describe(item)}</span>}
                  </Stack>
                )}
              </div>
              {item.status !== "pending" && <ActionStatus status={item.status} />}
            </div>
          ))}
        </div>
        {batch.items.length > SHOWN && (
          <Actions>
            <Button variant="link" aria-expanded={all} onClick={() => setAll(!all)}>
              {all ? `show only the first ${SHOWN}` : `show ${hidden} more`}
            </Button>
          </Actions>
        )}

        <Actions>
          {waiting.length > 0 && (
            <>
              <Button variant="primary" disabled={busy} onClick={() => onDecide(batch, "approve")}>
                approve all{waiting.length > 1 ? ` (${waiting.length})` : ""}
              </Button>
              {batch.rest.length > 0 && (
                <Button
                  disabled={busy}
                  title={`Approves everything waiting here, and every later request of ${batch.rest.join(", ")} from the same run (a chat answer until it is finished). Each one is still recorded in the audit log.`}
                  onClick={() => onDecide(batch, "approve", undefined, true)}
                >
                  approve for the whole run
                </Button>
              )}
              <Button disabled={busy} onClick={() => onDecide(batch, "reject")}>
                reject all{waiting.length > 1 ? ` (${waiting.length})` : ""}
              </Button>
            </>
          )}
          {waiting.length === 0 && batch.queued > 0 && (
            <Button disabled={busy} onClick={() => onDecide(batch, "reject")}>
              cancel the {batch.queued} queued
            </Button>
          )}
          <Button variant="link" onClick={() => onAudit(batch)}>
            show in the audit log
          </Button>
        </Actions>
      </article>
    </Row>
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
    <>
      <PageHeader title="Approvals and actions" />
      {shell && shell.policy !== "off" && !shell.available && (
        <Notice title="Changing shell commands are not available.">
          The chat cannot ask to run them until this is fixed:
          <p className="fd-dim">{shell.reason ?? "The host shell could not be reached."}</p>
          <p className="fd-dim">
            Turn them off in <a href="#/settings/approvals">Settings → Approvals</a> to hide this note.
          </p>
        </Notice>
      )}
      {extra && (
        <Panel
          title={`Approval #${extra.id}`}
          aside={
            <LinkButton variant="link" href="#/approvals">
              close
            </LinkButton>
          }
        >
          <BatchCard batch={extra} now={now} focused={false} busy={busy === extra.id} onDecide={decide} onAudit={showAudit} />
        </Panel>
      )}
      <Panel hazard={batches.length ? "warn" : undefined}>
        <div style={{ display: "flex", flexWrap: "wrap" }}>
          <div style={{ padding: "20px 24px 24px" }}>
            <Counter
              value={pending.data ? batches.length : "—"}
              tone={batches.length ? "warn" : "off"}
              size="xl"
              plain
              title="Waiting for you"
              label={
                batches.length
                  ? `${batches.length} approval${batches.length === 1 ? "" : "s"} · ${waitingItems} item${waitingItems === 1 ? "" : "s"}`
                  : ""
              }
            />
          </div>
          <div style={{ flex: "1 1 560px", minWidth: 0 }}>
            {error && (
              <Row>
                <ErrorText>{error}</ErrorText>
              </Row>
            )}
            {pending.data && batches.length === 0 ? (
              <Empty>Nothing is waiting. Actions set to “approve” in Settings appear here.</Empty>
            ) : (
              batches.map((b) => (
                <BatchCard
                  key={b.id}
                  batch={b}
                  now={now}
                  focused={b.id === focusId}
                  busy={busy === b.id}
                  onDecide={decide}
                  onAudit={showAudit}
                />
              ))
            )}
          </div>
        </div>
      </Panel>

      <div id="audit-log">
        <Panel
          title="Audit log"
          aside={
            <Toolbar>
              {batch && (
                <Button onClick={() => setBatch(null)} title="Show all actions again">
                  approval #{batch} · clear
                </Button>
              )}
              <Field label="status" width="180px">
                <Select aria-label="Status" value={status} onChange={(e) => { setStatus(e.target.value); setLimit(25); }}>
                  <option value="">all statuses</option>
                  {STATUSES.map((s) => <option key={s} value={s}>{label(s)}</option>)}
                </Select>
              </Field>
              <Field label="action" width="180px">
                <Select aria-label="Action" value={action} onChange={(e) => { setAction(e.target.value); setLimit(25); }}>
                  <option value="">all actions</option>
                  {actionNames.map((s) => <option key={s} value={s}>{s}</option>)}
                </Select>
              </Field>
            </Toolbar>
          }
        >
          {log.data && log.data.items.length === 0 ? (
            <Empty>No actions recorded{status || action || batch ? " for this filter" : ""}.</Empty>
          ) : (
            <Table>
              <thead>
                <tr><th>time</th><th>action</th><th>status</th><th>source</th><th>detail</th></tr>
              </thead>
              <tbody>
                {(log.data?.items ?? []).map((a) => (
                  <tr key={a.id}>
                    <td><span className="data fd-nowrap">{clock(a.ts)}</span></td>
                    <td>
                      <Stack>
                        <Meta>{a.action}</Meta>
                        <span className="data">{a.target}</span>
                      </Stack>
                    </td>
                    <td><ActionStatus status={a.status} /></td>
                    <td>
                      <span className="data fd-nowrap">
                        {a.source}
                        {a.decided_by ? ` · ${a.decided_by}` : ""}
                        {a.batch_id && !batch ? (
                          <>
                            {" "}
                            <Button variant="link" onClick={() => { setBatch(a.batch_id); setLimit(100); }} title="Only this approval">
                              #{a.batch_id}
                            </Button>
                          </>
                        ) : null}
                      </span>
                    </td>
                    <td className="fd-wrap">{describe(a)}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
          {log.data && log.data.total > log.data.items.length && (
            <Row>
              <Button variant="link" onClick={() => setLimit(limit + 25)}>
                show more ({log.data.total - log.data.items.length} older)
              </Button>
            </Row>
          )}
        </Panel>
      </div>
    </>
  );
}
