// Compositions of kit parts that the log views share. Placement only: the look is the kit's.
import type { ReactNode } from "react";
import { Status } from "../../kit";

/** The state of one LLM call: OK, failed or cancelled. */
export function CallStatus({ status }: { status: string }) {
  if (status === "ok") return <Status tone="ok">OK</Status>;
  if (status === "cancelled") return <Status tone="off">cancelled</Status>;
  return <Status tone="crit">failed</Status>;
}

/** A small heading over what it names. flex places it beside others in a Split. */
export function Section({ title, flex, children }: { title: ReactNode; flex?: string; children: ReactNode }) {
  return (
    <div style={{ flex, minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
      <h3 className="fd-sub">{title}</h3>
      {children}
    </div>
  );
}

/** A long quoted text scrolls inside this height instead of stretching the page. */
export function Scroll({ max, children }: { max: number; children: ReactNode }) {
  return <div style={{ minWidth: 0, maxHeight: max, overflow: "auto" }}>{children}</div>;
}
