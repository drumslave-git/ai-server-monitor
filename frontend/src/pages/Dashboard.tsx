import { useState } from "react";
import { useAutomation, sinceText } from "../automation";
import { api, type Collectors, type CollectorEntry, type StatusResponse } from "../api";
import { ago, bytes, duration, pct, rate } from "../fmt";
import { useMetricHistory, useStream } from "../live";
import {
  Badge,
  Dot,
  Empty,
  healthLevel,
  healthWord,
  Meter,
  Panel,
  ScoreRing,
  severityLevel,
  Spark,
  useNow,
  usePoll,
  type Level,
} from "../ui";

type Data = Record<string, any>;

export function DashboardPage() {
  const now = useNow();
  const status = usePoll(() => api.get<StatusResponse>("/api/status"), 15_000);
  const collectors = usePoll(() => api.get<Collectors>("/api/collectors"), 15_000);
  const { state: auto } = useAutomation();
  const paused = Boolean(auto?.paused);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  useStream(["report.done", "report.failed", "approval", "collector.status"], () => {
    status.reload();
    if (running) setRunning(false);
  });

  const run = async () => {
    setRunning(true);
    setRunError(null);
    try {
      await api.post("/api/reports/run", {});
    } catch (e) {
      setRunning(false);
      setRunError((e as Error).message);
    }
  };

  if (status.error && !status.data) return <p className="error">Backend unreachable: {status.error}</p>;
  if (!status.data || !collectors.data) return <p className="muted">Loading…</p>;
  const s = status.data;
  const c = collectors.data;
  const data = (name: string): Data => (c[name]?.state === "active" && c[name].result?.ok ? c[name].result!.data : {});

  return (
    <div className="stack">
      {paused && (
        <div className="notice" role="status">
          <strong>Data paused.</strong> The collectors are stopped{auto?.since ? ` (since ${sinceText(auto.since)})` : ""}, so the numbers below are the last ones collected and are not updating, and no report is written until the automation is resumed.
        </div>
      )}
      <HealthPanel status={s} now={now} running={running || s.next_report.running} onRun={run} runError={runError} paused={paused} />
      <div className="grid-live">
        <LivePanel system={data("system")} gpu={data("gpu")} />
        <DisksPanel system={data("system")} />
      </div>
      <div className="grid-live">
        <ContainersPanel docker={data("docker")} system={data("system")} />
        <AttentionPanel systemd={data("systemd")} apt={data("apt")} images={data("image_updates")} />
      </div>
      <SourcesPanel collectors={c} now={now} paused={paused} />
    </div>
  );
}

// --------------------------------------------------------------------------- health

function HealthPanel({
  status: s,
  now,
  running,
  onRun,
  runError,
  paused,
}: {
  status: StatusResponse;
  now: number;
  running: boolean;
  onRun: () => void;
  runError: string | null;
  paused: boolean;
}) {
  const r = s.report;
  const level = healthLevel(r?.health);
  const findings = (r?.findings ?? []).filter((f) => f.severity !== "info" || (r?.findings.length ?? 0) <= 4);
  return (
    <Panel className="health">
      <div className="health-main">
        <ScoreRing score={r?.score ?? null} level={level} />
        <div className="health-text">
          <p className="health-status">
            <Dot level={level} />
            {r ? healthWord(r.health) : "No report yet"}
          </p>
          {r ? (
            <p className="health-summary">{r.summary}</p>
          ) : s.last_attempt ? (
            <p className="health-summary muted">
              Last attempt {s.last_attempt.status}: {s.last_attempt.error}{" "}
              {!s.llm_configured && <a href="#/settings/llm">Set up the LLM</a>}
            </p>
          ) : (
            <p className="health-summary muted">The first report is written shortly after start.</p>
          )}
          <p className="meta">
            {r ? (
              <>
                <a href={`#/reports/${r.id}`}>Report {ago(r.ts, now)}</a>
                <span> · next {paused ? "paused" : s.next_report.next_run ? untilShort(s.next_report.next_run, now) : "soon"}</span>
              </>
            ) : null}
            <span> · up {duration(s.uptime_s)}</span>
          </p>
        </div>
        <div className="health-actions">
          <button className="btn btn-primary" onClick={onRun} disabled={running || !s.llm_configured}>
            {running ? "Writing report…" : "Run report now"}
          </button>
          {s.pending_approvals > 0 && (
            <a className="btn" href="#/approvals">
              {s.pending_approvals} awaiting approval
            </a>
          )}
          {runError && <span className="error small">{runError}</span>}
        </div>
      </div>
      {findings.length > 0 && (
        <ul className="findings">
          {findings.slice(0, 6).map((f, i) => (
            <li key={i}>
              <Dot level={severityLevel(f.severity)} />
              <span className="finding-title">{f.title}</span>
              {f.detail && <span className="muted finding-detail">{f.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function untilShort(ts: number, now: number): string {
  const sec = Math.round(ts - now);
  if (sec <= 5) return "any moment";
  return sec < 90 ? `in ${sec}s` : `in ${Math.round(sec / 60)}m`;
}

// --------------------------------------------------------------------------- live

function LiveRow({
  label,
  values,
  max,
  now,
  sub,
  level = "off",
}: {
  label: string;
  values: (number | null)[];
  max?: number;
  now: string;
  sub?: string;
  level?: Level;
}) {
  return (
    <div className={`live-row live-${level}`}>
      <span className="live-label">{label}</span>
      <span className="live-spark">
        <Spark values={values} max={max} />
      </span>
      <span className="live-value">
        {now}
        {sub && <small>{sub}</small>}
      </span>
    </div>
  );
}

function LivePanel({ system, gpu }: { system: Data; gpu: Data }) {
  const { system: ticks, gpu: gpuTicks } = useMetricHistory();
  const cpu = system.cpu ?? {};
  const mem = system.memory ?? {};
  const last = ticks[ticks.length - 1];
  const nic = (system.network ?? []) as Data[];
  const rx = last?.net_rx_bps ?? nic.reduce((a, n) => a + (n.rx_bps ?? 0), 0);
  const tx = last?.net_tx_bps ?? nic.reduce((a, n) => a + (n.tx_bps ?? 0), 0);
  const cores = cpu.cores_logical ?? 1;
  const load = last?.load ?? cpu.load?.[0] ?? null;
  const g0 = (gpu.gpus ?? [])[0] as Data | undefined;
  const gpuSeries = gpuTicks.map((t) => t.gpus[0]);
  const lvl = (v: number | null | undefined, warn: number, crit: number): Level =>
    v === null || v === undefined ? "off" : v >= crit ? "crit" : v >= warn ? "warn" : "ok";

  if (!system.cpu) {
    return (
      <Panel title="Live">
        <Empty>The system collector has no data.</Empty>
      </Panel>
    );
  }
  return (
    <Panel title="Live" aside={<span className="muted small">last {ticks.length ? Math.round((ticks.length * 15) / 60) : 0} min</span>}>
      <div className="live">
        <LiveRow label="CPU" values={ticks.map((t) => t.cpu)} max={100} now={pct(last?.cpu ?? cpu.percent)} sub={`${cores} threads`} level={lvl(last?.cpu ?? cpu.percent, 85, 95)} />
        <LiveRow
          label="Memory"
          values={ticks.map((t) => t.mem)}
          max={100}
          now={pct(last?.mem ?? mem.percent)}
          sub={`${bytes(mem.used)} of ${bytes(mem.total)}`}
          level={lvl(last?.mem ?? mem.percent, 90, 97)}
        />
        <LiveRow
          label="Load"
          values={ticks.map((t) => t.load)}
          now={load === null ? "—" : load.toFixed(2)}
          sub={`5 min ${cpu.load?.[1]?.toFixed(2) ?? "—"}`}
          level={lvl(load === null ? null : load / cores, 1.5, 3)}
        />
        <LiveRow label="Net in" values={ticks.map((t) => t.net_rx_bps)} now={rate(rx)} />
        <LiveRow label="Net out" values={ticks.map((t) => t.net_tx_bps)} now={rate(tx)} />
        {g0 && (
          <>
            <LiveRow
              label="GPU"
              values={gpuSeries.map((g) => g?.util ?? null)}
              max={100}
              now={pct(gpuTicks.length ? gpuSeries[gpuSeries.length - 1]?.util : g0.util_percent)}
              sub={`${g0.temperature_c}°C · ${Math.round(g0.power_w)} W`}
              level={lvl(g0.temperature_c, 85, 92)}
            />
            <LiveRow
              label="VRAM"
              values={gpuSeries.map((g) => g?.vram ?? null)}
              max={100}
              now={pct(gpuTicks.length ? gpuSeries[gpuSeries.length - 1]?.vram : g0.mem_percent)}
              sub={`${bytes(g0.mem_used)} of ${bytes(g0.mem_total)}`}
              level={lvl(g0.mem_percent, 90, 97)}
            />
          </>
        )}
      </div>
    </Panel>
  );
}

// --------------------------------------------------------------------------- disks

function DisksPanel({ system }: { system: Data }) {
  const fs = ((system.filesystems ?? []) as Data[]).toSorted((a, b) => b.percent - a.percent);
  return (
    <Panel title="Disks">
      {fs.length === 0 ? (
        <Empty>No filesystems reported.</Empty>
      ) : (
        <ul className="disks">
          {fs.map((d) => (
            <li key={d.mount}>
              <span className="disk-mount mono" title={d.device}>
                {d.mount}
              </span>
              <Meter value={d.percent} />
              <span className="disk-figures">
                <b>{pct(d.percent)}</b>
                <small>{bytes(d.avail)} free</small>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// --------------------------------------------------------------------------- containers & processes

function TopList({ title, rows }: { title: string; rows: { name: string; value: string; sub?: string }[] }) {
  return (
    <div className="toplist">
      <h4>{title}</h4>
      {rows.length === 0 ? (
        <p className="muted small">Nothing busy</p>
      ) : (
        <ol>
          {rows.map((r) => (
            <li key={r.name}>
              <span className="top-name" title={r.name}>
                {r.name}
              </span>
              <span className="top-value">{r.value}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function ContainersPanel({ docker, system }: { docker: Data; system: Data }) {
  const containers = ((docker.containers ?? []) as Data[]).filter((c) => c.state === "running");
  const summary = docker.summary ?? {};
  const byCpu = containers.toSorted((a, b) => b.cpu_percent - a.cpu_percent).slice(0, 5);
  const byMem = containers.toSorted((a, b) => b.mem_usage - a.mem_usage).slice(0, 5);
  const names = Object.fromEntries(((docker.containers ?? []) as Data[]).map((c) => [c.id, c.name]));
  const procs = (system.processes?.top_cpu ?? []) as Data[];
  const procLabel = (p: Data) => (p.container_id ? `${p.name} · ${names[p.container_id] ?? p.container_id}` : p.name);
  return (
    <Panel
      title="Containers and processes"
      aside={
        docker.containers ? (
          <span className="muted small">
            {summary.running} of {summary.total} running
            {summary.unhealthy?.length ? ` · ${summary.unhealthy.length} unhealthy` : ""}
          </span>
        ) : undefined
      }
    >
      {!docker.containers && !system.processes ? (
        <Empty>No container or process data.</Empty>
      ) : (
        <div className="toplists">
          {docker.containers && (
            <>
              <TopList title="Containers by CPU" rows={byCpu.map((c) => ({ name: c.name, value: pct(c.cpu_percent, 1) }))} />
              <TopList title="Containers by memory" rows={byMem.map((c) => ({ name: c.name, value: bytes(c.mem_usage) }))} />
            </>
          )}
          <TopList
            title="Processes by CPU"
            rows={procs.slice(0, 5).map((p) => ({ name: procLabel(p), value: pct(p.cpu_percent, 1) }))}
          />
        </div>
      )}
    </Panel>
  );
}

// --------------------------------------------------------------------------- attention

function AttentionPanel({ systemd, apt, images }: { systemd: Data; apt: Data; images: Data }) {
  const failed = (systemd.failed ?? []) as Data[];
  const restarting = (systemd.restarting ?? []) as Data[];
  const imageUpdates = ((images.images ?? []) as Data[]).filter((i) => i.status === "update_available");
  const hasApt = apt.upgradable_count !== undefined;
  return (
    <Panel title="Services and updates">
      <dl className="facts">
        <dt>Failed units</dt>
        <dd>
          {!systemd.system_state ? (
            <span className="muted">no data</span>
          ) : failed.length === 0 ? (
            <span className="ok-text">none</span>
          ) : (
            <ul className="plain">
              {failed.map((u: Data) => (
                <li key={u.name}>
                  <Dot level="warn" />
                  <span className="grow">
                    <span className="mono">{u.name}</span> <span className="muted">{u.result}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
          {restarting.length > 0 && (
            <span className="muted small block">
              Restarting: {restarting.map((u: Data) => `${u.name} (${u.n_restarts})`).join(", ")}
            </span>
          )}
        </dd>
        <dt>Package updates</dt>
        <dd>
          {!hasApt ? (
            <span className="muted">no data</span>
          ) : (
            <>
              {apt.upgradable_count} pending
              {apt.security_count > 0 && <span className="warn-text"> · {apt.security_count} security</span>}
              {apt.reboot_required && <span className="warn-text"> · reboot required</span>}
            </>
          )}
        </dd>
        <dt>Container images</dt>
        <dd>
          {!images.images ? (
            <span className="muted">not checked</span>
          ) : imageUpdates.length === 0 ? (
            <span className="ok-text">all current</span>
          ) : (
            <ul className="plain">
              {imageUpdates.slice(0, 5).map((i: Data) => (
                <li key={i.image}>
                  <span className="grow">
                    <span className="mono">{i.image}</span>
                    <span className="muted"> {(i.containers ?? []).join(", ")}</span>
                  </span>
                </li>
              ))}
              {imageUpdates.length > 5 && <li className="muted">and {imageUpdates.length - 5} more</li>}
            </ul>
          )}
        </dd>
      </dl>
    </Panel>
  );
}

// --------------------------------------------------------------------------- collectors

const SOURCE_LABELS: Record<string, string> = {
  system: "System",
  docker: "Docker",
  gpu: "GPU",
  systemd: "systemd",
  journal: "Journal",
  apt: "apt",
  image_updates: "Image updates",
  prometheus: "Prometheus",
};

function SourcesPanel({ collectors, now, paused }: { collectors: Collectors; now: number; paused: boolean }) {
  const rows = Object.entries(collectors) as [string, CollectorEntry][];
  const level = (c: CollectorEntry): Level =>
    c.state === "disabled" ? "off" : c.state === "unavailable" ? "off" : paused ? "off" : c.ok === false ? "crit" : c.ok ? "ok" : "info";
  return (
    <Panel title="Data sources" aside={<a href="#/settings/collectors">Configure</a>}>
      <ul className="sources">
        {rows.map(([name, c]) => (
          <li key={name} title={c.error ?? c.reason ?? undefined}>
            <Badge level={level(c)}>{SOURCE_LABELS[name] ?? name.replace(/_/g, " ")}</Badge>
            <span className="muted small">
              {c.state !== "active" ? c.state : paused ? `paused · last ${ago(c.last_ts, now)}` : c.ok === false ? "failing" : ago(c.last_ts, now)}
            </span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
