import { useEffect, useState } from "react";
import { api } from "../../api";
import { Empty, Panel } from "../../ui";
import { GROUP_SHORT, backendLabel, ms, runLabel, score, scoreLevel, shortDate, sizeLabel, tps } from "./format";
import type { Catalog, Run } from "./types";

type Cell = { text: string; raw: number | null };

/** Index (or indices) of the best value in `cells`; ties all count. `high` = larger is better. */
function best(cells: (number | null)[], high: boolean): Set<number> {
  const real = cells.map((c, i) => [c, i] as const).filter((p): p is readonly [number, number] => p[0] !== null);
  if (real.length < 2) return new Set();
  const target = (high ? Math.max : Math.min)(...real.map((p) => p[0]));
  const winners = real.filter((p) => Math.abs(p[0] - target) < 1e-9).map((p) => p[1]);
  return winners.length === real.length ? new Set() : new Set(winners); // all equal: nothing to highlight
}

function Row({ label, sub, cells, high, className }: { label: string; sub?: string; cells: Cell[]; high: boolean | null; className?: string }) {
  const winners = high === null ? new Set<number>() : best(cells.map((c) => c.raw), high);
  return (
    <tr className={className}>
      <th scope="row" className="cmp-label">
        {label}
        {sub && <span className="block muted small mono">{sub}</span>}
      </th>
      {cells.map((c, i) => (
        <td key={i} className={`num${winners.has(i) ? " cmp-best" : ""}`} data-label={`Run ${i + 1}`}>
          {c.text}
        </td>
      ))}
    </tr>
  );
}

export function Compare({ ids, catalog }: { ids: number[]; catalog: Catalog | null }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    Promise.all(ids.map((id) => api.get<Run>(`/api/bench/runs/${id}`))).then(
      (r) => alive && setRuns(r),
      (e: Error) => alive && setError(e.message),
    );
    return () => {
      alive = false;
    };
  }, [ids.join(",")]);

  if (error) return <p className="error">{error} <a href="#/bench">Back</a></p>;
  if (!runs) return <p className="muted">Loading…</p>;
  if (runs.length < 2) return <Empty>Pick at least two runs to compare. <a href="#/bench">Back</a></Empty>;

  const sameSet = new Set(runs.map((r) => r.scenario_hash)).size === 1;
  const scenarioIds = Array.from(new Set(runs.flatMap((r) => Object.keys(r.summary?.scenarios ?? {})))).sort();
  const groups = Array.from(new Set(runs.flatMap((r) => Object.keys(r.group_scores ?? {}))));
  const title = (id: string) => catalog?.scenarios.find((s) => s.id === id)?.title ?? id;
  const num = (v: number | null | undefined): number | null => (v === null || v === undefined ? null : v);
  const pct = (v: number | null | undefined): Cell => ({ text: score(v), raw: num(v) });
  const msCell = (v: number | null | undefined): Cell => ({ text: ms(v), raw: num(v) });

  return (
    <div className="stack bench-compare">
      <a className="back-link" href="#/bench">← All runs</a>
      <div className="page-head"><h2>Comparing {runs.length} runs</h2></div>
      {!sameSet && (
        <div className="notice">
          These runs used different scenario sets or scopes, so scores are only comparable on the scenarios they share. Differences are marked in the table below.
        </div>
      )}
      <Panel className="cmp-panel">
        <div className="bench-scroll">
          <table className="table bench-table compare-table">
            <thead>
              <tr>
                <th />
                {runs.map((r) => (
                  <th key={r.id} className="cmp-head">
                    <a href={`#/bench/run/${r.id}`} className="mono">{runLabel(r)}</a>
                    <span className="block muted small">#{r.id} · {shortDate(r.started)}</span>
                    <span className="block muted small">{backendLabel(r)}</span>
                    <span className="block muted small">{sizeLabel(r)}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr className="cmp-section"><th colSpan={runs.length + 1}>Setup</th></tr>
              <Row label="Temperature" high={null} cells={runs.map((r) => ({ text: String((r.meta?.llm as Record<string, unknown> | undefined)?.temperature ?? "—"), raw: null }))} />
              <Row label="Tool mode used" high={null} cells={runs.map((r) => ({ text: r.meta?.tool_mode ?? "—", raw: null }))} />
              <Row label="Attempts each" high={null} cells={runs.map((r) => ({ text: String(r.repeats), raw: null }))} />
              <Row label="Scenario set" high={null} cells={runs.map((r) => ({ text: r.scenario_hash ?? "—", raw: null }))} />
              <tr className="cmp-section"><th colSpan={runs.length + 1}>Accuracy</th></tr>
              <Row label="Overall" high cells={runs.map((r) => pct(r.overall))} className="cmp-strong" />
              {groups.map((g) => (
                <Row key={g} label={GROUP_SHORT[g] ?? g} high cells={runs.map((r) => pct(r.group_scores?.[g]))} />
              ))}
              <Row label="Pass rate" high cells={runs.map((r) => pct(r.summary?.pass_rate))} />
              <Row label="Failed attempts" high={false} cells={runs.map((r) => ({ text: r.summary ? String(r.summary.failures) : "—", raw: r.summary ? r.summary.failures : null }))} />
              <tr className="cmp-section"><th colSpan={runs.length + 1}>Speed</th></tr>
              <Row label="Median first token" high={false} cells={runs.map((r) => msCell(r.summary?.median_ttft_ms))} />
              <Row label="Median attempt time" high={false} cells={runs.map((r) => msCell(r.summary?.median_total_ms))} />
              <Row label="Generation speed" high cells={runs.map((r) => ({ text: tps(r.summary?.median_tokens_per_s), raw: num(r.summary?.median_tokens_per_s) }))} />
              <tr className="cmp-section"><th colSpan={runs.length + 1}>Per scenario: score · median time</th></tr>
              {scenarioIds.map((id) => (
                <ScenarioRow key={id} id={id} label={title(id)} runs={runs} />
              ))}
            </tbody>
          </table>
        </div>
        <p className="help">The best value in each row is highlighted. Time and failures: lower is better; scores and speed: higher is better.</p>
      </Panel>
    </div>
  );
}

function ScenarioRow({ id, label, runs }: { id: string; label: string; runs: Run[] }) {
  const stats = runs.map((r) => r.summary?.scenarios?.[id]);
  const scores = stats.map((s) => (s ? s.score : null));
  const times = stats.map((s) => s?.median_total_ms ?? null);
  const bestScore = best(scores, true);
  // Among the runs that scored the best, the fastest one wins the time.
  const topScore = scores.length ? Math.max(...scores.filter((s): s is number => s !== null)) : null;
  const eligible = times.map((t, i) => (scores[i] !== null && topScore !== null && Math.abs((scores[i] as number) - topScore) < 1e-9 ? t : null));
  const bestTime = best(eligible, false);
  return (
    <tr>
      <th scope="row" className="cmp-label">
        <a href={`#/bench/run/${runs[0].id}/${id}`}>{label}</a>
        <span className="block muted small mono">{id}</span>
      </th>
      {stats.map((s, i) => (
        <td key={i} className="cmp-cell" data-label={`Run ${i + 1}`}>
          {s ? (
            <>
              <span className={`score score-${scoreLevel(s.score)}${bestScore.has(i) ? " cmp-best" : ""}`}>{score(s.score)}</span>
              <span className={`cmp-time muted${bestTime.has(i) ? " cmp-best" : ""}`}> · {ms(s.median_total_ms)}</span>
            </>
          ) : (
            <span className="muted">not run</span>
          )}
        </td>
      ))}
    </tr>
  );
}
