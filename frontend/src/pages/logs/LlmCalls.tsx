import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import { OwnerRef } from "../../components/EntityRefs";
import { Actions, Button, Empty, ErrorText, Field, Input, Legend, Meta, Meter, Notice, Pane, Panel, Panes, Row, Select, Stack, Status, Table, Td, Text, TitleLink, Toolbar } from "../../kit";
import { useStream } from "../../live";
import { ms, purposeLabel, stamp, tokens } from "./format";
import { CallStatus, LogsHead, type CallsLink } from "./parts";
import type { LlmCallList, LlmCallSummary, TraceStats } from "./types";

const PAGE = 50;
const RANGES: { key: string; label: string; seconds: number | null }[] = [
  { key: "1h", label: "last hour", seconds: 3600 },
  { key: "24h", label: "last 24 hours", seconds: 86400 },
  { key: "7d", label: "last 7 days", seconds: 7 * 86400 },
  { key: "all", label: "everything stored", seconds: null },
];
/** Cells of the two meters of a row. */
const TOKEN_CELLS = 12;
const TIME_CELLS = 16;

export function LlmCallsList({ link }: { link: CallsLink | null }) {
  const [purpose, setPurpose] = useState("");
  const [status, setStatus] = useState("");
  const [range, setRange] = useState("all");
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<LlmCallList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<TraceStats | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const t = setTimeout(() => {
      setQ(text.trim());
      setOffset(0);
    }, 350);
    return () => clearTimeout(t);
  }, [text]);

  useEffect(() => {
    let alive = true;
    const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (purpose) params.set("purpose", purpose);
    if (status) params.set("status", status);
    const seconds = RANGES.find((r) => r.key === range)?.seconds;
    if (seconds) params.set("since", String(Date.now() / 1000 - seconds));
    if (q) params.set("q", q);
    if (link) {
      params.set("link_kind", link.kind);
      params.set("link_id", link.id);
      if (link.turn) params.set("turn", link.turn);
    }
    api.get<LlmCallList>(`/api/logs/llm?${params}`).then(
      (d) => {
        if (!alive) return;
        setData(d);
        setError(null);
        // One call, or a chat turn: open it (a turn's detail page carries the whole timeline).
        if (link && d.items.length > 0 && (d.items.length === 1 || link.turn)) {
          location.replace(`#/logs/llm/${d.items[d.items.length - 1].id}`);
        }
      },
      (e: Error) => alive && setError(e.message),
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [purpose, status, range, q, offset, tick, link?.kind, link?.id, link?.turn]);

  useEffect(() => {
    api.get<TraceStats>("/api/logs/llm/stats").then(setStats, () => undefined);
  }, [tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    const timer = setInterval(reload, 30_000);
    return () => clearInterval(timer);
  }, [reload]);
  useStream(["llm"], () => offset === 0 && reload());

  const items = data?.items ?? [];
  const filtered = Boolean(purpose || status || q || range !== "all" || link);
  // The meters of every row share one maximum, so the rows of a page compare.
  const maxTokens = Math.max(0, ...items.map((c) => c.prompt_tokens ?? 0));
  const maxMs = Math.max(0, ...items.map((c) => c.total_ms ?? 0));
  return (
    <>
      <LogsHead
        tab="llm"
        link={link}
        tools={
      <Toolbar>
        <Field label="search" flex="3 1 280px">
          <Input type="search" placeholder="Search prompts and answers" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search in messages" />
        </Field>
        <Field label="purpose" flex="1 1 180px">
          <Select value={purpose} onChange={(e) => { setPurpose(e.target.value); setOffset(0); }} aria-label="Purpose">
            <option value="">all purposes</option>
            {(stats?.purposes ?? []).map((p) => (
              <option key={p.purpose} value={p.purpose}>{purposeLabel(p.purpose)} ({p.count})</option>
            ))}
          </Select>
        </Field>
        <Field label="status" flex="1 1 150px">
          <Select value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }} aria-label="Status">
            <option value="">any status</option>
            <option value="ok">OK</option>
            <option value="error">failed</option>
            <option value="cancelled">cancelled</option>
          </Select>
        </Field>
        <Field label="range" flex="1 1 180px">
          <Select value={range} onChange={(e) => { setRange(e.target.value); setOffset(0); }} aria-label="Time range">
            {RANGES.map((r) => (<option key={r.key} value={r.key}>{r.label}</option>))}
          </Select>
        </Field>
      </Toolbar>
        }
      />
      <Panes>
      <Pane bare label="LLM calls">
      {stats && !stats.enabled && <Notice>LLM call logging is switched off. New calls are not recorded.</Notice>}
      {stats && stats.enabled && !stats.store_bodies && (
        <Notice>Only metadata is stored (no prompts or answers). Turn on full bodies in the log settings to see what was sent.</Notice>
      )}
      {error && <ErrorText>{error}</ErrorText>}
      {data && items.length === 0 && (
        <Panel>
          <Empty>{filtered ? "No calls match these filters." : "No LLM calls recorded yet. They appear here as soon as you ask the chat something or the agent runs."}</Empty>
        </Panel>
      )}
      {items.length > 0 && (
        <Panel>
          <Table
            stack
            stickyHead
            head={[
              "time",
              "purpose · model",
              { label: "prompt or answer · where it ran", wrap: true, minWidth: "140px" },
              "status",
              { label: "tokens in → out · bar = in", wrap: true },
              {
                label: "time: first token, total",
                wrap: true,
                head: (
                  <Legend>
                    <span>time</span>
                    <Status tone="info" plain>first token</Status>
                    <Status tone="ok" plain>total</Status>
                  </Legend>
                ),
              },
            ]}
          >
            <tbody>
              {items.map((c) => (<CallRow key={c.id} call={c} maxTokens={maxTokens} maxMs={maxMs} />))}
            </tbody>
          </Table>
          <Row>
            <Meta>
              {offset + 1}–{offset + items.length} of {data?.total}
              {data && !data.complete && " · search covers the newest 2,000 calls"}
            </Meta>
            <Actions toEnd>
              <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>newer</Button>
              <Button disabled={!data || offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>older</Button>
            </Actions>
          </Row>
        </Panel>
      )}
      </Pane>
      </Panes>
    </>
  );
}

function CallRow({ call: c, maxTokens, maxMs }: { call: LlmCallSummary; maxTokens: number; maxMs: number }) {
  const preview = c.status !== "ok" && c.error ? c.error : c.response_preview || c.request_preview || "";
  return (
    <tr>
      <Td nowrap>
        <Meta>{stamp(c.ts)}</Meta>
      </Td>
      <Td>
        <Stack>
          <Text kind="label">
            {purposeLabel(c.purpose).toLowerCase()}
            {c.attempt > 1 && ` · retry ${c.attempt}`}
            {c.tool_calls_count > 0 && ` · ${c.tool_calls_count} tool call${c.tool_calls_count === 1 ? "" : "s"}`}
          </Text>
          <Meta>{c.model ?? "—"}</Meta>
        </Stack>
      </Td>
      <Td fill tone={c.status === "error" ? "crit" : undefined}>
        <Stack>
          {/* The row's way into the call: its preview, or its number when nothing was stored to preview. */}
          <TitleLink truncate href={`#/logs/llm/${c.id}`}>
            {preview || `call #${c.id}`}
          </TitleLink>
          {c.link_kind && (
            <Meta>
              <OwnerRef kind={c.link_kind} id={c.link_id} step={c.link_step} />
            </Meta>
          )}
        </Stack>
      </Td>
      <Td>
        <CallStatus status={c.status} />
      </Td>
      <Td nowrap>
        <Stack gap={1}>
          <Meter value={c.prompt_tokens ?? 0} max={maxTokens} count={TOKEN_CELLS} tone="text" />
          <Meta>{tokens(c)}</Meta>
        </Stack>
      </Td>
      <Td>
        <Stack gap={1}>
          <Meter value={c.total_ms ?? 0} inner={c.ttft_ms ?? undefined} innerTone="info" max={maxMs} count={TIME_CELLS} tone="ok" />
          <Meta>
            first {ms(c.ttft_ms)} · total {ms(c.total_ms)}
          </Meta>
        </Stack>
      </Td>
    </tr>
  );
}
