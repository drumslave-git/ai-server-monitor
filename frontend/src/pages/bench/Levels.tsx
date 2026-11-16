import { Empty, Panel } from "../../ui";
import { EFFORTS, EFFORT_HELP, bestOf, chars, effortLabel, gb, ms, score, scoreLevel } from "./format";
import { vramStat } from "./Hardware";
import type { Catalog, Effort, LevelStat, Run } from "./types";

/** The thinking levels a run was tested at, in the API's order. */
export const levelsOf = (run: Run): Effort[] => EFFORTS.filter((e) => run.summary?.levels?.[e]);

const issues = (l: LevelStat): string => {
  const parts = [l.empty ? `${l.empty} empty` : "", l.limit ? `${l.limit} cut off` : ""].filter(Boolean);
  return parts.length ? parts.join(" · ") : "0";
};

/** Run page: one row per thinking level, so the levels can be compared on accuracy, time, tokens,
 * answers that did not come and hardware. */
export function LevelsPanel({ run }: { run: Run }) {
  const levels = levelsOf(run);
  if (levels.length === 0) return null;
  // the best level is marked on the accuracy shown; levels that tie are all marked, equal ones none
  const winners = bestOf(levels.map((e) => ({ text: score(run.summary?.levels?.[e]?.overall), raw: run.summary?.levels?.[e]?.overall ?? null })), true);
  const note = run.meta?.levels;
  return (
    <Panel title="Thinking levels" aside={<span className="muted small">every scenario at each level</span>}>
      <div className="bench-scroll">
        <table className="table bench-table level-table">
          <thead>
            <tr>
              <th>Level</th>
              <th className="num">Accuracy</th>
              <th className="num">Pass rate</th>
              <th className="num">Time per answer</th>
              <th className="num">Tokens out</th>
              <th className="num">Reasoning</th>
              <th className="num">Empty / cut off</th>
              <th className="num">Failed</th>
              <th className="num">VRAM peak</th>
            </tr>
          </thead>
          <tbody>
            {levels.map((e, i) => {
              const l = run.summary?.levels?.[e] as LevelStat;
              const vram = vramStat(l.hw);
              return (
                <tr key={e}>
                  <td data-label="Level">
                    <strong className="mono">{effortLabel(e)}</strong>
                    <span className="block muted small">{EFFORT_HELP[e]} · {l.attempts} attempts</span>
                  </td>
                  <td data-label="Accuracy" className="num">
                    <span className={`score score-${scoreLevel(l.overall)}${winners.has(i) ? " cmp-best" : ""}`}>{score(l.overall)}</span>
                  </td>
                  <td data-label="Pass rate" className="num">{score(l.pass_rate)}</td>
                  <td data-label="Time per answer" className="num">{ms(l.median_total_ms)}</td>
                  <td data-label="Tokens out" className="num">{l.median_tokens_out ?? "—"}</td>
                  <td data-label="Reasoning" className="num">{l.median_reasoning_chars ? `${chars(l.median_reasoning_chars)} chars` : "—"}</td>
                  <td data-label="Empty / cut off" className={`num${l.empty || l.limit ? " crit-text" : ""}`}>{issues(l)}</td>
                  <td data-label="Failed" className="num">{l.failed}/{l.attempts}</td>
                  <td data-label="VRAM peak" className="num">{vram ? gb(vram.peak) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="help">
        Time, tokens out and reasoning are medians per attempt; an attempt that ended without an answer counts with the time and tokens it spent. Tokens out include the reasoning; the API reports no
        split, so reasoning is counted in characters. {note ? `The level changes ${note.applies_to}. ${note.fixed}` : ""}
      </p>
    </Panel>
  );
}

/** Run page: the score and time of every scenario at every level. */
export function LevelMatrix({ run, catalog }: { run: Run; catalog: Catalog | null }) {
  const levels = levelsOf(run);
  const stats = run.summary?.scenarios ?? {};
  const ids = Object.keys(stats).filter((id) => stats[id].levels);
  if (levels.length < 2 || ids.length === 0) return null;
  const title = (id: string) => catalog?.scenarios.find((s) => s.id === id)?.title ?? id;
  return (
    <Panel title="Scenarios by level" aside={<span className="muted small">score · median time</span>}>
      <div className="bench-scroll">
        <table className="table bench-table level-matrix">
          <thead>
            <tr>
              <th>Scenario</th>
              {levels.map((e) => (
                <th key={e} className="num mono">{effortLabel(e)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ids.map((id) => (
              <tr key={id}>
                <td data-label="Scenario">
                  <a href={`#/bench/run/${run.id}/${id}`}>{title(id)}</a>
                </td>
                {levels.map((e) => {
                  const s = stats[id].levels?.[e];
                  return (
                    <td key={e} data-label={effortLabel(e)} className="num">
                      {s ? (
                        <>
                          <span className={`score score-${scoreLevel(s.score)}`}>{score(s.score)}</span>
                          <span className="muted"> · {ms(s.median_total_ms)}</span>
                          {s.empty > 0 && <span className="crit-text block small">{s.empty} empty</span>}
                        </>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {ids.length === 0 && <Empty>No attempts.</Empty>}
    </Panel>
  );
}
