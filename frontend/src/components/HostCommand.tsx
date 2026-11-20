import type { ActionRecord } from "../api";
import { Code, Column, Command, ErrorText, Fold, Inline, Meta, Status, Text } from "../kit";

/** Output longer than this opens on demand. */
const LONG_LINES = 12;
const LONG_CHARS = 1200;
/** The output scrolls inside this height (px). */
const OUTPUT_HEIGHT = 360;

/** A `host_command` action (a changing shell command the model asked to run as root): the
 *  command, why, and once it ran its exit code and output. */
export function HostCommand({ item }: { item: Pick<ActionRecord, "args" | "reason" | "status" | "result"> }) {
  const args = (item.args ?? {}) as { command?: string; reason?: string };
  const reason = args.reason || item.reason;
  const result = item.result ?? {};
  const ran = item.status === "executed" || (item.status === "failed" && typeof result.exit_code === "number");
  const output = typeof result.output === "string" ? result.output : "";
  const long = output.split("\n").length > LONG_LINES || output.length > LONG_CHARS;
  const shown = <Code maxHeight={OUTPUT_HEIGHT}>{output}</Code>;
  return (
    <Column gap={2}>
      <Command wrap>{args.command ?? "(no command)"}</Command>
      {reason && <Text block>{reason}</Text>}
      {ran && (
        <>
          <Inline gap={2}>
            <Status tone={result.exit_code === 0 ? "ok" : "crit"}>exit code {String(result.exit_code ?? "?")}</Status>
            {typeof result.duration_s === "number" && <Meta>· {result.duration_s.toFixed(1)} s</Meta>}
            {result.stopped && result.stopped !== "exit" && <Meta>· {String(result.stopped)}</Meta>}
            {result.truncated && <Meta>· output shortened</Meta>}
          </Inline>
          {output &&
            (long ? (
              <Fold inline title={`output (${output.split("\n").length} lines)`}>
                {shown}
              </Fold>
            ) : (
              shown
            ))}
          {!output && (
            <Text block dim>
              No output.
            </Text>
          )}
        </>
      )}
      {item.status === "failed" && !ran && <ErrorText>{String(result.error ?? "failed")}</ErrorText>}
      {item.status === "rejected" && (
        <Text block dim>
          Denied{result.detail && !/^rejected/i.test(String(result.detail)) ? `: ${String(result.detail)}` : ""}. The command did not run.
        </Text>
      )}
      {item.status === "expired" && (
        <Text block dim>
          Expired without a decision. The command did not run.
        </Text>
      )}
    </Column>
  );
}
