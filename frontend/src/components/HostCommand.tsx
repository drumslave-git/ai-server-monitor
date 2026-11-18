import type { ActionRecord } from "../api";
import { Badge } from "../ui";

/** Output longer than this opens on demand. */
const LONG_LINES = 12;
const LONG_CHARS = 1200;

/** A `host_command` action (a changing shell command the model asked to run as root): the
 *  command, why, and once it ran its exit code and output. */
export function HostCommand({ item }: { item: Pick<ActionRecord, "args" | "reason" | "status" | "result"> }) {
  const args = (item.args ?? {}) as { command?: string; reason?: string };
  const reason = args.reason || item.reason;
  const result = item.result ?? {};
  const ran = item.status === "executed" || (item.status === "failed" && typeof result.exit_code === "number");
  const output = typeof result.output === "string" ? result.output : "";
  const long = output.split("\n").length > LONG_LINES || output.length > LONG_CHARS;
  return (
    <div className="host-command">
      <pre className="host-command-line">{args.command ?? "(no command)"}</pre>
      {reason && <p className="muted small">{reason}</p>}
      {ran && (
        <div className="host-command-result">
          <p className="small">
            <Badge level={result.exit_code === 0 ? "ok" : "crit"}>Exit code {String(result.exit_code ?? "?")}</Badge>
            {typeof result.duration_s === "number" && <span className="muted"> · {result.duration_s.toFixed(1)} s</span>}
            {result.stopped && result.stopped !== "exit" && <span className="muted"> · {String(result.stopped)}</span>}
            {result.truncated && <span className="muted"> · output shortened</span>}
          </p>
          {output &&
            (long ? (
              <details>
                <summary className="small">Output ({output.split("\n").length} lines)</summary>
                <pre className="host-command-output">{output}</pre>
              </details>
            ) : (
              <pre className="host-command-output">{output}</pre>
            ))}
          {!output && <p className="muted small">No output.</p>}
        </div>
      )}
      {item.status === "failed" && !ran && <p className="error small">{String(result.error ?? "failed")}</p>}
      {item.status === "rejected" && (
        <p className="muted small">
          Denied{result.detail && !/^rejected/i.test(String(result.detail)) ? `: ${String(result.detail)}` : ""}. The command did not run.
        </p>
      )}
      {item.status === "expired" && <p className="muted small">Expired without a decision. The command did not run.</p>}
    </div>
  );
}
