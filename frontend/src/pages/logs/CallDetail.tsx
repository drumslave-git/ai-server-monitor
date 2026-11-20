import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { api } from "../../api";
import { bytes } from "../../fmt";
import { Actions, Bar, Button, Code, Empty, ErrorText, Fold, KeyValue, Legend, LinkButton, Meta, Notice, PageHeader, Panel, Row, Split, Status } from "../../kit";
import { approxTokens, linkHref, linkLabel, messageText, ms, prettyJson, purposeLabel, stamp, tokens } from "./format";
import { CallStatus, Scroll, Section } from "./parts";
import { TimelinePanel } from "./Timeline";
import type { ChatMessage, LlmCall, ResponseToolCall, ToolSpec } from "./types";

const SHOW_CHARS = 20_000;
const BACK = { label: "all LLM calls", href: "#/logs/llm" };

/** Panels one under the other inside a Split. */
const column = (flex: string): CSSProperties => ({ flex, minWidth: 0, display: "flex", flexDirection: "column", gap: "var(--space-4)" });

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
      <>
        <PageHeader title="Logs" back={BACK} meta={`call #${id}`} />
        <ErrorText>{error}</ErrorText>
      </>
    );
  }
  if (!call) {
    return (
      <>
        <PageHeader title="Logs" back={BACK} meta={`call #${id}`} />
        <Meta>Loading…</Meta>
      </>
    );
  }
  return <Loaded call={call} />;
}

/** Expand-all / collapse-all: a counter so pressing the same button twice still acts. */
type FoldAll = { open: boolean; n: number } | null;

function Loaded({ call }: { call: LlmCall }) {
  const [copied, setCopied] = useState(false);
  const [fold, setFold] = useState<FoldAll>(null);

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
  const facts: [ReactNode, ReactNode][] = [
    ["when", stamp(call.ts)],
    ["model", call.model ?? "—"],
    ["endpoint", `${call.base_url ?? "—"}${call.stream ? " · streamed" : ""}`],
    [
      "tokens",
      <>
        {tokens(call)}
        {call.tokens_per_s ? <span className="fd-dim"> · {call.tokens_per_s} tokens/s</span> : null}
      </>,
    ],
    ["finish", call.finish_reason ?? "—"],
    ["request", `${bytes(call.request_bytes)}, ${call.messages_count ?? 0} messages${call.tools_count ? `, ${call.tools_count} tools` : ""}`],
  ];
  if (call.attempt > 1) facts.push(["attempt", `${call.attempt} (earlier ones failed)`]);
  if (label) facts.push(["belongs to", href ? <a href={href}>{label}</a> : label]);

  return (
    <>
      <PageHeader title={purposeLabel(call.purpose)} back={BACK} meta={`call #${call.id}`}>
        <CallStatus status={call.status} />
        <Button onClick={() => setFold({ open: true, n: (fold?.n ?? 0) + 1 })}>expand all</Button>
        <Button onClick={() => setFold({ open: false, n: (fold?.n ?? 0) + 1 })}>collapse all</Button>
        <Button onClick={copy}>{copied ? "copied" : "copy as JSON"}</Button>
        <LinkButton href={`/api/logs/llm/${call.id}/export`} download>
          download
        </LinkButton>
      </PageHeader>

      {call.error && (
        <Notice title={<Status tone="crit">{call.http_status ? `HTTP ${call.http_status}` : "Request failed"}</Status>}>
          <Scroll max={200}>
            <Code wrap>{call.error}</Code>
          </Scroll>
        </Notice>
      )}
      {!call.bodies && <Notice>Only metadata was stored for this call (full bodies are switched off in the log settings).</Notice>}
      {call.truncated.length > 0 && <Notice>Stored with a size cap: {call.truncated.join(", ")} cut off. The marker “chars cut” shows where.</Notice>}

      <Split>
        <div style={column("3 1 560px")}>
          <Panel title="Overview">
            <div className="fd-panel-pad">
              <KeyValue items={facts} />
            </div>
            <Timing call={call} />
            <Params params={call.params} />
          </Panel>

          <TimelinePanel callId={call.id} />
        </div>

        <div style={column("2 1 380px")}>
          <Panel title="Sent to the model" meta={`${call.messages?.length ?? 0} messages`}>
            {call.messages ? (
              call.messages.map((m, i) => <Message key={i} message={m} index={i} last={i === call.messages!.length - 1} fold={fold} />)
            ) : (
              <Empty>Not stored.</Empty>
            )}
            {call.tools && call.tools.length > 0 && <ToolsSent tools={call.tools} tokensEstimate={call.tools_tokens} fold={fold} />}
          </Panel>

          <Panel title="Received">
            <Response call={call} fold={fold} />
          </Panel>
        </div>
      </Split>
    </>
  );
}

/** Where the call's time went, as one bar: the wait in the queue is the empty lead of the track,
 *  then the wait for the first token, then generating. */
function Timing({ call }: { call: LlmCall }) {
  const queued = call.queued_ms ?? 0;
  const total = call.total_ms ?? 0;
  const ttft = call.ttft_ms;
  const parts: { key: string; label: string; value: number; tone: "off" | "info" | "ok" }[] = [];
  if (queued >= 5) parts.push({ key: "q", label: "Waited", value: queued, tone: "off" });
  if (ttft !== null && ttft !== undefined) {
    parts.push({ key: "t", label: "To first token", value: ttft, tone: "info" });
    parts.push({ key: "g", label: "Generating", value: Math.max(total - ttft, 0), tone: "ok" });
  } else {
    parts.push({ key: "r", label: "Waiting for the reply", value: total, tone: "ok" });
  }
  const sum = parts.reduce((a, p) => a + p.value, 0);
  const lead = queued >= 5 ? queued : 0;
  return (
    <Row>
      <Bar
        size="sm"
        flex="1 0 100%"
        start={lead}
        inner={ttft !== null && ttft !== undefined ? lead + ttft : undefined}
        end={sum}
        total={sum}
        label={parts.map((p) => `${p.label} ${ms(p.value)}`).join(", ")}
      />
      <div style={{ flex: "1 0 100%", minWidth: 0 }}>
        <Legend>
          {parts.map((p) => (
            <Status key={p.key} tone={p.tone} hollow={p.tone === "off"} plain>
              {p.label.toLowerCase()} {ms(p.value)}
            </Status>
          ))}
          <span style={{ marginLeft: "auto" }}>total {ms(total + queued)}</span>
        </Legend>
      </div>
    </Row>
  );
}

function Params({ params }: { params: Record<string, unknown> }) {
  const entries = Object.entries(params).filter(([k]) => k !== "response_model");
  if (entries.length === 0) return null;
  const shown = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));
  return (
    <Row>
      {entries.map(([k, v]) => (
        <span key={k} className="data fd-wrap" style={{ minWidth: 0 }}>
          <span className="fd-dim">{k}</span> {shown(v)}
        </span>
      ))}
    </Row>
  );
}

function useFold(fold: FoldAll, initial: boolean) {
  const [open, setOpen] = useState(initial);
  useEffect(() => {
    if (fold) setOpen(fold.open);
  }, [fold]);
  return [open, setOpen] as const;
}

/** A kit Fold that follows expand all / collapse all and builds its body only while open.
 *  peek is a taste of the content, shown in the head while the fold is closed. */
function Folded({
  tag,
  title,
  stats,
  peek,
  initial = false,
  fold,
  children,
}: {
  tag: string;
  title: ReactNode;
  stats: string;
  peek?: string;
  initial?: boolean;
  fold: FoldAll;
  children: ReactNode;
}) {
  const [open, setOpen] = useFold(fold, initial);
  return (
    <Fold tag={tag} title={title} meta={open || !peek ? stats : `${stats} · ${peek}`} open={open} onToggle={setOpen}>
      {open && children}
    </Fold>
  );
}

/** Text quoted exactly; very long text is cut with a button to show the rest. */
function Body({ text, json = false, wrap = !json }: { text: string; json?: boolean; wrap?: boolean }) {
  const [all, setAll] = useState(false);
  const content = useMemo(() => (json ? prettyJson(text) : text), [text, json]);
  const long = content.length > SHOW_CHARS && !all;
  return (
    <>
      <Scroll max={480}>
        <Code wrap={wrap}>{long ? content.slice(0, SHOW_CHARS) : content}</Code>
      </Scroll>
      {long && (
        <Actions>
          <Button variant="link" onClick={() => setAll(true)}>
            show all {content.length.toLocaleString()} characters
          </Button>
        </Actions>
      )}
    </>
  );
}

function Message({ message, index, last, fold }: { message: ChatMessage; index: number; last: boolean; fold: FoldAll }) {
  const text = messageText(message.content);
  const role = message.role;
  const calls = message.tool_calls ?? [];
  const lastUser = role === "user" && last;
  const stats = `${text.length.toLocaleString()} chars · ~${approxTokens(text.length).toLocaleString()} tokens`;
  return (
    <Folded
      fold={fold}
      initial={lastUser || (role === "tool" && last)}
      tag={role}
      title={`#${index + 1}`}
      stats={message.name ? `${message.name} · ${stats}` : stats}
      peek={text.replace(/\s+/g, " ").slice(0, 90)}
    >
      {text && <Body text={text} />}
      {calls.length > 0 && (
        <Section title="tool calls requested">
          {calls.map((c, i) => (
            <Body key={i} json text={JSON.stringify({ id: c.id, name: c.function?.name, arguments: parse(c.function?.arguments) }, null, 2)} />
          ))}
        </Section>
      )}
      {message.tool_call_id && <Meta>answers call {message.tool_call_id}</Meta>}
    </Folded>
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

function ToolsSent({ tools, tokensEstimate, fold }: { tools: ToolSpec[]; tokensEstimate: number | null; fold: FoldAll }) {
  const json = JSON.stringify(tools, null, 2);
  return (
    <Folded
      fold={fold}
      tag="tools"
      title={`${tools.length} defined`}
      stats={`${json.length.toLocaleString()} chars · ~${(tokensEstimate ?? approxTokens(json.length)).toLocaleString()} tokens`}
      peek={tools.map((t) => t.function?.name).filter(Boolean).join(", ")}
    >
      <KeyValue items={tools.map((t): [ReactNode, ReactNode] => [t.function?.name, (t.function?.description ?? "").split("\n")[0].slice(0, 140)])} />
      <Body text={json} wrap={false} />
    </Folded>
  );
}

function Response({ call, fold }: { call: LlmCall; fold: FoldAll }) {
  const calls: ResponseToolCall[] = call.tool_calls ?? [];
  const nothing = !call.content && !call.reasoning && calls.length === 0;
  if (!call.bodies) return <Empty>Not stored.</Empty>;
  return (
    <>
      {call.reasoning && (
        <Folded
          fold={fold}
          tag="reasoning"
          title={`${call.reasoning.length.toLocaleString()} chars`}
          stats={`~${approxTokens(call.reasoning.length).toLocaleString()} tokens`}
        >
          <Body text={call.reasoning} />
        </Folded>
      )}
      {(call.content || calls.length > 0) && (
        <div className="fd-panel-pad" style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
          {call.content && (
            <Section title={`answer · ${call.content.length.toLocaleString()} chars`}>
              <Body text={call.content} />
            </Section>
          )}
          {calls.length > 0 && (
            <Section title={`tool calls · ${calls.length}`}>
              {calls.map((c) => (
                <Body key={c.id} json text={JSON.stringify({ id: c.id, name: c.name, arguments: parse(c.arguments) }, null, 2)} />
              ))}
            </Section>
          )}
        </div>
      )}
      {nothing && <Empty>{call.status === "ok" ? "The model returned an empty answer." : "No answer was received."}</Empty>}
    </>
  );
}
