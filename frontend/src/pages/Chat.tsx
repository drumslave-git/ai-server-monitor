import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Schemas } from "../api";
import { ApprovalCards } from "../components/ApprovalCards";
import { HostCommand } from "../components/HostCommand";
import { RunnableMarkdown } from "../components/RunnableMarkdown";
import { ago } from "../fmt";
import { takeExplain, type ExplainRequest } from "../components/Explain";
import { Code, Column, Command, Composer, Empty, ErrorText, Fold, IconButton, Inline, LinkButton, List, ListItem, Message, Meta, PageHeader, Pad, Pane, Panes, Status, Text, type Tone } from "../kit";
import { useStream } from "../live";
import { useNow, usePoll } from "../ui";

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
/** A command the owner ran from an answer (`run` event of POST /api/chat/sessions/{id}/run). */
type CommandRun = {
  command: string;
  mode?: "read-only" | "write";
  action_id?: number | null;
  exit_code: number | null;
  duration_s: number;
  /** "running" while it runs (only here), "error" when it could not run. */
  stopped: string | null;
  truncated: boolean;
  output: string;
};
type Turn =
  | { role: "user"; text: string; explain?: ExplainNote }
  | { role: "run"; run: CommandRun }
  | {
      role: "assistant";
      text: string;
      tools: ToolUse[];
      live?: boolean;
      error?: string;
      stopped?: boolean;
      /** The answer broke off and is being asked for again (event `retry`), until its first new token. */
      retry?: { attempt: number; of: number };
      /** The run stopped for these approvals (action ids); `text` is the waiting note. */
      waiting?: number[];
      /** Set once the note was rewritten to what was decided: action id → final status. */
      outcome?: Record<string, string>;
      /** Logs page link: the session and the turn id of this answer. */
      session?: string;
      /** The MCP part of the turn (meta `mcp_domains`); absent when no MCP tool was enabled. */
      domains?: McpDomainsLog;
      /** Commands the marking call could not label (meta `unmarked`): they run with change rights. */
      unmarked?: Unmarked;
      trace?: string;
    };

type Unmarked = { commands: number; error?: string };

/** "The model did not state read-only or write for 2 commands: they run with change rights." */
function unmarkedText(u: Unmarked): string {
  const n = u.commands === 1 ? "1 command: it runs" : `${u.commands} commands: they run`;
  return `The model did not state read-only or write for ${n} with change rights`;
}

type McpDomainsLog = {
  picked?: string[];
  offered?: string[];
  loaded?: string[];
  unsorted?: string[];
  unreachable?: Record<string, string>;
  classify_error?: string;
};

/** "domains: sonarr (picked), torrents (loaded) · server arr unreachable": one quiet line. */
function domainsText(d: McpDomainsLog): string {
  const picked = d.picked ?? [];
  const earlier = (d.offered ?? []).filter((n) => !picked.includes(n));
  const names = [
    ...picked.map((n) => `${n} (picked)`),
    ...earlier.map((n) => `${n} (loaded before)`),
    ...(d.loaded ?? []).map((n) => `${n} (loaded)`),
  ];
  const parts = [`domains: ${names.length ? names.join(", ") : "none"}`];
  if (d.classify_error) parts.push("classification failed");
  for (const server of Object.keys(d.unreachable ?? {})) parts.push(`server ${server} unreachable`);
  if (d.unsorted?.length) parts.push(`${d.unsorted.length} tool${d.unsorted.length === 1 ? "" : "s"} not sorted`);
  return parts.join(" · ");
}

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
    if (m.role === "user" && (m.meta as any)?.run) {
      if (current) turns.push(current);
      current = null;
      turns.push({ role: "run", run: (m.meta as any).run });
    } else if (m.role === "user") {
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
        const domains = (m.meta as any)?.mcp_domains;
        if (domains && typeof domains === "object") turn.domains = domains;
        const unmarked = (m.meta as any)?.unmarked;
        if (unmarked && typeof unmarked.commands === "number") turn.unmarked = unmarked;
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

/** Below this width the page shows the conversations or the thread, not both. */
const ONE_PANE = "(max-width: 820px)";

function useOnePane(): boolean {
  const [onePane, setOnePane] = useState(() => window.matchMedia(ONE_PANE).matches);
  useEffect(() => {
    const query = window.matchMedia(ONE_PANE);
    const onChange = () => setOnePane(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return onePane;
}

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
  const onePane = useOnePane();
  const sessions = usePoll(() => api.get<SessionList>("/api/chat/sessions?limit=50"), 60_000);
  const routeId = params[0] && params[0] !== "new" ? params[0] : null;
  const showThread = params[0] !== undefined;
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
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
          if (type === "run") {
            setTurns((all) => all.map((t, i) => (i === all.length - 2 && t.role === "run" ? { role: "run", run: data } : t)));
          } else if (type === "session") {
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
          } else if (type === "retry") {
            // The answer broke off and is asked for again: what it had shown since the last tool goes.
            patchLast((t) => {
              t.text = "";
              t.retry = { attempt: data.attempt, of: data.of };
            });
          } else if (type === "token") {
            patchLast((t) => {
              t.text += data.text;
              delete t.retry;
            });
          } else if (type === "error") {
            patchLast((t) => {
              t.error = data.message;
              delete t.retry;
            });
            finished = true;
            failed = true; // nothing of the answer is stored: keep the live thread with the error
            keepThread.current = sessionId.current;
          } else if (type === "done") {
            finished = true;
            patchLast((t) => {
              t.trace = data.trace_turn;
              t.session = data.session_id;
              if (data.mcp_domains) t.domains = data.mcp_domains;
              if (data.unmarked) t.unmarked = data.unmarked;
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

  // Run on a command in an answer: the click runs it (the server picks the shell by the
  // answer's mark), the card joins the thread and the model answers the result at once.
  const runCommand = async (command: string) => {
    const id = sessionId.current;
    if (!id || busy) return;
    const placeholder: CommandRun = { command, exit_code: null, duration_s: 0, stopped: "running", truncated: false, output: "" };
    await stream(`/api/chat/sessions/${id}/run`, { command }, { role: "run", run: placeholder });
  };

  const remove = async (id: string) => {
    if (!confirm("Delete this conversation?")) return;
    await api.del(`/api/chat/sessions/${id}`);
    sessions.reload();
    if (id === routeId) location.hash = "#/chat/new";
  };

  const items = sessions.data?.items ?? [];
  return (
    <>
      <PageHeader title="Chat" back={onePane && showThread ? { label: "all conversations", href: "#/chat" } : undefined}>
        <LinkButton variant="primary" icon="add" href="#/chat/new">
          new conversation
        </LinkButton>
      </PageHeader>
      <Panes>
        {(!onePane || !showThread) && (
          <Pane side label="Conversations">
            {sessions.data && items.length === 0 && <Empty>No conversations yet.</Empty>}
            <List label="Conversations">
              {items.map((s) => (
                <ListItem
                  key={s.id}
                  href={`#/chat/${s.id}`}
                  selected={s.id === routeId}
                  title={s.title || "Untitled"}
                  meta={`${s.channel === "telegram" ? "Telegram · " : ""}${ago(s.updated, now)} · ${s.messages} messages`}
                  action={<IconButton small icon="remove" label={`Delete conversation: ${s.title || "Untitled"}`} onClick={() => void remove(s.id)} />}
                />
              ))}
            </List>
          </Pane>
        )}
        {(!onePane || showThread) && (
          <Pane
            label="Conversation"
            endOn={turns}
            foot={
              <Composer
                onSubmit={(e) => {
                  e.preventDefault();
                  void send();
                }}
                actions={
                  busy ? (
                    <IconButton small icon="stop" label="stop" onClick={() => abort.current?.abort()} />
                  ) : (
                    <IconButton small type="submit" variant="primary" icon="send" label="send" disabled={!draft.trim()} />
                  )
                }
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
            }
          >
            {(loadError || turns.length > 0) && (
              <Pad>
                {loadError && <ErrorText>{loadError}</ErrorText>}
                {turns.map((t, i) =>
                  t.role === "user" ? (
                    <UserMessage key={i} text={t.text} explain={t.explain} />
                  ) : t.role === "run" ? (
                    <RunCard key={i} run={t.run} />
                  ) : (
                    <AssistantMessage key={i} turn={t} onRun={busy ? undefined : runCommand} />
                  ),
                )}
              </Pad>
            )}
            {turns.length === 0 && !busy && (
              <Empty>Ask about the server: what is using the disk, why a container restarted, what changed since yesterday.</Empty>
            )}
          </Pane>
        )}
      </Panes>
    </>
  );
}

function UserMessage({ text, explain }: { text: string; explain?: ExplainNote }) {
  // (a typed message keeps its line breaks)
  if (!explain)
    return (
      <Message who="you" typed>
        {text}
      </Message>
    );
  return (
    <Message who="you">
      <Text block>{explain.label}</Text>
      {explain.source && (
        <Fold inline title="source data">
          <Code wrap maxHeight={320}>
            {explain.source}
          </Code>
        </Fold>
      )}
    </Message>
  );
}

// MCP tools are named server__tool for the model; show "server · tool" (mcp_client.display_name).
function displayName(name: string): string {
  const i = name.indexOf("__");
  return i > 0 ? `${name.slice(0, i)} · ${name.slice(i + 2)}` : name;
}

/** A command the owner ran, with its exit code and output (stored in the conversation). */
function RunCard({ run }: { run: CommandRun }) {
  const failed = run.stopped === "error";
  const where = run.mode === "write" ? "as root (it can change the server)" : "in the read-only shell";
  // The owner's side of the thread, as wide as a message.
  if (run.stopped === "running")
    return (
      <Message who="you" says="running this command…" running wide>
        <Command wrap>{run.command}</Command>
      </Message>
    );
  return (
    <Message who="you" says={`you ran this command ${where}`} wide>
      <HostCommand
        item={{
          args: { command: run.command },
          reason: null,
          status: failed ? "failed" : "executed",
          result: failed
            ? { error: `Could not run: ${run.output}` }
            : { exit_code: run.exit_code, duration_s: run.duration_s, stopped: run.stopped, truncated: run.truncated, output: run.output },
        }}
      />
    </Message>
  );
}

function AssistantMessage({
  turn,
  onRun,
}: {
  turn: Extract<Turn, { role: "assistant" }>;
  onRun?: (command: string) => Promise<void>;
}) {
  const names = Array.from(new Set(turn.tools.map((t) => displayName(t.name))));
  // approvals this answer waits for, and older write tools whose result named a pending approval
  const approvals = Array.from(
    new Set([...(turn.waiting ?? []), ...turn.tools.filter((t) => t.approval?.status === "pending").map((t) => t.approval!.id)]),
  );
  return (
    <Message who="assistant">
      {turn.live &&
        turn.tools
          .filter((t) => t.ok === undefined)
          .map((t) => (
            <Inline key={t.id + t.name} align="baseline" gap={2}>
              <Status tone="info" blink>
                running {displayName(t.name)}
                {t.elapsed ? ` for ${running(t.elapsed)}` : "…"}
              </Status>
              <Meta>(big scans and slow services take a while)</Meta>
            </Inline>
          ))}
      {turn.tools.length > 0 && (
        <Fold inline title={`used ${turn.tools.length === 1 ? "a tool" : `${turn.tools.length} tools`}: ${names.join(", ")}`}>
          {turn.tools.map((t) => (
            <Column key={t.id + t.name} gap={1}>
              <Status tone={t.ok === undefined ? "off" : t.ok ? "ok" : "crit"}>{displayName(t.name)}</Status>
              {typeof t.args === "object" && t.args !== null && Object.keys(t.args).length > 0 && <Meta>{JSON.stringify(t.args)}</Meta>}
              {t.preview && (
                <Code wrap maxHeight={120}>
                  {t.preview}
                </Code>
              )}
            </Column>
          ))}
        </Fold>
      )}
      {approvals.length > 0 && <ApprovalCards key={approvals.join(",")} items={approvals} />}
      {turn.waiting && turn.text ? (
        <Status tone={outcomeTone(turn.outcome)} plain>
          {turn.text.trim()}
        </Status>
      ) : turn.text ? (
        <RunnableMarkdown text={turn.text} onRun={turn.live ? undefined : onRun} />
      ) : turn.live && !turn.error && turn.retry ? (
        <Status tone="warn" blink>
          answer broke off, asking again ({turn.retry.attempt} of {turn.retry.of})
        </Status>
      ) : turn.live && !turn.error ? (
        <Status tone="info" blink>
          thinking…
        </Status>
      ) : null}
      {turn.live && turn.text && (
        <Status tone="info" blink>
          answering…
        </Status>
      )}
      {!turn.live && turn.trace && turn.session && (
        <Meta>
          <a href={`#/logs/llm/for/chat/${turn.session}/${turn.trace}`}>trace</a>
          {turn.domains && <> · {domainsText(turn.domains)}</>}
          {turn.unmarked && (
            <>
              {" · "}
              <span title={turn.unmarked.error ? `Labelling failed: ${turn.unmarked.error}` : undefined}>{unmarkedText(turn.unmarked)}</span>
            </>
          )}
        </Meta>
      )}
      {turn.stopped && <Status tone="off">stopped</Status>}
      {turn.error && <ErrorText>{turn.error}</ErrorText>}
    </Message>
  );
}

/** Lamp of a waiting note: amber while waiting, then by what happened to its actions. */
function outcomeTone(outcome: Record<string, string> | undefined): Tone {
  if (!outcome) return "warn";
  const all = Object.values(outcome);
  if (all.some((s) => s === "failed")) return "crit";
  if (all.length && all.every((s) => s === "executed")) return "ok";
  return "off";
}
