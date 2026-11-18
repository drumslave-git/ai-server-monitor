import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type ActionRecord, type Schemas } from "../api";
import { HostCommand } from "../components/HostCommand";
import { ago } from "../fmt";
import { takeExplain, type ExplainRequest } from "../components/Explain";
import { useStream } from "../live";
import { Dot, Empty, Markdown, PageHead, useNow, usePoll, type Level } from "../ui";

type SessionList = Schemas["ChatSessionList"];
type SessionDoc = Schemas["ChatSession"];

type ToolUse = {
  id: string;
  name: string;
  args?: unknown;
  ok?: boolean;
  preview?: string;
  kind?: string | null;
  /** Seconds the tool has been running (progress events while it runs). */
  elapsed?: number;
  approval?: { id: number; status: string };
};
/** The first message of an Explain conversation: shown as its label, the source data folded. */
type ExplainNote = { label: string; source: string };
type Turn =
  | { role: "user"; text: string; explain?: ExplainNote }
  | {
      role: "assistant";
      text: string;
      tools: ToolUse[];
      live?: boolean;
      error?: string;
      stopped?: boolean;
      /** The run stopped for these approvals (action ids); `text` is the waiting note. */
      waiting?: number[];
      /** Set once the note was rewritten to what was decided: action id → final status. */
      outcome?: Record<string, string>;
      /** Logs page link: the session and the turn id of this answer. */
      session?: string;
      trace?: string;
    };

/** "45 s", "3 min 20 s": how long a tool has been running. */
function running(s: number): string {
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

/** A write tool's result mentions the audit record it created: {"action_id": 7, "status": "pending", ...}. */
function approvalOf(text: string | undefined): ToolUse["approval"] {
  if (!text) return undefined;
  const id = /"action_id"\s*:\s*(\d+)/.exec(text)?.[1];
  const status = /"status"\s*:\s*"(\w+)"/.exec(text)?.[1];
  return id && status ? { id: Number(id), status } : undefined;
}

/** Rebuild the thread from stored messages: tool traffic folds into the answer it led to. */
function toTurns(doc: SessionDoc): Turn[] {
  const turns: Turn[] = [];
  let current: Extract<Turn, { role: "assistant" }> | null = null;
  const open = () => (current ??= { role: "assistant", text: "", tools: [] });
  for (const m of doc.messages) {
    if (m.role === "user") {
      if (current) turns.push(current);
      current = null;
      const label = (m.meta as any)?.explain?.label;
      turns.push({
        role: "user",
        text: m.content ?? "",
        explain: typeof label === "string" ? { label, source: m.content ?? "" } : undefined,
      });
    } else if (m.role === "assistant") {
      const turn = open();
      for (const call of m.tool_calls ?? []) {
        const fn = (call as any).function ?? call;
        turn.tools.push({ id: String((call as any).id ?? ""), name: String(fn.name ?? "tool"), args: fn.arguments });
      }
      if (!(m.tool_calls && m.tool_calls.length) && m.content) {
        turn.text = m.content;
        const waiting = (m.meta as any)?.waiting_for;
        if (Array.isArray(waiting) && waiting.length) turn.waiting = waiting.map(Number);
        const outcome = (m.meta as any)?.outcome;
        if (outcome && typeof outcome === "object") turn.outcome = outcome;
        const traceTurn = (m.meta as any)?.trace_turn;
        if (typeof traceTurn === "string") {
          turn.trace = traceTurn;
          turn.session = doc.id;
        }
        turns.push(turn);
        current = null;
      }
    } else if (m.role === "tool") {
      const turn = open();
      const use = turn.tools.find((t) => t.id === m.tool_call_id) ?? { id: m.tool_call_id ?? "", name: m.name ?? "tool" };
      if (!turn.tools.includes(use)) turn.tools.push(use);
      use.ok = (m.meta as any)?.ok;
      use.preview = (m.content ?? "").slice(0, 400);
      use.approval = approvalOf(m.content ?? "");
    }
  }
  if (current) turns.push(current);
  return turns;
}

const chatRoute = () => location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).slice(1);

export function ChatPage() {
  // The page follows the hash itself: starting a conversation rewrites it (replaceState)
  // without a hashchange event, so App's route would be stale.
  const [params, setParams] = useState(chatRoute);
  useEffect(() => {
    const onHash = () => setParams(chatRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const now = useNow(30_000);
  const sessions = usePoll(() => api.get<SessionList>("/api/chat/sessions?limit=50"), 60_000);
  const routeId = params[0] && params[0] !== "new" ? params[0] : null;
  const showThread = params[0] !== undefined;
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const sessionId = useRef<string | null>(null);
  /** The session whose last turn failed: its error is not stored, so the live thread stays. */
  const keepThread = useRef<string | null>(null);

  const load = useCallback(async (id: string) => {
    try {
      setTurns(toTurns(await api.get<SessionDoc>(`/api/chat/sessions/${id}`)));
      setLoadError(null);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (busy) return; // the live stream owns the thread meanwhile
    if (routeId && routeId === keepThread.current) return;
    keepThread.current = null;
    sessionId.current = routeId;
    if (routeId) void load(routeId);
    else setTurns([]);
  }, [routeId, busy, load]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [turns]);

  // A run that waited for an approval continues on the server: its answer arrives as
  // chat.message, the rewritten waiting note as chat.message.updated (prefix match).
  useStream(["chat.message"], (event) => {
    if (busy || !sessionId.current || event.data.session_id !== sessionId.current) return;
    void load(sessionId.current);
    sessions.reload();
  });

  const patchLast = (fn: (t: Extract<Turn, { role: "assistant" }>) => void) =>
    setTurns((all) => {
      const copy = [...all];
      const last = copy[copy.length - 1];
      if (last?.role === "assistant") {
        const next = { ...last, tools: last.tools.map((t) => ({ ...t })) };
        fn(next);
        copy[copy.length - 1] = next;
      }
      return copy;
    });

  const send = async () => {
    const message = draft.trim();
    if (!message || busy) return;
    setDraft("");
    await stream("/api/chat", { message, session_id: sessionId.current }, { role: "user", text: message });
  };

  // Explain (Dashboard, Jobs): a new conversation whose first message the server composes.
  useEffect(() => {
    const request = takeExplain();
    if (request) void explain(request);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const explain = async (request: ExplainRequest) => {
    sessionId.current = null;
    const label = request.finding === null ? "Explain this run" : "Explain this finding";
    await stream("/api/chat/explain", { run_id: request.runId, finding: request.finding }, { role: "user", text: "", explain: { label, source: "" } });
  };

  const stream = async (url: string, body: unknown, question: Turn) => {
    setBusy(true);
    keepThread.current = null;
    setTurns((t) => [...(sessionId.current ? t : []), question, { role: "assistant", text: "", tools: [], live: true }]);
    const controller = new AbortController();
    abort.current = controller;
    let finished = false;
    let failed = false;
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        const body = await response.json().catch(() => null);
        throw new Error(String(body?.detail ?? `HTTP ${response.status}`));
      }
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        let cut: number;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          const type = /^event: (.+)$/m.exec(frame)?.[1];
          const raw = /^data: (.+)$/m.exec(frame)?.[1];
          if (!type || !raw) continue;
          const data = JSON.parse(raw);
          if (type === "session") {
            sessionId.current = data.session_id;
            if (data.explain) {
              const note: ExplainNote = { label: data.explain.label, source: data.explain.source };
              setTurns((all) => all.map((t, i) => (i === 0 && t.role === "user" ? { ...t, text: note.source, explain: note } : t)));
            }
            history.replaceState(null, "", `#/chat/${data.session_id}`);
            setParams([data.session_id]);
          } else if (type === "tool_call") {
            // Whatever was streamed before a tool call was the model thinking aloud, not the answer.
            patchLast((t) => {
              t.text = "";
              t.tools.push({ id: data.id, name: data.name, args: data.args, kind: data.kind });
            });
          } else if (type === "tool_progress") {
            patchLast((t) => {
              const use = t.tools.find((x) => x.id === data.id);
              if (use) use.elapsed = data.elapsed_s;
            });
          } else if (type === "tool_result") {
            patchLast((t) => {
              const use = t.tools.find((x) => x.id === data.id);
              if (use) {
                use.ok = data.ok;
                use.preview = data.preview;
                if (use.kind === "write") use.approval = approvalOf(data.preview);
              }
            });
          } else if (type === "token") {
            patchLast((t) => void (t.text += data.text));
          } else if (type === "error") {
            patchLast((t) => void (t.error = data.message));
            finished = true;
            failed = true; // nothing of the answer is stored: keep the live thread with the error
            keepThread.current = sessionId.current;
          } else if (type === "done") {
            finished = true;
            patchLast((t) => {
              t.trace = data.trace_turn;
              t.session = data.session_id;
              if (Array.isArray(data.waiting_for) && data.waiting_for.length) t.waiting = data.waiting_for;
            });
            setBusy(false); // the title event may still follow: the composer need not wait for it
          } else if (type === "title") {
            sessions.reload();
          }
        }
      }
    } catch (e) {
      if ((e as Error).name === "AbortError") patchLast((t) => void (t.stopped = true));
      else patchLast((t) => void (t.error = (e as Error).message));
    } finally {
      // (The stream can outlive the answer by the title event: by then a new question may be
      // running, which owns `abort` and the busy flag.)
      const current = abort.current === controller;
      if (current) {
        abort.current = null;
        setBusy(false);
      }
      sessions.reload();
      // Replace the live thread with the stored one once the answer is complete.
      if (finished && !failed && current && sessionId.current) {
        const id = sessionId.current;
        window.setTimeout(() => {
          if (sessionId.current === id) void load(id);
        }, 50);
      }
    }
  };

  const remove = async (id: string) => {
    if (!confirm("Delete this conversation?")) return;
    await api.del(`/api/chat/sessions/${id}`);
    sessions.reload();
    if (id === routeId) location.hash = "#/chat/new";
  };

  const items = sessions.data?.items ?? [];
  return (
    <div className={`chat ${showThread ? "has-thread" : ""}`}>
      <PageHead title="Chat">
        <a className="btn" href="#/chat/new">
          New conversation
        </a>
      </PageHead>
      <div className="split">
        <nav className="history" aria-label="Conversations">
          {sessions.data && items.length === 0 && <Empty>No conversations yet.</Empty>}
          <ul>
            {items.map((s) => (
              <li key={s.id} className="session-item">
                <a href={`#/chat/${s.id}`} className={`history-row${s.id === routeId ? " selected" : ""}`}>
                  <span className="history-top">
                    <span className="history-time">{s.title || "Untitled"}</span>
                  </span>
                  <span className="history-summary">
                    {s.channel === "telegram" ? "Telegram · " : ""}
                    {ago(s.updated, now)} · {s.messages} messages
                  </span>
                </a>
                <button className="btn btn-quiet session-delete" onClick={() => remove(s.id)} aria-label={`Delete conversation: ${s.title || "Untitled"}`}>
                  Delete
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <section className="thread">
          <a className="back" href="#/chat">
            All conversations
          </a>
          {loadError && <p className="error">{loadError}</p>}
          {turns.length === 0 && !busy && (
            <Empty>Ask about the server: what is using the disk, why a container restarted, what changed since yesterday.</Empty>
          )}
          <div className="messages">
            {turns.map((t, i) =>
              t.role === "user" ? <UserBubble key={i} text={t.text} explain={t.explain} /> : <AssistantBubble key={i} turn={t} />,
            )}
          </div>
          <div ref={bottom} />
          <form
            className="composer"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <textarea
              rows={2}
              value={draft}
              placeholder="Ask about the server"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  // Enter that confirms an IME candidate (Japanese, Chinese, Korean) is not a send.
                  // keyCode 229: Safari reports the composition as ended before this keydown.
                  if (e.nativeEvent.isComposing || e.keyCode === 229) return;
                  e.currentTarget.form?.requestSubmit();
                }
              }}
              aria-label="Message"
            />
            {busy ? (
              <button type="button" className="btn" onClick={() => abort.current?.abort()}>
                Stop
              </button>
            ) : (
              <button type="submit" className="btn btn-primary" disabled={!draft.trim()}>
                Send
              </button>
            )}
          </form>
        </section>
      </div>
    </div>
  );
}

function UserBubble({ text, explain }: { text: string; explain?: ExplainNote }) {
  if (!explain) return <div className="bubble bubble-user">{text}</div>;
  return (
    <div className="bubble bubble-user bubble-explain">
      {explain.label}
      {explain.source && (
        <details className="explain-source">
          <summary>Source data</summary>
          <pre>{explain.source}</pre>
        </details>
      )}
    </div>
  );
}

// MCP tools are named server__tool for the model; show "server · tool" (mcp_client.display_name).
function displayName(name: string): string {
  const i = name.indexOf("__");
  return i > 0 ? `${name.slice(0, i)} · ${name.slice(i + 2)}` : name;
}

function AssistantBubble({ turn }: { turn: Extract<Turn, { role: "assistant" }> }) {
  const names = Array.from(new Set(turn.tools.map((t) => displayName(t.name))));
  // approvals this answer waits for, and older write tools whose result named a pending approval
  const approvals = Array.from(
    new Set([...(turn.waiting ?? []), ...turn.tools.filter((t) => t.approval?.status === "pending").map((t) => t.approval!.id)]),
  );
  return (
    <div className="bubble bubble-assistant">
      {turn.live &&
        turn.tools
          .filter((t) => t.ok === undefined)
          .map((t) => (
            <p key={t.id + t.name} className="muted small tool-running">
              <Dot level="warn" /> Running <span className="mono">{displayName(t.name)}</span>
              {t.elapsed ? ` for ${running(t.elapsed)}` : "…"} (big scans and slow services take a while)
            </p>
          ))}
      {turn.tools.length > 0 && (
        <details className="tools">
          <summary>
            Used {turn.tools.length === 1 ? "a tool" : `${turn.tools.length} tools`}: {names.join(", ")}
          </summary>
          <ul>
            {turn.tools.map((t) => (
              <li key={t.id + t.name}>
                <Dot level={t.ok === undefined ? "off" : t.ok ? "ok" : "crit"} />
                <span className="mono">{displayName(t.name)}</span>
                {typeof t.args === "object" && t.args !== null && Object.keys(t.args).length > 0 && (
                  <span className="muted small"> {JSON.stringify(t.args)}</span>
                )}
                {t.preview && <pre className="muted">{t.preview}</pre>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {approvals.map((id) => (
        <ApprovalCard key={id} id={id} />
      ))}
      {turn.waiting && turn.text ? (
        <p className="waiting-note muted small">
          <Dot level={outcomeLevel(turn.outcome)} /> <span className="waiting-text">{turn.text.trim()}</span>
        </p>
      ) : turn.text ? (
        <Markdown text={turn.text} />
      ) : turn.live && !turn.error ? (
        <p className="muted">Thinking…</p>
      ) : null}
      {turn.live && turn.text && <span className="cursor" aria-hidden />}
      {!turn.live && turn.trace && turn.session && (
        <a className="trace-link" href={`#/logs/llm/for/chat/${turn.session}/${turn.trace}`}>
          Trace
        </a>
      )}
      {turn.stopped && <p className="muted small">Stopped.</p>}
      {turn.error && <p className="error">{turn.error}</p>}
    </div>
  );
}

/** Dot of a waiting note: amber while waiting, then by what happened to its actions. */
function outcomeLevel(outcome: Record<string, string> | undefined): Level {
  if (!outcome) return "warn";
  const all = Object.values(outcome);
  if (all.some((s) => s === "failed")) return "crit";
  if (all.length && all.every((s) => s === "executed")) return "ok";
  return "off";
}

const STATUS_TEXT: Record<string, string> = {
  pending: "Waiting for your approval",
  queued: "Approved; queued",
  approved: "Approved; running",
  executed: "Approved and run",
  failed: "Approved; failed",
  rejected: "Denied",
  expired: "Expired",
  dry_run: "Dry run",
};

/** One action a chat answer filed: what it is, Approve / Deny while pending, the outcome after. */
function ApprovalCard({ id }: { id: number }) {
  const action = usePoll(() => api.get<ActionRecord>(`/api/actions/${id}`), 60_000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useStream(["approval", "action"], (event) => {
    // events carry the action id (actions) or the batch (approvals): reload on ours or any batch
    if (event.data.id === id || event.type.startsWith("approval")) action.reload();
  });
  const row = action.data;
  if (action.error && !row) return <div className="approval-card muted small">Approval item #{id}: {action.error}</div>;
  if (!row) return null;
  const pending = row.status === "pending";
  const decide = async (decision: "approve" | "reject") => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/approvals/${row.batch_id ?? id}`, { decision, items: [id] });
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : (e as Error).message);
    }
    setBusy(false);
    action.reload();
  };
  const level = pending || row.status === "queued" ? "warn" : row.status === "approved" ? "info" : row.status === "executed" ? "ok" : row.status === "failed" ? "crit" : "off";
  return (
    <div className={`approval-card${pending ? "" : " settled"}`}>
      <div className="approval-card-body">
        <p className="approval-title">
          {row.action === "host_command" ? (
            <span>Command to run as root on the host</span>
          ) : (
            <>
              <span className="mono">{row.action}</span>
              <span>{row.target}</span>
            </>
          )}
        </p>
        {row.action === "host_command" ? <HostCommand item={row} /> : row.reason && <p className="small">{row.reason}</p>}
        <p className="muted small">
          <Dot level={level} /> {STATUS_TEXT[row.status] ?? row.status} ·{" "}
          <a href={`#/approvals/${row.batch_id ?? ""}`}>open in Approvals</a>
        </p>
        {error && <p className="error small">{error}</p>}
      </div>
      {pending && (
        <div className="approval-buttons">
          <button className="btn" disabled={busy} onClick={() => decide("reject")}>Deny</button>
          <button className="btn btn-primary" disabled={busy} onClick={() => decide("approve")}>Approve</button>
        </div>
      )}
    </div>
  );
}
