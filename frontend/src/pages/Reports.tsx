import { useEffect, useRef, useState } from "react";
import { api, type Report, type ReportList, type ReportSummary } from "../api";
import { ago, clock } from "../fmt";
import { useStream } from "../live";
import { Dot, Empty, healthLevel, healthWord, Markdown, PageHead, Panel, ScoreRing, severityLevel, useNow, usePoll } from "../ui";

type Live = { text: string; active: boolean; error: string | null };

export function ReportsPage({ params }: { params: string[] }) {
  const now = useNow();
  const list = usePoll(() => api.get<ReportList>("/api/reports?limit=50"), 30_000);
  const items = list.data?.items ?? [];
  // On wide screens the newest report is open by default; on phones the list comes first.
  const wide = typeof matchMedia === "function" && matchMedia("(min-width: 821px)").matches;
  const selected = params[0] ? Number(params[0]) : wide && items.length ? items[0].id : null;
  const [live, setLive] = useState<Live>({ text: "", active: false, error: null });
  const [runError, setRunError] = useState<string | null>(null);

  useStream(["report"], (event) => {
    if (event.type === "report.started") setLive({ text: "", active: true, error: null });
    else if (event.type === "report.token") setLive((l) => ({ ...l, text: l.text + event.data.text }));
    else if (event.type === "report.done") {
      setLive({ text: "", active: false, error: null });
      list.reload();
      location.hash = `#/reports/${event.data.id}`;
    } else if (event.type === "report.failed") {
      setLive({ text: "", active: false, error: event.data.error });
      list.reload();
    }
  });

  const run = async () => {
    setRunError(null);
    try {
      await api.post("/api/reports/run", {});
    } catch (e) {
      setRunError((e as Error).message);
    }
  };

  const showDetail = selected !== null && (params[0] !== undefined || wide);

  return (
    <div className={`reports ${showDetail ? "has-detail" : ""}`}>
      <PageHead title="Reports">
        {runError && <span className="error small">{runError}</span>}
        <button className="btn btn-primary" onClick={run} disabled={live.active}>
          {live.active ? "Writing…" : "Run report now"}
        </button>
      </PageHead>
      {live.error && <div className="notice">Last attempt failed: {live.error}</div>}
      {live.active && <LivePanel text={live.text} />}
      <div className="split">
        <nav className="history" aria-label="Report history">
          {list.error && !list.data && <p className="error">{list.error}</p>}
          {list.data && items.length === 0 && <Empty>No reports yet. Run one, or wait for the next scheduled report.</Empty>}
          <ul>
            {items.map((r) => (
              <HistoryRow key={r.id} report={r} now={now} selected={r.id === selected} />
            ))}
          </ul>
          {list.data && list.data.total > items.length && (
            <p className="muted small history-more">Showing the latest {items.length} of {list.data.total}.</p>
          )}
        </nav>
        <div className="detail">
          {selected === null ? (
            <Empty>{items.length ? "Select a report to read it." : ""}</Empty>
          ) : (
            <Detail id={selected} />
          )}
        </div>
      </div>
    </div>
  );
}

function HistoryRow({ report: r, now, selected }: { report: ReportSummary; now: number; selected: boolean }) {
  const level = r.status === "ok" ? healthLevel(r.health) : "off";
  return (
    <li>
      <a href={`#/reports/${r.id}`} className={`history-row${selected ? " selected" : ""}${r.status === "ok" ? "" : " problem"}`}>
        <span className="history-top">
          <Dot level={level} />
          <span className="history-time">{clock(r.ts)}</span>
          <span className="muted small">{ago(r.ts, now)}</span>
          {r.status === "ok" && r.score !== null && <span className="history-score">{r.score}</span>}
        </span>
        <span className="history-summary">
          {r.status === "ok" ? r.summary : <span className="muted">{r.status === "failed" ? "Failed" : "Skipped"}: {r.error}</span>}
        </span>
      </a>
    </li>
  );
}

function LivePanel({ text }: { text: string }) {
  const box = useRef<HTMLPreElement>(null);
  useEffect(() => {
    box.current?.scrollTo({ top: box.current.scrollHeight });
  }, [text]);
  return (
    <Panel title={<><span className="pulse-dot" aria-hidden /> Writing a report</>} className="live-report">
      {text ? <pre ref={box}>{text}</pre> : <p className="muted">Waiting for the model. Reasoning models think for a few seconds before the first words.</p>}
    </Panel>
  );
}

function Detail({ id }: { id: number }) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setReport(null);
    setError(null);
    api.get<Report>(`/api/reports/${id}`).then(setReport, (e: Error) => setError(e.message));
  }, [id]);
  if (error) return <p className="error">{error}</p>;
  if (!report) return <p className="muted">Loading…</p>;

  if (report.status !== "ok") {
    return (
      <Panel title={`${report.status === "failed" ? "Failed" : "Skipped"} ${clock(report.ts)}`}>
        <p>{report.error}</p>
        {report.status === "skipped" && <a href="#/settings/llm">Open LLM settings</a>}
      </Panel>
    );
  }
  const level = healthLevel(report.health);
  return (
    <article className="report">
      <a className="back" href="#/reports">
        All reports
      </a>
      <header className="report-head">
        <ScoreRing score={report.score ?? null} level={level} />
        <div>
          <p className="health-status">
            <Dot level={level} />
            {healthWord(report.health)}
          </p>
          <p className="health-summary">{report.summary}</p>
          <p className="meta">
            {clock(report.ts)} · {report.model}
            {report.tokens_in != null && ` · ${report.tokens_in}/${report.tokens_out} tokens`}
            {report.latency_ms != null && ` · ${(report.latency_ms / 1000).toFixed(0)}s`}
            {!report.parsed && " · header unreadable, values derived from rules"}
          </p>
        </div>
      </header>
      {report.findings.length > 0 && (
        <ul className="findings">
          {report.findings.map((f, i) => (
            <li key={i}>
              <Dot level={severityLevel(f.severity)} />
              <span className="finding-title">{f.title}</span>
              {f.detail && <span className="muted finding-detail">{f.detail}</span>}
            </li>
          ))}
        </ul>
      )}
      <Markdown text={report.markdown} />
      {report.digest && (
        <details className="digest">
          <summary>
            Data the model saw <span className="muted">({report.digest.length} characters)</span>
          </summary>
          <pre>{report.digest}</pre>
        </details>
      )}
    </article>
  );
}
