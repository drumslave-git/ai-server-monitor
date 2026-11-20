import { SEVERITY_LEVEL, type Finding as RunFinding } from "../jobs";
import { Finding } from "../kit";
import { ExplainButton } from "./Explain";

const SEVERITY_ORDER = { crit: 0, warn: 1, info: 2 } as const;

/** The findings of one run, worst first. With a run id each gets its Explain button. */
export function Findings({ findings, runId, source }: { findings: RunFinding[]; runId?: number; source?: string }) {
  // (sorted for display; the Explain button names a finding by its stored index)
  const sorted = findings.map((f, index) => ({ f, index })).sort((a, b) => SEVERITY_ORDER[a.f.severity] - SEVERITY_ORDER[b.f.severity]);
  return (
    <>
      {sorted.map(({ f, index }) => (
        <Finding
          key={index}
          tone={SEVERITY_LEVEL[f.severity] as "info" | "warn" | "crit"}
          source={source}
          title={f.title}
          evidence={(f.evidence ?? []).length > 0 ? (f.evidence ?? []).join("\n") : undefined}
          action={runId !== undefined ? <ExplainButton small runId={runId} finding={index} /> : undefined}
        >
          {[f.detail, f.verified === false ? "Not confirmed by command output." : ""].filter(Boolean).join(" ") || undefined}
        </Finding>
      ))}
    </>
  );
}
