import type { ActionRecord } from "../api";
import { Code, Command, ErrorText, Fold, Meta, Status } from "../kit";

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
  // (the output scrolls inside its own height)
  const shown = (
    <div style={{ maxHeight: 360, overflow: "auto" }}>
      <Code>{output}</Code>
    </div>
  );
  return (
    <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
      <Command wrap>{args.command ?? "(no command)"}</Command>
      {reason && <p>{reason}</p>}
      {ran && (
        <>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
            <Status tone={result.exit_code === 0 ? "ok" : "crit"}>exit code {String(result.exit_code ?? "?")}</Status>
            {typeof result.duration_s === "number" && <Meta>· {result.duration_s.toFixed(1)} s</Meta>}
            {result.stopped && result.stopped !== "exit" && <Meta>· {String(result.stopped)}</Meta>}
            {result.truncated && <Meta>· output shortened</Meta>}
          </div>
          {output &&
            (long ? (
              <Fold inline title={`output (${output.split("\n").length} lines)`}>
                {shown}
              </Fold>
            ) : (
              shown
            ))}
          {!output && <p className="fd-dim">No output.</p>}
        </>
      )}
      {item.status === "failed" && !ran && <ErrorText>{String(result.error ?? "failed")}</ErrorText>}
      {item.status === "rejected" && (
        <p className="fd-dim">
          Denied{result.detail && !/^rejected/i.test(String(result.detail)) ? `: ${String(result.detail)}` : ""}. The command did not run.
        </p>
      )}
      {item.status === "expired" && <p className="fd-dim">Expired without a decision. The command did not run.</p>}
    </div>
  );
}
