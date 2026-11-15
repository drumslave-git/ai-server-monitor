import { useEffect, useState } from "react";
import { api } from "../../api";
import { bytes } from "../../fmt";
import { Panel } from "../../ui";
import { ms, num, prettyJson } from "./format";
import type { Timeline, TimelineEntry, ToolCallDetail } from "./types";

/** The LLM calls and tool calls that belong together (a chat turn, a report with retries),
 *  in time order on one scale. Renders nothing for a call that stands alone. */
export function TimelinePanel({ callId }: { callId: number }) {
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  useEffect(() => {
    setTimeline(null);
    api.get<Timeline>(`/api/logs/llm/${callId}/timeline`).then(setTimeline, () => undefined);
  }, [callId]);
  if (!timeline || timeline.entries.length < 2) return null;
  return <TimelineView timeline={timeline} />;
}

export function TimelineView({ timeline }: { timeline: Timeline }) {
  const entries = timeline.entries;
  const t0 = Math.min(...entries.map((e) => e.ts));
  const end = Math.max(...entries.map((e) => e.ts + duration(e) / 1000));
  const span = Math.max(end - t0, 0.001);
  const llm = entries.filter((e) => e.type === "llm").length;
  const tools = entries.length - llm;
  return (
    <Panel
      title="Timeline"
      aside={
        <span className="muted">
          {llm} LLM {llm === 1 ? "call" : "calls"}, {tools} tool {tools === 1 ? "call" : "calls"} · {ms(span * 1000)}
        </span>
      }
    >
      <ol className="tl">
        {entries.map((e) => (
          <TimelineRow key={`${e.type}-${e.id}`} entry={e} t0={t0} span={span} selected={e.type === "llm" && e.id === timeline.selected} />
        ))}
      </ol>
    </Panel>
  );
}

const duration = (e: TimelineEntry) => (e.type === "llm" ? (e.total_ms ?? 0) : (e.duration_ms ?? 0));

function TimelineRow({ entry: e, t0, span, selected }: { entry: TimelineEntry; t0: number; span: number; selected: boolean }) {
  const [open, setOpen] = useState(false);
  const left = ((e.ts - t0) / span) * 100;
  const width = Math.max((duration(e) / 1000 / span) * 100, 0.8);
  const failed = e.type === "llm" ? e.status !== "ok" : e.ok === false;
  return (
    <li className={`tl-row tl-${e.type}${selected ? " tl-selected" : ""}`}>
      <div className="tl-label">
        <span className="tl-kind">{e.type === "llm" ? (e.link_step ? `LLM · step ${e.link_step}` : "LLM") : "tool"}</span>
        {e.type === "llm" ? (
          <>
            {selected ? <strong>this call</strong> : <a href={`#/logs/llm/${e.id}`}>call #{e.id}</a>}
            <span className="muted">
              {num(e.prompt_tokens)} → {num(e.completion_tokens)} tokens
              {e.tool_calls_count ? ` · asked for ${e.tool_calls_count} tool${e.tool_calls_count === 1 ? "" : "s"}` : e.finish_reason ? ` · ${e.finish_reason}` : ""}
            </span>
          </>
        ) : (
          <>
            <button className="linklike mono" onClick={() => setOpen(!open)} aria-expanded={open}>{e.tool}</button>
            <span className="muted">{e.result_size !== null && e.result_size !== undefined ? bytes(e.result_size) : ""}</span>
          </>
        )}
        {failed && <span className="warn-text">{e.type === "llm" ? e.status : "failed"}</span>}
        <span className="tl-ms">{ms(duration(e))}</span>
      </div>
      <div className="tl-track" aria-hidden>
        <span className={`tl-bar ${e.type === "llm" ? "seg-gen" : "seg-tool"}${failed ? " tl-failed" : ""}`} style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }} />
      </div>
      {open && e.type === "tool" && <ToolBody id={e.id} fallbackError={e.error ?? null} />}
    </li>
  );
}

function ToolBody({ id, fallbackError }: { id: number; fallbackError: string | null }) {
  const [tool, setTool] = useState<ToolCallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.get<ToolCallDetail>(`/api/logs/tools/${id}`).then(setTool, (e: Error) => setError(e.message));
  }, [id]);
  if (error) return <p className="error small">{error}</p>;
  if (!tool) return <p className="muted small">Loading…</p>;
  const show = (v: unknown) => (typeof v === "string" ? prettyJson(v) : JSON.stringify(v, null, 2));
  return (
    <div className="tl-tool">
      <h4>Arguments</h4>
      <pre className="mono-box">{tool.args === null || tool.args === undefined ? "—" : show(tool.args)}</pre>
      <h4>{tool.ok ? "Result" : "Error"}{tool.truncated ? " (cut at the storage cap)" : ""}</h4>
      <pre className="mono-box">{tool.result === null || tool.result === undefined ? (tool.error ?? fallbackError ?? "—") : show(tool.result)}</pre>
      {!tool.ok && tool.error && tool.result !== null && <p className="error small">{tool.error}</p>}
    </div>
  );
}
