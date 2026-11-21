// Approvals and the audit log. All names and values are invented. Times are relative to the moment the
// module loads, so "requested 10m ago" / "expires in 2h" read the same whenever the tests run.
import type { ActionRecord, ApprovalBatch } from "../../../src/api";

const NOW = Math.floor(Date.now() / 1000);
export const MINUTE = 60;
export const HOUR = 3600;

export const CHAT_ID = "abc12345-0000-4000-8000-000000000001";

type ItemOver = Partial<ActionRecord> & { id: number };

/** One audit row: a call of `test-server`'s `delete_item` tool, unless told otherwise. */
export function action(over: ItemOver): ActionRecord {
  return {
    ts: NOW - 10 * MINUTE,
    action: "mcp_call",
    target: `item-${over.id}`,
    args: { server: "test-server", tool: "delete_item", arguments: { service: "example", id: `a${over.id}` } },
    policy: "approve",
    status: "pending",
    source: `chat:${CHAT_ID}`,
    reason: null,
    result: null,
    decided_by: null,
    expires_at: NOW + 2 * HOUR + 30,
    batch_id: null,
    ...over,
  };
}

type BatchOver = Partial<ApprovalBatch> & { id: number; items: ActionRecord[] };

/** An approval made of its items: counts, totals and the waiting number follow from them. */
export function batch(over: BatchOver): ApprovalBatch {
  const items = over.items.map((i) => ({ ...i, batch_id: over.id }));
  const counts: Record<string, number> = {};
  for (const i of items) counts[i.status] = (counts[i.status] ?? 0) + 1;
  return {
    ts: NOW - 10 * MINUTE,
    action: "mcp_call",
    label: null,
    title: `Batch ${over.id}`,
    summary: "test-server · delete_item",
    source: `chat:${CHAT_ID}`,
    expires_at: NOW + 2 * HOUR + 30,
    total: items.length,
    pending: counts.pending ?? 0,
    queued: counts.queued ?? 0,
    next_in_s: null,
    queue_note: "",
    counts,
    outcome: "",
    rest: [],
    covered: [],
    ...over,
    items,
  };
}

/** Three items wait; later calls of the same tool can be approved with the run. From a chat. */
export const multiBatch = batch({
  id: 41,
  title: "Delete three items",
  rest: ["test-server · delete_item"],
  items: [action({ id: 101, target: "item alpha" }), action({ id: 102, target: "item beta" }), action({ id: 103, target: "item gamma" })],
});

/** One item waits; it comes from a job. */
export const singleBatch = batch({
  id: 42,
  title: "Restart the example container",
  source: "job:example-job",
  action: "mcp_call",
  summary: "test-server · restart",
  expires_at: NOW + 45 * MINUTE,
  items: [
    action({
      id: 104,
      target: "container host-a",
      source: "job:example-job",
      args: { server: "test-server", tool: "restart", arguments: { name: "host-a" } },
      reason: "It stopped answering",
    }),
  ],
});

/** A changing shell command waits. */
export const commandBatch = batch({
  id: 43,
  title: "Free disk space",
  action: "host_command",
  summary: "host command",
  source: `chat:${CHAT_ID}`,
  items: [
    action({
      id: 105,
      action: "host_command",
      target: "host",
      args: { command: "journalctl --vacuum-size=200M", reason: "The journal is large" },
    }),
  ],
});

/** Approved, but waiting for the hourly limit. */
export const queuedBatch = batch({
  id: 44,
  title: "Prune two items",
  source: "job:example-job",
  expires_at: null,
  queue_note: ", the next one runs in 12m",
  next_in_s: 720,
  items: [action({ id: 106, status: "queued", target: "item delta", expires_at: null }), action({ id: 107, status: "queued", target: "item epsilon", expires_at: null })],
});

/** Nobody answered in time: it expired. Not waiting any more. */
export const expiredBatch = batch({
  id: 45,
  title: "Remove the old cache",
  expires_at: NOW - HOUR,
  items: [action({ id: 108, status: "expired", target: "cache old", expires_at: NOW - HOUR })],
});

/** Decided: one ran, one was denied with a reason, one failed. */
export const decidedBatch = batch({
  id: 46,
  title: "Clean up three items",
  covered: ["test-server · delete_item"],
  expires_at: NOW - 30 * MINUTE,
  items: [
    action({ id: 109, status: "executed", target: "item zeta", reason: "done", result: {}, decided_by: "user" }),
    action({ id: 110, status: "rejected", target: "item eta", result: { detail: "Denied by the owner" }, decided_by: "user" }),
    action({ id: 111, status: "failed", target: "item theta", result: { error: "tool timed out" }, decided_by: "user" }),
  ],
});

/** The approvals waiting: what `GET /api/approvals` answers by default. */
export const pendingBatches: ApprovalBatch[] = [multiBatch, singleBatch, commandBatch, queuedBatch];

/** Every approval the server knows, for `GET /api/approvals/{id}`. */
export const allBatches: ApprovalBatch[] = [...pendingBatches, expiredBatch, decidedBatch];

/** The audit log, newest first: every item of every approval plus one row without an approval. */
export const auditLog: ActionRecord[] = [
  action({
    id: 120,
    ts: NOW - 5 * MINUTE,
    action: "notify",
    target: "telegram",
    args: null,
    status: "dry_run",
    source: "scheduler",
    reason: null,
    result: { would: "send a message" },
  }),
  ...allBatches.flatMap((b) => b.items).sort((a, b) => b.id - a.id),
];
