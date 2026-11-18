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

export function Markdown({ text }: { text: string }) {
  const html = DOMPurify.sanitize(marked.parse(text, { async: false, gfm: true }) as string);
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}
