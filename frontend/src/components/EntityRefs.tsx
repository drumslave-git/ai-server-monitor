// Where one thing names another, as links. The addresses are the ones the pages already have; each helper
// is the kit's EntityLink with the address and the words of that kind, so every page names the same thing
// the same way. A source that is no entity of its own (the scheduler, an action, a Telegram chat) stays text.
import type { ReactNode } from "react";
import { MODEL_WORD } from "../jobs";
import { EntityLink, EntityTail } from "../kit";

export const chatHref = (id: string) => `#/chat/${encodeURIComponent(id)}`;
export const jobHref = (slug: string) => `#/jobs/${encodeURIComponent(slug)}`;
export const runHref = (slug: string, id: number | string) => `#/jobs/${encodeURIComponent(slug)}/runs/${id}`;
export const approvalHref = (batch: number | string) => `#/approvals/${batch}`;
export const serverHref = (name: string) => `#/settings/mcp/${encodeURIComponent(name)}`;
export const callHref = (id: number | string) => `#/logs/llm/${id}`;

/** A chat session, by the start of its id. */
export function ChatRef({ id, children }: { id: string; children?: ReactNode }) {
  return (
    <EntityLink kind="chat" mono href={chatHref(id)}>
      {children ?? `chat ${id.slice(0, 8)}`}
    </EntityLink>
  );
}

/** A job or summary, by its name (or its slug when no name is at hand). */
export function JobRef({ slug, name, summary }: { slug: string; name?: string; summary?: boolean }) {
  return (
    <EntityLink kind={summary ? "summary" : "job"} href={jobHref(slug)}>
      {name ?? slug}
    </EntityLink>
  );
}

/** One run of a job. */
export function RunRef({ slug, id, children }: { slug: string; id: number; children: ReactNode }) {
  return (
    <EntityLink kind="run" href={runHref(slug, id)}>
      {children}
    </EntityLink>
  );
}

/** An approval, by its number. */
export function ApprovalRef({ batch }: { batch: number }) {
  return (
    <EntityLink kind="approval" href={approvalHref(batch)}>
      approval #{batch}
    </EntityLink>
  );
}

/** An MCP server, by its name; the link opens its card in Settings. */
export function ServerRef({ name }: { name: string }) {
  return (
    <EntityLink kind="server" mono href={serverHref(name)}>
      {name}
    </EntityLink>
  );
}

/** What an approval or an action says it came from: `chat:<id>`, `user:run-button:<id>` (a chat's run
 *  button) and `job:<slug>` open the chat or the job; anything else is shown as it is. */
export function SourceRef({ source }: { source: string }) {
  const chat = /^(?:chat|user:run-button):(.+)$/.exec(source);
  if (chat) return <ChatRef id={chat[1]} />;
  const job = /^job:(.+)$/.exec(source);
  if (job) return <JobRef slug={job[1]} />;
  return <>{source}</>;
}

/** What an action item acts on: a call of an MCP server's tool names the server as a link ("server · tool");
 *  any other item shows its target as it is. */
export function ItemTarget({ item }: { item: { target: string | null; args: Record<string, unknown> | null } }) {
  const { server, tool } = item.args ?? {};
  if (typeof server !== "string" || typeof tool !== "string") return <>{item.target}</>;
  return <EntityTail link={<ServerRef name={server} />}> · {tool}</EntityTail>;
}

/** A tool as the model names it: an MCP tool is `server__tool` and shows as "server · tool" with the server
 *  as a link; any other tool shows its name. */
export function ToolName({ name }: { name: string }) {
  const i = name.indexOf("__");
  if (i <= 0) return <>{name}</>;
  return <EntityTail link={<ServerRef name={name.slice(0, i)} />}> · {name.slice(i + 2)}</EntityTail>;
}

/** What an LLM call belongs to (its link kind, id and step): a chat or a job opens that chat or job. The kinds of
 *  removed features in old rows have nothing to open and say "Earlier feature". */
export function OwnerRef({ kind, id, step }: { kind: string; id: string | null; step?: number | null }) {
  const stepText = step ? ` · step ${step}` : "";
  if (kind === "chat" && id) return <ChatRef id={id}>{`chat ${id.slice(0, 8)}${stepText}`}</ChatRef>;
  if (kind === "job" && id)
    return (
      <EntityLink kind="job" href={jobHref(id)}>
        {`job ${id}${stepText}`}
      </EntityLink>
    );
  return <>{kind === "chat" || kind === "job" ? `${kind} ${id ?? ""}${stepText}` : "Earlier feature"}</>;
}

/** The model a job runs on ("main model", "worker model"): opens that model's settings. */
export function ModelRef({ model }: { model: "main" | "worker" }) {
  return (
    <EntityLink kind={model === "main" ? "model" : "worker"} href={`#/settings/${model === "main" ? "llm" : "worker"}`}>
      {MODEL_WORD[model]} model
    </EntityLink>
  );
}
