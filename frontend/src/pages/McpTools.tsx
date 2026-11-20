import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api";
import type { components } from "../api-types";
import { ago } from "../fmt";
import { errorText } from "../jobs";
import {
  Actions,
  Button,
  Cells,
  Checkbox,
  Clamp,
  Column,
  Counter,
  Counters,
  Dialog,
  Empty,
  ErrorText,
  Field,
  IconButton,
  Inline,
  Input,
  Legend,
  List,
  ListItem,
  Meta,
  Notice,
  PageHeader,
  Pane,
  Panes,
  Pad,
  Panel,
  Select,
  Stack,
  Status,
  Switch,
  Table,
  Tag,
  Td,
  Text,
  Toolbar,
  Tr,
  type Cell,
} from "../kit";

/* All tools of the configured MCP servers, organised by domain (the area a person asks about).
   The domains are listed on the left ("Not sorted" on top when a sorting failed); the open ones
   show their tools on the right; on/off goes to the server's tool_filter (the same value as the
   Settings table). Domains start open; the ones the owner closed are remembered in this browser
   by domain name (a new or renamed domain starts open). While a search or filter is on, every
   domain with matches is open and what is remembered stays as it was. */

type Page = components["schemas"]["McpToolsPage"];
type Tool = components["schemas"]["McpTool"];
type Domain = components["schemas"]["McpDomain"];
type Kind = "all" | "read" | "write";
type OnOff = "all" | "on" | "off";
type Act = (p: Promise<unknown>) => Promise<unknown>;
/** One line of the list: a domain, or the tools without one (`domain` undefined). */
type Entry = { key: string; name: string; domain?: Domain; rows: Tool[]; all: Tool[] };

const NEW_DOMAIN = "__new__";
/** The "Not sorted" group's key among the closed names (no domain can be called this). */
const UNSORTED = "(not sorted)";
const CLOSED_KEY = "asm.mcp-tools.collapsed";

function readClosed(): Set<string> {
  try {
    const stored = JSON.parse(localStorage.getItem(CLOSED_KEY) ?? "[]");
    return new Set(Array.isArray(stored) ? stored.filter((n) => typeof n === "string") : []);
  } catch {
    return new Set();
  }
}

function writeClosed(names: Set<string>) {
  try {
    localStorage.setItem(CLOSED_KEY, JSON.stringify([...names]));
  } catch {
    /* no storage (private window, blocked): the state lasts until the page is left */
  }
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function McpToolsPage() {
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [server, setServer] = useState("all");
  const [kind, setKind] = useState<Kind>("all");
  const [onOff, setOnOff] = useState<OnOff>("all");
  const [closed, setClosedState] = useState<Set<string>>(readClosed);
  const setClosed = (next: Set<string>) => { setClosedState(next); writeClosed(next); };
  const [sortOpen, setSortOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(
    () => api.get<Page>("/api/mcp/tools").then((p) => { setPage(p); setError(null); }, (e) => setError(errorText(e))),
    [],
  );
  useEffect(() => { void load(); }, [load]);
  // While a sorting runs (also one started by a Settings save), look again every 2 s.
  useEffect(() => {
    if (!page?.sorting) return;
    const timer = setInterval(() => void load(), 2000);
    return () => clearInterval(timer);
  }, [page?.sorting, load]);

  /** One request for several tools (the same tool_filter as the single switch). */
  const switchAll = (list: Tool[], on: boolean) =>
    void act(api.post("/api/mcp/tools/switch", { tools: list.map((t) => ({ server: t.server, tool: t.tool })), enabled: on })).catch(() => undefined);
  const act: Act = (p) => {
    const done = p.then(load);
    done.catch((e) => setError(errorText(e)));
    return done;
  };

  const filtering = query.trim() !== "" || server !== "all" || kind !== "all" || onOff !== "all";
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (page?.tools ?? []).filter(
      (t) =>
        (server === "all" || t.server === server) &&
        (kind === "all" || t.kind === kind) &&
        (onOff === "all" || (onOff === "on") === t.enabled) &&
        (!q || t.tool.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)),
    );
  }, [page, query, server, kind, onOff]);

  if (!page) {
    return (
      <>
        <PageHeader title="MCP tools" />
        {error ? <ErrorText>{error}</ErrorText> : <Meta>Loading…</Meta>}
      </>
    );
  }

  const tools = page.tools;
  const servers = page.servers;
  const unsorted = shown.filter((t) => t.domain_id == null);
  const on = tools.filter((t) => t.enabled).length;
  const writes = tools.filter((t) => t.kind === "write").length;
  const writesOn = tools.filter((t) => t.enabled && t.kind === "write").length;
  const nServers = new Set(tools.map((t) => t.server)).size;
  const down = servers.filter((s) => s.error);
  const allNames = [UNSORTED, ...page.domains.map((d) => d.name)];
  const anyClosed = allNames.some((n) => closed.has(n));
  /** Whether a domain is open: open with matches under a filter, else what the owner left it at. */
  const isOpen = (name: string) => filtering || !closed.has(name);
  /** The owner opened or closed a domain: remembered, except under a filter (forced open). */
  const toggled = (name: string, open: boolean) => {
    if (!filtering && open === closed.has(name)) setClosed(toggle(closed, name, open));
  };
  const toOn = shown.filter((t) => !t.enabled);
  const toOff = shown.filter((t) => t.enabled);

  // The list: the tools without a domain first, then every domain (under a filter, those with matches).
  const entries: Entry[] = [];
  if (unsorted.length > 0) entries.push({ key: "unsorted", name: UNSORTED, rows: unsorted, all: tools.filter((t) => t.domain_id == null) });
  for (const d of page.domains) {
    const rows = shown.filter((t) => t.domain_id === d.id);
    if (filtering && rows.length === 0) continue;
    entries.push({ key: String(d.id), name: d.name, domain: d, rows, all: tools.filter((t) => t.domain_id === d.id) });
  }
  const open = entries.filter((e) => isOpen(e.name));

  /** One cell per tool: read and on, then write and on, then off. */
  const cells: Cell[] = [...Array<Cell>(on - writesOn).fill("ok"), ...Array<Cell>(writesOn).fill("warn"), ...Array<Cell>(tools.length - on).fill("")];

  const filters = (
    <Toolbar
      side={
        <Field label="search">
          <Input type="search" placeholder="Search tools and descriptions" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search tools and descriptions" />
        </Field>
      }
    >
      <Field label="server" flex="1 1 150px">
        <Select value={server} onChange={(e) => setServer(e.target.value)} aria-label="Server">
          <option value="all">all servers</option>
          {servers.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
        </Select>
      </Field>
      <Field label="kind" flex="1 1 150px">
        <Select value={kind} onChange={(e) => setKind(e.target.value as Kind)} aria-label="Read or write">
          <option value="all">read and write</option>
          <option value="read">read</option>
          <option value="write">write</option>
        </Select>
      </Field>
      <Field label="state" flex="1 1 150px">
        <Select value={onOff} onChange={(e) => setOnOff(e.target.value as OnOff)} aria-label="On or off">
          <option value="all">on and off</option>
          <option value="on">on</option>
          <option value="off">off</option>
        </Select>
      </Field>
      <Actions toEnd>
        <IconButton
          icon={anyClosed ? "expandAll" : "collapseAll"}
          label={anyClosed ? "expand all" : "collapse all"}
          hint={filtering ? "While searching or filtering, every domain with matches is open" : undefined}
          disabled={filtering}
          onClick={() => setClosed(anyClosed ? new Set() : new Set(allNames))}
        />
        <IconButton
          icon="selectAll"
          label={filtering ? "select shown" : "select all"}
          count={toOn.length}
          hint={filtering ? "Switches on every tool the search and filters show" : "Switches on every tool"}
          disabled={toOn.length === 0}
          onClick={() => switchAll(toOn, true)}
        />
        <IconButton
          icon="deselectAll"
          label={filtering ? "deselect shown" : "deselect all"}
          count={toOff.length}
          hint={filtering ? "Switches off every tool the search and filters show" : "Switches off every tool"}
          disabled={toOff.length === 0}
          onClick={() => switchAll(toOff, false)}
        />
      </Actions>
    </Toolbar>
  );

  return (
    <>
      <PageHeader title="MCP tools" tools={servers.length > 0 ? filters : undefined} fold="filters">
        <Button variant="primary" icon="sort" onClick={() => setSortOpen(true)} disabled={page.sorting || servers.length === 0}>
          sort into domains…
        </Button>
      </PageHeader>
      {servers.length === 0 ? (
        <Notice title="No MCP server is configured.">Add one under Settings → MCP servers.</Notice>
      ) : (
        <Panes>
          {entries.length > 0 && (
            <Pane side label="Domains">
              <List label="Domains">
                {entries.map((e) => (
                  <ListItem
                    key={e.key}
                    mono
                    expanded={isOpen(e.name)}
                    onClick={() => toggled(e.name, !isOpen(e.name))}
                    title={e.domain ? e.name : "not sorted"}
                    trailing={e.domain ? counts(e) : <Meta tone="warn">{counts(e)}</Meta>}
                  />
                ))}
              </List>
            </Pane>
          )}
          <Pane bare label="Tools">
            <Panel pad>
              <Inline align="end" gap={4}>
                <Counters flush flex="0 1 720px">
                  <Counter size="sm" value={tools.length} label={`tool${tools.length === 1 ? "" : "s"} on ${plural(nServers, "server")}`} />
                  <Counter size="sm" value={page.domains.length} label={`domain${page.domains.length === 1 ? "" : "s"}`} />
                  <Counter size="sm" value={on} label="on" tone={on ? "ok" : "off"} />
                  <Counter size="sm" value={writes} label="write" tone={writes ? "warn" : "off"} />
                </Counters>
                <Column gap={2} flex="1 1 380px">
                  <Cells cells={cells} label={`${plural(tools.length, "tool")}: ${on - writesOn} read and on, ${writesOn} write and on, ${tools.length - on} off`} />
                  <Legend>
                    <span>one cell = one tool</span>
                    <Status tone="ok" plain>read, on</Status>
                    <Status tone="warn" plain>write, on</Status>
                    <Status tone="off" hollow plain>off</Status>
                  </Legend>
                </Column>
              </Inline>
            </Panel>
            {page.sorting && (
              <Notice title={<Status tone="info" blink>sorting tools into domains…</Status>}>
                The page updates when it is done; until then some tools may still be without a domain.
              </Notice>
            )}
            {down.map((s) => (
              <Notice key={s.name} tone="warn">
                <Text tone="warn">
                  Server <strong>{s.name}</strong> could not be listed{s.listed_ts ? ` ${ago(s.listed_ts)}` : ""}: {s.error}. Its tools are shown as last listed.
                </Text>
              </Notice>
            ))}
            {notice && <Notice>{notice}</Notice>}
            {error && <ErrorText>{error}</ErrorText>}
            {filtering && shown.length === 0 && (
              <Panel>
                <Empty>No tool matches.</Empty>
              </Panel>
            )}
            {open.map((e) =>
              e.domain && e.all.length === 0 ? (
                <EmptyDomain key={e.key} domain={e.domain} act={act} />
              ) : (
                <Section
                  key={e.key}
                  title={e.domain ? e.name : "Not sorted"}
                  description={e.domain ? e.domain.description : "These tools have no domain: they are not offered to the chat or to jobs."}
                  count={e.rows}
                  total={e.all}
                  onSwitch={(to) => switchAll(e.rows, to)}
                  domain={e.domain}
                  act={act}
                >
                  {e.rows.map((t) => <ToolRow key={t.name} tool={t} domains={page.domains} servers={servers} act={act} reason={!e.domain} />)}
                </Section>
              ),
            )}
          </Pane>
        </Panes>
      )}
      {sortOpen && (
        <SortDialog
          page={page}
          onClose={() => setSortOpen(false)}
          onStart={() => { setNotice(null); setPage({ ...page, sorting: true }); }}
          onDone={(text) => { setNotice(text); void load(); }}
        />
      )}
    </>
  );
}

function toggle(set: Set<string>, key: string, open: boolean): Set<string> {
  const next = new Set(set);
  if (open) next.delete(key);
  else next.add(key);
  return next;
}

/** A domain's line in the list: how many of its tools are on, and how many a filter shows. */
function counts(e: Entry): string {
  if (e.all.length === 0) return "no tools";
  const on = `${e.all.filter((t) => t.enabled).length} of ${e.all.length} on`;
  return e.rows.length !== e.all.length ? `${e.rows.length} shown · ${on}` : on;
}

/** An open domain (or the tools without one): its counts, its bulk buttons and its tools. */
function Section({
  title, description, count, total, onSwitch, domain, act, children,
}: {
  title: string; description: string; count: Tool[]; total: Tool[];
  onSwitch: (on: boolean) => void; domain?: Domain; act: Act; children: ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const on = total.filter((t) => t.enabled).length;
  const writes = total.filter((t) => t.kind === "write").length;
  // Under a search or filter the buttons act on the tools shown; the "on" count is the domain's.
  const part = count.length !== total.length;
  return (
    <Panel
      title={title}
      meta={
        <>
          <Meta tone={on ? "ok" : "off"}>
            {part ? `${count.length} shown · ${on} of ${total.length} on in this ${domain ? "domain" : "group"}` : `${on} of ${total.length} on`}
          </Meta>
          {" · "}
          {total.length - writes} read · {writes} write
          {domain && domain.used_by.length > 0 && <span title={domain.used_by.join(", ")}> · {plural(domain.used_by.length, "job")}</span>}
        </>
      }
      aside={
        <>
          <IconButton small icon="selectAll" label={part ? "select shown" : "select all"} disabled={count.every((t) => t.enabled)} onClick={() => onSwitch(true)} />
          <IconButton small icon="deselectAll" label={part ? "deselect shown" : "deselect all"} disabled={!count.some((t) => t.enabled)} onClick={() => onSwitch(false)} />
          {domain && <IconButton small icon="rename" label={`Rename or describe ${domain.name}`} onClick={() => setEditing(true)} />}
        </>
      }
    >
      {description && (
        <Pad>
          <Text block dim={Boolean(domain)} tone={domain ? undefined : "warn"}>
            {description}
          </Text>
        </Pad>
      )}
      <Table stack stickyHead head={["on", "tool", { label: "what it does", minWidth: "260px" }, "domain"]}>
        <tbody>{children}</tbody>
      </Table>
      {editing && domain && <DomainEdit domain={domain} act={act} onClose={() => setEditing(false)} />}
    </Panel>
  );
}

function EmptyDomain({ domain, act }: { domain: Domain; act: Act }) {
  const [editing, setEditing] = useState(false);
  return (
    <Panel
      title={domain.name}
      meta={<>no tools{domain.used_by.length > 0 && ` · used by ${domain.used_by.join(", ")}`}</>}
      aside={
        <IconButton small icon="rename" label={`Rename or describe ${domain.name}`} onClick={() => setEditing(true)} />
      }
    >
      {domain.description && (
        <Pad>
          <Text block dim>
            {domain.description}
          </Text>
        </Pad>
      )}
      {editing && <DomainEdit domain={domain} act={act} onClose={() => setEditing(false)} />}
    </Panel>
  );
}

const NAME_MAX = 24;
const NAME_OK = /^[a-z0-9][a-z0-9-]*$/;

/** The app's dialog for a domain: a new one (`domain` undefined; `onCreated` gets it) or
 *  renaming / describing / deleting one. The name rule is shown and checked while typing. */
function DomainEdit({
  domain, act, onClose, onCreated,
}: {
  domain?: Domain; act: Act; onClose: () => void; onCreated?: (d: Domain) => Promise<unknown>;
}) {
  const [name, setName] = useState(domain?.name ?? "");
  const [description, setDescription] = useState(domain?.description ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const trimmed = name.trim();
  const problem = !trimmed ? null : !NAME_OK.test(trimmed) ? "Only lowercase letters, digits and hyphens; start with a letter or digit" : null;
  const valid = trimmed !== "" && problem === null && (domain !== undefined || description.trim() !== "");
  const length = `${trimmed.length} of ${NAME_MAX} characters`;
  const run = (p: Promise<unknown>) => {
    setBusy(true);
    setError(null);
    act(p).then(onClose, (e) => { setBusy(false); setError(errorText(e)); });
  };
  const save = () =>
    run(
      domain
        ? api.put(`/api/mcp/domains/${domain.id}`, { name: trimmed, description })
        : api.post<Domain>("/api/mcp/domains", { name: trimmed, description }).then((d) => onCreated?.(d)),
    );
  const remove = () => {
    if (!domain || !confirm(`Delete the domain "${domain.name}"?${domain.tools ? ` Its ${domain.tools} tools are sorted again at the next listing.` : ""}`)) return;
    run(api.del(`/api/mcp/domains/${domain.id}`));
  };
  return (
    <Dialog title={domain ? `Domain ${domain.name}` : "New domain"} onClose={onClose}>
        <Field
          label="name"
          help={problem ? undefined : `Lowercase letters, digits and hyphens · ${length}`}
          error={problem ? `${problem} · ${length}` : undefined}
        >
          <Input value={name} maxLength={NAME_MAX} onChange={(e) => setName(e.target.value)} invalid={problem !== null} autoFocus />
        </Field>
        <Field label="description" help={domain ? undefined : "One line; the chat's model reads it to pick the domain."}>
          <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What a person can ask about there" />
        </Field>
        {error && <ErrorText>{error}</ErrorText>}
        <Actions>
          <Button variant="primary" onClick={save} disabled={!valid || busy}>
            {domain ? "save" : "create and move"}
          </Button>
          <Button onClick={onClose}>cancel</Button>
          {domain && (
            <Button variant="danger" toEnd onClick={remove} disabled={busy || domain.used_by.length > 0} title={domain.used_by.length ? `Used by ${domain.used_by.join(", ")}` : undefined}>
              delete domain
            </Button>
          )}
        </Actions>
    </Dialog>
  );
}

function ToolRow({
  tool, domains, servers, act, reason,
}: {
  tool: Tool; domains: Domain[]; servers: Page["servers"]; act: Act; reason?: boolean;
}) {
  const state = servers.find((s) => s.name === tool.server);
  const path = `/api/mcp/tools/${encodeURIComponent(tool.server)}/${encodeURIComponent(tool.tool)}`;
  const [creating, setCreating] = useState(false);
  const move = (value: string) => {
    if (value === NEW_DOMAIN) setCreating(true);
    else if (value) void act(api.patch(path, { domain_id: Number(value) })).catch(() => undefined);
  };
  return (
    <Tr off={!tool.enabled}>
      <Td>
        <Switch checked={tool.enabled} onChange={(to) => void act(api.patch(path, { enabled: to }))} label={`${tool.name} on`} title={tool.enabled ? "Turn off" : "Turn on"} />
      </Td>
      <Td>
        <Stack gap={1}>
          <Text kind="label" nowrap>
            {tool.tool}
          </Text>
          <Inline nowrap gap={2}>
            <Tag tone={tool.kind === "write" ? "warn" : undefined}>{tool.kind}</Tag>
            <Tag>{tool.server}</Tag>
            {state && !state.enabled && <Status tone="off" hollow>server off</Status>}
            {state?.error && <Status tone="crit">unreachable</Status>}
          </Inline>
        </Stack>
      </Td>
      <Td>
        <Clamp>{tool.description}</Clamp>
        {reason && (
          <Text block wrap tone="warn">
            {tool.sort_error ?? "Not sorted yet: it is sorted at the next listing."}
          </Text>
        )}
      </Td>
      <Td>
        <Select width="190px" value="" onChange={(e) => move(e.target.value)} aria-label={`Move ${tool.tool} to another domain`}>
          <option value="">move to…</option>
          {domains.filter((d) => d.id !== tool.domain_id).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          <option value={NEW_DOMAIN}>new domain…</option>
        </Select>
        {creating && (
          <DomainEdit act={act} onClose={() => setCreating(false)} onCreated={(d) => api.patch(path, { domain_id: d.id })} />
        )}
      </Td>
    </Tr>
  );
}

type SortResult = { sorted: number; error: string | null; unreachable: Record<string, string> };

function SortDialog({ page, onClose, onDone, onStart }: { page: Page; onClose: () => void; onDone: (text: string) => void; onStart: () => void }) {
  const [server, setServer] = useState("");
  const [mine, setMine] = useState(false);
  const byHand = page.tools.filter((t) => t.set_by === "owner" && t.domain_id != null && (!server || t.server === server)).length;
  const start = () => {
    onStart();
    onClose();
    api.post<SortResult>("/api/mcp/sort", { server: server || null, include_owner: mine }).then(
      (r) => {
        const down = Object.entries(r.unreachable).map(([s, why]) => `Server ${s} could not be listed: ${why}.`);
        const done = r.error ? `Sorting failed: ${r.error}` : `Sorted ${r.sorted} tool${r.sorted === 1 ? "" : "s"} into domains.`;
        onDone([done, ...down].join(" "));
      },
      (e) => onDone(`Sorting failed: ${errorText(e)}`),
    );
  };
  return (
    <Dialog title="Sort into domains" onClose={onClose}>
        <Text block dim>
          The main model sorts the tools again; domain names and descriptions you wrote stay.
        </Text>
        <Field label="servers">
          <Select value={server} onChange={(e) => setServer(e.target.value)}>
            <option value="">all enabled servers</option>
            {page.servers.filter((s) => s.enabled).map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
          </Select>
        </Field>
        <Text block>
          {byHand === 0 ? "You placed no tool by hand." : `${byHand} tool${byHand === 1 ? "" : "s"} you placed by hand stay${byHand === 1 ? "s" : ""} where ${byHand === 1 ? "it is" : "they are"}.`}
        </Text>
        <Checkbox checked={mine} onChange={setMine} disabled={byHand === 0} label="Also re-sort the tools I placed by hand" />
        <Actions>
          <Button variant="primary" onClick={start}>
            sort
          </Button>
          <Button onClick={onClose}>cancel</Button>
        </Actions>
    </Dialog>
  );
}
