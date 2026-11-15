import { PageHead } from "../ui";
import { AppLogView } from "./logs/AppLog";
import { CallDetail } from "./logs/CallDetail";
import { LlmCallsList } from "./logs/LlmCalls";
import "./logs/logs.css";

/** Routes (params after `#/logs`):
 *    (none) | llm            LLM calls list
 *    llm/<id>                one call
 *    llm/for/<kind>/<id>[/<turn>]   calls belonging to a report / chat; jumps to the call when
 *                            there is one (or to the first call of a chat turn)
 *    app                     app log
 */
export function LogsPage({ params }: { params: string[] }) {
  const tab = params[0] === "app" ? "app" : "llm";
  const callId = tab === "llm" && /^\d+$/.test(params[1] ?? "") ? Number(params[1]) : null;
  const link = tab === "llm" && params[1] === "for" && params[2] && params[3] ? { kind: params[2], id: params[3], turn: params[4] } : null;

  return (
    <div className="logs">
      <PageHead title="Logs">
        <nav className="log-tabs" aria-label="Log type">
          <a href="#/logs/llm" className={tab === "llm" ? "active" : undefined} aria-current={tab === "llm" ? "page" : undefined}>LLM calls</a>
          <a href="#/logs/app" className={tab === "app" ? "active" : undefined} aria-current={tab === "app" ? "page" : undefined}>App log</a>
        </nav>
      </PageHead>
      {tab === "app" ? <AppLogView /> : callId !== null ? <CallDetail id={callId} /> : <LlmCallsList link={link} />}
    </div>
  );
}
