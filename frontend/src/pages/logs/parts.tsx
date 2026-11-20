// Compositions of kit parts that the log views share.
import { Status } from "../../kit";

/** The state of one LLM call: OK, failed or cancelled. */
export function CallStatus({ status }: { status: string }) {
  if (status === "ok") return <Status tone="ok">OK</Status>;
  if (status === "cancelled") return <Status tone="off">cancelled</Status>;
  return <Status tone="crit">failed</Status>;
}
