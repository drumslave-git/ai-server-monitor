import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useStream } from "../live";
import { PageHead } from "../ui";
import { CatalogPanel } from "./bench/Catalog";
import { Compare } from "./bench/Compare";
import { RunDetail } from "./bench/RunDetail";
import { RunPanel } from "./bench/RunPanel";
import { RunsTable } from "./bench/RunsTable";
import { SnapshotsPanel } from "./bench/Snapshots";
import type { BenchStatus, Catalog, Run, SnapshotInfo } from "./bench/types";
import "./bench/bench.css";

/** Routes (params after `#/bench`):
 *    (none)                                  run controls, runs, scenarios
 *    run/<id>[/<scenario>[/<attempt id>]]    one run, one scenario of it, one attempt
 *    compare/<id>,<id>,…                     runs side by side
 */
export function BenchmarksPage({ params }: { params: string[] }) {
  const [status, setStatus] = useState<BenchStatus | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [snapshots, setSnapshots] = useState<SnapshotInfo[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(() => api.get<BenchStatus>("/api/bench/status").then(setStatus, (e: Error) => setError(e.message)), []);
  const loadRuns = useCallback(() => api.get<{ items: Run[] }>("/api/bench/runs").then((d) => setRuns(d.items), (e: Error) => setError(e.message)), []);
  const loadSnapshots = useCallback(() => api.get<{ items: SnapshotInfo[] }>("/api/bench/snapshots").then((d) => setSnapshots(d.items), () => undefined), []);
  const reload = useCallback(() => {
    void loadStatus();
    void loadRuns();
  }, [loadStatus, loadRuns]);

  useEffect(() => {
    api.get<Catalog>("/api/bench/catalog?smoke=true").then(setCatalog, (e: Error) => setError(e.message));
    reload();
    void loadSnapshots();
  }, [reload, loadSnapshots]);

  useStream(["bench"], (event) => {
    if (event.type === "bench.done") reload();
    else setStatus((s) => (s ? { ...s, running: true, progress: event.data as BenchStatus["progress"] } : s));
  });
  // While a run is going, keep the table fresh as attempts finish.
  const running = status?.running ?? false;
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(reload, 5000);
    return () => clearInterval(timer);
  }, [running, reload]);

  // The app's own test scenarios are known (runs of them can be opened) but never offered.
  const offered = catalog ? { ...catalog, scenarios: catalog.scenarios.filter((s) => !s.smoke) } : null;
  const page = params[0];
  if (page === "run" && /^\d+$/.test(params[1] ?? "")) {
    const attemptId = /^\d+$/.test(params[3] ?? "") ? Number(params[3]) : undefined;
    return <RunDetail id={Number(params[1])} scenarioId={params[2]} attemptId={attemptId} catalog={catalog} />;
  }
  if (page === "compare") {
    const ids = (params[1] ?? "").split(",").filter((s) => /^\d+$/.test(s)).map(Number);
    return <Compare ids={ids} catalog={catalog} />;
  }

  return (
    <div className="stack bench">
      <PageHead title="Benchmarks" />
      {error && <p className="error">{error}</p>}
      <RunPanel status={status} catalog={offered} reload={reload} />
      {runs && <RunsTable runs={runs} selected={selected} onSelect={setSelected} />}
      <CatalogPanel catalog={offered} />
      <SnapshotsPanel snapshots={snapshots} reload={() => void loadSnapshots()} />
    </div>
  );
}
