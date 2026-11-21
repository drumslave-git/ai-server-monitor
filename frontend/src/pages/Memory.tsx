import { useMemo, useState } from "react";
import { api, type Schemas } from "../api";
import { ChatRef, RunRef } from "../components/EntityRefs";
import { ago } from "../fmt";
import { errorText } from "../jobs";
import {
  Actions,
  Button,
  Checkbox,
  Confirm,
  Empty,
  ErrorText,
  Field,
  IconButton,
  Input,
  KeyValue,
  List,
  ListItem,
  Meta,
  PageHeader,
  Pad,
  Pane,
  Panes,
  SaveBar,
  Select,
  Tag,
  Textarea,
  Toolbar,
} from "../kit";
import { useStream } from "../live";
import { Markdown, useMatchMedia, useNow, usePoll } from "../ui";

/* The notes the chat model keeps (and the owner may edit): they outlive a conversation, the model reads and writes them
   in the chat and in summaries, and a job reads the notes filed under its domains. The list is on the left, the open
   note on the right (on a phone one of the two, as in the chat); a note the owner edits stays the owner's: the model
   can read it but not change it. Nothing here limits how many notes there are or how long they are.
     #/memory             the list
     #/memory/<id>        a note
     #/memory/<id>/edit   its form */

type Note = Schemas["Note"];
type NoteList = Schemas["NoteList"];

/** Below this width the page shows the list or the note, not both (the width where the panes stand under each other). */
const ONE_PANE = "(max-width: 760px)";

const noteHref = (id: number, sub?: string) => `#/memory/${id}${sub ? `/${sub}` : ""}`;

export function MemoryPage({ params }: { params: string[] }) {
  const [rawId, sub] = params;
  const id = /^\d+$/.test(rawId ?? "") ? Number(rawId) : null;
  const notes = usePoll(() => api.get<NoteList>("/api/memory"), 60_000);
  useStream(["memory"], notes.reload);
  const now = useNow(30_000);
  const onePane = useMatchMedia(ONE_PANE);
  const [query, setQuery] = useState("");
  const [area, setArea] = useState("all");

  const items = notes.data?.items;
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (items ?? []).filter(
      (n) =>
        (area === "all" || (area === "none" ? n.domains.length === 0 : n.domains.includes(area))) &&
        (!q || n.title.toLowerCase().includes(q) || n.body.toLowerCase().includes(q)),
    );
  }, [items, query, area]);

  if (!notes.data) {
    return (
      <>
        <PageHeader title="Memory" />
        {notes.error ? <ErrorText>{notes.error}</ErrorText> : <Meta>Loading…</Meta>}
      </>
    );
  }

  const { domains } = notes.data;
  const open = id === null ? null : (notes.data.items.find((n) => n.id === id) ?? null);
  const filtering = query.trim() !== "" || area !== "all";
  // A phone shows the open note alone, under a way back; its head carries the title of the page like any other.
  const detailOnly = onePane && id !== null;

  const filters = (
    <Toolbar
      side={
        <Field label="search">
          <Input type="search" placeholder="Search titles and notes" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search titles and notes" />
        </Field>
      }
    >
      <Field label="area" flex="1 1 150px">
        <Select value={area} onChange={(e) => setArea(e.target.value)} aria-label="Area">
          <option value="all">all areas</option>
          <option value="none">no area</option>
          {domains.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </Select>
      </Field>
    </Toolbar>
  );

  return (
    <>
      <PageHeader
        title="Memory"
        back={detailOnly ? { label: "all notes", href: "#/memory" } : undefined}
        tools={!detailOnly && notes.data.items.length > 0 ? filters : undefined}
        fold="filters"
      />
      {notes.error && <ErrorText>{notes.error}</ErrorText>}
      {notes.data.items.length === 0 ? (
        <Empty>No notes yet. The chat model writes one when it learns something worth keeping.</Empty>
      ) : (
        <Panes stack>
          {!detailOnly && (
            <Pane side="wide" label="Notes" meta={filtering ? `${shown.length} of ${items!.length}` : String(items!.length)} title="Notes">
              {shown.length === 0 ? (
                <Empty>No note matches.</Empty>
              ) : (
                <List label="Notes">
                  {shown.map((n) => (
                    <ListItem
                      key={n.id}
                      href={noteHref(n.id)}
                      selected={n.id === id}
                      title={n.title}
                      meta={[n.domains.join(", ") || "no area", ago(n.updated, now)].join(" · ")}
                      trailing={n.author === "owner" ? "yours" : undefined}
                    />
                  ))}
                </List>
              )}
            </Pane>
          )}
          {(!onePane || id !== null) &&
            (open ? (
              sub === "edit" ? (
                <NoteForm key={`${open.id}:${open.updated}`} note={open} domains={domains} onSaved={notes.reload} />
              ) : (
                <NoteView key={open.id} note={open} now={now} onDeleted={notes.reload} />
              )
            ) : (
              <Pane label="Note" bare>
                <Empty>{id === null ? "Pick a note." : "No such note."}</Empty>
              </Pane>
            ))}
        </Panes>
      )}
    </>
  );
}

/** Where a note was written: the chat or the run, as a link while it still exists. */
function Writer({ note }: { note: Note }) {
  const s = note.source;
  if (!s) return <>unknown</>;
  if (s.kind === "chat") return s.exists ? <ChatRef id={s.id}>chat {s.id.slice(0, 8)}</ChatRef> : <>a chat that is gone</>;
  const what = s.label ? `run #${s.id} of ${s.label}` : `run #${s.id}`;
  return s.exists && s.label ? <RunRef slug={s.label} id={Number(s.id)}>{what}</RunRef> : <>{what}{s.exists ? "" : " (gone)"}</>;
}

function NoteView({ note, now, onDeleted }: { note: Note; now: number; onDeleted: () => void }) {
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const remove = async () => {
    try {
      await api.del(`/api/memory/${note.id}`);
      location.hash = "#/memory";
      onDeleted();
    } catch (e) {
      setError(errorText(e));
    }
  };
  return (
    <Pane
      label="Note"
      title={note.title}
      meta={`updated ${ago(note.updated, now)}`}
      aside={
        <Actions>
          <IconButton icon="rename" label="edit" onClick={() => (location.hash = noteHref(note.id, "edit"))} />
          <IconButton icon="remove" label="delete" variant="danger" onClick={() => setAsking(true)} />
        </Actions>
      }
    >
      <Pad>
        {error && <ErrorText>{error}</ErrorText>}
        <KeyValue
          items={[
            ["area", note.domains.length ? note.domains.map((d) => <Tag key={d}>{d}</Tag>) : "none"],
            ["written in", <Writer key="w" note={note} />],
            ["created", ago(note.created, now)],
            ...(note.author === "owner" ? ([["edited by", "you: the model can read this note but not change it"]] as [string, string][]) : []),
          ]}
        />
        <Markdown text={note.body} />
      </Pad>
      {asking && (
        <Confirm title={`Delete “${note.title}”?`} confirm="delete" onConfirm={() => void remove()} onClose={() => setAsking(false)}>
          The note is gone for good; the model and the jobs no longer see it.
        </Confirm>
      )}
    </Pane>
  );
}

function NoteForm({ note, domains, onSaved }: { note: Note; domains: string[]; onSaved: () => void }) {
  const [title, setTitle] = useState(note.title);
  const [body, setBody] = useState(note.body);
  const [picked, setPicked] = useState(note.domains);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const changed = title !== note.title || body !== note.body || picked.join() !== note.domains.join();
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.put(`/api/memory/${note.id}`, { title: title.trim(), body: body.trim(), domains: picked });
      onSaved();
      location.hash = noteHref(note.id);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const toggle = (d: string, on: boolean) => setPicked(domains.filter((x) => (x === d ? on : picked.includes(x))));
  return (
    <Pane
      label="Edit note"
      title="Edit note"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
      foot={
        <SaveBar>
          <Button variant="primary" icon="save" type="submit" disabled={busy || !changed || !title.trim() || !body.trim()}>
            save
          </Button>
          <Button icon="discard" onClick={() => (location.hash = noteHref(note.id))}>
            cancel
          </Button>
        </SaveBar>
      }
    >
      <Pad>
        <Field label="title">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label="note">
          <Textarea rows={12} value={body} onChange={(e) => setBody(e.target.value)} />
        </Field>
        <Field
          label="area"
          group
          help={domains.length ? "The jobs of these domains read the note; the chat sees every title." : "There are no MCP domains yet, so a note has no area."}
        >
          {domains.map((d) => (
            <Checkbox key={d} label={d} checked={picked.includes(d)} onChange={(on) => toggle(d, on)} />
          ))}
        </Field>
        {error && <ErrorText>{error}</ErrorText>}
      </Pad>
    </Pane>
  );
}
