import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Schemas } from "../api";
import { ago } from "../fmt";
import { useStream } from "../live";
import { Dot, Empty, Markdown, PageHead, useNow, usePoll } from "../ui";

type SessionList = Schemas["ChatSessionList"];
type SessionDoc = Schemas["ChatSession"];

type ToolUse = {
  id: string;
  name: string;
  args?: unknown;
  ok?: boolean;
  preview?: string;
  kind?: string | null;
  approval?: { id: number; status: string };
};
type Turn =
  | { role: "user"; text: string }
  | { role: "assistant"; text: string; tools: ToolUse[]; live?: boolean; error?: string; stopped?: boolean };

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
      turns.push({ role: "user", text: m.content ?? "" });
    } else if (m.role === "assistant") {
      const turn = open();
      for (const call of m.tool_calls ?? []) {
        const fn = (call as any).function ?? call;
        turn.tools.push({ id: String((call as any).id ?? ""), name: String(fn.name ?? "tool"), args: fn.arguments });
      }
      if (!(m.tool_calls && m.tool_calls.length) && m.content) {
        turn.text = m.content;
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
    sessionId.current = routeId;
    if (routeId) void load(routeId);
    else setTurns([]);
  }, [routeId, busy, load]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [turns]);

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
    setBusy(true);
    setTurns((t) => [...t, { role: "user", text: message }, { role: "assistant", text: "", tools: [], live: true }]);
    const controller = new AbortController();
    abort.current = controller;
    let finished = false;
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, session_id: sessionId.current }),
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
            history.replaceState(null, "", `#/chat/${data.session_id}`);
            setParams([data.session_id]);
          } else if (type === "tool_call") {
            // Whatever was streamed before a tool call was the model thinking aloud, not the answer.
            patchLast((t) => {
              t.text = "";
              t.tools.push({ id: data.id, name: data.name, args: data.args, kind: data.kind });
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
          } else if (type === "done") {
            finished = true;
          }
        }
      }
    } catch (e) {
      if ((e as Error).name === "AbortError") patchLast((t) => void (t.stopped = true));
      else patchLast((t) => void (t.error = (e as Error).message));
    } finally {
      abort.current = null;
      setBusy(false);
      sessions.reload();
      // Replace the live thread with the stored one once the answer is complete.
      if (finished && sessionId.current) {
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
                <button className="btn btn-quiet session-delete" onClick={() => remove(s.id)} aria-label="Delete conversation">
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
            <Empty>Ask about the server: what is using the disk, why a container restarted, what changed since the last report.</Empty>
          )}
          <div className="messages">
            {turns.map((t, i) => (t.role === "user" ? <UserBubble key={i} text={t.text} /> : <AssistantBubble key={i} turn={t} />))}
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
                  void send();
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

function UserBubble({ text }: { text: string }) {
  return <div className="bubble bubble-user">{text}</div>;
}

function AssistantBubble({ turn }: { turn: Extract<Turn, { role: "assistant" }> }) {
  const names = Array.from(new Set(turn.tools.map((t) => t.name)));
  return (
    <div className="bubble bubble-assistant">
      {turn.tools.length > 0 && (
        <details className="tools">
          <summary>
            Used {turn.tools.length === 1 ? "a tool" : `${turn.tools.length} tools`}: {names.join(", ")}
          </summary>
          <ul>
            {turn.tools.map((t) => (
              <li key={t.id + t.name}>
                <Dot level={t.ok === undefined ? "off" : t.ok ? "ok" : "crit"} />
                <span className="mono">{t.name}</span>
                {typeof t.args === "object" && t.args !== null && Object.keys(t.args).length > 0 && (
                  <span className="muted small"> {JSON.stringify(t.args)}</span>
                )}
                {t.preview && <pre className="muted">{t.preview}</pre>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {turn.tools.filter((t) => t.approval && t.approval.status === "pending").map((t) => (
        <ApprovalCard key={t.id} id={t.approval!.id} name={t.name} />
      ))}
      {turn.text ? <Markdown text={turn.text} /> : turn.live && !turn.error ? <p className="muted">Thinking…</p> : null}
      {turn.live && turn.text && <span className="cursor" aria-hidden />}
      {turn.stopped && <p className="muted small">Stopped.</p>}
      {turn.error && <p className="error">{turn.error}</p>}
    </div>
  );
}

function ApprovalCard({ id, name }: { id: number; name: string }) {
  const pending = usePoll(() => api.get<{ items: { id: number; target: string | null; action: string }[] }>("/api/approvals"), 20_000);
  const [done, setDone] = useState<string | null>(null);
  useStream(["approval", "action"], () => pending.reload());
  const row = pending.data?.items.find((a) => a.id === id);
  const decide = async (decision: "approve" | "reject") => {
    try {
      const result = await api.post<{ status: string }>(`/api/approvals/${id}`, { decision });
      setDone(result.status);
    } catch (e) {
      setDone((e as Error).message);
    }
    pending.reload();
  };
  if (!row && !done && pending.data) return <div className="approval-card muted small">Approval #{id} for {name} was decided.</div>;
  return (
    <div className="approval-card">
      <div>
        <p className="approval-title">
          <span className="mono">{row?.action ?? name}</span>
          <span>{row?.target}</span>
        </p>
        <p className="muted small">
          Waiting for your approval · <a href="#/approvals">open Approvals</a>
        </p>
      </div>
      {done ? (
        <span className="muted small">{done}</span>
      ) : (
        <div className="approval-buttons">
          <button className="btn" onClick={() => decide("reject")}>Reject</button>
          <button className="btn btn-primary" onClick={() => decide("approve")}>Approve</button>
        </div>
      )}
    </div>
  );
}
