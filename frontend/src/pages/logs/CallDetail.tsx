import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../../api";
import { bytes } from "../../fmt";
import { Empty, Panel } from "../../ui";
import { TimelinePanel } from "./Timeline";
import type { ChatMessage, LlmCall, ResponseToolCall, ToolSpec } from "./types";
import { approxTokens, linkHref, linkLabel, messageText, ms, prettyJson, purposeLabel, stamp, tokens } from "./format";

const SHOW_CHARS = 20_000;

export function CallDetail({ id }: { id: number }) {
  const [call, setCall] = useState<LlmCall | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setCall(null);
    setError(null);
    api.get<LlmCall>(`/api/logs/llm/${id}`).then(setCall, (e: Error) => setError(e.message));
  }, [id]);

  if (error) {
    return (
      <div className="stack">
        <a className="back-link" href="#/logs/llm">← All LLM calls</a>
        <p className="error">{error}</p>
      </div>
    );
  }
  if (!call) return <p className="muted">Loading…</p>;
  return <Loaded call={call} />;
}

function Loaded({ call }: { call: LlmCall }) {
  const [copied, setCopied] = useState(false);
  // Expand-all / collapse-all: a counter so pressing the same button twice still acts.
  const [fold, setFold] = useState<{ open: boolean; n: number } | null>(null);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(call, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked: the download button still works */
    }
  };

  const href = linkHref(call);
  const label = linkLabel(call);
  return (
    <div className="stack call-detail">
      <a className="back-link" href="#/logs/llm">← All LLM calls</a>
      <header className="call-head">
        <div className="call-title">
          <h2>{purposeLabel(call.purpose)}</h2>
          <StatusMark status={call.status} />
          <span className="muted">call #{call.id}</span>
        </div>
        <div className="page-actions">
          <button className="btn" onClick={() => setFold({ open: true, n: (fold?.n ?? 0) + 1 })}>Expand all</button>
          <button className="btn" onClick={() => setFold({ open: false, n: (fold?.n ?? 0) + 1 })}>Collapse all</button>
          <button className="btn" onClick={copy}>{copied ? "Copied" : "Copy as JSON"}</button>
          <a className="btn" href={`/api/logs/llm/${call.id}/export`} download>Download</a>
        </div>
      </header>

      {call.error && (
        <div className="notice notice-error">
          <strong>{call.http_status ? `HTTP ${call.http_status}` : "Request failed"}</strong>
          <pre>{call.error}</pre>
        </div>
      )}
      {!call.bodies && <div className="notice">Only metadata was stored for this call (full bodies are switched off in the log settings).</div>}
      {call.truncated.length > 0 && (
        <div className="notice">Stored with a size cap: {call.truncated.join(", ")} cut off. The marker “chars cut” shows where.</div>
      )}

      <Panel title="Overview">
        <dl className="call-facts">
          <dt>When</dt><dd>{stamp(call.ts)}</dd>
          <dt>Model</dt><dd className="mono">{call.model ?? "—"}</dd>
          <dt>Endpoint</dt><dd className="mono">{call.base_url ?? "—"}{call.stream ? " · streamed" : ""}</dd>
          <dt>Tokens</dt><dd>{tokens(call)}{call.tokens_per_s ? <span className="muted"> · {call.tokens_per_s} tokens/s</span> : null}</dd>
          <dt>Finish</dt><dd>{call.finish_reason ?? "—"}</dd>
          <dt>Request</dt><dd>{bytes(call.request_bytes)}, {call.messages_count ?? 0} messages{call.tools_count ? `, ${call.tools_count} tools` : ""}</dd>
          {call.attempt > 1 && (<><dt>Attempt</dt><dd>{call.attempt} (earlier ones failed)</dd></>)}
          {label && (<><dt>Belongs to</dt><dd>{href ? <a href={href}>{label}</a> : label}</dd></>)}
        </dl>
        <TimingBar call={call} />
        <Params params={call.params} />
      </Panel>

      <TimelinePanel callId={call.id} />

      <Panel title="Sent to the model" aside={<span className="muted">{call.messages?.length ?? 0} messages</span>}>
        {call.messages ? (
          <ul className="msgs">
            {call.messages.map((m, i) => (
              <Message key={i} message={m} index={i} last={i === call.messages!.length - 1} fold={fold} />
            ))}
          </ul>
        ) : (
          <Empty>Not stored.</Empty>
        )}
        {call.tools && call.tools.length > 0 && <ToolsSent tools={call.tools} tokensEstimate={call.tools_tokens} fold={fold} />}
      </Panel>

      <Panel title="Received">
        <Response call={call} fold={fold} />
      </Panel>
    </div>
  );
}

export function StatusMark({ status }: { status: string }) {
  const level = status === "ok" ? "ok" : status === "cancelled" ? "warn" : "crit";
  return (
    <span className={`status-mark status-${level}`}>
      <span className={`dot dot-${level}`} aria-hidden />
      {status === "ok" ? "OK" : status === "cancelled" ? "Cancelled" : "Failed"}
    </span>
  );
}

function TimingBar({ call }: { call: LlmCall }) {
  const queued = call.queued_ms ?? 0;
  const total = call.total_ms ?? 0;
  const ttft = call.ttft_ms;
  const parts: { key: string; label: string; value: number; tone: string }[] = [];
  if (queued >= 5) parts.push({ key: "q", label: "Waited", value: queued, tone: "queue" });
  if (ttft !== null && ttft !== undefined) {
    parts.push({ key: "t", label: "To first token", value: ttft, tone: "ttft" });
    parts.push({ key: "g", label: "Generating", value: Math.max(total - ttft, 0), tone: "gen" });
  } else {
    parts.push({ key: "r", label: "Waiting for the reply", value: total, tone: "gen" });
  }
  const sum = parts.reduce((a, p) => a + p.value, 0) || 1;
  return (
    <div className="timing">
      <div className="timing-bar" role="img" aria-label={parts.map((p) => `${p.label} ${ms(p.value)}`).join(", ")}>
        {parts.map((p) => (
          <span key={p.key} className={`timing-seg seg-${p.tone}`} style={{ width: `${Math.max((p.value / sum) * 100, 0.8)}%` }} />
        ))}
      </div>
      <ul className="timing-legend">
        {parts.map((p) => (
          <li key={p.key}>
            <span className={`swatch seg-${p.tone}`} aria-hidden />
            {p.label} <strong>{ms(p.value)}</strong>
          </li>
        ))}
        <li className="timing-total">Total <strong>{ms(total + queued)}</strong></li>
      </ul>
    </div>
  );
}

function Params({ params }: { params: Record<string, unknown> }) {
  const entries = Object.entries(params).filter(([k]) => k !== "response_model");
  if (entries.length === 0) return null;
  const shown = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));
  return (
    <p className="params">
      {entries.map(([k, v]) => (
        <span key={k} className="param"><span className="muted">{k}</span> {shown(v)}</span>
      ))}
    </p>
  );
}

function useFold(fold: { open: boolean; n: number } | null, initial: boolean) {
  const [open, setOpen] = useState(initial);
  useEffect(() => {
    if (fold) setOpen(fold.open);
  }, [fold]);
  return [open, setOpen] as const;
}

function Fold({
  summary,
  initial = false,
  fold,
  children,
  className = "",
}: {
  summary: ReactNode;
  initial?: boolean;
  fold: { open: boolean; n: number } | null;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useFold(fold, initial);
  return (
    <details className={`fold ${className}`} open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>{summary}</summary>
      {open && <div className="fold-body">{children}</div>}
    </details>
  );
}

/** Text in a box; very long text is cut with a button to show the rest. */
function Body({ text, json = false }: { text: string; json?: boolean }) {
  const [all, setAll] = useState(false);
  const content = useMemo(() => (json ? prettyJson(text) : text), [text, json]);
  const long = content.length > SHOW_CHARS && !all;
  return (
    <div className="body">
      <pre className="mono-box">{long ? content.slice(0, SHOW_CHARS) : content}</pre>
      {long && (
        <button className="btn btn-quiet" onClick={() => setAll(true)}>
          Show all {content.length.toLocaleString()} characters
        </button>
      )}
    </div>
  );
}

function Message({ message, index, last, fold }: { message: ChatMessage; index: number; last: boolean; fold: { open: boolean; n: number } | null }) {
  const text = messageText(message.content);
  const role = message.role;
  const calls = message.tool_calls ?? [];
  const lastUser = role === "user" && last;
  return (
    <li>
      <Fold
        fold={fold}
        initial={lastUser || (role === "tool" && last)}
        className={`msg msg-${role}`}
        summary={
          <span className="msg-sum">
            <span className="role">{role}</span>
            {message.name && <span className="mono muted">{message.name}</span>}
            <span className="muted">
              #{index + 1} · {text.length.toLocaleString()} chars · ~{approxTokens(text.length).toLocaleString()} tokens
            </span>
            <span className="msg-peek">{text.replace(/\s+/g, " ").slice(0, 90)}</span>
          </span>
        }
      >
        {text && <Body text={text} />}
        {calls.length > 0 && (
          <div>
            <h4>Tool calls requested</h4>
            {calls.map((c, i) => (
              <Body key={i} json text={JSON.stringify({ id: c.id, name: c.function?.name, arguments: parse(c.function?.arguments) }, null, 2)} />
            ))}
          </div>
        )}
        {message.tool_call_id && <p className="muted small">answers call {message.tool_call_id}</p>}
      </Fold>
    </li>
  );
}

function parse(text: string | undefined): unknown {
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function ToolsSent({ tools, tokensEstimate, fold }: { tools: ToolSpec[]; tokensEstimate: number | null; fold: { open: boolean; n: number } | null }) {
  const json = JSON.stringify(tools, null, 2);
  return (
    <div className="tools-sent">
      <Fold
        fold={fold}
        summary={
          <span className="msg-sum">
            <span className="role">tools</span>
            <span className="muted">
              {tools.length} defined · {json.length.toLocaleString()} chars · ~{(tokensEstimate ?? approxTokens(json.length)).toLocaleString()} tokens
            </span>
            <span className="msg-peek">{tools.map((t) => t.function?.name).filter(Boolean).join(", ")}</span>
          </span>
        }
      >
        <ul className="tool-names">
          {tools.map((t, i) => (
            <li key={i}>
              <span className="mono">{t.function?.name}</span>
              <span className="muted"> {(t.function?.description ?? "").split("\n")[0].slice(0, 140)}</span>
            </li>
          ))}
        </ul>
        <Body text={json} />
      </Fold>
    </div>
  );
}

function Response({ call, fold }: { call: LlmCall; fold: { open: boolean; n: number } | null }) {
  const calls: ResponseToolCall[] = call.tool_calls ?? [];
  const nothing = !call.content && !call.reasoning && calls.length === 0;
  if (!call.bodies) return <Empty>Not stored.</Empty>;
  return (
    <div className="response">
      {call.reasoning && (
        <Fold
          fold={fold}
          summary={
            <span className="msg-sum">
              <span className="role">reasoning</span>
              <span className="muted">{call.reasoning.length.toLocaleString()} chars · ~{approxTokens(call.reasoning.length).toLocaleString()} tokens</span>
            </span>
          }
        >
          <Body text={call.reasoning} />
        </Fold>
      )}
      {call.content && (
        <div>
          <h4>Answer · {call.content.length.toLocaleString()} chars</h4>
          <Body text={call.content} />
        </div>
      )}
      {calls.length > 0 && (
        <div>
          <h4>Tool calls · {calls.length}</h4>
          {calls.map((c) => (
            <Body key={c.id} json text={JSON.stringify({ id: c.id, name: c.name, arguments: parse(c.arguments) }, null, 2)} />
          ))}
        </div>
      )}
      {nothing && <Empty>{call.status === "ok" ? "The model returned an empty answer." : "No answer was received."}</Empty>}
    </div>
  );
}
