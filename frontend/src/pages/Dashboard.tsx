import { useAutomation, sinceText } from "../automation";
import { api, type StatusResponse } from "../api";
import { duration } from "../fmt";
import { useStream } from "../live";
import { Badge, PageHead, Panel, usePoll } from "../ui";

export function DashboardPage() {
  const status = usePoll(() => api.get<StatusResponse>("/api/status"), 15_000);
  const { state: auto } = useAutomation();
  useStream(["approval", "action", "automation"], () => status.reload());

  if (status.error && !status.data) return <p className="error">Backend unreachable: {status.error}</p>;
  if (!status.data) return <p className="muted">Loading…</p>;
  const s = status.data;
  const paused = Boolean(auto?.paused);

  return (
    <div className="stack">
      <PageHead title="Dashboard" />
      <Panel title="Server">
        <dl>
          <dt>Version</dt>
          <dd>v{s.version}</dd>
          <dt>Uptime</dt>
          <dd>{duration(s.uptime_s)}</dd>
          <dt>Model</dt>
          <dd>
            {s.llm_configured ? (
              <>
                <span className="mono">{s.main_model}</span>
                {s.worker_model && s.worker_model !== s.main_model && (
                  <span className="muted"> · worker <span className="mono">{s.worker_model}</span></span>
                )}
              </>
            ) : (
              <>
                <Badge level="warn">Not configured</Badge> <a href="#/settings/llm">Open LLM settings</a>
              </>
            )}
          </dd>
          <dt>Automation</dt>
          <dd>
            {paused ? (
              <Badge level="warn">Paused{auto?.since ? ` since ${sinceText(auto.since)}` : ""}</Badge>
            ) : (
              <Badge level="ok">Running</Badge>
            )}
          </dd>
          <dt>Approvals</dt>
          <dd>
            {s.pending_approvals > 0 ? (
              <a href="#/approvals">{s.pending_approvals} waiting for a decision</a>
            ) : (
              <span className="muted">Nothing waiting</span>
            )}
          </dd>
        </dl>
      </Panel>
    </div>
  );
}
