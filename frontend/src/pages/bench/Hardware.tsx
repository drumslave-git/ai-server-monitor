import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { Empty, Panel } from "../../ui";
import { clock, gb, gbOf, pct, providerName, watts } from "./format";
import type { AttemptRow, HwMeta, HwNumbers, HwSeries, HwStat, Run } from "./types";

/** Does this host's load stand for the model's? "elsewhere" = no, the LLM server is on another machine. */
export const hwScopeOf = (hw: HwMeta | null | undefined): "none" | "old" | "here" | "elsewhere" | "unknown" => {
  if (!hw || !hw.recorded) return "old";
  if (hw.llm_here === "elsewhere") return "elsewhere";
  return hw.llm_here === "here" ? "here" : "unknown";
};

export const hwScope = (run: Pick<Run, "meta">) => hwScopeOf(run.meta?.hardware);

/** The VRAM figure that stands for the model: its own process when the GPU lists it, else the whole GPU. */
export const vramStat = (hw: HwNumbers | undefined): HwStat => (hw ? hw.llm_vram ?? hw.vram_used : null);

/** Where the hardware numbers come from: the model's own process on this host, named after the provider
 * the run recorded (Ollama runs its models in a process called llama-server). */
export function hardwareLabel(meta: HwMeta | undefined, backendKind?: string | null): string {
  if (!meta?.recorded) return "not recorded";
  if (!meta.gpu) return "no GPU on this host";
  if (meta.llm_here === "elsewhere") return "this host; the LLM server is elsewhere";
  if (meta.llm_here === "here") {
    const provider = providerName(backendKind);
    return `${provider ? `${provider}'s model process` : "the LLM server's process"} on this host (${meta.llm_process})`;
  }
  return "this host; model process not recognised";
}

function Tile({ label, value, sub, title }: { label: string; value: string; sub?: string; title?: string }) {
  return (
    <div className="hw-tile" title={title}>
      <span className="muted small">{label}</span>
      <strong className="big">{value}</strong>
      {sub && <span className="muted small">{sub}</span>}
    </div>
  );
}

const avgPeak = (s: HwStat | undefined, fmt: (v: number) => string): { value: string; sub: string } =>
  s ? { value: fmt(s.peak), sub: `avg ${fmt(s.avg)}` } : { value: "—", sub: "" };

function Tiles({ run, hw }: { run: Run; hw: NonNullable<HwNumbers> }) {
  const meta = run.meta?.hardware;
  const loaded = run.meta?.loaded_model;
  const own = hw.llm_vram;
  const vram = avgPeak(hw.vram_used, (v) => gbOf(v, hw.vram_total));
  const mine = avgPeak(own, gb);
  const util = avgPeak(hw.gpu_util, pct);
  const power = avgPeak(hw.power_w, watts);
  const cpu = avgPeak(hw.cpu, pct);
  const ram = avgPeak(hw.ram_used, (v) => gbOf(v, hw.ram_total));
  const elsewhere = meta?.llm_here === "elsewhere";
  const host = elsewhere ? "Host " : "";
  // the same wording as the Compare page's "Measured from" row
  const where = hardwareLabel(meta, run.backend_kind);
  return (
    <div className="hw-tiles">
      {meta?.gpu ? (
        <>
          <Tile label={`${host}VRAM, peak`} value={vram.value} sub={vram.sub ? `${vram.sub} · whole GPU` : undefined} title="Memory in use on the whole GPU, all programs together" />
          {own ? (
            <Tile label="Model's VRAM, peak" value={mine.value} sub={`${mine.sub} · ${where}`} title={`Memory held by ${where}`} />
          ) : (
            <Tile label="Model's VRAM" value="—" sub={elsewhere ? "server is elsewhere" : "process not recognised"} title="The GPU did not list an LLM server process, so only the whole-GPU figure is known" />
          )}
          <Tile label={`${host}GPU busy, peak`} value={util.value} sub={util.sub} />
          <Tile label={`${host}GPU power, peak`} value={power.value} sub={[power.sub, hw.temp_c ? `up to ${Math.round(hw.temp_c.peak)} °C` : ""].filter(Boolean).join(" · ")} />
        </>
      ) : (
        <div className="hw-nogpu muted">No GPU on this host: no VRAM, GPU or power figures.</div>
      )}
      <Tile label={`${host}CPU, peak`} value={cpu.value} sub={cpu.sub} title="Share of all CPU cores in use on this host" />
      <Tile label={`${host}RAM, peak`} value={ram.value} sub={ram.sub} title="Memory in use on this host (total minus available)" />
      {loaded && (
        <Tile
          label="Model on GPU"
          value={loaded.gpu_percent === null ? "—" : `${loaded.gpu_percent}%`}
          sub={[loaded.gpu_percent === null ? null : loaded.gpu_percent < 100 ? `${100 - loaded.gpu_percent}% on CPU` : "fully on GPU", loaded.size ? `${gb(loaded.size)} loaded` : null, loaded.context_length ? `${Math.round(loaded.context_length / 1024)}k context` : null].filter(Boolean).join(" · ")}
          title={`From the backend itself (${loaded.source}) after the model was loaded`}
        />
      )}
    </div>
  );
}

export function HardwareSection({ run }: { run: Run }) {
  const scope = hwScope(run);
  const meta = run.meta?.hardware;
  const hw = run.summary?.hardware ?? null;
  const [series, setSeries] = useState<HwSeries | null>(null);
  useEffect(() => {
    if (scope === "old") return;
    let alive = true;
    api.get<HwSeries>(`/api/bench/runs/${run.id}/hardware`).then((d) => alive && setSeries(d), () => undefined);
    return () => {
      alive = false;
    };
  }, [run.id, scope]);

  if (scope === "old") {
    return (
      <Panel title="Hardware">
        <Empty>
          Hardware was not recorded for this run.
          {meta && !meta.recorded && <span className="help block">Recording was switched off in Settings → Benchmarks when it ran.</span>}
        </Empty>
      </Panel>
    );
  }
  const baseline = meta?.baseline;
  return (
    <Panel title="Hardware" className="hw-panel" aside={<span className="chip">{scope === "elsewhere" ? "this host · LLM elsewhere" : "this host"}</span>}>
      {scope === "elsewhere" && (
        <div className="notice hw-notice">
          <strong>These are this host's numbers, not the model's.</strong> The LLM server is elsewhere: its endpoint is not a local address and no LLM process showed up on this host's GPU. Host load while the run went is shown for reference only.
          {run.meta?.loaded_model && <span className="block">"Model on GPU" is what the backend itself reports.</span>}
        </div>
      )}
      {!hw ? <Empty>No hardware samples fall inside the attempts of this run.</Empty> : <Tiles run={run} hw={hw} />}
      {scope === "unknown" && meta?.gpu && <p className="help">The endpoint is this machine, but the GPU listed no process recognised as an LLM server, so the VRAM figures cover the whole GPU, including other programs.</p>}
      {scope === "here" && <p className="help">Peak and average over the attempts (the warm-up request and the judge are left out). The whole-GPU figure includes other programs; the model's own VRAM is what its process holds.</p>}
      <p className="help">
        {meta?.gpu_name ? `${meta.gpu_name}${meta.gpu_count && meta.gpu_count > 1 ? ` ×${meta.gpu_count}` : ""} · ` : ""}
        {meta?.samples ?? 0} samples, about every {meta?.interval_s ?? "?"} s
        {baseline?.vram_used ? ` · ${gb(baseline.vram_used)} of VRAM was already in use before the model loaded` : ""}
      </p>
      {series && series.count > 1 && <HwChart run={run} series={series} />}
    </Panel>
  );
}

// ---- chart

const COLORS = { gpu: "var(--hw-gpu)", vram: "var(--hw-vram)", cpu: "var(--hw-cpu)", ram: "var(--hw-ram)" } as const;
type Key = keyof typeof COLORS;

function percentSeries(s: HwSeries, key: Key): (number | null)[] {
  const p = s.points;
  const ratio = (used: (number | null)[], total: (number | null)[]) => used.map((u, i) => (u === null || !total[i] ? null : Math.min(100, (u / (total[i] as number)) * 100)));
  if (key === "gpu") return p.gpu_util;
  if (key === "cpu") return p.cpu;
  return key === "vram" ? ratio(p.vram_used, p.vram_total) : ratio(p.ram_used, p.ram_total);
}

const LABEL: Record<Key, string> = { gpu: "GPU busy", vram: "VRAM", cpu: "CPU", ram: "RAM" };

function HwChart({ run, series }: { run: Run; series: HwSeries }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(240, Math.floor(el.clientWidth))));
    ro.observe(el);
    setWidth(Math.max(240, Math.floor(el.clientWidth)));
    return () => ro.disconnect();
  }, []);

  const ts = series.points.ts as number[];
  const keys = (["gpu", "vram", "cpu", "ram"] as Key[]).filter((k) => percentSeries(series, k).some((v) => v !== null));
  const t0 = ts[0];
  const span = Math.max(ts[ts.length - 1] - t0, 1);
  const H = 190;
  const m = { l: 34, r: 8, t: 8, b: 22 };
  const w = width - m.l - m.r;
  const h = H - m.t - m.b;
  const x = (t: number) => m.l + ((t - t0) / span) * w;
  const y = (v: number) => m.t + (1 - v / 100) * h;
  const attempts = [...(run.attempts ?? [])].filter((a) => a.started >= t0 - 1 && a.started <= t0 + span + 1).sort((a, b) => a.started - b.started);

  const path = (vals: (number | null)[]) => {
    let d = "";
    let open = false;
    vals.forEach((v, i) => {
      if (v === null) {
        open = false;
        return;
      }
      d += `${open ? "L" : "M"}${x(ts[i]).toFixed(1)} ${y(v).toFixed(1)}`;
      open = true;
    });
    return d;
  };

  const tickCount = Math.max(2, Math.min(6, Math.floor(w / 90)));
  const ticks = Array.from({ length: tickCount }, (_, i) => (span * i) / (tickCount - 1));
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const t = t0 + ((e.clientX - rect.left - m.l) / w) * span;
    let best = 0;
    ts.forEach((v, i) => {
      if (Math.abs(v - t) < Math.abs(ts[best] - t)) best = i;
    });
    setHover(best);
  };
  const current = (i: number): AttemptRow | undefined => attempts.findLast((a) => a.started <= ts[i]);
  const hv = hover === null ? null : hover;
  const at = hv === null ? undefined : current(hv);
  const p = series.points;

  return (
    <div className="hw-chart" ref={box}>
      <h4>Over the run</h4>
      <svg width={width} height={H} role="img" aria-label="Hardware load over the run: GPU busy, VRAM, CPU and RAM in percent" onPointerMove={onMove} onPointerDown={onMove} onPointerLeave={() => setHover(null)}>
        {[0, 50, 100].map((v) => (
          <g key={v}>
            <line x1={m.l} x2={width - m.r} y1={y(v)} y2={y(v)} className="hw-grid" />
            <text x={m.l - 6} y={y(v) + 4} textAnchor="end" className="hw-axis">{v}%</text>
          </g>
        ))}
        {ticks.map((t, i) => (
          <text key={i} x={x(t0 + t)} y={H - 6} textAnchor={i === 0 ? "start" : i === ticks.length - 1 ? "end" : "middle"} className="hw-axis">{clock(t)}</text>
        ))}
        {attempts.map((a) => (
          <line key={a.id} x1={x(a.started)} x2={x(a.started)} y1={m.t} y2={m.t + h} className="hw-attempt" />
        ))}
        {keys.map((k) => (
          <path key={k} d={path(percentSeries(series, k))} fill="none" stroke={COLORS[k]} strokeWidth="1.6" strokeLinejoin="round" />
        ))}
        {hv !== null && <line x1={x(ts[hv])} x2={x(ts[hv])} y1={m.t} y2={m.t + h} className="hw-cursor" />}
        {hv !== null && keys.map((k) => {
          const v = percentSeries(series, k)[hv];
          return v === null ? null : <circle key={k} cx={x(ts[hv])} cy={y(v)} r="3" fill={COLORS[k]} />;
        })}
      </svg>
      <ul className="hw-legend">
        {keys.map((k) => (
          <li key={k}><span className="hw-swatch" style={{ background: COLORS[k] }} aria-hidden />{LABEL[k]}</li>
        ))}
        <li className="muted small"><span className="hw-dash" aria-hidden />an attempt starts</li>
      </ul>
      <p className="hw-readout small" aria-live="polite">
        {hv === null ? (
          <span className="muted">Hover or touch the chart to read the values at that moment.</span>
        ) : (
          <>
            <strong>{clock(ts[hv] - t0)}</strong>
            {at ? <span className="muted"> · {at.scenario_id} #{at.attempt}</span> : <span className="muted"> · before the first attempt</span>}
            {" — "}
            {p.gpu_util[hv] !== null && <>GPU {pct(p.gpu_util[hv])} · </>}
            {p.vram_used[hv] !== null && <>VRAM {gbOf(p.vram_used[hv], p.vram_total[hv])}{p.llm_vram[hv] ? ` (model ${gb(p.llm_vram[hv])})` : ""} · </>}
            {p.power_w[hv] !== null && <>{watts(p.power_w[hv])} · </>}
            CPU {pct(p.cpu[hv])} · RAM {gbOf(p.ram_used[hv], p.ram_total[hv])}
          </>
        )}
      </p>
    </div>
  );
}
