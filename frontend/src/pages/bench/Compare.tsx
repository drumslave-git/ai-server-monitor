import { Fragment, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { api } from "../../api";
import { Empty, Panel } from "../../ui";
import { EFFORTS, EFFORT_HELP, GROUP_SHORT, automationText, backendLabel, bestOf, chars, coldStartText, effortLabel, gb, gbOf, ms, pct as pctText, routingLabel, runLabel, samplingText, score, scoreLevel, shortDate, sizeLabel, tps, watts } from "./format";
import type { Shown } from "./format";
import { hardwareLabel, hwScope, vramStat } from "./Hardware";
import { levelsOf } from "./Levels";
import type { Catalog, Run } from "./types";

type Cell = Shown & { muted?: boolean; wrap?: boolean };

function Row({ label, sub, cells, high, className }: { label: string; sub?: string; cells: Cell[]; high: boolean | null; className?: string }) {
  const winners = high === null ? new Set<number>() : bestOf(cells, high);
  return (
    <tr className={className}>
      <th scope="row" className="cmp-label">
        {label}
        {sub && <span className="block muted small">{sub}</span>}
      </th>
      {cells.map((c, i) => (
        <td key={i} className={`num${winners.has(i) ? " cmp-best" : ""}${c.muted ? " muted" : ""}${c.wrap || c.raw === null ? " cmp-wrap" : ""}`} data-label={`Run ${i + 1}`}>
          {c.text}
        </td>
      ))}
    </tr>
  );
}

/** A section: white space above, then a heading row. The row's height is the pinned header's, so the
 * two line up where the real one scrolls under the pinned one. */
function Section({ title, cols, first }: { title: string; cols: number; first?: boolean }) {
  return (
    <>
      {!first && <tr className="cmp-gap" aria-hidden="true"><td colSpan={cols} /></tr>}
      <tr className="cmp-section" data-title={title}>
        <th colSpan={cols}><span className="cmp-section-title">{title}</span></th>
      </tr>
    </>
  );
}

function RunHead({ run, pinned = false }: { run: Run; pinned?: boolean }) {
  return (
    <>
      <a href={`#/bench/run/${run.id}`} className="mono" tabIndex={pinned ? -1 : undefined}>{runLabel(run)}</a>
      <span className="block muted small">#{run.id}<span className={pinned ? "cmp-pin-date" : undefined}> · {shortDate(run.started)}</span></span>
      {!pinned && <span className="block muted small">{backendLabel(run)}</span>}
      {!pinned && <span className="block muted small">{sizeLabel(run)}</span>}
    </>
  );
}

type Stick = { on: boolean; section: string; left: number; widths: number[]; width: number };
const NO_STICK: Stick = { on: false, section: "", left: 0, widths: [], width: 0 };

/** Pins the column header and the current section's heading at the top of the window while the table
 * scrolls past. CSS `position: sticky` cannot do it: the table sits in a box that scrolls sideways on
 * a phone, and such a box is the only thing a sticky cell would stick to. So a copy of the header is
 * laid over the table (same column widths, shifted by the sideways scroll) once the real one is out of
 * sight, and the heading shown in it is the last section whose row has reached the pinned header. */
function useStickyHead(table: RefObject<HTMLTableElement | null>, scroller: RefObject<HTMLDivElement | null>, bar: RefObject<HTMLDivElement | null>, key: string): Stick {
  const [st, setSt] = useState<Stick>(NO_STICK);
  useEffect(() => {
    const t = table.current;
    const box = scroller.current;
    if (!t || !box) return;
    let frame = 0;
    const measure = () => {
      frame = 0;
      const head = t.tHead;
      if (!head) return;
      const h = head.getBoundingClientRect();
      const rect = t.getBoundingClientRect();
      const barHeight = bar.current?.getBoundingClientRect().height ?? 0;
      // the pinned header is shorter than the real one: a section takes over where it ends
      const pinned = bar.current?.querySelector("thead")?.getBoundingClientRect().height || h.height;
      let section = "";
      for (const row of Array.from(t.querySelectorAll<HTMLElement>("tr.cmp-section"))) {
        if (row.getBoundingClientRect().top > pinned + 1) break;
        section = row.dataset.title ?? "";
      }
      const next: Stick = {
        on: h.bottom <= 0 && rect.bottom > barHeight,
        section,
        left: box.scrollLeft,
        widths: Array.from(head.rows[0].cells).map((c) => c.getBoundingClientRect().width),
        width: rect.width,
      };
      setSt((p) => (p.on === next.on && p.section === next.section && p.left === next.left && p.width === next.width && p.widths.join() === next.widths.join() ? p : next));
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(measure);
    };
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    box.addEventListener("scroll", schedule, { passive: true });
    const ro = new ResizeObserver(schedule);
    ro.observe(t);
    measure();
    return () => {
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      box.removeEventListener("scroll", schedule);
      ro.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, [key]);
  return st;
}

export function Compare({ ids, catalog }: { ids: number[]; catalog: Catalog | null }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const table = useRef<HTMLTableElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const bar = useRef<HTMLDivElement>(null);
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
  const stick = useStickyHead(table, scroller, bar, runs ? runs.map((r) => r.id).join(",") : "");

  if (error) return <p className="error">{error} <a href="#/bench">Back</a></p>;
  if (!runs) return <p className="muted">Loading…</p>;
  if (runs.length < 2) return <Empty>Pick at least two runs to compare. <a href="#/bench">Back</a></Empty>;

  const cols = runs.length + 1;
  const sameSet = new Set(runs.map((r) => r.scenario_hash)).size === 1;
  const scenarioIds = Array.from(new Set(runs.flatMap((r) => Object.keys(r.summary?.scenarios ?? {})))).sort();
  const groups = Array.from(new Set(runs.flatMap((r) => Object.keys(r.group_scores ?? {}))));
  const title = (id: string) => catalog?.scenarios.find((s) => s.id === id)?.title ?? id;
  const num = (v: number | null | undefined): number | null => (v === null || v === undefined ? null : v);
  const pct = (v: number | null | undefined): Cell => ({ text: score(v), raw: num(v) });
  const msCell = (v: number | null | undefined): Cell => ({ text: ms(v), raw: num(v) });
  const plain = (text: string, muted = false): Cell => ({ text, raw: null, muted });
  const sampling = (key: string): Cell[] =>
    runs.map((r) => {
      const text = samplingText(r.meta?.llm, key);
      return plain(text, text === "not recorded");
    });

  return (
    <div className="stack bench-compare">
      <a className="back-link" href="#/bench">← All runs</a>
      <div className="page-head">
        <h2>Comparing {runs.length} runs</h2>
        <div className="page-actions">
          <a className="btn" href={`/api/bench/compare.pdf?ids=${ids.join(",")}`} download>Download PDF</a>
        </div>
      </div>
      {!sameSet && (
        <div className="notice">
          These runs used different scenario sets or scopes, so scores are only comparable on the scenarios they share. Differences are marked in the table below.
        </div>
      )}
      <Panel className="cmp-panel">
        <div className={`cmp-stick${stick.on ? " is-on" : ""}`} aria-hidden="true" inert>
          <div className="cmp-stick-bar" ref={bar}>
            <div className="cmp-stick-inner" style={{ width: stick.width, transform: `translateX(${-stick.left}px)` }}>
              <table className="table bench-table compare-table">
                <colgroup>{stick.widths.map((w, i) => <col key={i} style={{ width: w }} />)}</colgroup>
                <thead>
                  <tr>
                    <th />
                    {runs.map((r) => (
                      <th key={r.id} className="cmp-head"><RunHead run={r} pinned /></th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <tr className="cmp-section">
                    <th colSpan={cols}><span className="cmp-section-title" style={{ transform: `translateX(${stick.left}px)` }}>{stick.section}</span></th>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <div className="bench-scroll" ref={scroller}>
          <table className="table bench-table compare-table" ref={table}>
            <thead>
              <tr>
                <th />
                {runs.map((r) => (
                  <th key={r.id} className="cmp-head"><RunHead run={r} /></th>
                ))}
              </tr>
            </thead>
            <tbody>
              <Section title="Setup" cols={cols} first />
              <Row label="Temperature" high={null} cells={sampling("temperature")} />
              <Row label="Top P" high={null} cells={sampling("top_p")} />
              <Row label="Top K" high={null} cells={sampling("top_k")} />
              <Row label="Tool mode used" high={null} cells={runs.map((r) => plain(r.meta?.tool_mode ?? "—"))} />
              <Row label="Tool routing" high={null} cells={runs.map((r) => plain(routingLabel(r), routingLabel(r) === "not recorded"))} />
              <Row label="Attempts each" high={null} cells={runs.map((r) => plain(String(r.repeats)))} />
              <Row label="Automation" high={null} cells={runs.map((r) => plain(automationText(r.meta?.automation)))} />
              <Row label="Scenario set" high={null} cells={runs.map((r) => plain(r.scenario_hash ?? "—"))} />
              <Section title="Accuracy" cols={cols} />
              <Row label="Overall" high cells={runs.map((r) => pct(r.overall))} className="cmp-strong" />
              {groups.map((g) => (
                <Row key={g} label={GROUP_SHORT[g] ?? g} high cells={runs.map((r) => pct(r.group_scores?.[g]))} />
              ))}
              <Row label="Pass rate" high cells={runs.map((r) => pct(r.summary?.pass_rate))} />
              <Row label="Failed attempts" high={false} cells={runs.map((r) => ({ text: r.summary ? String(r.summary.failures) : "—", raw: r.summary ? r.summary.failures : null }))} />
              <Section title="Speed" cols={cols} />
              <Row label="Median first token" high={false} cells={runs.map((r) => msCell(r.summary?.median_ttft_ms))} />
              <Row label="Median attempt time" high={false} cells={runs.map((r) => msCell(r.summary?.median_total_ms))} />
              <Row label="Cold start (not in the numbers)" high={false} cells={runs.map((r) => plain(coldStartText(r.meta?.cold_start)))} />
              <Row label="Generation speed" high cells={runs.map((r) => ({ text: tps(r.summary?.median_tokens_per_s), raw: num(r.summary?.median_tokens_per_s) }))} />
              <HardwareRows runs={runs} />
              <LevelRows runs={runs} />
              <Section title="Per scenario: score · median time" cols={cols} />
              {scenarioIds.map((id) => (
                <ScenarioRow key={id} id={id} label={title(id)} runs={runs} />
              ))}
            </tbody>
          </table>
        </div>
        <p className="help">
          The best value in each row is highlighted; values that print the same tie, and a row where all are equal highlights nothing. Time, failures and hardware load: lower is better; scores, speed and the share of the model on the GPU: higher is better. Hardware numbers of a model served from another machine are the host's, labelled "(host)" and not highlighted.
          Under each scenario's numbers is why points were lost, from the checks recorded for the attempts: "(2 of 3)" is the number of attempts that missed that check.
        </p>
      </Panel>
    </div>
  );
}

/** Per thinking level: accuracy, time, tokens, reasoning, answers that did not come and VRAM peak,
 * each level as a group with its own heading. Only the levels some run was tested at appear; a run
 * without that level shows "—". */
function LevelRows({ runs }: { runs: Run[] }) {
  const levels = EFFORTS.filter((e) => runs.some((r) => levelsOf(r).includes(e)));
  if (levels.length === 0) return null;
  const cols = runs.length + 1;
  const val = (v: number | null | undefined): number | null => (v === null || v === undefined ? null : v);
  return (
    <>
      <Section title="Per thinking level" cols={cols} />
      {levels.map((e, n) => {
        const l = (r: Run) => r.summary?.levels?.[e];
        const none: Cell = { text: "—", raw: null };
        return (
          <Fragment key={e}>
            {n > 0 && <tr className="cmp-gap cmp-gap-sm" aria-hidden="true"><td colSpan={cols} /></tr>}
            <tr className="cmp-level">
              <th colSpan={cols}>
                <span className="cmp-level-title">
                  <span className="mono">{effortLabel(e)}</span>
                  <span className="muted"> · {EFFORT_HELP[e]}</span>
                </span>
              </th>
            </tr>
            <Row label="Accuracy" high cells={runs.map((r) => (l(r) ? { text: score(l(r)?.overall), raw: val(l(r)?.overall) } : none))} className="cmp-strong" />
            <Row label="Time per answer" high={false} cells={runs.map((r) => (l(r) ? { text: ms(l(r)?.median_total_ms), raw: val(l(r)?.median_total_ms) } : none))} />
            <Row label="Tokens out" high={null} cells={runs.map((r) => (l(r) ? { text: String(l(r)?.median_tokens_out ?? "—"), raw: null } : none))} />
            <Row label="Reasoning" high={null} cells={runs.map((r) => (l(r) ? { text: l(r)?.median_reasoning_chars ? `${chars(l(r)?.median_reasoning_chars)} chars` : "—", raw: null } : none))} />
            <Row label="Empty / cut off" high={false} cells={runs.map((r) => (l(r) ? { text: `${l(r)?.empty} / ${l(r)?.limit}`, raw: (l(r)?.empty ?? 0) + (l(r)?.limit ?? 0) } : none))} />
            <Row
              label="VRAM peak"
              high={null}
              cells={runs.map((r) => {
                const v = vramStat(l(r)?.hw);
                return v ? { text: hwScope(r) === "elsewhere" ? `${gb(v.peak)} (host)` : gb(v.peak), raw: null } : none;
              })}
            />
          </Fragment>
        );
      })}
    </>
  );
}

type Picker = (r: Run) => Cell | null;

/** The hardware rows. Lower is better for what the model costs (VRAM, power, CPU, RAM); GPU load is
 * only shown. When the LLM server was on another machine the numbers are this host's, so they are
 * labelled and never highlighted as the model's. */
function HardwareRows({ runs }: { runs: Run[] }) {
  const cell = (r: Run, pick: Picker): Cell => {
    if (hwScope(r) === "old") return { text: "not recorded", raw: null, muted: true };
    const c = r.summary?.hardware ? pick(r) : null;
    if (!c) return { text: "—", raw: null };
    return hwScope(r) === "elsewhere" ? { text: `${c.text} (host)`, raw: null } : c;
  };
  const gpuCell = (r: Run, pick: Picker): Cell => {
    const meta = r.meta?.hardware;
    return meta?.recorded && !meta.gpu ? { text: "no GPU", raw: null } : cell(r, pick);
  };
  const vram: Picker = (r) => {
    const hw = r.summary?.hardware;
    const s = hw ? hw.llm_vram ?? hw.vram_used : null;
    return s ? { text: gbOf(s.peak, hw?.vram_total ?? null), raw: s.peak } : null;
  };
  const stat = (key: "gpu_util" | "power_w" | "cpu" | "ram_used", part: "avg" | "peak", text: (v: number, r: Run) => string): Picker => (r) => {
    const s = r.summary?.hardware?.[key];
    return s ? { text: text(s[part], r), raw: s[part] } : null;
  };
  const loaded: Picker = (r) => {
    const g = r.meta?.loaded_model?.gpu_percent;
    return g === null || g === undefined ? null : { text: `${g}%`, raw: g };
  };
  const ownVram = runs.some((r) => r.summary?.hardware?.llm_vram);
  const cols = runs.length + 1;
  return (
    <>
      <Section title="Hardware" cols={cols} />
      <Row
        label="Measured from"
        sub="the process that holds this model in VRAM"
        high={null}
        cells={runs.map((r) => {
          const text = hardwareLabel(r.meta?.hardware, r.backend_kind);
          return { text, raw: null, muted: text === "not recorded", wrap: true };
        })}
      />
      <Row label="VRAM, peak" sub={ownVram ? "model's own process when seen" : "whole GPU"} high={false} cells={runs.map((r) => gpuCell(r, vram))} />
      <Row label="GPU busy, average" high={null} cells={runs.map((r) => gpuCell(r, stat("gpu_util", "avg", (v) => pctText(v))))} />
      <Row label="GPU power, average" high={false} cells={runs.map((r) => gpuCell(r, stat("power_w", "avg", (v) => watts(v))))} />
      <Row label="CPU, average" high={false} cells={runs.map((r) => cell(r, stat("cpu", "avg", (v) => pctText(v))))} />
      <Row label="RAM, peak" high={false} cells={runs.map((r) => cell(r, stat("ram_used", "peak", (v, run) => gbOf(v, run.summary?.hardware?.ram_total ?? null))))} />
      <Row label="Model on GPU" sub="rest runs on the CPU" high cells={runs.map((r) => (hwScope(r) === "old" ? { text: "not recorded", raw: null, muted: true } : (loaded(r) ?? { text: "—", raw: null })))} />
    </>
  );
}

function ScenarioRow({ id, label, runs }: { id: string; label: string; runs: Run[] }) {
  const stats = runs.map((r) => r.summary?.scenarios?.[id]);
  const scores = stats.map((s) => (s ? s.score : null));
  const times = stats.map((s) => s?.median_total_ms ?? null);
  const scoreCells: Shown[] = scores.map((s) => ({ text: score(s), raw: s }));
  const bestScore = bestOf(scoreCells, true);
  // Among the runs whose score prints as the best one, the fastest wins the time.
  const real = scores.filter((s): s is number => s !== null);
  const top = real.length ? score(Math.max(...real)) : null;
  const bestTime = bestOf(times.map((t, i) => ({ text: ms(t), raw: scores[i] !== null && score(scores[i]) === top ? t : null })), false);
  return (
    <tr>
      <th scope="row" className="cmp-label">
        <a href={`#/bench/run/${runs[0].id}/${id}`}>{label}</a>
        <span className="block muted small mono">{id}</span>
      </th>
      {stats.map((s, i) => {
        const why = runs[i].explanations?.[id];
        return (
          <td key={i} className="cmp-cell" data-label={`Run ${i + 1}`}>
            {s ? (
              <>
                <span className={`score score-${scoreLevel(s.score)}${bestScore.has(i) ? " cmp-best" : ""}`}>{score(s.score)}</span>
                <span className={`cmp-time muted${bestTime.has(i) ? " cmp-best" : ""}`}> · {ms(s.median_total_ms)}</span>
                {why && why.lines.map((line, n) => <span key={n} className={`cmp-why block small${why.ok ? " cmp-why-ok" : ""}`}>{line}</span>)}
              </>
            ) : (
              <span className="muted">not run</span>
            )}
          </td>
        );
      })}
    </tr>
  );
}
