import { useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import { Badge, Empty, Markdown, Panel } from "../../ui";
import { ScenarioDetail } from "./Catalog";
import { GROUP_SHORT, automationText, backendLabel, chars, effortLabel, coldStartText, elapsed, gb, ms, pct, routingLabel, samplingMissing, samplingText, score, scoreLevel, shortDate, sizeLabel, statusLevel, tps } from "./format";
import { HardwareSection, hwScope, vramStat } from "./Hardware";
import { LevelMatrix, LevelsPanel } from "./Levels";
import type { Attempt, AttemptRow, Catalog, GpuInfo, HwNumbers, Run, ScenarioInfo } from "./types";

export function ScoreBar({ value }: { value: number | null | undefined }) {
  const level = scoreLevel(value);
  return (
    <span className="score-bar" role="img" aria-label={score(value)}>
      <span className={`score-fill score-fill-${level}`} style={{ width: `${Math.round((value ?? 0) * 100)}%` }} />
    </span>
  );
}

function gpuText(gpu: GpuInfo | undefined): string {
  if (!gpu?.gpus?.length) return "not available";
  return gpu.gpus
    .map((g) => `${g.name ?? "GPU"}${g.mem_total ? `, ${(g.mem_total / 1024 ** 3).toFixed(0)} GB` : ""}${g.mem_used !== undefined && g.mem_total ? `, ${((g.mem_used / g.mem_total) * 100).toFixed(0)}% used at start` : ""}${g.util_percent !== undefined ? `, ${g.util_percent}% busy` : ""}`)
    .join("; ");
}

export function RunDetail({ id, scenarioId, attemptId, catalog }: { id: number; scenarioId?: string; attemptId?: number; catalog: Catalog | null }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setRun(null);
    const load = () =>
      api.get<Run>(`/api/bench/runs/${id}`).then(
        (r) => alive && (setRun(r), setError(null)),
        (e: Error) => alive && setError(e.message),
      );
    void load();
    return () => {
      alive = false;
    };
  }, [id]);

  const remove = async () => {
    if (!confirm(`Delete run #${id} and all its attempts? This cannot be undone.`)) return;
    try {
      await api.del(`/api/bench/runs/${id}`);
      location.hash = "#/bench";
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : (e as Error).message);
    }
  };

  if (error) return <p className="error">{error} <a href="#/bench">Back</a></p>;
  if (!run) return <p className="muted">Loading…</p>;
  const meta = run.meta ?? {};
  const llm = (meta.llm ?? {}) as Record<string, unknown>;
  const backend = (meta.backend ?? {}) as Record<string, unknown>;
  const scenarioInfo = catalog?.scenarios.find((s) => s.id === scenarioId);

  return (
    <div className="stack bench-detail">
      <a className="back-link" href={scenarioId ? `#/bench/run/${id}` : "#/bench"}>← {scenarioId ? `Run #${id}` : "All runs"}</a>
      <div className="page-head">
        <h2>
          Run #{run.id} · <span className="mono">{run.model}</span>
        </h2>
        <div className="page-actions">
          <Badge level={statusLevel(run.status)}>{run.status}</Badge>
          <a className="btn" href={`/api/bench/runs/${id}/export.pdf`} download>Download PDF</a>
          <a className="btn" href={`/api/bench/runs/${id}/export`} download>Export JSON</a>
          <button className="btn" onClick={() => void remove()}>Delete</button>
        </div>
      </div>
      {run.error && <div className="notice notice-error">{run.error}</div>}

      {!scenarioId && (
        <>
          <Panel title="What was measured">
            <dl className="bench-meta">
              <dt>Model</dt>
              <dd className="mono">{run.model}</dd>
              <dt>Backend</dt>
              <dd>{backendLabel(run)}</dd>
              <dt>Size</dt>
              <dd>
                {sizeLabel(run)}
                {typeof backend.family === "string" && <span className="muted"> · family {String(backend.family)}</span>}
              </dd>
              <dt>Settings</dt>
              <dd>
                temperature <Sampled llm={llm} k="temperature" /> · top P <Sampled llm={llm} k="top_p" /> · top K <Sampled llm={llm} k="top_k" /> · max output {String(llm.max_output_tokens ?? "—")} tokens · thinking {meta.levels ? `tested at ${meta.levels.tested.join(", ")}` : String(llm.reasoning_effort ?? "—")} · context setting {String(llm.max_context_tokens ?? "—")}
              </dd>
              <dt>Tool mode</dt>
              <dd>
                {String(llm.tool_mode ?? "—")}
                {meta.tool_mode && llm.tool_mode !== meta.tool_mode && <> → used <strong>{meta.tool_mode}</strong></>}
              </dd>
              <dt>Tool routing</dt>
              <dd>
                {routingLabel(run)}
                <span className="muted small block">The chat setting at the start of the run: which tool groups the model gets per question. The classifier call counts in an attempt's LLM calls.</span>
              </dd>
              <dt>Cold start</dt>
              <dd>
                {coldStartText(meta.cold_start)}
                <span className="muted small block">Warm-up request before the first attempt; not part of any score or timing below.</span>
              </dd>
              <dt>Scenarios</dt>
              <dd>
                {run.scope === "all" ? "all" : run.scope.replace("group:", "group ")} · {run.repeats} attempt(s) each · set <span className="mono">{run.scenario_hash}</span> · app {run.app_version}
              </dd>
              <dt>Time</dt>
              <dd>
                {shortDate(run.started)} · took {run.finished ? elapsed(run.started, run.finished) : "—"}
              </dd>
              <dt>GPU</dt>
              <dd>{gpuText(meta.gpu)}</dd>
              <dt>Automation</dt>
              <dd>{automationText(meta.automation)}</dd>
              {meta.judge && (
                <>
                  <dt>Judge</dt>
                  <dd className="mono">{meta.judge.model}</dd>
                </>
              )}
            </dl>
            {run.summary && (run.summary.reload_suspected ?? 0) > 0 && (
              <p className="help warn-text">
                {run.summary.reload_suspected} attempt(s) are marked "reload suspected": the backend may have unloaded the model meanwhile. They are included in the numbers; open the scenario to see which.
              </p>
            )}
            {meta.automation?.paused && (
              <p className="help">The monitor's scheduled jobs and automations were paused during this run, so no report or collector loaded the backend.</p>
            )}
            {run.summary && run.summary.overlapped > 0 && (
              <p className="help">
                {run.summary.overlapped} attempt(s) overlapped a report being written by the monitor; their timings can be slower than they would be on an idle backend.
              </p>
            )}
          </Panel>

          <Panel title="Results">
            <div className="bench-stats">
              <div><span className="muted small">Overall</span><strong className={`score score-${scoreLevel(run.overall)} big`}>{score(run.overall)}</strong></div>
              <div><span className="muted small">Pass rate</span><strong className="big">{score(run.summary?.pass_rate)}</strong></div>
              <div><span className="muted small">Median first token</span><strong className="big">{ms(run.summary?.median_ttft_ms)}</strong></div>
              <div><span className="muted small">Median attempt</span><strong className="big">{ms(run.summary?.median_total_ms)}</strong></div>
              <div><span className="muted small">Speed</span><strong className="big">{tps(run.summary?.median_tokens_per_s)}</strong></div>
              <div><span className="muted small">Failed attempts</span><strong className="big">{run.summary?.failures ?? "—"}</strong></div>
              {(run.summary?.empty_answers ?? 0) > 0 && <div><span className="muted small">Empty answers</span><strong className="big crit-text">{run.summary?.empty_answers}</strong></div>}
            </div>
            <ul className="group-bars">
              {Object.entries(run.group_scores ?? {}).map(([g, s]) => (
                <li key={g}>
                  <span>{GROUP_SHORT[g] ?? g}</span>
                  <ScoreBar value={s} />
                  <span className="num">{score(s)}</span>
                </li>
              ))}
            </ul>
          </Panel>

          <LevelsPanel run={run} />

          <HardwareSection run={run} />

          <LevelMatrix run={run} catalog={catalog} />

          <ScenarioTable run={run} catalog={catalog} />
        </>
      )}

      {scenarioId && <ScenarioView run={run} scenarioId={scenarioId} info={scenarioInfo} attemptId={attemptId} />}
    </div>
  );
}

function ScenarioTable({ run, catalog }: { run: Run; catalog: Catalog | null }) {
  const stats = run.summary?.scenarios ?? {};
  const title = (id: string) => catalog?.scenarios.find((s) => s.id === id)?.title ?? id;
  const ids = Object.keys(stats);
  const scope = hwScope(run);
  const showHw = scope !== "old";
  const host = scope === "elsewhere" ? "Host " : "";
  if (ids.length === 0) return <Panel title="Scenarios"><Empty>No attempts finished.</Empty></Panel>;
  return (
    <Panel title="Scenarios" aside={<span className="muted small">Open one to see every attempt, its checks and its answer</span>}>
      <div className="bench-scroll">
        <table className="table bench-table">
          <thead>
            <tr>
              <th>Scenario</th>
              <th>Group</th>
              <th colSpan={2}>Score</th>
              <th className="num">Passed</th>
              <th className="num">Time</th>
              <th className="num">TTFT</th>
              {showHw && <th className="num">{host}VRAM peak</th>}
              {showHw && <th className="num">{host}GPU avg</th>}
              {showHw && <th className="num">{host}CPU avg</th>}
            </tr>
          </thead>
          <tbody>
            {ids.map((id) => {
              const s = stats[id];
              return (
                <tr key={id}>
                  <td data-label="Scenario">
                    <a href={`#/bench/run/${run.id}/${id}`}>{title(id)}</a>
                    <span className="block muted small mono">{id}</span>
                  </td>
                  <td data-label="Group" className="muted">{GROUP_SHORT[s.group] ?? s.group}</td>
                  <td data-label="Score"><ScoreBar value={s.score} /></td>
                  <td className="num"><span className={`score score-${scoreLevel(s.score)}`}>{score(s.score)}</span></td>
                  <td data-label="Passed" className="num">{s.passed}/{s.attempts}{s.failed > 0 && <span className="crit-text"> · {s.failed} failed</span>}</td>
                  <td data-label="Time" className="num">{ms(s.median_total_ms)}</td>
                  <td data-label="TTFT" className="num">{ms(s.median_ttft_ms)}</td>
                  {showHw && <HwCells hw={s.hw} />}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

/** VRAM peak, GPU avg and CPU avg cells of a scenario or attempt row. */
function HwCells({ hw }: { hw: HwNumbers | undefined }) {
  const vram = vramStat(hw);
  return (
    <>
      <td data-label="VRAM peak" className="num">{vram ? gb(vram.peak) : "—"}</td>
      <td data-label="GPU avg" className="num">{hw?.gpu_util ? pct(hw.gpu_util.avg) : "—"}</td>
      <td data-label="CPU avg" className="num">{hw?.cpu ? pct(hw.cpu.avg) : "—"}</td>
    </>
  );
}

function ScenarioView({ run, scenarioId, info, attemptId }: { run: Run; scenarioId: string; info?: ScenarioInfo; attemptId?: number }) {
  const attempts = (run.attempts ?? []).filter((a) => a.scenario_id === scenarioId);
  const selected = attempts.find((a) => a.id === attemptId) ?? attempts[0];
  return (
    <>
      <Panel title={info?.title ?? scenarioId} aside={<span className="muted small mono">{scenarioId}</span>}>
        {info ? <ScenarioDetail s={info} /> : <p className="muted">This scenario is no longer in the installed set.</p>}
      </Panel>
      <Panel title={`Attempts (${attempts.length})`}>
        {attempts.length === 0 ? (
          <Empty>No attempts of this scenario in this run.</Empty>
        ) : (
          <div className="bench-scroll">
            <table className="table bench-table attempts-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Level</th>
                  <th>Score</th>
                  <th>Status</th>
                  <th className="num">TTFT</th>
                  <th className="num">Total</th>
                  <th className="num">Tokens in → out</th>
                  <th className="num">Reasoning</th>
                  <th className="num">Speed</th>
                  <th className="num">LLM steps</th>
                  <th className="num">Tools</th>
                  <th>Mode</th>
                  <th className="num">Judge</th>
                  {hwScope(run) !== "old" && <th className="num">{hwScope(run) === "elsewhere" ? "Host " : ""}VRAM peak</th>}
                  {hwScope(run) !== "old" && <th className="num">{hwScope(run) === "elsewhere" ? "Host " : ""}GPU avg</th>}
                  {hwScope(run) !== "old" && <th className="num">{hwScope(run) === "elsewhere" ? "Host " : ""}CPU avg</th>}
                </tr>
              </thead>
              <tbody>
                {attempts.map((a) => (
                  <AttemptLine key={a.id} a={a} run={run} active={a.id === selected?.id} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      {selected && <AttemptDetail run={run} row={selected} />}
    </>
  );
}

function AttemptLine({ a, run, active }: { a: AttemptRow; run: Run; active: boolean }) {
  return (
    <tr className={active ? "is-selected" : undefined}>
      <td data-label="Attempt"><a href={`#/bench/run/${run.id}/${a.scenario_id}/${a.id}`}>#{a.attempt}</a></td>
      <td data-label="Level" className="mono">{effortLabel(a.effort)}</td>
      <td data-label="Score"><span className={`score score-${scoreLevel(a.score)}`}>{score(a.score)}</span></td>
      <td data-label="Status">
        <Badge level={a.status === "ok" ? "ok" : "crit"}>{a.status}</Badge>
        {a.answer_issue === "empty" && <span className="block crit-text small" title="The model wrote no answer: it used the whole output limit on thinking">no answer</span>}
        {a.answer_issue === "limit" && <span className="block warn-text small" title="The answer was cut off by the output limit">cut off</span>}
        {a.report_overlap && <span className="block muted small" title="A report was being written meanwhile">report overlapped</span>}
        {a.reload_suspected && <span className="block warn-text small" title={a.reload_suspected}>reload suspected</span>}
      </td>
      <td data-label="TTFT" className="num">{ms(a.ttft_ms)}</td>
      <td data-label="Total" className="num">{ms(a.total_ms)}</td>
      <td data-label="Tokens" className="num">{a.tokens_in ?? "—"} → {a.tokens_out ?? "—"}</td>
      <td data-label="Reasoning" className="num">{a.reasoning_chars ? `${chars(a.reasoning_chars)} chars` : "—"}</td>
      <td data-label="Speed" className="num">{tps(a.tokens_per_s)}</td>
      <td data-label="LLM steps" className="num">{a.llm_calls ?? "—"}</td>
      <td data-label="Tools" className="num">{a.tool_calls ?? "—"}</td>
      <td data-label="Mode" className="muted">{a.tool_mode ?? "—"}</td>
      <td data-label="Judge" className="num">{a.judge_score ?? "—"}</td>
      {hwScope(run) !== "old" && <HwCells hw={a.hw} />}
    </tr>
  );
}

function AttemptDetail({ run, row }: { run: Run; row: AttemptRow }) {
  const [a, setA] = useState<Attempt | null>(null);
  useEffect(() => {
    let alive = true;
    setA(null);
    api.get<Attempt>(`/api/bench/runs/${run.id}/attempts/${row.id}`).then((d) => alive && setA(d), () => undefined);
    return () => {
      alive = false;
    };
  }, [run.id, row.id]);
  if (!a) return <p className="muted">Loading attempt…</p>;
  const checks = a.checks ?? [];
  const passed = checks.filter((c) => c.passed).length;
  const detail = a.detail ?? {};
  const isReport = a.grp === "report" || Boolean(detail.report);
  return (
    <>
      <Panel title={`Attempt #${a.attempt}: checks`} aside={<span className="muted small">{passed} of {checks.length} passed · score {score(a.score)}</span>}>
        {a.error && <p className="error">{a.error}</p>}
        {a.reload_suspected && <p className="warn-text">Reload suspected: {a.reload_suspected}. The attempt is counted as it is; its timings may include loading the model.</p>}
        <ul className="check-results">
          {checks.map((c, i) => (
            <li key={i} className={c.passed ? "pass" : "fail"}>
              <span className="check-mark" aria-label={c.passed ? "passed" : "failed"}>{c.passed ? "✓" : "✗"}</span>
              <span className="check-text">
                {c.describe}
                {c.weight !== 1 && <span className="muted small"> · weight {c.weight}</span>}
                <span className="block muted small">{c.reason}</span>
              </span>
            </li>
          ))}
        </ul>
        {a.judge_score !== null && (
          <p className="judge">
            Judge: <strong>{a.judge_score}/5</strong> <span className="muted">{a.judge_reason}</span>
            <span className="help block">The judge model's grade is shown separately and never part of the score above.</span>
          </p>
        )}
      </Panel>

      <Panel title="Answer">
        {a.answer ? isReport ? <Markdown text={a.answer} /> : <div className="bench-answer"><Markdown text={a.answer} /></div> : <Empty>No answer was produced.</Empty>}
      </Panel>

      <div className="grid-2">
        <Panel title="Timings and size">
          <dl className="bench-meta">
            <dt>First token</dt><dd>{ms(a.ttft_ms)}</dd>
            <dt>Total</dt><dd>{ms(a.total_ms)}</dd>
            <dt>Thinking level</dt><dd className="mono">{a.effort ? effortLabel(a.effort) : "not applicable"}</dd>
            <dt>Tokens</dt><dd>{a.tokens_in ?? "—"} in · {a.tokens_out ?? "—"} out · {tps(a.tokens_per_s)}</dd>
            <dt>Reasoning</dt><dd>{a.reasoning_chars ? `${a.reasoning_chars} characters` : "none written"}</dd>
            <dt>LLM calls</dt><dd>{a.llm_calls ?? "—"}{detail.repairs ? ` · ${detail.repairs} repair prompt(s)` : ""}</dd>
            <dt>Tool mode</dt><dd>{a.tool_mode ?? "—"}</dd>
            <dt>Started</dt><dd>{shortDate(a.started)}</dd>
          </dl>
          {a.trace_ids && a.trace_ids.length > 0 && (
            <p className="trace-links">
              LLM traces:{" "}
              {a.trace_ids.map((t) => (
                <a key={t} href={`#/logs/llm/${t}`} className="mono">#{t}</a>
              ))}
              <span className="help block">Traces are kept as long as the Logs retention allows.</span>
            </p>
          )}
        </Panel>
        <Panel title="What the model did">
          {(detail.tool_calls ?? []).length === 0 && !detail.prom && !detail.report ? (
            <Empty>No tool calls.</Empty>
          ) : (
            <ul className="tool-list">
              {(detail.tool_calls ?? []).map((t, i) => (
                <li key={i}>
                  <span className={`dot dot-${t.ok === false ? "crit" : "ok"}`} aria-hidden /> <strong>{t.name}</strong>
                  <pre>{JSON.stringify(t.args)}</pre>
                </li>
              ))}
              {(detail.actions ?? []).map((x, i) => (
                <li key={`a${i}`}>
                  <span className="dot dot-info" aria-hidden /> action <strong>{x.action}</strong> → {x.status}
                </li>
              ))}
            </ul>
          )}
          {detail.report && (
            <p className="muted small">
              Report: health {detail.report.health ?? "—"}, score {detail.report.score ?? "—"}, {detail.report.findings.length} finding(s){detail.report.parsed ? "" : ", JSON header not understood"}
            </p>
          )}
          {detail.prom && (
            <ul className="tool-list">
              {detail.prom.first_replies.map((q, i) => (
                <li key={i}>
                  <span className={`dot dot-${q.bare_counters.length || !q.job_scoped ? "warn" : "ok"}`} aria-hidden /> first reply · {q.job}
                  <pre>{q.query}</pre>
                </li>
              ))}
              <li className="muted small">{detail.prom.accepted.length} accepted · {detail.prom.rejected.length} rejected by the planner</li>
            </ul>
          )}
        </Panel>
      </div>
    </>
  );
}

/** A sampling setting as the run resolved it; "not recorded" (muted) for runs made before it was kept. */
function Sampled({ llm, k }: { llm: Record<string, unknown>; k: string }) {
  const text = samplingText(llm, k);
  return samplingMissing(text) ? <span className="muted">{text}</span> : <>{text}</>;
}
