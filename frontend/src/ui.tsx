// Hooks and the markdown renderer shared by the pages. Everything visible comes from ./kit.
import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useState } from "react";
import { Prose, type Tone } from "./kit";

/** A status level; the kit calls it a tone. */
export type Level = Tone;

export function useNow(intervalMs = 15_000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** Fetch `load()` now and every `intervalMs`; `reload` triggers it on demand. `load` is the one of the render in which
 *  the fetch starts: when it reads something that changes (a filter), name that in `deps` to fetch again with it. */
export function usePoll<T>(load: () => Promise<T>, intervalMs: number, deps: readonly unknown[] = []) {
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
  }, [tick, intervalMs, ...deps]);
  return { data, error, reload: () => setTick((t) => t + 1) };
}

export function Markdown({ text }: { text: string }) {
  const html = DOMPurify.sanitize(marked.parse(text, { async: false, gfm: true }) as string);
  return <Prose html={html} />;
}
