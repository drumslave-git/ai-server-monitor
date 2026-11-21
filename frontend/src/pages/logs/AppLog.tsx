import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api";
import { clock } from "../../fmt";
import { Axis, Button, Cells, Code, Column, Counter, Counters, Empty, ErrorText, Field, Inline, Input, Meta, Notice, Pane, Panel, Panes, Row, Select, Status, Switch, Table, Td, Text, Toolbar, type Cell } from "../../kit";
import { clockTime } from "./format";
import { LogsHead } from "./parts";
import type { AppLogList, AppLogRecord } from "./types";

const MAX_ROWS = 1500;
const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
/** The strip over the loaded records has at most this many cells: one per minute while the records
 *  span no more than that, otherwise each cell covers as many whole minutes as it takes. */
const STRIP_CELLS = 60;

/** error and above → crit, warning → warn, info → info, debug → off. */
const levelTone = (levelno: number) => (levelno >= 40 ? "crit" : levelno >= 30 ? "warn" : levelno >= 20 ? "info" : "off");

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
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
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

  // The records on the page belong to one query; they count as loaded only while the filters still make that query,
  // so in the very render that changes a filter the tail below does not start from the old `newest`.
  const key = query().toString();
  const loaded = loadedFor === key;

  // First page whenever a filter changes.
  useEffect(() => {
    const mine = ++generation.current;
    setSkipped(0);
    api.get<AppLogList>(`/api/logs/app?${query()}`).then(
      (d) => {
        if (mine !== generation.current) return;
        setRows(d.items);
        setNewest(d.newest);
        setMore(d.items.length === 200);
        setError(null);
        setLoadedFor(key);
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
    try {
      const d = await api.get<AppLogList>(`/api/logs/app?${query({ before: String(before) })}`);
      setRows((old) => [...old, ...d.items]);
      setMore(d.items.length === 200);
      setError(null);
    } catch (e) {
      setError((e as Error).message); // shown the way a failed first page is
    }
  };

  return (
    <>
      <LogsHead
        tab="app"
        tools={
      <Toolbar>
        <Field label="search the log" flex="3 1 260px">
          <Input type="search" placeholder="Search messages" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search the log" />
        </Field>
        <Field label="minimum level" flex="1 1 170px">
          <Select value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Minimum level">
            {LEVELS.map((l) => (<option key={l} value={l}>{l === "DEBUG" ? "all levels" : `${l.toLowerCase()} and above`}</option>))}
          </Select>
        </Field>
        <Field label="logger" flex="1 1 170px">
          <Select value={logger} onChange={(e) => setLogger(e.target.value)} aria-label="Logger">
            <option value="">all loggers</option>
            {loggers.map((l) => (<option key={l} value={l}>{l.replace(/^ai_server_monitor\./, "")}</option>))}
          </Select>
        </Field>
        <Field label="source" flex="1 1 210px">
          <Select value={source} onChange={(e) => setSource(e.target.value as "memory" | "stored")} aria-label="Source">
            <option value="memory">since the last start</option>
            <option value="stored">saved warnings and errors</option>
          </Select>
        </Field>
        {source === "memory" && (
          <Inline>
            <Switch checked={live} onChange={setLive} label="Live" text="live" />
            {live && (
              <Status tone={connected ? "ok" : "off"} hollow={!connected}>
                {connected ? "connected" : "not connected"}
              </Status>
            )}
          </Inline>
        )}
      </Toolbar>
        }
      />
      <Panes>
      <Pane bare label="App log">
      {skipped > 0 && <Notice>The live view is rate-limited: {skipped.toLocaleString()} records were left out. Reload to see them all.</Notice>}
      {error && <ErrorText>{error}</ErrorText>}
      {loaded && rows.length === 0 && (
        <Panel>
          <Empty>Nothing matches.</Empty>
        </Panel>
      )}
      {rows.length > 0 && <LoadedSummary rows={rows} debug={level === "DEBUG"} />}
      {rows.length > 0 && (
        <Panel>
          <Table dense stack stickyHead head={["time", "level", "logger", { label: "message", width: "100%" }]}>
            <tbody>
              {rows.map((r) => (<LogRow key={`${r.seq ?? "s"}-${r.id ?? ""}-${r.ts}`} rec={r} />))}
            </tbody>
          </Table>
          {more && (
            <Row>
              <Button onClick={older}>older records</Button>
            </Row>
          )}
        </Panel>
      )}
      </Pane>
      </Panes>
    </>
  );
}

const WORSE: Record<string, number> = { "": 0, info: 1, warn: 2, crit: 3 };

/** What the records on the page add up to: how many of each level, and the worst level of each
 *  stretch of time between the oldest and the newest of them. Nothing is fetched for it. */
function LoadedSummary({ rows, debug }: { rows: AppLogRecord[]; debug: boolean }) {
  const { counts, cells, per, from, to } = useMemo(() => {
    const counts = { off: 0, info: 0, warn: 0, crit: 0 };
    let first = Infinity;
    let last = -Infinity;
    for (const r of rows) {
      counts[levelTone(r.levelno)] += 1;
      first = Math.min(first, r.ts);
      last = Math.max(last, r.ts);
    }
    const start = Math.floor(first / 60);
    const minutes = Math.floor(last / 60) - start + 1;
    const per = Math.max(1, Math.ceil(minutes / STRIP_CELLS));
    const cells: Cell[] = Array.from({ length: Math.ceil(minutes / per) }, () => "");
    for (const r of rows) {
      const tone = levelTone(r.levelno);
      const i = Math.floor((Math.floor(r.ts / 60) - start) / per);
      // (a stretch with nothing above debug stays empty: a cell has no mark for "off")
      if (tone !== "off" && WORSE[tone] > WORSE[cells[i]]) cells[i] = tone;
    }
    return { counts, cells, per, from: start * 60, to: (start + (cells.length - 1) * per) * 60 };
  }, [rows]);
  const cell = per === 1 ? "one minute" : `${per} minutes`;
  return (
    <Panel title="Loaded records" meta={`${rows.length.toLocaleString()} on this page, counted by level`} pad>
      <Inline align="end" gap={5}>
        <Counters flush>
          {debug && <Counter size="sm" value={counts.off} label="debug" tone="off" />}
          <Counter size="sm" value={counts.info} label="info" tone={counts.info ? "info" : "off"} />
          <Counter size="sm" value={counts.warn} label="warn" tone={counts.warn ? "warn" : "off"} />
          <Counter size="sm" value={counts.crit} label="error" tone={counts.crit ? "crit" : "off"} />
        </Counters>
        <Column gap={2} flex="1 1 420px">
          <Cells cells={cells} label={`Worst level logged in each stretch of ${cell}, from ${clock(from)} to ${clock(to)}`} />
          <Axis labels={[clock(from), clock(to)]} />
          <Meta>
            one cell = {cell} · colour = the worst level logged in it
          </Meta>
        </Column>
      </Inline>
    </Panel>
  );
}

function LogRow({ rec }: { rec: AppLogRecord }) {
  const [open, setOpen] = useState(false);
  const message = (
    <Text kind="data" keep>
      {rec.message}
    </Text>
  );
  return (
    <tr>
      <Td nowrap>
        <Meta>{clockTime(rec.ts)}</Meta>
      </Td>
      <Td nowrap>
        <Status tone={levelTone(rec.levelno)}>{rec.level === "WARNING" ? "WARN" : rec.level}</Status>
      </Td>
      <Td nowrap>
        <Meta title={rec.logger}>{rec.logger.replace(/^ai_server_monitor\./, "")}</Meta>
      </Td>
      <Td fill>
        {rec.exc ? (
          <Column gap={2}>
            <Inline>
              {message}
              <Button variant="link" onClick={() => setOpen(!open)} aria-expanded={open}>
                {open ? "hide traceback" : "traceback"}
              </Button>
            </Inline>
            {open && (
              <Code wrap maxHeight={320}>
                {rec.exc}
              </Code>
            )}
          </Column>
        ) : (
          message
        )}
      </Td>
    </tr>
  );
}
