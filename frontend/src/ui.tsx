import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useState, type ReactNode } from "react";

export type Level = "ok" | "info" | "warn" | "crit" | "off";

export function useNow(intervalMs = 15_000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** Fetch `load()` now and every `intervalMs`; `reload` triggers it on demand. */
export function usePoll<T>(load: () => Promise<T>, intervalMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    const run = () =>
      load().then(
        (d) => {
          if (alive) {
            setData(d);
            setError(null);
          }
        },
        (e: Error) => alive && setError(e.message),
      );
    void run();
    const timer = setInterval(run, intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, intervalMs]);
  return { data, error, reload: () => setTick((t) => t + 1) };
}

export function Dot({ level }: { level: Level }) {
  return <span className={`dot dot-${level}`} aria-hidden />;
}

export function Badge({ level, children }: { level: Level; children: ReactNode }) {
  return (
    <span className={`badge badge-${level}`}>
      <Dot level={level} />
      {children}
    </span>
  );
}

export const healthLevel = (h: string | null | undefined): Level =>
  h === "ok" ? "ok" : h === "warn" ? "warn" : h === "crit" ? "crit" : "off";
export const severityLevel = (s: string | null | undefined): Level =>
  s === "crit" ? "crit" : s === "warn" ? "warn" : "info";
export const healthWord = (h: string | null | undefined) =>
  h === "ok" ? "Healthy" : h === "warn" ? "Needs attention" : h === "crit" ? "Critical" : "Unknown";

export function Panel({
  title,
  aside,
  children,
  className = "",
}: {
  title?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || aside) && (
        <header className="panel-head">
          <h3>{title}</h3>
          {aside && <div className="panel-aside">{aside}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function PageHead({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="page-head">
      <h2>{title}</h2>
      <div className="page-actions">{children}</div>
    </div>
  );
}

/** Horizontal usage bar; turns amber/red past the thresholds. */
export function Meter({ value, warn = 85, crit = 95 }: { value: number; warn?: number; crit?: number }) {
  const level = value >= crit ? "crit" : value >= warn ? "warn" : "ok";
  return (
    <span className="meter" role="img" aria-label={`${value.toFixed(0)}%`}>
      <span className={`meter-fill meter-${level}`} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </span>
  );
}

/** Minimal line chart: no axes, last value marked. */
export function Spark({
  values,
  max,
  min = 0,
  width = 160,
  height = 32,
}: {
  values: (number | null)[];
  max?: number;
  min?: number;
  width?: number;
  height?: number;
}) {
  const points = values.filter((v): v is number => v !== null);
  if (points.length < 2) return <svg className="spark spark-empty" viewBox={`0 0 ${width} ${height}`} aria-hidden />;
  const top = max ?? Math.max(...points, 1);
  const low = Math.min(min, ...points);
  const span = top - low || 1;
  const inset = 3; // keeps the end marker inside the box
  const step = (width - 2 * inset) / Math.max(values.length - 1, 1);
  const coords = values
    .map((v, i) => (v === null ? null : [inset + i * step, height - 3 - ((v - low) / span) * (height - 6)]))
    .filter((p): p is number[] => p !== null);
  const path = coords.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const [lx, ly] = coords[coords.length - 1];
  return (
    <svg className="spark" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden>
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      <circle cx={lx} cy={ly} r="2.5" fill="currentColor" />
    </svg>
  );
}

export function ScoreRing({ score, level }: { score: number | null; level: Level }) {
  const r = 40;
  const c = 2 * Math.PI * r;
  const filled = score === null ? 0 : (Math.min(100, Math.max(0, score)) / 100) * c;
  return (
    <div className={`ring ring-${level}`} role="img" aria-label={score === null ? "No score" : `Score ${score} of 100`}>
      <svg viewBox="0 0 100 100" width="100%" height="100%">
        <circle cx="50" cy="50" r={r} className="ring-track" fill="none" strokeWidth="8" />
        <circle
          cx="50"
          cy="50"
          r={r}
          className="ring-value"
          fill="none"
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={`${filled} ${c}`}
          transform="rotate(-90 50 50)"
        />
      </svg>
      <span className="ring-number">{score === null ? "—" : score}</span>
    </div>
  );
}

export function Markdown({ text }: { text: string }) {
  const html = DOMPurify.sanitize(marked.parse(text, { async: false, gfm: true }) as string);
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}
