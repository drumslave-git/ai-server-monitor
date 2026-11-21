import { useEffect, useRef, useState } from "react";
import { api, type ActionRecord, type ApprovalBatch, type StatusResponse } from "../api";
import { callArguments } from "../components/ApprovalCards";
import { ApprovalRef, ItemTarget, SourceRef } from "../components/EntityRefs";
import { HostCommand } from "../components/HostCommand";
import { ago, clock, until } from "../fmt";
import { useAutomation } from "../automation";
import {
  Actions,
  Button,
  Checkbox,
  CheckboxBlank,
  Column,
  Counter,
  Empty,
  ErrorText,
  Field,
  Heading,
  IconLink,
  Inline,
  Meta,
  Notice,
  PageHeader,
  Pad,
  Pane,
  Panel,
  Panes,
  Row,
  Scroll,
  Select,
  Stack,
  Status,
  Table,
  Td,
  Text,
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
  const list = (
    <Column gap={2}>
      {visible.map((item) => (
        <Inline key={item.id} nowrap gap={3} align={item.status === "pending" ? "center" : "start"}>
          {selectable &&
            (item.status === "pending" ? (
              <Checkbox hideLabel label={`Select ${item.target ?? item.id}`} checked={selected.has(item.id)} onChange={() => toggle(item.id)} />
            ) : (
              <CheckboxBlank />
            ))}
          <Column gap={0} flex="1">
            {item.action === "host_command" ? (
              <HostCommand item={item} />
            ) : (
              <Stack>
                <Text kind="label" wrap dim={item.status !== "pending"}>
                  <ItemTarget item={item} />
                </Text>
                {callArguments(item) && (
                  <Text kind="data" wrap dim={item.status !== "pending"}>
                    {callArguments(item)}
                  </Text>
                )}
                {item.reason && item.status === "pending" && <Text dim>{item.reason}</Text>}
                {item.status !== "pending" && <Text dim>{describe(item)}</Text>}
              </Stack>
            )}
          </Column>
          {item.status !== "pending" && <ActionStatus status={item.status} />}
        </Inline>
      ))}
    </Column>
  );

  return (
    <Row current={focused} column label={batch.title} ref={ref}>
        <Meta>
          #{batch.id} · from <SourceRef source={batch.source} /> · requested {ago(batch.ts, now)}
          {waiting.length > 0 && <> · expires {until(batch.expires_at, now)}</>}
        </Meta>
        <Heading>{batch.title}</Heading>
        {batch.covered.length > 0 && (
          <Text block dim>
            Approved for the whole run: later requests of {batch.covered.join(", ")} run without asking and are listed here.
          </Text>
        )}
        {done.length > 0 && (
          <Inline gap={4}>
            {done.map(([status, n]) => (
              <ActionStatus key={status} status={status} count={n} />
            ))}
            {batch.queued > 0 && <Meta>{paused ? "approved; waiting because automation is paused, it runs after the resume" : batch.queue_note.replace(/^, /, "")}</Meta>}
          </Inline>
        )}

        {selectable && (
          <Toolbar>
            <Checkbox
              checked={chosen.length === waiting.length}
              some={chosen.length > 0}
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

        {all ? <Scroll max="60vh">{list}</Scroll> : list}
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
              <Button variant="primary" icon="approve" disabled={busy} onClick={() => onDecide(batch, "approve")}>
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
  const log = usePoll(() => api.get<Listing>(`/api/actions?${query}`), 30_000, [query.toString()]);
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
      setError((e as Error).message); // an ApiError's message already reads its detail (a validation list as "field: message")
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
      <Panes>
      <Pane bare label="Approvals and audit log">
      {shell && shell.policy !== "off" && !shell.available && (
        <Notice title="Changing shell commands are not available.">
          The chat cannot ask to run them until this is fixed:
          <Text block dim>
            {shell.reason ?? "The host shell could not be reached."}
          </Text>
          <Text block dim>
            Turn them off in <a href="#/settings/approvals">Settings → Approvals</a> to hide this note.
          </Text>
        </Notice>
      )}
      {extra && (
        <Panel
          title={`Approval #${extra.id}`}
          aside={
            <IconLink small icon="close" label="close" hint="Close" href="#/approvals" />
          }
        >
          <BatchCard batch={extra} now={now} focused={false} busy={busy === extra.id} onDecide={decide} onAudit={showAudit} />
        </Panel>
      )}
      <Panel hazard={batches.length ? "warn" : undefined}>
        <Inline align="start" gap={0}>
          <Pad>
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
          </Pad>
          <Column gap={0} flex="1 1 560px">
            {(error ?? pending.error) && (
              <Row>
                <ErrorText>{error ?? pending.error}</ErrorText>
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
          </Column>
        </Inline>
      </Panel>

        <Panel
          id="audit-log"
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
            <Table stack stickyHead head={["time", { label: "action", minWidth: "240px" }, "status", "source", { label: "detail", minWidth: "110px" }]}>
              <tbody>
                {(log.data?.items ?? []).map((a) => (
                  <tr key={a.id}>
                    <Td nowrap>
                      <Text kind="data">{clock(a.ts)}</Text>
                    </Td>
                    <Td fill>
                      <Stack>
                        <Meta>{a.action}</Meta>
                        <Text kind="data">
                          <ItemTarget item={a} />
                        </Text>
                      </Stack>
                    </Td>
                    <Td>
                      <ActionStatus status={a.status} />
                    </Td>
                    <Td>
                      <Stack>
                        <Text kind="data">
                          <SourceRef source={a.source} />
                          {a.decided_by ? ` · ${a.decided_by}` : ""}
                        </Text>
                        {a.batch_id && <ApprovalRef batch={a.batch_id} />}
                      </Stack>
                    </Td>
                    <Td wrap>{describe(a)}</Td>
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
      </Pane>
      </Panes>
    </>
  );
}
