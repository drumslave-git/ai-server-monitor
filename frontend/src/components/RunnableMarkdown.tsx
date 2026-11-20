import DOMPurify from "dompurify";
import { Marked, type Tokens } from "marked";
import { useMemo, type MouseEvent } from "react";

/** Code blocks with these tags get run buttons. */
const SHELLS = new Set(["bash", "sh", "shell", "zsh", "console"]);

const escape = (text: string) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** The commands of a shell block: groups of lines split at blank lines, and before a `#`
 *  comment that follows a command (its leading comments belong to the command). A line
 *  ending in `\` continues the command. */
export function commandGroups(text: string): string[] {
  const groups: string[][] = [];
  let current: string[] = [];
  let continued = false;
  const isComment = (line: string) => /^\s*#/.test(line);
  for (const line of text.replace(/\s+$/, "").split("\n")) {
    if (!continued && !line.trim()) {
      if (current.length) groups.push(current);
      current = [];
      continue;
    }
    if (!continued && isComment(line) && current.length && !isComment(current[current.length - 1])) {
      groups.push(current);
      current = [];
    }
    current.push(line);
    continued = /\\\s*$/.test(line);
  }
  if (current.length) groups.push(current);
  return groups.map((g) => g.join("\n"));
}

/** What runs for a group: the text as shown; in a `console` block only the `$ ` lines,
 *  without the prompt (the other lines are output). */
function runnable(group: string, lang: string): string {
  if (lang !== "console") return group;
  const lines = group.split("\n").filter((l) => /^\s*\$ /.test(l));
  return lines.length ? lines.map((l) => l.replace(/^\s*\$ /, "")).join("\n") : group;
}

/** The answer's mark on a command (chat prompt): `# [read-only] ...` or `# [write] ...`
 *  above it. The server reads the same mark: only read-only runs in the sandbox. */
const MARK = /^\s*#\s*\[(read-only|write)\]/gim;

export function commandMode(group: string): "read-only" | "write" | "unmarked" {
  const marks = new Set([...group.matchAll(MARK)].map((m) => m[1].toLowerCase()));
  if (marks.size === 1 && marks.has("read-only")) return "read-only";
  return marks.has("write") ? "write" : "unmarked";
}

const LABELS = {
  "read-only": "run",
  write: "run (changes the server)",
  unmarked: "run (may change the server)",
};
const TITLES = {
  "read-only": "Run this read-only command on the server; the result goes to the chat and the model answers it",
  write: "Run this command as root on the server (it changes the server); the result goes to the chat",
  unmarked: "Not marked read-only by the answer: it runs as root and may change the server",
};

/** One command in the markup of the kit's `Command`: its leading comment lines dim, the
 *  command, and (when it can be run) the button attached. */
function commandHtml(group: string, button: string): string {
  const lines = group.split("\n");
  let lead = 0;
  while (lead < lines.length && /^\s*#/.test(lines[lead])) lead += 1;
  const comment = lines.slice(0, lead).join("\n");
  const rest = lines.slice(lead).join("\n");
  const dim = comment ? `<span class="fd-dim">${escape(comment)}${rest ? "\n" : ""}</span>` : "";
  return `<div class="fd-command"><pre class="fd-code">${dim}${escape(rest)}</pre>${button}</div>`;
}

/** `runnableNow`: the commands get their run buttons (not while the answer is still written
 *  or another command runs). */
function render(text: string, runnableNow: boolean): { html: string; commands: string[] } {
  const commands: string[] = [];
  const marked = new Marked({
    gfm: true,
    renderer: {
      code({ text: code, lang }: Tokens.Code) {
        const tag = (lang ?? "").trim().split(/\s+/)[0].toLowerCase();
        if (!SHELLS.has(tag)) return false;
        const parts = commandGroups(code).map((group) => {
          const index = commands.push(runnable(group, tag)) - 1;
          const mode = commandMode(group);
          const button = runnableNow
            ? `<button type="button" class="fd-btn${mode === "read-only" ? "" : " fd-btn-warn"}" data-run="${index}" data-label="${LABELS[mode]}" title="${TITLES[mode]}">${LABELS[mode]}</button>`
            : "";
          return commandHtml(group, button);
        });
        // (the commands of one block stand under each other with the gap of the text around them)
        return `<div class="fd-md">${parts.join("")}</div>`;
      },
    },
  });
  const html = DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
  return { html, commands };
}

/** An answer's Markdown, with a run button on every command of its shell code blocks. */
export function RunnableMarkdown({ text, onRun }: { text: string; onRun?: (command: string) => Promise<void> }) {
  const canRun = Boolean(onRun);
  const { html, commands } = useMemo(() => render(text, canRun), [text, canRun]);
  const click = async (event: MouseEvent<HTMLDivElement>) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-run]");
    if (!button || !onRun || button.disabled) return;
    const command = commands[Number(button.dataset.run)];
    if (command === undefined) return;
    button.disabled = true;
    button.textContent = "running…";
    try {
      await onRun(command);
    } finally {
      button.disabled = false;
      button.textContent = button.dataset.label ?? "run";
    }
  };
  return <div className="fd-md" onClick={click} dangerouslySetInnerHTML={{ __html: html }} />;
}
