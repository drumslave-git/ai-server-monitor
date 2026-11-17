import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { Empty } from "../../ui";
import { clockTime } from "./format";
import type { AppLogList, AppLogRecord } from "./types";

const MAX_ROWS = 1500;
const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;

export function AppLogView() {
  const [source, setSource] = useState<"memory" | "stored">("memory");
  const [level, setLevel] = useState<string>("INFO");
  const [logger, setLogger] = useState("");
  const [loggers, setLoggers] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [live, setLive] = useState(true);
  const [rows, setRows] = useState<AppLogRecord[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [newest, setNewest] = useState<number | null>(null);
  const [skipped, setSkipped] = useState(0);
  const [connected, setConnected] = useState(false);
  const generation = useRef(0);

  useEffect(() => {
    const t = setTimeout(() => setQ(text.trim()), 300);
    return () => clearTimeout(t);
  }, [text]);

  useEffect(() => {
    api.get<string[]>("/api/logs/app/loggers").then(setLoggers, () => undefined);
  }, [source]);

  const query = useCallback(
    (extra: Record<string, string> = {}) => {
      const p = new URLSearchParams({ source, limit: "200", ...extra });
      if (level !== "DEBUG") p.set("level", level);
      if (logger) p.set("logger", logger);
      if (q) p.set("q", q);
      return p;
    },
    [source, level, logger, q],
  );

  // First page whenever a filter changes.
  useEffect(() => {
    const mine = ++generation.current;
    setLoaded(false);
    setSkipped(0);
    api.get<AppLogList>(`/api/logs/app?${query()}`).then(
      (d) => {
        if (mine !== generation.current) return;
        setRows(d.items);
        setNewest(d.newest);
        setMore(d.items.length === 200);
        setError(null);
        setLoaded(true);
      },
      (e: Error) => {
        if (mine === generation.current) setError(e.message);
      },
    );
  }, [query]);

  // Live tail: only for the in-memory log.
  useEffect(() => {
    if (!live || source !== "memory" || !loaded || typeof EventSource === "undefined") {
      setConnected(false);
      return;
    }
    const p = new URLSearchParams();
    if (level !== "DEBUG") p.set("level", level);
    if (logger) p.set("logger", logger);
    if (q) p.set("q", q);
    if (newest !== null) p.set("after", String(newest));
    const es = new EventSource(`/api/logs/app/stream?${p}`);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("log", (raw) => {
      try {
        const data = JSON.parse((raw as MessageEvent<string>).data) as { records: AppLogRecord[]; skipped: number };
        setRows((old) => [...data.records.toReversed(), ...old].slice(0, MAX_ROWS));
        if (data.skipped) setSkipped((n) => n + data.skipped);
      } catch {
        /* ignore a malformed frame */
      }
    });
    return () => es.close();
    // `newest` is only the starting point of a tail; a new tail starts when the filters change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, source, loaded, level, logger, q]);

  const older = async () => {
    const last = rows[rows.length - 1];
    const before = source === "memory" ? last?.seq : last?.id;
    if (before === null || before === undefined) return;
    const d = await api.get<AppLogList>(`/api/logs/app?${query({ before: String(before) })}`);
    setRows((old) => [...old, ...d.items]);
    setMore(d.items.length === 200);
  };

  return (
    <div className="stack">
      <div className="log-filters">
        <input type="search" className="log-search" placeholder="Search messages" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search the log" />
        <select value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Minimum level">
          {LEVELS.map((l) => (<option key={l} value={l}>{l === "DEBUG" ? "All levels" : `${l[0]}${l.slice(1).toLowerCase()} and above`}</option>))}
        </select>
        <select value={logger} onChange={(e) => setLogger(e.target.value)} aria-label="Logger">
          <option value="">All loggers</option>
          {loggers.map((l) => (<option key={l} value={l}>{l.replace(/^ai_server_monitor\./, "")}</option>))}
        </select>
        <select value={source} onChange={(e) => setSource(e.target.value as "memory" | "stored")} aria-label="Source">
          <option value="memory">Since the last start</option>
          <option value="stored">Saved warnings and errors</option>
        </select>
        {source === "memory" && (
          <label className="live-toggle">
            <span className="switch">
              <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
              <span />
            </span>
            Live
            {live && <span className={`dot ${connected ? "dot-ok" : ""}`} aria-hidden />}
          </label>
        )}
      </div>
      {skipped > 0 && <p className="muted small">The live view is rate-limited: {skipped.toLocaleString()} records were left out. Reload to see them all.</p>}
      {error && <p className="error">{error}</p>}
      {loaded && rows.length === 0 && <Empty>Nothing matches.</Empty>}
      {rows.length > 0 && (
        <section className="panel log-panel">
          <ul className="applog">
            {rows.map((r) => (<LogRow key={`${r.seq ?? "s"}-${r.id ?? ""}-${r.ts}`} rec={r} />))}
          </ul>
          {more && (
            <div className="pager"><button className="btn" onClick={older}>Older records</button></div>
          )}
        </section>
      )}
    </div>
  );
}

function LogRow({ rec }: { rec: AppLogRecord }) {
  const [open, setOpen] = useState(false);
  const lvl = rec.levelno >= 40 ? "crit" : rec.levelno >= 30 ? "warn" : rec.levelno >= 20 ? "info" : "off";
  return (
    <li className={`log-row log-${lvl}`}>
      <span className="log-time">{clockTime(rec.ts)}</span>
      <span className="log-level">{rec.level === "WARNING" ? "WARN" : rec.level}</span>
      <span className="log-logger" title={rec.logger}>{rec.logger.replace(/^ai_server_monitor\./, "")}</span>
      <span className="log-msg">
        {rec.message}
        {rec.exc && (
          <>
            {" "}
            <button className="linklike" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "hide traceback" : "traceback"}</button>
          </>
        )}
        {open && rec.exc && <pre className="mono-box">{rec.exc}</pre>}
      </span>
    </li>
  );
}
