import { ChevronRight, Pencil, Search, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { components } from "../api-types";
import { Description } from "../components/McpTools";
import { ago } from "../fmt";
import { errorText } from "../jobs";
import { Empty, PageHead } from "../ui";

/* All tools of the configured MCP servers, organised by domain (the area a person asks about).
   One collapsible card per domain; "Not sorted" on top when a sorting failed; on/off goes to
   the server's tool_filter (the same value as the Settings table). */

type Page = components["schemas"]["McpToolsPage"];
type Tool = components["schemas"]["McpTool"];
type Domain = components["schemas"]["McpDomain"];
type Kind = "all" | "read" | "write";
type OnOff = "all" | "on" | "off";

const NEW_DOMAIN = "__new__";

export function McpToolsPage() {
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [server, setServer] = useState("all");
  const [kind, setKind] = useState<Kind>("all");
  const [onOff, setOnOff] = useState<OnOff>("all");
  const [closed, setClosed] = useState<Set<string>>(new Set());
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
  const act = (p: Promise<unknown>) => {
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

  if (!page) return <div className="stack"><PageHead title="MCP tools" />{error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}</div>;

  const tools = page.tools;
  const servers = page.servers;
  const unsorted = shown.filter((t) => t.domain_id == null);
  const byDomain = (d: Domain) => shown.filter((t) => t.domain_id === d.id);
  const on = tools.filter((t) => t.enabled).length;
  const writes = tools.filter((t) => t.kind === "write").length;
  const nServers = new Set(tools.map((t) => t.server)).size;
  const down = servers.filter((s) => s.error);
  const allNames = ["unsorted", ...page.domains.map((d) => d.name)];
  const toOn = shown.filter((t) => !t.enabled);
  const toOff = shown.filter((t) => t.enabled);

  return (
    <div className="stack mt-page">
      <PageHead title="MCP tools">
        <button className="btn btn-sm" type="button" onClick={() => setSortOpen(true)} disabled={page.sorting || servers.length === 0}>
          <Sparkles size={14} strokeWidth={1.5} aria-hidden /> Sort into domains…
        </button>
      </PageHead>
      {servers.length === 0 ? (
        <Empty>No MCP server is configured. Add one under Settings → MCP servers.</Empty>
      ) : (
        <>
          <p className="muted small mt-summary">
            {tools.length} tool{tools.length === 1 ? "" : "s"} on {nServers} server{nServers === 1 ? "" : "s"} in {page.domains.length} domain
            {page.domains.length === 1 ? "" : "s"} · {on} on · {writes} write
          </p>
          {page.sorting && (
            <p className="mt-note" role="status">
              <span className="spinner" aria-hidden /> Sorting tools into domains… The page updates when it is done; until then some tools may still be without a domain.
            </p>
          )}
          {down.map((s) => (
            <p key={s.name} className="mt-note mt-warn">
              Server <strong>{s.name}</strong> could not be listed{s.listed_ts ? ` ${ago(s.listed_ts)}` : ""}: {s.error}. Its tools are shown as last listed.
            </p>
          ))}
          {notice && <p className="mt-note" role="status">{notice}</p>}
          {error && <p className="error">{error}</p>}
          <div className="mt-toolbar">
            <label className="mt-search">
              <Search size={14} strokeWidth={1.5} aria-hidden />
              <input type="search" placeholder="Search tools and descriptions" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search tools and descriptions" />
            </label>
            <select value={server} onChange={(e) => setServer(e.target.value)} aria-label="Server">
              <option value="all">All servers</option>
              {servers.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
            </select>
            <select value={kind} onChange={(e) => setKind(e.target.value as Kind)} aria-label="Read or write">
              <option value="all">Read and write</option>
              <option value="read">Read</option>
              <option value="write">Write</option>
            </select>
            <select value={onOff} onChange={(e) => setOnOff(e.target.value as OnOff)} aria-label="On or off">
              <option value="all">On and off</option>
              <option value="on">On</option>
              <option value="off">Off</option>
            </select>
            <button type="button" className="btn btn-quiet btn-sm" onClick={() => setClosed(closed.size ? new Set() : new Set(allNames))}>
              {closed.size ? "Expand all" : "Collapse all"}
            </button>
            <span className="mt-bulk">
              <button type="button" className="btn btn-sm" disabled={toOn.length === 0} onClick={() => switchAll(toOn, true)}
                title={filtering ? "Switches on every tool the search and filters show" : "Switches on every tool"}>
                Select all ({toOn.length})
              </button>
              <button type="button" className="btn btn-sm" disabled={toOff.length === 0} onClick={() => switchAll(toOff, false)}
                title={filtering ? "Switches off every tool the search and filters show" : "Switches off every tool"}>
                Deselect all ({toOff.length})
              </button>
            </span>
          </div>
          {filtering && shown.length === 0 && <Empty>No tool matches.</Empty>}
          {unsorted.length > 0 && (
            <Section
              id="unsorted"
              title="Not sorted"
              description="These tools have no domain: they are not offered to the chat or to jobs."
              count={unsorted}
              total={tools.filter((t) => t.domain_id == null)}
              open={!closed.has("unsorted")}
              onToggle={(o) => setClosed(toggle(closed, "unsorted", o))}
              onSwitch={(on) => switchAll(tools.filter((t) => t.domain_id == null), on)}
              warn
            >
              {unsorted.map((t) => <Row key={t.name} tool={t} domains={page.domains} servers={servers} act={act} reason />)}
            </Section>
          )}
          {page.domains.map((d) => {
            const rows = byDomain(d);
            const all = tools.filter((t) => t.domain_id === d.id);
            if (filtering && rows.length === 0) return null;
            if (all.length === 0) return <EmptyDomain key={d.id} domain={d} act={act} />;
            return (
              <Section
                key={d.id}
                id={d.name}
                title={d.name}
                description={d.description}
                count={rows}
                total={all}
                open={filtering || !closed.has(d.name)}
                onToggle={(o) => setClosed(toggle(closed, d.name, o))}
                onSwitch={(on) => switchAll(all, on)}
                domain={d}
                act={act}
              >
                {rows.map((t) => <Row key={t.name} tool={t} domains={page.domains} servers={servers} act={act} />)}
              </Section>
            );
          })}
        </>
      )}
      {sortOpen && (
        <SortDialog
          page={page}
          onClose={() => setSortOpen(false)}
          onStart={() => { setNotice(null); setPage({ ...page, sorting: true }); }}
          onDone={(text) => { setNotice(text); void load(); }}
        />
      )}
    </div>
  );
}

function toggle(set: Set<string>, key: string, open: boolean): Set<string> {
  const next = new Set(set);
  if (open) next.delete(key);
  else next.add(key);
  return next;
}

function Section({
  id, title, description, count, total, open, onToggle, onSwitch, domain, act, warn, children,
}: {
  id: string; title: string; description: string; count: Tool[]; total: Tool[]; open: boolean;
  onToggle: (open: boolean) => void; onSwitch: (on: boolean) => void; domain?: Domain; act?: (p: Promise<unknown>) => Promise<unknown>; warn?: boolean;
  children: React.ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const on = total.filter((t) => t.enabled).length;
  return (
    <>
    <details className={`mt-domain${warn ? " mt-unsorted" : ""}`} open={open} onToggle={(e) => onToggle((e.target as HTMLDetailsElement).open)} id={`domain-${id}`}>
      <summary>
        <ChevronRight className="mt-chev" size={16} strokeWidth={1.5} aria-hidden />
        <span className="mt-dtext">
          <span className="mt-dname">{title}</span>
          {description && <span className="mt-ddesc"> · {description}</span>}
        </span>
        <span className="mt-dmeta">
          {count.length !== total.length && <span>{count.length} shown · </span>}
          {on} of {total.length} on
          <button type="button" className="link-btn mt-dswitch" disabled={on === total.length} onClick={(e) => { e.preventDefault(); onSwitch(true); }}>
            Select all
          </button>
          <button type="button" className="link-btn mt-dswitch" disabled={on === 0} onClick={(e) => { e.preventDefault(); onSwitch(false); }}>
            Deselect all
          </button>
          {domain && domain.used_by.length > 0 && <span title={domain.used_by.join(", ")}> · {domain.used_by.length} job{domain.used_by.length === 1 ? "" : "s"}</span>}
          {domain && (
            <button type="button" className="btn-icon" aria-label={`Rename or describe ${domain.name}`} onClick={(e) => { e.preventDefault(); setEditing(true); }}>
              <Pencil size={14} strokeWidth={1.5} aria-hidden />
            </button>
          )}
        </span>
      </summary>
      <div className="mt-tools">{children}</div>
    </details>
    {/* outside <details>: a dialog inside a closed one would not show */}
    {editing && domain && act && <DomainEdit domain={domain} act={act} onClose={() => setEditing(false)} />}
    </>
  );
}

function EmptyDomain({ domain, act }: { domain: Domain; act: (p: Promise<unknown>) => Promise<unknown> }) {
  const [editing, setEditing] = useState(false);
  return (
    <div className="mt-domain mt-empty-domain">
      <div className="mt-empty-line">
        <span className="mt-dtext">
          <span className="mt-dname">{domain.name}</span>
          {domain.description && <span className="mt-ddesc"> · {domain.description}</span>}
        </span>
        <span className="mt-dmeta">
          no tools{domain.used_by.length > 0 && ` · used by ${domain.used_by.join(", ")}`}
          <button type="button" className="btn-icon" aria-label={`Rename or describe ${domain.name}`} onClick={() => setEditing(true)}>
            <Pencil size={14} strokeWidth={1.5} aria-hidden />
          </button>
        </span>
      </div>
      {editing && <DomainEdit domain={domain} act={act} onClose={() => setEditing(false)} />}
    </div>
  );
}

const NAME_MAX = 24;
const NAME_OK = /^[a-z0-9][a-z0-9-]*$/;

/** The app's dialog for a domain: a new one (`domain` undefined; `onCreated` gets it) or
 *  renaming / describing / deleting one. The name rule is shown and checked while typing. */
function DomainEdit({
  domain, act, onClose, onCreated,
}: {
  domain?: Domain; act: (p: Promise<unknown>) => Promise<unknown>; onClose: () => void; onCreated?: (d: Domain) => Promise<unknown>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(domain?.name ?? "");
  const [description, setDescription] = useState(domain?.description ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { ref.current?.showModal(); }, []);
  const trimmed = name.trim();
  const problem = !trimmed ? null : !NAME_OK.test(trimmed) ? "Only lowercase letters, digits and hyphens; start with a letter or digit" : null;
  const valid = trimmed !== "" && problem === null && (domain !== undefined || description.trim() !== "");
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
    <dialog ref={ref} className="mt-dialog" onClose={onClose}>
      <h3>{domain ? `Domain ${domain.name}` : "New domain"}</h3>
      <label className="mt-dialog-field">
        Name
        <input value={name} maxLength={NAME_MAX} onChange={(e) => setName(e.target.value)} className="mono" aria-invalid={problem !== null} autoFocus />
        <span className={`small ${problem ? "mt-reason" : "muted"}`}>
          {problem ?? "Lowercase letters, digits and hyphens"} · {trimmed.length} of {NAME_MAX} characters
        </span>
      </label>
      <label className="mt-dialog-field">
        Description
        <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What a person can ask about there" />
        {!domain && <span className="small muted">One line; the chat's model reads it to pick the domain.</span>}
      </label>
      {error && <p className="error">{error}</p>}
      <div className="form-buttons">
        <button type="button" className="btn btn-primary" onClick={save} disabled={!valid || busy}>{domain ? "Save" : "Create and move"}</button>
        <button type="button" className="btn btn-quiet" onClick={onClose}>Cancel</button>
        {domain && (
          <button type="button" className="btn btn-quiet mt-delete" onClick={remove} disabled={busy || domain.used_by.length > 0} title={domain.used_by.length ? `Used by ${domain.used_by.join(", ")}` : undefined}>
            Delete domain
          </button>
        )}
      </div>
    </dialog>
  );
}

function Row({
  tool, domains, servers, act, reason,
}: {
  tool: Tool; domains: Domain[]; servers: Page["servers"]; act: (p: Promise<unknown>) => Promise<unknown>; reason?: boolean;
}) {
  const state = servers.find((s) => s.name === tool.server);
  const path = `/api/mcp/tools/${encodeURIComponent(tool.server)}/${encodeURIComponent(tool.tool)}`;
  const [creating, setCreating] = useState(false);
  const move = (value: string) => {
    if (value === NEW_DOMAIN) setCreating(true);
    else if (value) void act(api.patch(path, { domain_id: Number(value) })).catch(() => undefined);
  };
  return (
    <div className={`mt-tool${tool.enabled ? "" : " off"}`}>
      <label className="switch" title={tool.enabled ? "Turn off" : "Turn on"}>
        <input type="checkbox" checked={tool.enabled} onChange={(e) => act(api.patch(path, { enabled: e.target.checked }))} aria-label={`${tool.name} on`} />
        <span />
      </label>
      <div className="mt-tmain">
        <div className="mt-tline">
          <span className="mt-tname">{tool.tool}</span>
          <span className={`mt-kind ${tool.kind}`}>{tool.kind === "write" ? "Write" : "Read"}</span>
          <span className="mt-server">
            {tool.server}
            {state && !state.enabled && " · server off"}
            {state?.error && " · unreachable"}
          </span>
        </div>
        <Description text={tool.description} />
        {reason && <p className="mt-reason">{tool.sort_error ?? "Not sorted yet: it is sorted at the next listing."}</p>}
      </div>
      <select className="mt-move" value="" onChange={(e) => move(e.target.value)} aria-label={`Move ${tool.tool} to another domain`}>
        <option value="">Move to…</option>
        {domains.filter((d) => d.id !== tool.domain_id).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        <option value={NEW_DOMAIN}>New domain…</option>
      </select>
      {creating && (
        <DomainEdit act={act} onClose={() => setCreating(false)} onCreated={(d) => api.patch(path, { domain_id: d.id })} />
      )}
    </div>
  );
}

type SortResult = { sorted: number; error: string | null; unreachable: Record<string, string> };

function SortDialog({ page, onClose, onDone, onStart }: { page: Page; onClose: () => void; onDone: (text: string) => void; onStart: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [server, setServer] = useState("");
  const [mine, setMine] = useState(false);
  useEffect(() => { ref.current?.showModal(); }, []);
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
    <dialog ref={ref} className="mt-dialog" onClose={onClose}>
      <h3>Sort into domains</h3>
      <p className="muted small">The main model sorts the tools again; domain names and descriptions you wrote stay.</p>
      <label className="mt-dialog-field">
        Servers
        <select value={server} onChange={(e) => setServer(e.target.value)}>
          <option value="">All enabled servers</option>
          {page.servers.filter((s) => s.enabled).map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
        </select>
      </label>
      <p className="small">
        {byHand === 0 ? "You placed no tool by hand." : `${byHand} tool${byHand === 1 ? "" : "s"} you placed by hand stay${byHand === 1 ? "s" : ""} where ${byHand === 1 ? "it is" : "they are"}.`}
      </p>
      <label className="mt-check">
        <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} disabled={byHand === 0} /> Also re-sort the tools I placed by hand
      </label>
      <div className="form-buttons">
        <button type="button" className="btn btn-primary" onClick={start}>Sort</button>
        <button type="button" className="btn btn-quiet" onClick={onClose}>Cancel</button>
      </div>
    </dialog>
  );
}
