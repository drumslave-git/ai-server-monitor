// Flight Deck UI kit: every screen is built from these components and nothing else.
// The look lives in tokens.css and kit.css; a page never sets a colour, a font or a border itself.
// A part that is missing is added here first and then used (see README.md in this folder).
import "@fontsource/big-shoulders/800.css";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./tokens.css";
import "./kit.css";
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type Announcements,
  type DragEndEvent,
  type UniqueIdentifier,
} from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type AnchorHTMLAttributes,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type FormEvent,
  type InputHTMLAttributes,
  type MouseEvent,
  type ReactNode,
  type Ref,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

/** ok = fine or on, warn = warning or waiting, crit = critical or failed, info = info or running, off = nothing to say. */
export type Tone = "ok" | "warn" | "crit" | "info" | "off";
/** What a cell of a strip can show: a tone, plain text colour, a scheduled run, the "now" mark, or empty. */
export type Cell = "ok" | "warn" | "crit" | "info" | "text" | "next" | "now" | "";

const cx = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(" ");
const TONE_WORD: Record<Tone, string> = { ok: "ok", warn: "warning", crit: "critical", info: "info", off: "" };
const toneText = (tone: Tone | undefined) => (tone ? `fd-t-${tone}` : undefined);

/** Where a block sits in its parent's flex or grid: the only layout a component takes from outside.
 *  flex and width as in CSS; span: how many columns of a FormGrid it takes ("all": the whole line);
 *  toEnd: pushed to the far end of its row. */
type Place = { flex?: string; width?: string; span?: number | "all"; toEnd?: boolean };
function place({ flex, width, span, toEnd }: Place): CSSProperties | undefined {
  if (!flex && !width && !span && !toEnd) return undefined;
  return { flex, width, gridColumn: span === "all" ? "1 / -1" : span ? `span ${span}` : undefined, marginLeft: toEnd ? "auto" : undefined };
}

/** A step of the spacing scale (tokens.css): 1 = 4px, 2 = 8px, 3 = 12px, 4 = 16px, 5 = 20px, 6 = 24px. */
type Gap = 0 | 1 | 2 | 3 | 4 | 5 | 6;
const gapClass = (gap: Gap | undefined) => (gap === undefined ? undefined : `fd-gap-${gap}`);
type Align = "start" | "center" | "end" | "baseline";
const alignClass = (align: Align | undefined) => (align ? `fd-align-${align}` : undefined);

/** With onSubmit the block is a form (id lets a button outside it submit it). */
type AsForm = { id?: string; onSubmit?: (e: FormEvent<HTMLFormElement>) => void };

// ---- layout ----------------------------------------------------------------

export function Shell({ children }: { children: ReactNode }) {
  return <div className="fd">{children}</div>;
}

export type NavItem = { key: string; label: string; href: string; count?: number };

/** The navigation rail: brand, numbered nav, and whatever stands at its foot (children). */
export function Rail({
  items,
  active,
  live,
  version,
  children,
}: {
  items: NavItem[];
  active: string;
  /** Live updates connected: the lamp beside the mark. */
  live: boolean;
  version?: string | null;
  children?: ReactNode;
}) {
  return (
    <aside className="fd-rail">
      <div className="fd-brand">
        <div className="fd-brand-mark">
          ASM
          <Lamp tone={live ? "ok" : "warn"} title={live ? "Live updates on" : "Reconnecting"} />
        </div>
        <span className="fd-meta">ai server monitor</span>
        <span className="fd-meta">{[version, live ? "live" : "reconnecting"].filter(Boolean).join(" · ")}</span>
      </div>
      <nav className="fd-nav" aria-label="Main">
        {items.map((item, i) => (
          <a key={item.key} className="fd-nav-item" href={item.href} aria-current={item.key === active ? "page" : undefined}>
            <span>{String(i + 1).padStart(2, "0")}</span>
            <span>{item.label}</span>
            {item.count ? <Count>{item.count}</Count> : null}
          </a>
        ))}
      </nav>
      {children && <div className="fd-auto">{children}</div>}
    </aside>
  );
}

export function Main({ children }: { children: ReactNode }) {
  return <main className="fd-main">{children}</main>;
}

/** The head of a page. Write the title in normal case; actions go in as children. */
export function PageHeader({
  title,
  note,
  back,
  meta,
  children,
}: {
  title: string;
  note?: ReactNode;
  back?: { label: string; href: string };
  meta?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="fd-head">
      <div className="fd-head-text">
        {(back || meta) && (
          <div className="fd-head-over">
            {back && (
              <a className="fd-head-back" href={back.href}>
                ← {back.label}
              </a>
            )}
            {meta && <span className="fd-meta">{meta}</span>}
          </div>
        )}
        <h1>{title}</h1>
        {note && <p className="fd-head-note">{note}</p>}
      </div>
      {children && <div className="fd-head-actions">{children}</div>}
    </header>
  );
}

/** A section of the page (note: a line under the title; children: its buttons). */
export function SectionHead({ title, note, children }: { title: ReactNode; note?: ReactNode; children?: ReactNode }) {
  return (
    <div className="fd-section-head">
      <div className="fd-section-text">
        <h2>{title}</h2>
        {note && <p className="fd-head-note">{note}</p>}
      </div>
      {children && <div className="fd-actions">{children}</div>}
    </div>
  );
}

/** A box on the page. Its body is ruled lines (Row, Table, Fold, Finding, List) and padded parts (Pad), in any order. */
export function Panel({
  title,
  meta,
  aside,
  hazard,
  pad,
  inset,
  id,
  children,
  ...where
}: {
  /** An anchor to scroll to. */
  id?: string;
  title?: ReactNode;
  /** A mono line beside the title. */
  meta?: ReactNode;
  /** Controls at the right end of the head. */
  aside?: ReactNode;
  /** The stripe for something that waits on the owner. */
  hazard?: "warn" | "crit";
  /** The whole body is one Pad (prose, a form). */
  pad?: boolean;
  /** Sit on the page ground: a card inside another panel. */
  inset?: boolean;
  children?: ReactNode;
} & Place) {
  return (
    <section id={id} className={cx("fd-panel", inset && "fd-panel-inset")} style={place(where)}>
      {hazard && <Hazard tone={hazard} />}
      {(title || aside) && (
        <div className="fd-panel-head">
          {title && <h2>{title}</h2>}
          {meta && <span className="fd-meta">{meta}</span>}
          {aside && <div className="fd-panel-aside">{aside}</div>}
        </div>
      )}
      {pad ? <Pad>{children}</Pad> : children}
    </section>
  );
}

/** A padded part of a panel: its children under each other. Under the panel's head it needs no top padding. */
export function Pad({ gap, children, ...where }: { gap?: Gap; children: ReactNode } & Place) {
  return (
    <div className={cx("fd-panel-pad", gapClass(gap))} style={place(where)}>
      {children}
    </div>
  );
}

/** One line of a panel, ruled off from the one above. Its children stand side by side and wrap.
 *  column: under each other instead. sticky: stays at the foot of the window while its panel is in view
 *  (the composer of a thread). label names it for assistive tech: an article of its own. */
export function Row({
  current,
  column,
  gap,
  align,
  nowrap,
  sticky,
  label,
  ref,
  id,
  onSubmit,
  children,
}: {
  current?: boolean;
  column?: boolean;
  gap?: Gap;
  align?: Align;
  nowrap?: boolean;
  sticky?: boolean;
  label?: string;
  ref?: Ref<HTMLElement>;
  children: ReactNode;
} & AsForm) {
  const className = cx("fd-row", column && "fd-row-col", nowrap && "fd-flex-nowrap", sticky && "fd-row-sticky", gapClass(gap), alignClass(align));
  const current_ = current ? "true" : undefined;
  if (onSubmit)
    return (
      <form id={id} className={className} aria-current={current_} onSubmit={onSubmit}>
        {children}
      </form>
    );
  if (label)
    return (
      <article ref={ref} id={id} className={className} aria-current={current_} aria-label={label}>
        {children}
      </article>
    );
  return (
    <div ref={ref as Ref<HTMLDivElement>} id={id} className={className} aria-current={current_}>
      {children}
    </div>
  );
}

export function Hazard({ tone = "warn" }: { tone?: "warn" | "crit" }) {
  return <div className={cx("fd-hazard", tone === "crit" && "fd-hazard-crit")} aria-hidden />;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="fd-empty">{children}</p>;
}

/** A line the owner has to read before going on, with its action as children. */
export function Notice({ title, children, action, tone }: { title?: ReactNode; children?: ReactNode; action?: ReactNode; tone?: "warn" | "crit" }) {
  return (
    <div className={cx("fd-notice", tone && `fd-notice-${tone}`)} role="status">
      <div className="fd-notice-text">
        {title && <strong>{title}</strong>}
        {children && <div>{children}</div>}
      </div>
      {action}
    </div>
  );
}

/** The top of the dashboard: one status word, the sentence under it, its buttons (children), and counters beside (aside).
 *  compact: one of several side by side (a smaller word; the headline is a second-level heading). */
export function Hero({
  meta,
  status,
  word,
  tone,
  headline,
  compact,
  children,
  aside,
}: {
  meta?: ReactNode;
  compact?: boolean;
  /** A Status shown above the word while something is in progress. */
  status?: ReactNode;
  word?: ReactNode;
  tone?: Tone;
  headline: ReactNode;
  children?: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className={cx("fd-hero", compact && "fd-hero-compact")}>
      <div className="fd-hero-text">
        {meta && <span className="fd-meta">{meta}</span>}
        {status}
        {word && <span className={cx("fd-hero-word", toneText(tone))}>{word}</span>}
        {compact ? <h2 className="fd-hero-headline">{headline}</h2> : <h1 className="fd-hero-headline">{headline}</h1>}
        {children}
      </div>
      {aside}
    </div>
  );
}

/** Buttons side by side. */
export function Actions({ children, ...where }: { children: ReactNode } & Place) {
  return (
    <div className="fd-actions" style={place(where)}>
      {children}
    </div>
  );
}

/** What the marks of a strip mean: a row of plain Status items. */
export function Legend({ children, ...where }: { children: ReactNode } & Place) {
  return (
    <div className="fd-legend" style={place(where)}>
      {children}
    </div>
  );
}

/** Children side by side that wrap on a narrow screen (panels in a row, a list beside its detail).
 *  stretch: all as tall as the tallest. */
export function Split({ stretch, id, onSubmit, children }: { stretch?: boolean; children: ReactNode } & AsForm) {
  const className = cx("fd-split", stretch && "fd-split-stretch");
  if (onSubmit)
    return (
      <form id={id} className={className} onSubmit={onSubmit}>
        {children}
      </form>
    );
  return (
    <div id={id} className={className}>
      {children}
    </div>
  );
}

/** Small things side by side on one line that wraps: controls, a status beside its note, fields in a row.
 *  between: the first at the start, the last at the end. */
export function Inline({
  gap,
  align,
  between,
  nowrap,
  children,
  ...where
}: { gap?: Gap; align?: Align; between?: boolean; nowrap?: boolean; children: ReactNode } & Place) {
  return (
    <div className={cx("fd-inline", between && "fd-inline-between", nowrap && "fd-flex-nowrap", gapClass(gap), alignClass(align))} style={place(where)}>
      {children}
    </div>
  );
}

/** Content that scrolls inside a height limit (CSS length) instead of stretching the page. */
export function Scroll({ max, children }: { max: string; children: ReactNode }) {
  return (
    <div className="fd-scroll" style={{ maxHeight: max }}>
      {children}
    </div>
  );
}

/** A small heading over what it names. */
export function Labeled({ label, children, ...where }: { label: ReactNode; children: ReactNode } & Place) {
  return (
    <div className="fd-labeled" style={place(where)}>
      <h3 className="fd-sub">{label}</h3>
      {children}
    </div>
  );
}

/** A part of the page whose changes are read out (a test's result appearing). Empty, it takes no room. */
export function Live({ children }: { children?: ReactNode }) {
  return (
    <div className="fd-live" role="status" aria-live="polite">
      {children}
    </div>
  );
}

/** A window over the page that must be answered: its title, then fields and an Actions row.
 *  It is open while it is rendered; onClose is called when it closes (Esc included). */
export function Dialog({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);
  return (
    <dialog ref={ref} className="fd-dialog" aria-labelledby={titleId} onClose={onClose}>
      <div className="fd-dialog-body">
        <h2 id={titleId}>{title}</h2>
        {children}
      </div>
    </dialog>
  );
}

/** A modal <dialog> that opens when it mounts. Closing goes through dialog.close(), so the browser gives
 *  focus back to the control that opened it. */
function useModal() {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);
  return ref;
}

/** A question before something that cannot be taken back (delete, reset). The safe answer has the focus;
 *  Esc is the same as "cancel". onConfirm runs only for the confirm button; onClose runs either way. */
export function Confirm({
  title,
  confirm,
  variant = "danger",
  onConfirm,
  onClose,
  children,
}: {
  title: ReactNode;
  /** The confirm button's words: "delete", "reset". */
  confirm: string;
  variant?: "danger" | "warn" | "primary";
  onConfirm: () => void;
  onClose: () => void;
  children?: ReactNode;
}) {
  const ref = useModal();
  const titleId = useId();
  const textId = useId();
  const closed = () => {
    const dialog = ref.current;
    const yes = dialog?.returnValue === "yes";
    if (dialog) dialog.returnValue = "";
    if (yes) onConfirm();
    onClose();
  };
  return (
    <dialog ref={ref} className="fd-dialog" aria-labelledby={titleId} aria-describedby={children ? textId : undefined} onClose={closed}>
      <form method="dialog" className="fd-dialog-body">
        <h2 id={titleId}>{title}</h2>
        {children && <div id={textId}>{children}</div>}
        <div className="fd-actions fd-dialog-actions">
          <button className={buttonClass({})} value="" autoFocus>
            cancel
          </button>
          <button className={buttonClass({ variant })} value="yes">
            {confirm}
          </button>
        </div>
      </form>
    </dialog>
  );
}

export type PickerItem = { value: string; title: ReactNode; meta?: ReactNode; /** What the filter looks in. */ text: string };
export type PickerGroup = { label: string; items: PickerItem[] };

/** A dialog to pick one thing from a long list: a filter box over the things in their groups.
 *  Picking closes it; onClose runs either way. */
export function Picker({
  title,
  groups,
  onPick,
  onClose,
  filterLabel = "filter",
  empty = "Nothing to pick.",
}: {
  title: ReactNode;
  groups: PickerGroup[];
  onPick: (value: string) => void;
  onClose: () => void;
  filterLabel?: string;
  empty?: ReactNode;
}) {
  const ref = useModal();
  const titleId = useId();
  const filterId = useId();
  const [filter, setFilter] = useState("");
  const words = filter.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const shown = groups
    .map((g) => ({ ...g, items: g.items.filter((it) => words.every((w) => it.text.toLowerCase().includes(w))) }))
    .filter((g) => g.items.length);
  const count = shown.reduce((n, g) => n + g.items.length, 0);
  const pick = (value: string) => {
    onPick(value);
    ref.current?.close();
  };
  return (
    <dialog ref={ref} className="fd-dialog fd-dialog-wide" aria-labelledby={titleId} onClose={onClose}>
      <div className="fd-dialog-body">
        <h2 id={titleId}>{title}</h2>
        <div className="fd-field">
          <label className="fd-field-label" htmlFor={filterId}>
            {filterLabel}
          </label>
          <input id={filterId} className="fd-input" type="search" autoFocus value={filter} onChange={(e) => setFilter(e.target.value)} autoComplete="off" />
        </div>
        <div className="fd-sr" role="status">
          {filter ? `${count} found` : ""}
        </div>
        <div className="fd-picker">
          {shown.length === 0 && <div className="fd-empty">{groups.some((g) => g.items.length) ? "Nothing matches." : empty}</div>}
          {shown.map((g) => (
            <section key={g.label} className="fd-picker-group" aria-label={g.label}>
              <h3 className="fd-picker-head">{g.label}</h3>
              <div className="fd-list">
                {g.items.map((it) => (
                  <div key={it.value} className="fd-item">
                    <button type="button" className="fd-item-main" onClick={() => pick(it.value)}>
                      <span className="fd-item-body">
                        <span className="fd-item-title">{it.title}</span>
                        {it.meta && <span className="fd-meta">{it.meta}</span>}
                      </span>
                    </button>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
        <div className="fd-actions fd-dialog-actions">
          <button type="button" className={buttonClass({})} onClick={() => ref.current?.close()}>
            close
          </button>
        </div>
      </div>
    </dialog>
  );
}

export type SortableEntry = {
  id: string;
  /** Plain words for what is read out and for the controls' names. */
  name: string;
  title: ReactNode;
  meta?: ReactNode;
  trailing?: ReactNode;
};

/** A list whose order matters and can be changed: drag a row by its number (mouse or touch), pick it up
 *  with the keyboard (space, arrows, space; Esc cancels), or use its "up" and "down" buttons.
 *  onMove gets the old and the new index; onRemove, when given, adds a "remove" button to each row. */
export function SortableList({
  label,
  items,
  onMove,
  onRemove,
}: {
  /** What the list holds, for screen readers: "jobs of Health". */
  label: string;
  items: SortableEntry[];
  onMove: (from: number, to: number) => void;
  onRemove?: (id: string) => void;
}) {
  const sensors = useSensors(useSensor(PointerSensor), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));
  const [said, setSaid] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const refocus = useRef<{ id: string; which: "up" | "down" } | null>(null);
  const at = (id: UniqueIdentifier) => items.findIndex((it) => it.id === id);
  const name = (id: UniqueIdentifier) => items[at(id)]?.name ?? String(id);
  const place = (index: number) => `position ${index + 1} of ${items.length}`;
  const announcements: Announcements = {
    onDragStart: ({ active }) => `Picked up ${name(active.id)}, ${place(at(active.id))}.`,
    onDragOver: ({ active, over }) => (over ? `${name(active.id)} is over ${place(at(over.id))}.` : `${name(active.id)} is outside the list.`),
    onDragEnd: ({ active, over }) => (over ? `${name(active.id)} dropped at ${place(at(over.id))}.` : `${name(active.id)} dropped where it was.`),
    onDragCancel: ({ active }) => `Moving ${name(active.id)} was cancelled; it stays at ${place(at(active.id))}.`,
  };
  const dragEnd = ({ active, over }: DragEndEvent) => {
    if (over && active.id !== over.id) onMove(at(active.id), at(over.id));
  };
  const step = (index: number, by: -1 | 1) => {
    const to = index + by;
    if (to < 0 || to >= items.length) return;
    refocus.current = { id: items[index].id, which: by < 0 ? "up" : "down" };
    setSaid("");
    onMove(index, to);
    window.setTimeout(() => setSaid(`${items[index].name} is now at ${place(to)}.`), 500);
  };
  useLayoutEffect(() => {
    const want = refocus.current;
    if (!want) return;
    refocus.current = null;
    const row = listRef.current?.querySelector<HTMLElement>(`[data-sort-id="${CSS_ESCAPE(want.id)}"]`);
    const buttons = row ? [...row.querySelectorAll<HTMLButtonElement>("button[data-step]")] : [];
    const same = buttons.find((b) => b.dataset.step === want.which && !b.disabled);
    (same ?? buttons.find((b) => !b.disabled))?.focus();
  });
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragEnd={dragEnd}
      accessibility={{
        announcements,
        screenReaderInstructions: {
          draggable: "To move this row, press space, then the up and down arrows, then space to drop it. Escape cancels.",
        },
      }}
    >
      <SortableContext items={items.map((it) => it.id)} strategy={verticalListSortingStrategy}>
        <div ref={listRef} className="fd-sort" role="list" aria-label={label}>
          {items.map((it, i) => (
            <SortableRow key={it.id} entry={it} index={i} count={items.length} onStep={(by) => step(i, by)} onRemove={onRemove} />
          ))}
        </div>
      </SortableContext>
      <div className="fd-sr" role="status">
        {said}
      </div>
    </DndContext>
  );
}

/** The global CSS (dnd-kit's CSS helper has the same name). */
const CSS_ESCAPE = (value: string) => globalThis.CSS.escape(value);

/** Where the row at `index` stands while the row at `from` is held over `to` (no move: where it is). */
function movedPosition(index: number, from: number, to: number) {
  if (from < 0 || to < 0 || from === to) return index;
  if (index === from) return to;
  if (from < index && index <= to) return index - 1;
  if (to <= index && index < from) return index + 1;
  return index;
}

function SortableRow({
  entry,
  index,
  count,
  onStep,
  onRemove,
}: {
  entry: SortableEntry;
  index: number;
  count: number;
  onStep: (by: -1 | 1) => void;
  onRemove?: (id: string) => void;
}) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging, activeIndex, overIndex } = useSortable({ id: entry.id });
  const look: CSSProperties = { transform: CSS.Translate.toString(transform), transition };
  // while a row is being moved, every number shows where its row stands now, from the top
  const shown = movedPosition(index, activeIndex, overIndex);
  return (
    <div ref={setNodeRef} className={cx("fd-sort-item", isDragging && "fd-sort-dragging")} style={look} role="listitem" data-sort-id={entry.id}>
      <button
        ref={setActivatorNodeRef}
        type="button"
        className="fd-sort-handle"
        {...attributes}
        {...listeners}
        aria-label={`Move ${entry.name}, ${shown + 1} of ${count}`}
        aria-pressed={isDragging}
      >
        {String(shown + 1).padStart(2, "0")}
      </button>
      <span className="fd-sort-body">
        <span className="fd-item-title">{entry.title}</span>
        {entry.meta && <span className="fd-meta">{entry.meta}</span>}
      </span>
      {entry.trailing && <span className="fd-sort-trailing">{entry.trailing}</span>}
      <span className="fd-sort-buttons">
        <button type="button" className="fd-btn fd-btn-small" data-step="up" disabled={shown === 0} onClick={() => onStep(-1)} aria-label={`Move ${entry.name} up`}>
          up
        </button>
        <button type="button" className="fd-btn fd-btn-small" data-step="down" disabled={shown === count - 1} onClick={() => onStep(1)} aria-label={`Move ${entry.name} down`}>
          down
        </button>
        {onRemove && (
          <button type="button" className="fd-btn fd-btn-small" onClick={() => onRemove(entry.id)} aria-label={`Remove ${entry.name}`}>
            remove
          </button>
        )}
      </span>
    </div>
  );
}

/** Controls in a row above a list: filters, a search box, bulk buttons. */
export function Toolbar({ children }: { children: ReactNode }) {
  return <div className="fd-toolbar">{children}</div>;
}

/** Fields in columns that collapse to one on a narrow screen. */
export function FormGrid({ children }: { children: ReactNode }) {
  return <div className="fd-form">{children}</div>;
}

// ---- controls ---------------------------------------------------------------

type ButtonLook = { variant?: "default" | "primary" | "warn" | "danger" | "link"; block?: boolean };
const buttonClass = ({ variant, block }: ButtonLook) =>
  cx("fd-btn", variant && variant !== "default" && `fd-btn-${variant}`, block && "fd-btn-block");
/** What a control takes from the page: everything of its element except the look. */
type NoLook<T> = Omit<T, "className" | "style">;

/** Labels are lowercase verbs. Primary at most once per panel; warn is for something that changes the server;
 *  danger deletes something for good. */
export function Button({ variant, block, type = "button", toEnd, ...rest }: ButtonLook & Pick<Place, "toEnd"> & NoLook<ButtonHTMLAttributes<HTMLButtonElement>>) {
  return <button {...rest} type={type} className={buttonClass({ variant, block })} style={place({ toEnd })} />;
}

/** A link that looks like a button. */
export function LinkButton({ variant, block, ...rest }: ButtonLook & NoLook<AnchorHTMLAttributes<HTMLAnchorElement>>) {
  return <a {...rest} className={buttonClass({ variant, block })} />;
}

/** label names the switch for assistive tech; text, when given, is shown beside it. */
export function Switch({
  checked,
  onChange,
  label,
  text,
  title,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  text?: ReactNode;
  title?: string;
  disabled?: boolean;
}) {
  const button = (
    <button type="button" role="switch" className="fd-switch" aria-checked={checked} aria-label={label} title={title} disabled={disabled} onClick={() => onChange(!checked)} />
  );
  if (!text) return button;
  return (
    <span className="fd-switch-row">
      {button}
      <span>{text}</span>
    </span>
  );
}

export function Checkbox({
  checked,
  onChange,
  label,
  hideLabel,
  some,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: ReactNode;
  /** Keep the label for assistive tech only (a checkbox in a table row). */
  hideLabel?: boolean;
  /** Some, not all, of what this checkbox stands for is selected. */
  some?: boolean;
  disabled?: boolean;
}) {
  return (
    <label className="fd-check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        ref={(el) => {
          if (el) el.indeterminate = Boolean(some) && !checked;
        }}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className={hideLabel ? "fd-sr" : undefined}>{label}</span>
    </label>
  );
}

/** Only the room of a checkbox: a line with nothing to tick among lines that have one. */
export function CheckboxBlank() {
  return <span className="fd-check-blank" aria-hidden />;
}

/** A labelled control with its help line. group: the control is a set of buttons, not one input. */
export function Field({
  label,
  help,
  error,
  group,
  htmlFor,
  children,
  ...where
}: {
  label: ReactNode;
  help?: ReactNode;
  error?: ReactNode;
  group?: boolean;
  /** Id of the control, when it is not the first input inside. */
  htmlFor?: string;
  children: ReactNode;
} & Place) {
  const body = (
    <>
      <span className="fd-field-label">{label}</span>
      {children}
      {help && <span className="fd-field-help">{help}</span>}
      {error && <span className="fd-field-error">{error}</span>}
    </>
  );
  if (group)
    return (
      <div className="fd-field" role="group" aria-label={typeof label === "string" ? label : undefined} style={place(where)}>
        {body}
      </div>
    );
  return (
    <label className="fd-field" htmlFor={htmlFor} style={place(where)}>
      {body}
    </label>
  );
}

export function Input({ invalid, flex, width, ...rest }: { invalid?: boolean } & Pick<Place, "flex" | "width"> & Omit<NoLook<InputHTMLAttributes<HTMLInputElement>>, "width">) {
  return <input {...rest} className="fd-input" style={place({ flex, width })} aria-invalid={invalid || undefined} />;
}

export function Textarea({ invalid, ...rest }: { invalid?: boolean } & NoLook<TextareaHTMLAttributes<HTMLTextAreaElement>>) {
  return <textarea {...rest} className="fd-textarea" aria-invalid={invalid || undefined} />;
}

export function Select({ invalid, flex, width, ...rest }: { invalid?: boolean } & Pick<Place, "flex" | "width"> & NoLook<SelectHTMLAttributes<HTMLSelectElement>>) {
  return <select {...rest} className="fd-select" style={place({ flex, width })} aria-invalid={invalid || undefined} />;
}

/** Controls joined into one line: the inputs share the width, a button stands at the end
 *  (a secret with "clear", a name and its value with "remove"). */
export function Joined({ children, ...where }: { children: ReactNode } & Place) {
  return (
    <div className="fd-joined" style={place(where)}>
      {children}
    </div>
  );
}

/** A value shown where a control would stand: it cannot be changed here. */
export function FieldValue({ children }: { children: ReactNode }) {
  return <span className="fd-field-value">{children}</span>;
}

/** One of several chosen things, with the button that takes it away. label names that button for assistive tech. */
export function Chip({ children, onRemove, label }: { children: ReactNode; onRemove: () => void; label: string }) {
  return (
    <span className="fd-chip">
      <span>{children}</span>
      <button type="button" aria-label={label} onClick={onRemove}>
        remove
      </button>
    </span>
  );
}

export type Choice<T extends string> = { value: T; label: ReactNode };

/** One of a few options, all visible. */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
  disabled,
  ...where
}: {
  options: Choice<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  disabled?: boolean;
} & Place) {
  // A radio group: Tab reaches the chosen option, the arrow keys move the choice.
  const move = (step: number) => {
    const i = options.findIndex((o) => o.value === value);
    onChange(options[(i + step + options.length) % options.length].value);
  };
  return (
    <div
      className="fd-seg"
      role="radiogroup"
      aria-label={label}
      style={place(where)}
      onKeyDown={(e) => {
        if (disabled) return;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") move(1);
        else if (e.key === "ArrowLeft" || e.key === "ArrowUp") move(-1);
        else return;
        e.preventDefault();
        // the chosen option carries the focus
        requestAnimationFrame(() => e.currentTarget.querySelector<HTMLElement>('[aria-checked="true"]')?.focus());
      }}
    >
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          className="fd-seg-item"
          aria-checked={o.value === value}
          tabIndex={o.value === value ? 0 : -1}
          disabled={disabled}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Views of one page. A tab with an href is a link; otherwise onChange gets its key. */
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: { key: T; label: ReactNode; href?: string }[];
  value: T;
  onChange?: (key: T) => void;
  label: string;
}) {
  return (
    <div className="fd-seg fd-tabs" role="tablist" aria-label={label}>
      {tabs.map((t) =>
        t.href ? (
          <a key={t.key} className="fd-seg-item" role="tab" aria-selected={t.key === value} href={t.href}>
            {t.label}
          </a>
        ) : (
          <button key={t.key} type="button" className="fd-seg-item" role="tab" aria-selected={t.key === value} onClick={() => onChange?.(t.key)}>
            {t.label}
          </button>
        ),
      )}
    </div>
  );
}

// ---- status and data ---------------------------------------------------------

/** A flat square. Never alone for a state that matters: pair it with a word (Status). */
export function Lamp({
  tone,
  hollow,
  blink,
  title,
}: {
  tone?: Tone | "text" | "next";
  /** Outlined, not filled: nothing ran. */
  hollow?: boolean;
  /** Blinks while something is running. */
  blink?: boolean;
  title?: string;
}) {
  return (
    <span
      className={cx("fd-lamp", tone && tone !== "off" && `fd-lamp-${tone}`, hollow && "fd-lamp-hollow", blink && "fd-spin")}
      title={title}
      aria-hidden={title ? undefined : true}
    />
  );
}

/** A lamp plus its word. plain keeps the word in the text colour (legends, the rail). */
export function Status({
  tone,
  plain,
  hollow,
  blink,
  children,
}: {
  tone?: Tone | "text" | "next";
  plain?: boolean;
  hollow?: boolean;
  blink?: boolean;
  children: ReactNode;
}) {
  const word = !plain && tone && tone !== "text" && tone !== "next" ? toneText(tone) : undefined;
  return (
    <span className={cx("fd-status", word)}>
      <Lamp tone={tone} hollow={hollow} blink={blink} />
      <span>{children}</span>
    </span>
  );
}

export function Tag({ tone, children }: { tone?: Exclude<Tone, "off">; children: ReactNode }) {
  return <span className={cx("fd-tag", tone && `fd-tag-${tone}`)}>{children}</span>;
}

/** A small number that asks for attention: waiting approvals (warn), errors (crit). */
export function Count({ tone, children }: { tone?: "warn" | "crit"; children: ReactNode }) {
  return <span className={cx("fd-count", tone === "crit" && "fd-count-crit")}>{children}</span>;
}

/** A name in a table row or list that opens the thing it names. mono for machine names and commands. */
export function TitleLink({ mono, truncate, ...rest }: { mono?: boolean; truncate?: boolean } & NoLook<AnchorHTMLAttributes<HTMLAnchorElement>>) {
  return <a {...rest} className={cx("fd-title-link", mono && "fd-mono", truncate && "fd-truncate")} />;
}

/** Children under each other with the panel gap, or the gap given. */
export function Column({ gap, align, id, onSubmit, children, ...where }: { gap?: Gap; align?: "start" | "end"; children: ReactNode } & AsForm & Place) {
  const className = cx("fd-col", gapClass(gap), alignClass(align));
  if (onSubmit)
    return (
      <form id={id} className={className} style={place(where)} onSubmit={onSubmit}>
        {children}
      </form>
    );
  return (
    <div id={id} className={className} style={place(where)}>
      {children}
    </div>
  );
}

/** Counters stand in a row; flush drops the first one's rule at a panel's edge. */
export function Counters({ flush, children, ...where }: { flush?: boolean; children: ReactNode } & Place) {
  return (
    <div className={cx("fd-counters", flush && "fd-counters-flush")} style={place(where)}>
      {children}
    </div>
  );
}

/** A big numeral for a real count. */
export function Counter({
  value,
  label,
  tone,
  size,
  plain,
  title,
}: {
  value: ReactNode;
  label: ReactNode;
  tone?: Tone;
  size?: "sm" | "xl";
  /** No rule and padding: the counter stands alone. */
  plain?: boolean;
  /** A heading between the numeral and its label. */
  title?: ReactNode;
}) {
  return (
    <div className={cx("fd-counter", size && `fd-counter-${size}`, plain && "fd-counter-plain")}>
      <span className={cx("fd-counter-value", toneText(tone))}>{value}</span>
      {title && <h2 className="fd-counter-title">{title}</h2>}
      <span className="fd-counter-label">{label}</span>
    </div>
  );
}

type StripLook = { size?: "sm" | "lg"; fixed?: boolean; label?: string } & Place;

/** A strip of cells. label describes it to assistive tech; without one the strip is decoration for nearby text. */
export function Cells({ cells, size, fixed, label, ...where }: { cells: Cell[] } & StripLook) {
  return (
    <div
      className={cx("fd-cells", size && `fd-cells-${size}`, fixed && "fd-cells-fixed")}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      style={place(where)}
    >
      {cells.map((c, i) => (
        <span key={i} className={cx("fd-cell", c && `fd-cell-${c}`)} />
      ))}
    </div>
  );
}

/** value out of max as filled cells. inner is a second value drawn first, in innerTone. Rows of one list share max. */
export function Meter({
  value,
  max,
  count = 20,
  tone = "ok",
  inner,
  innerTone = "info",
  fluid,
  label,
  ...where
}: {
  value: number;
  max: number;
  count?: number;
  /** A tone, or "text" for a plain amount. "off" draws the amount in the empty colour. */
  tone?: Tone | "text";
  inner?: number;
  innerTone?: Exclude<Tone, "off"> | "text";
  /** The cells share the width given instead of keeping their own. */
  fluid?: boolean;
  label?: string;
} & Place) {
  const fill = (v: number | undefined) => (v && v > 0 && max > 0 ? Math.max(1, Math.min(count, Math.round((v / max) * count))) : 0);
  const a = fill(value);
  const b = fill(inner);
  const filled: Cell = tone === "off" ? "" : tone;
  const cells: Cell[] = Array.from({ length: count }, (_, i) => (i < b ? innerTone : i < a ? filled : ""));
  return <Cells cells={cells} size="sm" fixed={!fluid} label={label} {...where} />;
}

export type ScheduleRun = { at: number; tone: Tone };
const WORST: Record<Tone, number> = { off: 0, ok: 1, info: 2, warn: 3, crit: 4 };

/** A job's runs on a clock of equal slots: `past` slots back from now, the "now" mark, `future` slots ahead.
 *  runs carry the time a run started (seconds); a slot shows the worst run that fell into it.
 *  next is the next scheduled run and every the seconds between runs (interval jobs): they give the dashed cells. */
export function Schedule({
  runs,
  now,
  slot = 1800,
  past = 24,
  future = 6,
  next,
  every,
  size,
  label,
  ...where
}: {
  runs: ScheduleRun[];
  now: number;
  slot?: number;
  past?: number;
  future?: number;
  next?: number | null;
  every?: number | null;
  size?: "sm" | "lg";
  label?: string;
} & Place) {
  const cells: Cell[] = [];
  const start = now - past * slot;
  const slots: Tone[] = Array.from({ length: past }, () => "off");
  for (const run of runs) {
    const i = Math.floor((run.at - start) / slot);
    if (i >= 0 && i < past && WORST[run.tone] > WORST[slots[i]]) slots[i] = run.tone;
  }
  for (const tone of slots) cells.push(tone === "off" ? "" : tone);
  if (future > 0) cells.push("now");
  const planned = new Set<number>();
  if (next && next > now) {
    for (let t = next; t <= now + future * slot; t += every && every > 0 ? every : Infinity) planned.add(Math.floor((t - now) / slot));
  }
  for (let j = 0; j < future; j++) cells.push(planned.has(j) ? "next" : "");
  return <Cells cells={cells} size={size} label={label} {...where} />;
}

/** One job on the clock: its lamp and name, its schedule, its latest result in a line, and when. */
export function Lane({
  name,
  tone,
  href,
  line,
  when,
  blink,
  stack,
  children,
}: {
  name: ReactNode;
  tone?: Tone;
  href: string;
  line?: ReactNode;
  when?: ReactNode;
  blink?: boolean;
  /** Phone layout: name and times on one line, the strip and the result under it. */
  stack?: boolean;
  /** The Schedule. */
  children: ReactNode;
}) {
  return (
    <div className={cx("fd-lane", stack && "fd-lane-stack")}>
      <a className="fd-lane-name" href={href}>
        <Lamp tone={tone} hollow={!tone || tone === "off"} blink={blink} />
        {name}
      </a>
      {stack && <span className="fd-meta">{when}</span>}
      {children}
      <div className="fd-lane-note">
        <span>{line}</span>
        {!stack && <span className="fd-meta">{when}</span>}
      </div>
    </div>
  );
}

/** One span from start to end on an axis of length total (same unit for all). Up to three parts:
 *  queued is where a wait before start began (drawn hollow, like a hollow lamp), inner is a point
 *  between start and end: the part before it is drawn in innerTone (waiting, then working). */
export function Bar({
  start,
  end,
  total,
  tone = "ok",
  inner,
  innerTone = "info",
  queued,
  size,
  label,
  ...where
}: {
  start: number;
  end: number;
  total: number;
  tone?: Exclude<Cell, "next" | "now" | "">;
  inner?: number;
  innerTone?: Exclude<Cell, "next" | "now" | "">;
  queued?: number;
  size?: "sm";
  label?: string;
} & Place) {
  const span = total > 0 ? total : 1;
  const a = Math.max(0, start);
  const b = Math.min(span, end);
  const m = inner === undefined ? a : Math.max(a, Math.min(b, inner));
  const q = queued === undefined ? a : Math.max(0, Math.min(a, queued));
  const pct = (v: number) => `${(v / span) * 100}%`;
  return (
    <div
      className={cx("fd-bar", size === "sm" && "fd-bar-sm")}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      style={place(where)}
    >
      {a > q && <span className="fd-bar-fill fd-bar-queued" style={{ left: pct(q), width: pct(a - q) }} />}
      {m > a && <span className={`fd-bar-fill fd-cell-${innerTone}`} style={{ left: pct(a), width: pct(m - a) }} />}
      <span className={`fd-bar-fill fd-cell-${tone}`} style={{ left: pct(m), width: `max(2px, ${pct(b - m)})` }} />
    </div>
  );
}

/** Evenly spread tick labels under a strip or a bar of the same width. */
export function Axis({ labels, ...where }: { labels: ReactNode[] } & Place) {
  return (
    <div className="fd-axis" aria-hidden style={place(where)}>
      {labels.map((l, i) => (
        <span key={i}>{l}</span>
      ))}
    </div>
  );
}

/** Something a machine said, quoted exactly. comment is a dim line above it. */
export function Code({
  inline,
  wrap,
  comment,
  maxHeight,
  children,
  ...where
}: { inline?: boolean; wrap?: boolean; comment?: ReactNode; /** Scroll inside this height (px). */ maxHeight?: number; children: ReactNode } & Place) {
  const className = cx("fd-code", inline && "fd-code-inline", wrap && "fd-code-wrap", maxHeight !== undefined && "fd-code-scroll");
  if (inline) return <code className={className}>{children}</code>;
  return (
    <pre className={className} style={maxHeight !== undefined ? { ...place(where), maxHeight } : place(where)}>
      {comment && <span className="fd-dim">{comment}{"\n"}</span>}
      {children}
    </pre>
  );
}

/** A shell command with its button attached (action). */
export function Command({ comment, wrap, action, children, ...where }: { comment?: ReactNode; wrap?: boolean; action?: ReactNode; children: ReactNode } & Place) {
  return (
    <div className="fd-command" style={place(where)}>
      <Code comment={comment} wrap={wrap}>
        {children}
      </Code>
      {action}
    </div>
  );
}

/** A section that opens. inline: a small one inside running text. Leave open undefined to let the reader toggle it.
 *  Its head is one line: a long title or meta is cut with an ellipsis. */
export function Fold({
  title,
  meta,
  tag,
  tagTone,
  open,
  onToggle,
  inline,
  children,
}: {
  title: ReactNode;
  meta?: ReactNode;
  tag?: ReactNode;
  tagTone?: Exclude<Tone, "off">;
  open?: boolean;
  onToggle?: (open: boolean) => void;
  inline?: boolean;
  children: ReactNode;
}) {
  return (
    <details className={cx("fd-fold", inline && "fd-fold-inline")} open={open} onToggle={onToggle && ((e) => onToggle(e.currentTarget.open))}>
      <summary>
        {tag && <Tag tone={tagTone}>{tag}</Tag>}
        <span>{title}</span>
        {meta && <span className="fd-meta">{meta}</span>}
      </summary>
      <div className="fd-fold-body">{children}</div>
    </details>
  );
}

/** A list of ListItems. With a label it is a navigation landmark of that name. */
export function List({ label, children }: { label?: string; children: ReactNode }) {
  if (label)
    return (
      <nav className="fd-list" aria-label={label}>
        {children}
      </nav>
    );
  return <div className="fd-list">{children}</div>;
}

/** A row that can be chosen: a conversation, a settings section, a domain, a run.
 *  With href it is a link (onClick may take the click over); with onClick alone it is a button, and
 *  expanded says whether what it opens is open. children are marks inside the row (a Meter, a Count);
 *  action is a control of its own beside it (delete), shown while the row is pointed at, focused or selected. */
export function ListItem({
  title,
  meta,
  index,
  trailing,
  selected,
  expanded,
  href,
  onClick,
  mono,
  action,
  children,
}: {
  title: ReactNode;
  meta?: ReactNode;
  index?: ReactNode;
  trailing?: ReactNode;
  selected?: boolean;
  expanded?: boolean;
  href?: string;
  onClick?: (e: MouseEvent<HTMLElement>) => void;
  /** Machine names (domains, sections) in mono. */
  mono?: boolean;
  action?: ReactNode;
  children?: ReactNode;
}) {
  const body = (
    <>
      {index && <span className="fd-item-index">{index}</span>}
      <span className="fd-item-body">
        <span className="fd-item-title">{title}</span>
        {meta && <span className="fd-meta">{meta}</span>}
      </span>
      {trailing && <span className="fd-meta">{trailing}</span>}
      {children}
    </>
  );
  return (
    <div className={cx("fd-item", mono && "fd-item-mono", (selected || expanded) && "fd-item-on")}>
      {href !== undefined ? (
        <a className="fd-item-main" href={href} onClick={onClick} aria-current={selected ? "true" : undefined}>
          {body}
        </a>
      ) : (
        <button type="button" className="fd-item-main" onClick={onClick} aria-expanded={expanded} aria-current={selected ? "true" : undefined}>
          {body}
        </button>
      )}
      {action && <span className="fd-item-action">{action}</span>}
    </div>
  );
}

/** One finding of a run: severity and where it came from, its title, the detail (children), the evidence, its action. */
export function Finding({
  tone,
  source,
  title,
  evidence,
  action,
  children,
}: {
  tone: Exclude<Tone, "off" | "ok">;
  source?: ReactNode;
  title: ReactNode;
  evidence?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <article className="fd-finding">
      <div className="fd-finding-body">
        <Status tone={tone}>
          {TONE_WORD[tone]}
          {source ? <> · {source}</> : null}
        </Status>
        <h3 className="fd-finding-title">{title}</h3>
        {children && <div className="fd-dim">{children}</div>}
        {evidence && <Code>{evidence}</Code>}
      </div>
      {action}
    </article>
  );
}

/** One turn of a conversation. who "you" is the owner; anything else is the speaker's name.
 *  says replaces the name with what the speaker is doing ("you ran this command"); running: the speaker
 *  is still at it (the lamp blinks). typed: the body is text as it was typed and keeps its line breaks.
 *  wide: the owner's turn takes the whole width of a message (a command and its output). */
export function Message({
  who,
  says,
  running,
  meta,
  typed,
  wide,
  children,
}: {
  who: string;
  says?: ReactNode;
  running?: boolean;
  meta?: ReactNode;
  typed?: boolean;
  wide?: boolean;
  children: ReactNode;
}) {
  const you = who === "you";
  return (
    <div className={cx("fd-msg", you && "fd-msg-you", wide && "fd-msg-wide")}>
      <div className="fd-msg-who">
        {you && !says && !running ? (
          <span className="fd-meta">you</span>
        ) : (
          <Status tone={running || you ? "info" : "ok"} blink={running}>
            {says ?? who}
          </Status>
        )}
        {meta && <span className="fd-meta">{meta}</span>}
      </div>
      <div className={cx("fd-msg-body", typed && "fd-keep")}>{children}</div>
    </div>
  );
}

/** Something that asks for the owner's yes: the hazard stripe while it waits, what will happen (title,
 *  command, note, and children for a list of items), then the buttons and the state (actions).
 *  decided: nothing waits any more, the stripe is gone. */
export function Approval({
  title,
  command,
  note,
  decided,
  inset,
  actions,
  children,
  ...where
}: {
  title: ReactNode;
  command?: ReactNode;
  note?: ReactNode;
  decided?: boolean;
  inset?: boolean;
  actions?: ReactNode;
  children?: ReactNode;
} & Place) {
  return (
    <section className={cx("fd-panel", inset && "fd-panel-inset")} style={place(where)}>
      {!decided && <Hazard />}
      <div className="fd-approval-body">
        <strong>{title}</strong>
        {command && <Code wrap>{command}</Code>}
        {note && <p className="fd-dim">{note}</p>}
        {children}
        {actions && <div className="fd-approval-actions">{actions}</div>}
      </div>
    </section>
  );
}

/** A column of a Table: its heading (head, when the heading is more than the label), and its width. */
export type TableColumn = string | { label: string; head?: ReactNode; width?: string; minWidth?: string };

/** A table. head names its columns; the body is tbody elements of Tr and Td.
 *  On a narrow screen it scrolls inside itself; with stack each row becomes a block there instead,
 *  every cell under the name of its column. minWidth keeps the scrolling kind from squeezing its columns. */
export function Table({
  dense,
  head,
  stack,
  minWidth,
  children,
}: {
  dense?: boolean;
  head?: TableColumn[];
  stack?: boolean;
  minWidth?: string;
  children: ReactNode;
}) {
  const columns = (head ?? []).map((c) => (typeof c === "string" ? { label: c } : c));
  // the names of the columns for the stacked rows (kit.css reads --fd-l1 … --fd-l8)
  const labels = Object.fromEntries(columns.map((c, i) => [`--fd-l${i + 1}`, JSON.stringify(c.label)]));
  return (
    <div className="fd-table-wrap">
      <table className={cx("fd-table", dense && "fd-table-dense", stack && "fd-table-stack")} style={{ minWidth, ...labels }}>
        {columns.length > 0 && (
          <thead>
            <tr>
              {columns.map((c, i) => (
                <th key={i} scope="col" style={c.width || c.minWidth ? { width: c.width, minWidth: c.minWidth } : undefined}>
                  {c.head ?? c.label}
                </th>
              ))}
            </tr>
          </thead>
        )}
        {children}
      </table>
    </div>
  );
}

/** A row of a Table. off: what it stands for is switched off. group: a heading row over the rows after it. */
export function Tr({ off, selected, group, children }: { off?: boolean; selected?: boolean; group?: boolean; children: ReactNode }) {
  return (
    <tr className={cx(off && "fd-off", group && "fd-group") || undefined} aria-selected={selected ? "true" : undefined}>
      {children}
    </tr>
  );
}

/** A cell of a Table. fill: it takes the width the other columns leave (a line that is cut with an ellipsis).
 *  wrap: long words break. right: its content stands at the right edge. */
export function Td({
  nowrap,
  wrap,
  right,
  fill,
  tone,
  minWidth,
  colSpan,
  children,
}: {
  nowrap?: boolean;
  wrap?: boolean;
  right?: boolean;
  fill?: boolean;
  tone?: Tone;
  minWidth?: string;
  colSpan?: number;
  children?: ReactNode;
}) {
  return (
    <td
      className={cx(nowrap && "fd-nowrap", wrap && "fd-wrap", right && "fd-right", fill && "fd-fill", toneText(tone)) || undefined}
      style={minWidth ? { minWidth } : undefined}
      colSpan={colSpan}
    >
      {children}
    </td>
  );
}

/** A few lines under each other, close together (two lines in a table cell). */
export function Stack({ gap, children, ...where }: { gap?: 1 | 2; children: ReactNode } & Place) {
  return (
    <div className={cx("fd-stack", gapClass(gap))} style={place(where)}>
      {children}
    </div>
  );
}

/** Facts as name and value pairs. */
export function KeyValue({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="fd-kv">
      {items.map(([k, v], i) => (
        <KeyValueRow key={i} name={k} value={v} />
      ))}
    </dl>
  );
}
function KeyValueRow({ name, value }: { name: ReactNode; value: ReactNode }) {
  return (
    <>
      <dt>{name}</dt>
      <dd>{value}</dd>
    </>
  );
}

/** The unsaved changes of a form, with its buttons. */
export function SaveBar({ children }: { children: ReactNode }) {
  return <div className="fd-savebar">{children}</div>;
}

/** One line of an outcome: ok or failed, and what was said. running: the line shown while it is awaited. */
export function TestLine({ ok, running, children }: { ok?: boolean; running?: boolean; children?: ReactNode }) {
  if (running)
    return (
      <Row>
        <Status tone="info" blink>
          testing…
        </Status>
      </Row>
    );
  return (
    <Row>
      <Status tone={ok ? "ok" : "crit"}>{ok ? "ok" : "failed"}</Status>
      <span className="fd-testline-text">{children}</span>
    </Row>
  );
}

/** Text written by a model or a person as Markdown, already turned into safe HTML by the caller.
 *  onClick hears clicks on what is inside (the run buttons of commands). */
export function Prose({ html, onClick }: { html: string; onClick?: (e: MouseEvent<HTMLDivElement>) => void }) {
  return <div className="fd-md" onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />;
}

/** Text that shows its first three lines until the reader asks for the rest; opened, it keeps its line breaks. */
export function Clamp({ children }: { children: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [open, setOpen] = useState(false);
  const [long, setLong] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && !open) setLong(el.scrollHeight > el.clientHeight + 1);
  }, [children, open]);
  if (!children) return null;
  return (
    <>
      <p ref={ref} className={cx("fd-dim fd-wrap", open ? "fd-pre-line" : "fd-clamp")}>
        {children}
      </p>
      {(long || open) && (
        <Button variant="link" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "show less" : "show more"}
        </Button>
      )}
    </>
  );
}

/** Text helpers: the cases where words need a kit style without a component of their own. */

/** The small dim mono line: when, how many, where from. block: a line of its own. */
export function Meta({ children, tone, title, block, ...where }: { children: ReactNode; tone?: Tone; title?: string; block?: boolean } & Place) {
  const className = cx("fd-meta", toneText(tone));
  if (block)
    return (
      <p className={className} title={title} style={place(where)}>
        {children}
      </p>
    );
  return (
    <span className={className} title={title} style={place(where)}>
      {children}
    </span>
  );
}

/** Words in a kit text style. kind: "data" is something a machine said (mono), "label" a name (mono, heavier).
 *  dim and tone colour it. block: a paragraph of its own. wrap: long words break; nowrap: it stays on one line;
 *  keep: it keeps the line breaks it was written with. */
export function Text({
  kind,
  dim,
  tone,
  block,
  wrap,
  nowrap,
  keep,
  title,
  children,
  ...where
}: {
  kind?: "data" | "label";
  dim?: boolean;
  tone?: Tone;
  block?: boolean;
  wrap?: boolean;
  nowrap?: boolean;
  keep?: boolean;
  title?: string;
  children: ReactNode;
} & Place) {
  const className = cx(kind && `fd-${kind}`, dim && "fd-dim", toneText(tone), wrap && "fd-wrap", nowrap && "fd-nowrap", keep && "fd-keep") || undefined;
  if (block)
    return (
      <p className={className} title={title} style={place(where)}>
        {children}
      </p>
    );
  return (
    <span className={className} title={title} style={place(where)}>
      {children}
    </span>
  );
}

/** The title of a thing inside a panel (an approval, a card). */
export function Heading({ children }: { children: ReactNode }) {
  return <h3 className="fd-heading">{children}</h3>;
}

export function ErrorText({ children }: { children: ReactNode }) {
  return (
    <p className="fd-error" role="alert">
      {children}
    </p>
  );
}

/** A stable id for wiring a label to a control that Field cannot wrap. */
export const useFieldId = useId;
