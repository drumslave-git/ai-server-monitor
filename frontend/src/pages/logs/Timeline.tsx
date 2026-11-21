import { useEffect, useState } from "react";
import { api } from "../../api";
import { callHref } from "../../components/EntityRefs";
import { bytes } from "../../fmt";
import { Axis, Bar, Button, Code, Column, EntityLink, ErrorText, Labeled, Legend, Meta, Panel, Row, Split, Status, Text } from "../../kit";
import { ms, num, prettyJson } from "./format";
import { CallStatus } from "./parts";
import type { Timeline, TimelineEntry, ToolCallDetail } from "./types";

/** The LLM calls and tool calls that belong together (a chat turn, an agent run),
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
    <Panel title="Timeline" meta={`${llm} LLM ${llm === 1 ? "call" : "calls"}, ${tools} tool ${tools === 1 ? "call" : "calls"} · ${ms(span * 1000)}`}>
      {entries.map((e) => (
        <TimelineRow key={`${e.type}-${e.id}`} entry={e} t0={t0} span={span} selected={e.type === "llm" && e.id === timeline.selected} />
      ))}
      <Row column gap={2}>
          {/* Ticks at every quarter of the span the bars above share. */}
          <Axis labels={[0, 1, 2, 3, 4].map((i) => (i === 0 ? "0" : ms((span * 1000 * i) / 4)))} />
          <Legend>
            <Status tone="info" plain>waiting for the first token</Status>
            <Status tone="ok" plain>generating</Status>
            <Status tone="text" plain>tool running</Status>
            {entries.some(failed) && <Status tone="crit" plain>failed</Status>}
          </Legend>
      </Row>
    </Panel>
  );
}

const duration = (e: TimelineEntry) => (e.type === "llm" ? (e.total_ms ?? 0) : (e.duration_ms ?? 0));
const failed = (e: TimelineEntry) => (e.type === "llm" ? e.status !== "ok" : e.ok === false);

function TimelineRow({ entry: e, t0, span, selected }: { entry: TimelineEntry; t0: number; span: number; selected: boolean }) {
  const [open, setOpen] = useState(false);
  const start = e.ts - t0;
  const bad = failed(e);
  const llm = e.type === "llm";
  // An LLM call that went well shows its wait for the first token inside its bar.
  const firstToken = llm && !bad && e.ttft_ms !== null && e.ttft_ms !== undefined ? start + e.ttft_ms / 1000 : undefined;
  return (
    <Row current={selected}>
      <Meta flex="0 0 104px">{llm ? (e.link_step ? `LLM · step ${e.link_step}` : "LLM") : "tool"}</Meta>
      {llm ? (
        <>
          {selected ? (
            <strong>this call</strong>
          ) : (
            <Text kind="data">
              <EntityLink kind="call" href={callHref(e.id)}>call #{e.id}</EntityLink>
            </Text>
          )}
          <Text dim>
            {num(e.prompt_tokens)} → {num(e.completion_tokens)} tokens
            {e.tool_calls_count ? ` · asked for ${e.tool_calls_count} tool${e.tool_calls_count === 1 ? "" : "s"}` : e.finish_reason ? ` · ${e.finish_reason}` : ""}
          </Text>
        </>
      ) : (
        <>
          <Button variant="link" onClick={() => setOpen(!open)} aria-expanded={open}>
            {e.tool}
          </Button>
          {e.result_size !== null && e.result_size !== undefined && <Text dim>{bytes(e.result_size)}</Text>}
        </>
      )}
      {bad && <CallStatus status={llm ? (e.status ?? "") : "error"} />}
      <Text kind="data" toEnd>
        {ms(duration(e))}
      </Text>
      <Bar size="sm" flex="1 0 100%" start={start} inner={firstToken} end={start + duration(e) / 1000} total={span} tone={bad ? "crit" : llm ? "ok" : "text"} />
      {open && !llm && <ToolBody id={e.id} fallbackError={e.error ?? null} />}
    </Row>
  );
}

function ToolBody({ id, fallbackError }: { id: number; fallbackError: string | null }) {
  const [tool, setTool] = useState<ToolCallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.get<ToolCallDetail>(`/api/logs/tools/${id}`).then(setTool, (e: Error) => setError(e.message));
  }, [id]);
  const show = (v: unknown) => (typeof v === "string" ? prettyJson(v) : JSON.stringify(v, null, 2));
  return (
    <Column gap={2} flex="1 0 100%">
      {error ? (
        <ErrorText>{error}</ErrorText>
      ) : !tool ? (
        <Meta>Loading…</Meta>
      ) : (
        <>
          <Split>
            <Labeled label="arguments" flex="1 1 260px">
              <Code maxHeight={240}>{tool.args === null || tool.args === undefined ? "—" : show(tool.args)}</Code>
            </Labeled>
            <Labeled label={`${tool.ok ? "result" : "error"}${tool.truncated ? " (cut at the storage cap)" : ""}`} flex="1 1 260px">
              <Code wrap maxHeight={240}>
                {tool.result === null || tool.result === undefined ? (tool.error ?? fallbackError ?? "—") : show(tool.result)}
              </Code>
            </Labeled>
          </Split>
          {!tool.ok && tool.error && tool.result !== null && <ErrorText>{tool.error}</ErrorText>}
        </>
      )}
    </Column>
  );
}
