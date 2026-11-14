import { api, type CollectorEntry } from "../api";
import { ago, bytes, duration, pct } from "../fmt";
import { Badge, Dot, Empty, PageHead, Panel, severityLevel, useNow, usePoll } from "../ui";

type Data = Record<string, any>;

export function ArrPage() {
  const now = useNow();
  const arr = usePoll(() => api.get<CollectorEntry>("/api/collectors/arr"), 30_000);
  if (arr.error && !arr.data) return <p className="error">{arr.error}</p>;
  if (!arr.data) return <p className="muted">Loading…</p>;
  const c = arr.data;

  if (c.state !== "active" || !c.result) {
    return (
      <div className="stack">
        <PageHead title="*arr and downloads" />
        <Panel>
          <Empty>
            {c.state === "disabled"
              ? "The *arr collector is turned off."
              : c.reason ?? "No *arr app, Prowlarr or qBittorrent is configured yet."}{" "}
            <a href="#/settings/arr">Open *arr settings</a>
          </Empty>
        </Panel>
      </div>
    );
  }
  if (!c.result.ok) {
    return (
      <div className="stack">
        <PageHead title="*arr and downloads" />
        <Panel>
          <p className="error">{c.result.error}</p>
          <a href="#/settings/arr">Open *arr settings</a>
        </Panel>
      </div>
    );
  }

  const d = c.result.data as Data;
  const sum = (d.summary ?? {}) as Data;
  const apps = (d.apps ?? []) as Data[];
  const findings = (d.findings ?? []) as Data[];
  const qbit = d.qbittorrent as Data | null;
  const prowlarr = d.prowlarr as Data | null;

  return (
    <div className="stack">
      <PageHead title="*arr and downloads">
        <span className="muted small">checked {ago(c.last_ts, now)}</span>
      </PageHead>

      <Panel className="figures">
        <dl className="figure-row">
          <Figure label="Queued" value={sum.queue_total} sub={`${sum.queue_problems} with problems`} warn={sum.queue_problems > 0} />
          <Figure label="Missing" value={sum.wanted_missing} sub={`${sum.wanted_cutoff_unmet} below cutoff`} />
          <Figure label="Stalled torrents" value={sum.stalled_candidates} warn={sum.stalled_candidates > 0} />
          <Figure label="Failing indexers" value={sum.indexers_failing} warn={sum.indexers_failing > 0} />
          <Figure label="Unreachable" value={(sum.unreachable ?? []).length} warn={(sum.unreachable ?? []).length > 0} />
        </dl>
      </Panel>

      {findings.length > 0 && (
        <Panel title="Needs attention">
          <ul className="findings">
            {findings.map((f, i) => (
              <li key={i}>
                <Dot level={severityLevel(f.severity)} />
                <span className="finding-title">{f.source}</span>
                <span className="muted finding-detail">{f.message}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <div className="grid-2">
        {apps.map((a) => (
          <AppPanel key={`${a.type}/${a.name}`} app={a} />
        ))}
        {prowlarr && <ProwlarrPanel p={prowlarr} />}
        {qbit && <QbitPanel q={qbit} />}
      </div>
    </div>
  );
}

function Figure({ label, value, sub, warn }: { label: string; value: number | undefined; sub?: string; warn?: boolean }) {
  return (
    <div className="figure">
      <dt>{label}</dt>
      <dd className={warn ? "warn-text" : ""}>{value ?? "—"}</dd>
      {sub && <small className="muted">{sub}</small>}
    </div>
  );
}

function Health({ items }: { items: Data[] }) {
  if (!items?.length) return null;
  return (
    <ul className="plain health-list">
      {items.map((h, i) => (
        <li key={i}>
          <Dot level={h.level === "error" ? "crit" : "warn"} />
          {h.message}
        </li>
      ))}
    </ul>
  );
}

function AppPanel({ app }: { app: Data }) {
  const title = `${app.name}`;
  if (!app.ok) {
    return (
      <Panel title={title} aside={<Badge level="crit">unreachable</Badge>}>
        <p className="error small">{app.error}</p>
      </Panel>
    );
  }
  const q = app.queue ?? {};
  const rows = ((q.items ?? []) as Data[]).slice(0, 8);
  return (
    <Panel title={title} aside={<span className="muted small">{app.type} {app.version}</span>}>
      <p className="muted small">
        {q.total} in queue{q.problems ? ` · ${q.problems} with problems` : ""} · {app.wanted?.missing ?? "?"} missing · {app.wanted?.cutoff_unmet ?? "?"} below cutoff
      </p>
      <Health items={app.health} />
      {rows.length > 0 && (
        <ul className="queue">
          {rows.map((r) => (
            <li key={r.id}>
              <Dot level={r.problem ? "warn" : "off"} />
              <span className="queue-title" title={r.title}>{r.title}</span>
              <span className="muted small">{r.tracked_state ?? r.status}</span>
            </li>
          ))}
          {q.total > rows.length && <li className="muted small">and {q.total - rows.length} more</li>}
        </ul>
      )}
    </Panel>
  );
}

function ProwlarrPanel({ p }: { p: Data }) {
  if (!p.ok) {
    return (
      <Panel title={p.name} aside={<Badge level="crit">unreachable</Badge>}>
        <p className="error small">{p.error}</p>
      </Panel>
    );
  }
  return (
    <Panel title={p.name} aside={<span className="muted small">prowlarr {p.version}</span>}>
      <p className="muted small">
        {p.summary?.enabled} of {p.summary?.total} indexers enabled{p.summary?.failing ? ` · ${p.summary.failing} failing` : ""}
      </p>
      <Health items={p.health} />
      <ul className="queue">
        {((p.indexers ?? []) as Data[]).map((ix) => (
          <li key={ix.id}>
            <Dot level={ix.failing ? "crit" : ix.enabled ? "ok" : "off"} />
            <span className="queue-title">{ix.name}</span>
            <span className="muted small">{ix.failing ? "failing" : ix.enabled ? ix.protocol : "disabled"}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function QbitPanel({ q }: { q: Data }) {
  if (!q.ok) {
    return (
      <Panel title="qBittorrent" aside={<Badge level="crit">unreachable</Badge>}>
        <p className="error small">{q.error}</p>
      </Panel>
    );
  }
  const states = Object.entries((q.summary?.by_state ?? {}) as Record<string, number>).sort((a, b) => b[1] - a[1]);
  return (
    <Panel title="qBittorrent" aside={<span className="muted small">{q.version}</span>}>
      <p className="muted small">
        {q.summary?.total} torrents · {states.map(([s, n]) => `${n} ${s}`).join(", ")}
      </p>
      {q.stalled?.length > 0 && (
        <>
          <h4 className="subhead">Stalled or broken ({q.stalled_total})</h4>
          <ul className="queue">
            {(q.stalled as Data[]).slice(0, 8).map((t) => (
              <li key={t.hash}>
                <Dot level="warn" />
                <span className="queue-title" title={t.name}>{t.name}</span>
                <span className="muted small">
                  {pct((t.progress ?? 0) * 100)} · idle {duration(t.inactive_s)} · {bytes(t.size)}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}
