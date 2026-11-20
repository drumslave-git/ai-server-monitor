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
  useId,
  type AnchorHTMLAttributes,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type InputHTMLAttributes,
  type ReactNode,
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

/** Where a block sits in its parent's flex or grid: the only layout a component takes from outside. */
type Place = { flex?: string; width?: string; span?: number };
function place({ flex, width, span }: Place): CSSProperties | undefined {
  if (!flex && !width && !span) return undefined;
  return { flex, width, gridColumn: span ? `span ${span}` : undefined };
}

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

export function Panel({
  title,
  meta,
  aside,
  hazard,
  pad,
  inset,
  children,
  ...where
}: {
  title?: ReactNode;
  /** A mono line beside the title. */
  meta?: ReactNode;
  /** Controls at the right end of the head. */
  aside?: ReactNode;
  /** The stripe for something that waits on the owner. */
  hazard?: "warn" | "crit";
  /** Pad the body (for prose and forms; rows and tables bring their own padding). */
  pad?: boolean;
  /** Sit on the page ground: a card inside another panel. */
  inset?: boolean;
  children?: ReactNode;
} & Place) {
  return (
    <section className={cx("fd-panel", inset && "fd-panel-inset")} style={place(where)}>
      {hazard && <Hazard tone={hazard} />}
      {(title || aside) && (
        <div className="fd-panel-head">
          {title && <h2>{title}</h2>}
          {meta && <span className="fd-meta">{meta}</span>}
          {aside && <div className="fd-panel-aside">{aside}</div>}
        </div>
      )}
      {pad ? <div className="fd-panel-pad">{children}</div> : children}
    </section>
  );
}

/** One line of a panel, ruled off from the one above. */
export function Row({ current, children }: { current?: boolean; children: ReactNode }) {
  return (
    <div className="fd-row" aria-current={current ? "true" : undefined}>
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

/** The top of the dashboard: one status word, the sentence under it, its buttons (children), and counters beside (aside). */
export function Hero({
  meta,
  status,
  word,
  tone,
  headline,
  children,
  aside,
}: {
  meta?: ReactNode;
  /** A Status shown above the word while something is in progress. */
  status?: ReactNode;
  word?: ReactNode;
  tone?: Tone;
  headline: ReactNode;
  children?: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className="fd-hero">
      <div className="fd-hero-text">
        {meta && <span className="fd-meta">{meta}</span>}
        {status}
        {word && <span className={cx("fd-hero-word", toneText(tone))}>{word}</span>}
        <h1 className="fd-hero-headline">{headline}</h1>
        {children}
      </div>
      {aside}
    </div>
  );
}

/** Buttons side by side. */
export function Actions({ children }: { children: ReactNode }) {
  return <div className="fd-actions">{children}</div>;
}

/** What the marks of a strip mean: a row of plain Status items. */
export function Legend({ children }: { children: ReactNode }) {
  return <div className="fd-legend">{children}</div>;
}

/** Children side by side that wrap on a narrow screen (panels in a row, a list beside its detail). */
export function Split({ children }: { children: ReactNode }) {
  return <div className="fd-split">{children}</div>;
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

type ButtonLook = { variant?: "default" | "primary" | "warn" | "link"; block?: boolean };
const buttonClass = ({ variant, block }: ButtonLook) =>
  cx("fd-btn", variant === "primary" && "fd-btn-primary", variant === "warn" && "fd-btn-warn", variant === "link" && "fd-btn-link", block && "fd-btn-block");

/** Labels are lowercase verbs. Primary at most once per panel; warn is for something that changes the server. */
export function Button({ variant, block, type = "button", ...rest }: ButtonLook & ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button {...rest} type={type} className={buttonClass({ variant, block })} />;
}

/** A link that looks like a button. */
export function LinkButton({ variant, block, ...rest }: ButtonLook & AnchorHTMLAttributes<HTMLAnchorElement>) {
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

export function Input({ invalid, ...rest }: { invalid?: boolean } & InputHTMLAttributes<HTMLInputElement>) {
  return <input {...rest} className="fd-input" aria-invalid={invalid || undefined} />;
}

export function Textarea({ invalid, ...rest }: { invalid?: boolean } & TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...rest} className="fd-textarea" aria-invalid={invalid || undefined} />;
}

export function Select({ invalid, ...rest }: { invalid?: boolean } & SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...rest} className="fd-select" aria-invalid={invalid || undefined} />;
}

export type Choice<T extends string> = { value: T; label: ReactNode };

/** One of a few options, all visible. */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
  disabled,
}: {
  options: Choice<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  disabled?: boolean;
}) {
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
export function TitleLink({ mono, truncate, ...rest }: { mono?: boolean; truncate?: boolean } & AnchorHTMLAttributes<HTMLAnchorElement>) {
  return <a {...rest} className={cx("fd-title-link", mono && "fd-mono", truncate && "fd-truncate")} />;
}

/** Children under each other with the panel gap (tight: the small gap). */
export function Column({ tight, children, ...where }: { tight?: boolean; children: ReactNode } & Place) {
  return (
    <div className={cx("fd-col", tight && "fd-col-tight")} style={place(where)}>
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
  label?: string;
} & Place) {
  const fill = (v: number | undefined) => (v && v > 0 && max > 0 ? Math.max(1, Math.min(count, Math.round((v / max) * count))) : 0);
  const a = fill(value);
  const b = fill(inner);
  const filled: Cell = tone === "off" ? "" : tone;
  const cells: Cell[] = Array.from({ length: count }, (_, i) => (i < b ? innerTone : i < a ? filled : ""));
  return <Cells cells={cells} size="sm" fixed label={label} {...where} />;
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

/** One span from start to end on an axis of length total (same unit for all).
 *  inner is a point between them: the part before it is drawn in innerTone (waiting, then working). */
export function Bar({
  start,
  end,
  total,
  tone = "ok",
  inner,
  innerTone = "info",
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
  size?: "sm";
  label?: string;
} & Place) {
  const span = total > 0 ? total : 1;
  const a = Math.max(0, start);
  const b = Math.min(span, end);
  const m = inner === undefined ? a : Math.max(a, Math.min(b, inner));
  const pct = (v: number) => `${(v / span) * 100}%`;
  return (
    <div
      className={cx("fd-bar", size === "sm" && "fd-bar-sm")}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      style={place(where)}
    >
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

/** A section that opens. inline: a small one inside running text. Leave open undefined to let the reader toggle it. */
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

/** A row that can be chosen: a conversation, a settings section, a domain. Extra controls go in as children. */
export function ListItem({
  title,
  meta,
  index,
  trailing,
  selected,
  href,
  mono,
  children,
}: {
  title: ReactNode;
  meta?: ReactNode;
  index?: ReactNode;
  trailing?: ReactNode;
  selected?: boolean;
  href: string;
  /** Machine names (domains, sections) in mono. */
  mono?: boolean;
  children?: ReactNode;
}) {
  return (
    <a className={cx("fd-item", mono && "fd-item-mono")} href={href} aria-current={selected ? "true" : undefined}>
      {index && <span className="fd-item-index">{index}</span>}
      <span className="fd-item-body">
        <span className="fd-item-title">{title}</span>
        {meta && <span className="fd-meta">{meta}</span>}
      </span>
      {trailing && <span className="fd-meta">{trailing}</span>}
      {children}
    </a>
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

/** One turn of a conversation. who "you" is the owner; anything else is the speaker's name. */
export function Message({ who, meta, children }: { who: string; meta?: ReactNode; children: ReactNode }) {
  const you = who === "you";
  return (
    <div className={cx("fd-msg", you && "fd-msg-you")}>
      <div className="fd-msg-who">
        {you ? <span className="fd-meta">you</span> : <Status tone="ok">{who}</Status>}
        {meta && <span className="fd-meta">{meta}</span>}
      </div>
      <div className="fd-msg-body">{children}</div>
    </div>
  );
}

/** Something that waits for the owner's yes: the hazard stripe, what will happen, and the buttons (children). */
export function Approval({
  title,
  command,
  note,
  inset,
  children,
  ...where
}: { title: ReactNode; command?: ReactNode; note?: ReactNode; inset?: boolean; children: ReactNode } & Place) {
  return (
    <section className={cx("fd-panel", inset && "fd-panel-inset")} style={place(where)}>
      <Hazard />
      <div className="fd-approval-body">
        <strong>{title}</strong>
        {command && <Code wrap>{command}</Code>}
        {note && <p>{note}</p>}
        <div className="fd-approval-actions">{children}</div>
      </div>
    </section>
  );
}

/** A table that scrolls inside itself on a narrow screen. Rows are plain tr/td; a chosen row takes aria-selected. */
export function Table({ dense, children }: { dense?: boolean; children: ReactNode }) {
  return (
    <div className="fd-table-wrap">
      <table className={cx("fd-table", dense && "fd-table-dense")}>{children}</table>
    </div>
  );
}

/** Two lines in one table cell. */
export function Stack({ children }: { children: ReactNode }) {
  return <div className="fd-stack">{children}</div>;
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

/** Text helpers: the few cases where prose needs a kit style without a component of its own. */
export function Meta({ children, tone }: { children: ReactNode; tone?: Tone }) {
  return <span className={cx("fd-meta", toneText(tone))}>{children}</span>;
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
