// Compositions of kit parts that the log views share.
import type { ReactNode } from "react";
import { OwnerRef } from "../../components/EntityRefs";
import { PageHeader, Status, Tabs } from "../../kit";

/** Which calls a list shows when it was opened from a chat or a job run. */
export type CallsLink = { kind: string; id: string; turn?: string };

/** The head of the Logs page: the tabs, and the filters of the open log under them (folded on a phone). */
export function LogsHead({ tab, link, tools }: { tab: "llm" | "app"; link?: CallsLink | null; tools: ReactNode }) {
  return (
    <PageHeader
      title="Logs"
      note={
        link ? (
          <>
            Calls belonging to <OwnerRef kind={link.kind} id={link.id} /> · <a href="#/logs/llm">show all</a>
          </>
        ) : undefined
      }
      tools={tools}
      fold="filters"
    >
      <Tabs
        label="Log type"
        value={tab}
        tabs={[
          { key: "llm", label: "LLM calls", href: "#/logs/llm" },
          { key: "app", label: "App log", href: "#/logs/app" },
        ]}
      />
    </PageHeader>
  );
}

/** The state of one LLM call: OK, failed or cancelled. */
export function CallStatus({ status }: { status: string }) {
  if (status === "ok") return <Status tone="ok">OK</Status>;
  if (status === "cancelled") return <Status tone="off">cancelled</Status>;
  return <Status tone="crit">failed</Status>;
}
