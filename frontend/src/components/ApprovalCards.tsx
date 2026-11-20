import { useState } from "react";
import { api, ApiError, type ActionRecord, type ApprovalBatch } from "../api";
import { Actions, Approval, Button, Code, Column, ErrorText, Inline, Lamp, Meta, Scroll, Stack, Status, Text, type Tone } from "../kit";
import { useStream } from "../live";
import { usePoll } from "../ui";
import { HostCommand } from "./HostCommand";

/** Items of a long approval shown before "show n more". */
const SHOWN = 5;

const STATUS_TEXT: Record<string, string> = {
  pending: "waiting for your approval",
  queued: "approved; queued",
  approved: "approved; running",
  executed: "approved and run",
  failed: "approved; failed",
  rejected: "denied",
  expired: "expired",
  dry_run: "dry run: nothing was sent",
};

export const statusDot = (status: string): Tone =>
  status === "pending" || status === "queued"
    ? "warn"
    : status === "approved"
      ? "info"
      : status === "executed"
        ? "ok"
        : status === "failed"
          ? "crit"
          : "off";

type Item = Pick<ActionRecord, "args">;

const given = (item: Item): Record<string, unknown> => {
  const args = (item.args as { arguments?: unknown } | null)?.arguments;
  return args && typeof args === "object" ? (args as Record<string, unknown>) : {};
};

/** "service: qbittorrent · id: c17f… · remove_from_client: true": the arguments of an MCP
 *  call, without the `skip` ones. */
export function callArguments(item: Item, skip: string[] = []): string {
  return Object.entries(given(item))
    .filter(([key]) => !skip.includes(key))
    // (an empty text is shown as "", so it does not look like a value that went missing)
    .map(([key, value]) => `${key}: ${typeof value === "string" && value ? value : JSON.stringify(value)}`)
    .join(" · ");
}

/** The arguments every one of several calls has with the same value: said once for all, so
 *  each line shows only what differs (which item it removes). */
export function sharedArguments(items: Item[]): string[] {
  if (items.length < 2) return [];
  const first = given(items[0]);
  return Object.keys(first).filter((key) =>
    items.every((i) => key in given(i) && JSON.stringify(given(i)[key]) === JSON.stringify(first[key])),
  );
}

/** Why an item did not happen (failed, refused by policy), in the words of whoever said no. */
export function failure(item: Pick<ActionRecord, "status" | "result">): string {
  const result = item.result ?? {};
  if (item.status === "failed") return String(result.error ?? "failed");
  if (item.status === "rejected" && result.detail) return String(result.detail);
  return "";
}

/** The lamp of a whole approval: waiting, then the worst of what became of its items. */
function batchDot(batch: ApprovalBatch): Tone {
  const n = batch.counts;
  if (n.pending || n.queued) return "warn";
  if (n.failed) return "crit";
  if (n.approved) return "info";
  return n.executed ? "ok" : "off";
}

/** The approvals a chat answer asked for (by the ids of their items): one card per approval,
 *  however many items it has. */
export function ApprovalCards({ items }: { items: number[] }) {
  const query = items.map((id) => `of=${id}`).join("&");
  const found = usePoll(() => api.get<{ batches: ApprovalBatch[] }>(`/api/approvals?${query}`), 60_000);
  const batches = found.data?.batches ?? [];
  useStream(["approval", "action"], (event) => {
    // approval events carry the approval's id, action events the item's approval
    const batch = event.type.startsWith("approval") ? event.data.id : event.data.batch_id;
    if (batches.some((b) => b.id === batch) || items.includes(event.data.id)) found.reload();
  });
  if (found.error && !found.data) return <Meta>Approval: {found.error}</Meta>;
  return (
    <>
      {batches.map((batch) => (
        <ApprovalCard key={batch.id} batch={batch} onChange={found.reload} />
      ))}
    </>
  );
}

function ApprovalCard({ batch, onChange }: { batch: ApprovalBatch; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const waiting = batch.pending;
  const many = batch.total > 1;
  const command = batch.action === "host_command";
  const decide = async (decision: "approve" | "reject", rest = false) => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/approvals/${batch.id}`, rest ? { decision, rest } : { decision });
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : (e as Error).message);
    }
    setBusy(false);
    onChange();
  };
  // What did not happen comes first, so a failure is never hidden behind "show n more".
  const ordered = [...batch.items.filter((i) => failure(i)), ...batch.items.filter((i) => !failure(i))];
  const visible = all ? ordered : ordered.slice(0, SHOWN);
  const count = waiting > 1 ? ` all ${waiting}` : "";
  const shared = command ? [] : sharedArguments(batch.items);
  // (when the calls differ in nothing, each line keeps all of its arguments)
  const perItem = shared.length > 0 && shared.length < Object.keys(given(batch.items[0])).length;
  const settled = Object.entries(batch.counts)
    .filter(([status]) => status !== "pending")
    .map(([status, n]) => `${n} ${status.replace(/_/g, " ")}`)
    .join(", ");
  const tone = batchDot(batch);
  // The kit's Approval, with the list of items between its title and its buttons; the stripe
  // stays only while something waits.
  const list = (
    <Column gap={2}>
      {visible.map((item) =>
        command ? (
          <HostCommand key={item.id} item={item} />
        ) : (
          <Inline key={item.id} nowrap align="baseline" gap={2}>
            {many && <Lamp tone={statusDot(item.status)} blink={item.status === "approved"} title={STATUS_TEXT[item.status] ?? item.status} />}
            <Stack>
              <Text kind="data" wrap>
                {callArguments(item, perItem ? shared : []) || item.target}
              </Text>
              {many && !["pending", "executed"].includes(item.status) && !failure(item) && <Meta>{STATUS_TEXT[item.status] ?? item.status}</Meta>}
              {failure(item) && <ErrorText>{failure(item)}</ErrorText>}
            </Stack>
          </Inline>
        ),
      )}
    </Column>
  );
  return (
    <Approval
      inset
      decided={!waiting}
      title={
        command ? (
          <>{many ? `${batch.total} commands` : "Command"} to run as root on the host</>
        ) : (
          <>
            <Text kind="label">{batch.action}</Text> {batch.summary}
          </>
        )
      }
      note={batch.covered.length > 0 ? `Approved for this whole answer: later calls of ${batch.covered.join(", ")} run without asking and are listed here.` : undefined}
      actions={
        <>
          {waiting > 0 && (
            <>
              <Button variant="primary" disabled={busy} onClick={() => decide("approve")}>
                approve{count}
              </Button>
              {batch.rest.length > 0 && (
                <Button
                  disabled={busy}
                  title={`Approves ${waiting > 1 ? "these" : "this"}, and every later call of ${batch.rest.join(", ")} the model makes until this answer is finished. Each call is still listed here and in the audit log.`}
                  onClick={() => decide("approve", true)}
                >
                  approve for this whole answer
                </Button>
              )}
              <Button disabled={busy} onClick={() => decide("reject")}>
                deny{count}
              </Button>
            </>
          )}
          <Status tone={tone} blink={tone === "info"}>
            {many
              ? [waiting ? `${waiting} waiting for your approval` : "", settled].filter(Boolean).join(" · ")
              : (STATUS_TEXT[batch.items[0]?.status ?? ""] ?? settled)}{" "}
            · <a href={`#/approvals/${batch.id}`}>open in Approvals</a>
          </Status>
        </>
      }
    >
      {perItem && (
        <Text block>
          <Text dim>Each with </Text>
          <Code inline>{callArguments(batch.items[0], Object.keys(given(batch.items[0])).filter((k) => !shared.includes(k)))}</Code>
        </Text>
      )}
      {all ? <Scroll max="50vh">{list}</Scroll> : list}
      {batch.items.length > SHOWN && (
        <Actions>
          <Button variant="link" aria-expanded={all} onClick={() => setAll(!all)}>
            {all ? `show only the first ${SHOWN}` : `show ${batch.items.length - SHOWN} more`}
          </Button>
        </Actions>
      )}
      {error && <ErrorText>{error}</ErrorText>}
    </Approval>
  );
}
