import { useMemo, useState } from "react";
import { Badge, Empty, Panel } from "../../ui";
import { GROUP_SHORT, backendLabel, ms, score, scoreLevel, shortDate, sizeLabel, statusLevel, tps } from "./format";
import type { Run } from "./types";

type SortKey = "started" | "model" | "overall" | "ttft" | "total" | "tps" | "pass" | "failures";

const value = (r: Run, key: SortKey): number | string => {
  switch (key) {
    case "started": return r.started;
    case "model": return (r.model ?? "").toLowerCase();
    case "overall": return r.overall ?? -1;
    case "ttft": return r.summary?.median_ttft_ms ?? Number.POSITIVE_INFINITY;
    case "total": return r.summary?.median_total_ms ?? Number.POSITIVE_INFINITY;
    case "tps": return r.summary?.median_tokens_per_s ?? -1;
    case "pass": return r.summary?.pass_rate ?? -1;
    case "failures": return r.summary?.failures ?? -1;
  }
};

export function RunsTable({ runs, selected, onSelect }: { runs: Run[]; selected: number[]; onSelect: (ids: number[]) => void }) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "started", dir: -1 });
  const sorted = useMemo(() => {
    const list = [...runs];
    list.sort((a, b) => {
      const x = value(a, sort.key);
      const y = value(b, sort.key);
      return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
    });
    return list;
  }, [runs, sort]);
  const toggle = (id: number) => onSelect(selected.includes(id) ? selected.filter((i) => i !== id) : [...selected, id]);
  const head = (key: SortKey, label: string, className = "") => (
    <th className={className} aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
      <button className="th-sort" onClick={() => setSort({ key, dir: sort.key === key ? ((sort.dir * -1) as 1 | -1) : key === "model" ? 1 : -1 })}>
        {label}
        {sort.key === key && <span aria-hidden>{sort.dir === 1 ? " ↑" : " ↓"}</span>}
      </button>
    </th>
  );
  const ready = selected.length >= 2;

  return (
    <Panel
      title="Runs"
      aside={
        <>
          <span className="muted small">{ready ? `${selected.length} selected` : "Tick two or more runs to compare"}</span>
          <a className={`btn${ready ? " btn-primary" : ""}`} aria-disabled={!ready} href={ready ? `#/bench/compare/${selected.join(",")}` : undefined} onClick={(e) => !ready && e.preventDefault()}>
            Compare
          </a>
        </>
      }
    >
      {runs.length === 0 ? (
        <Empty>No runs yet. Start one above; every run is kept until you delete it.</Empty>
      ) : (
        <div className="bench-scroll">
          <table className="table bench-table runs-table">
            <thead>
              <tr>
                <th className="check-col"><span className="sr-only">Compare</span></th>
                {head("started", "Date")}
                {head("model", "Model")}
                <th>Backend</th>
                <th>Size</th>
                {head("overall", "Score", "num")}
                <th>Groups</th>
                {head("ttft", "TTFT", "num")}
                {head("total", "Time", "num")}
                {head("tps", "Speed", "num")}
                {head("pass", "Pass", "num")}
                {head("failures", "Fail", "num")}
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => (
                <tr key={r.id} className={selected.includes(r.id) ? "is-selected" : undefined}>
                  <td className="check-col">
                    <input type="checkbox" checked={selected.includes(r.id)} onChange={() => toggle(r.id)} aria-label={`Compare run ${r.id}`} />
                  </td>
                  <td data-label="Date" className="nowrap">
                    <a href={`#/bench/run/${r.id}`}>{shortDate(r.started)}</a>
                    {r.status !== "done" && <span className="block"><Badge level={statusLevel(r.status)}>{r.status}</Badge></span>}
                  </td>
                  <td data-label="Model" className="run-model">
                    <a href={`#/bench/run/${r.id}`} className="mono">{r.model ?? "—"}</a>
                    <span className="block muted small">
                      {r.scope === "all" ? "all scenarios" : r.scope.replace("group:", "")} · ×{r.repeats}
                      {r.temperature !== null && r.temperature !== undefined ? ` · temp ${r.temperature}` : ""}
                    </span>
                  </td>
                  <td data-label="Backend" className="muted small">{backendLabel(r)}</td>
                  <td data-label="Size" className="muted small nowrap">{sizeLabel(r)}</td>
                  <td data-label="Score" className="num"><span className={`score score-${scoreLevel(r.overall)}`}>{score(r.overall)}</span></td>
                  <td data-label="Groups" className="group-chips">
                    {Object.entries(r.group_scores ?? {}).map(([g, s]) => (
                      <span key={g} className={`chip chip-${scoreLevel(s)}`} title={`${GROUP_SHORT[g] ?? g}: ${score(s)}`}>
                        {GROUP_SHORT[g] ?? g} {score(s)}
                      </span>
                    ))}
                  </td>
                  <td data-label="TTFT" className="num">{ms(r.summary?.median_ttft_ms)}</td>
                  <td data-label="Time" className="num">{ms(r.summary?.median_total_ms)}</td>
                  <td data-label="Speed" className="num">{tps(r.summary?.median_tokens_per_s)}</td>
                  <td data-label="Pass" className="num">{score(r.summary?.pass_rate)}</td>
                  <td data-label="Fail" className="num">{r.summary ? r.summary.failures : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
