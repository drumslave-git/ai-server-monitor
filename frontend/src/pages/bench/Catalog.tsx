import { useState } from "react";
import { Panel } from "../../ui";
import type { Catalog, ScenarioInfo } from "./types";

export function inputSummary(s: ScenarioInfo): string {
  if (s.input.kind === "report") return "Write the status report for the frozen server data.";
  if (s.input.kind === "chat") return (s.input.messages ?? []).map((m) => `${m.role === "user" ? "User" : "Assistant"}: ${m.content}`).join("\n");
  const jobs = s.input.catalog?.jobs ?? [];
  return `Choose Prometheus queries for ${jobs.length} job(s): ${jobs.map((j) => `${j.job} (${Object.keys(j.metrics).length} metrics)`).join(", ")}.`;
}

export function ScenarioDetail({ s }: { s: ScenarioInfo }) {
  return (
    <div className="scenario-body">
      <p>{s.description}</p>
      <h4>Input{s.tool_mode !== "auto" ? ` · tools offered as ${s.tool_mode}` : ""}</h4>
      <pre className="scenario-input">{inputSummary(s)}</pre>
      <h4>
        Expected ({s.checks.length} checks{s.judge ? ", judged" : ""})
      </h4>
      <ul className="check-list">
        {s.checks.map((c, i) => (
          <li key={i}>
            <span>{c.describe}</span>
            {c.weight !== 1 && <span className="muted small"> · weight {c.weight}</span>}
            {c.note && <span className="block muted small">{c.note}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function CatalogPanel({ catalog }: { catalog: Catalog | null }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!catalog) return null;
  return (
    <Panel title="Scenarios" aside={<span className="muted small">set {catalog.hash || "—"} · {catalog.scenarios.length} scenarios</span>}>
      {catalog.problems.length > 0 && (
        <div className="notice notice-error">
          <strong>{catalog.problems.length} scenario file(s) could not be loaded:</strong>
          <pre>{catalog.problems.join("\n")}</pre>
        </div>
      )}
      {catalog.scenarios.length === 0 && <p className="empty">No scenarios are installed.</p>}
      <p className="help">Every scenario runs against frozen data and a simulated world: no real service is contacted and nothing is ever changed. Only the language model is live.</p>
      <div className="scenario-groups">
        {catalog.groups.map((g) => {
          const items = catalog.scenarios.filter((s) => s.group === g.key);
          if (items.length === 0) return null;
          return (
            <section key={g.key} className="scenario-group">
              <h4>
                {g.title} <span className="muted">· {items.length}</span>
              </h4>
              <ul>
                {items.map((s) => (
                  <li key={s.id} className={open === s.id ? "open" : undefined}>
                    <button className="scenario-head" aria-expanded={open === s.id} onClick={() => setOpen(open === s.id ? null : s.id)}>
                      <span className="scenario-title">{s.title}</span>
                      <span className="muted small mono">{s.id}</span>
                    </button>
                    {open === s.id && <ScenarioDetail s={s} />}
                  </li>
                ))}
              </ul>
            </section>
          );
        })}
      </div>
    </Panel>
  );
}
