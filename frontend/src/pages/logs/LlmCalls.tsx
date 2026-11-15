import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import { useStream } from "../../live";
import { Empty } from "../../ui";
import { StatusMark } from "./CallDetail";
import { linkLabel, ms, purposeLabel, stamp, tokens } from "./format";
import type { LlmCallList, LlmCallSummary, TraceStats } from "./types";

const PAGE = 50;
const RANGES: { key: string; label: string; seconds: number | null }[] = [
  { key: "1h", label: "Last hour", seconds: 3600 },
  { key: "24h", label: "Last 24 hours", seconds: 86400 },
  { key: "7d", label: "Last 7 days", seconds: 7 * 86400 },
  { key: "all", label: "Everything stored", seconds: null },
];

export function LlmCallsList({ link }: { link: { kind: string; id: string; turn?: string } | null }) {
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
  return (
    <div className="stack">
      {stats && !stats.enabled && <div className="notice">LLM call logging is switched off. New calls are not recorded.</div>}
      {stats && stats.enabled && !stats.store_bodies && (
        <div className="notice">Only metadata is stored (no prompts or answers). Turn on full bodies in the log settings to see what was sent.</div>
      )}
      {link && (
        <p className="muted">
          Calls belonging to {link.kind} {link.id} · <a href="#/logs/llm">show all</a>
        </p>
      )}
      <div className="log-filters">
        <input type="search" className="log-search" placeholder="Search prompts and answers" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search in messages" />
        <select value={purpose} onChange={(e) => { setPurpose(e.target.value); setOffset(0); }} aria-label="Purpose">
          <option value="">All purposes</option>
          {(stats?.purposes ?? []).map((p) => (
            <option key={p.purpose} value={p.purpose}>{purposeLabel(p.purpose)} ({p.count})</option>
          ))}
        </select>
        <select value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }} aria-label="Status">
          <option value="">Any status</option>
          <option value="ok">OK</option>
          <option value="error">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <select value={range} onChange={(e) => { setRange(e.target.value); setOffset(0); }} aria-label="Time range">
          {RANGES.map((r) => (<option key={r.key} value={r.key}>{r.label}</option>))}
        </select>
      </div>

      {error && <p className="error">{error}</p>}
      {data && items.length === 0 && (
        <Empty>{filtered ? "No calls match these filters." : "No LLM calls recorded yet. They appear here as soon as a report runs or you ask the chat something."}</Empty>
      )}
      {items.length > 0 && (
        <section className="panel call-list-panel">
          <div className="call-row call-header" aria-hidden>
            <span>Time</span><span>Call</span><span>Status</span><span className="num">Tokens in → out</span><span className="num">First token</span><span className="num">Total</span>
          </div>
          <ul className="call-list">
            {items.map((c) => (<CallRow key={c.id} call={c} />))}
          </ul>
          <div className="pager">
            <span className="muted small">
              {offset + 1}–{offset + items.length} of {data?.total}
              {data && !data.complete && " · search covers the newest 2,000 calls"}
            </span>
            <span className="pager-buttons">
              <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Newer</button>
              <button className="btn" disabled={!data || offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>Older</button>
            </span>
          </div>
        </section>
      )}
    </div>
  );
}

function CallRow({ call: c }: { call: LlmCallSummary }) {
  const label = linkLabel(c);
  return (
    <li>
      <a className={`call-row${c.status !== "ok" ? " call-bad" : ""}`} href={`#/logs/llm/${c.id}`}>
        <span className="call-time">{stamp(c.ts)}</span>
        <span className="call-what">
          <span className="call-purpose">
            {purposeLabel(c.purpose)}
            {c.attempt > 1 && <span className="muted"> · retry {c.attempt}</span>}
            {c.tool_calls_count > 0 && <span className="muted"> · {c.tool_calls_count} tool call{c.tool_calls_count === 1 ? "" : "s"}</span>}
          </span>
          <span className="muted call-model mono">{c.model ?? "—"}</span>
          <span className="call-preview">{c.status !== "ok" && c.error ? c.error : (c.response_preview || c.request_preview || "")}</span>
          {label && <span className="call-link muted">{label}</span>}
        </span>
        <span className="call-status"><StatusMark status={c.status} /></span>
        <span className="call-nums">
          <span className="num"><span className="lbl">Tokens </span>{tokens(c)}</span>
          <span className="num"><span className="lbl">First token </span>{ms(c.ttft_ms)}</span>
          <span className="num"><span className="lbl">Total </span>{ms(c.total_ms)}</span>
        </span>
      </a>
    </li>
  );
}
