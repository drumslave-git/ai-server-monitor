import { PageHeader, Tabs } from "../kit";
import { AppLogView } from "./logs/AppLog";
import { CallDetail } from "./logs/CallDetail";
import { LlmCallsList } from "./logs/LlmCalls";

/** Routes (params after `#/logs`):
 *    (none) | llm            LLM calls list
 *    llm/<id>                one call
 *    llm/for/<kind>/<id>[/<turn>]   calls belonging to a chat; jumps to the call when
 *                            there is one (or to the first call of a chat turn)
 *    app                     app log
 */
export function LogsPage({ params }: { params: string[] }) {
  const tab = params[0] === "app" ? "app" : "llm";
  const callId = tab === "llm" && /^\d+$/.test(params[1] ?? "") ? Number(params[1]) : null;
  const link = tab === "llm" && params[1] === "for" && params[2] && params[3] ? { kind: params[2], id: params[3], turn: params[4] } : null;

  // One call has its own header: the way back, the call's number, its buttons.
  if (callId !== null) return <CallDetail id={callId} />;

  return (
    <>
      <PageHeader
        title="Logs"
        note={
          link ? (
            <>
              Calls belonging to {link.kind} {link.id} · <a href="#/logs/llm">show all</a>
            </>
          ) : undefined
        }
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
      {tab === "app" ? <AppLogView /> : <LlmCallsList link={link} />}
    </>
  );
}
