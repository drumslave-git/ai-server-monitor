import DOMPurify from "dompurify";
import { Marked, type Tokens } from "marked";
import { useMemo, type MouseEvent } from "react";

/** Code blocks with these tags get Run buttons. */
const SHELLS = new Set(["bash", "sh", "shell", "zsh", "console"]);
const PLAY =
  '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>';

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

function render(text: string): { html: string; commands: string[] } {
  const commands: string[] = [];
  const marked = new Marked({
    gfm: true,
    renderer: {
      code({ text: code, lang }: Tokens.Code) {
        const tag = (lang ?? "").trim().split(/\s+/)[0].toLowerCase();
        if (!SHELLS.has(tag)) return false;
        const parts = commandGroups(code).map((group) => {
          const index = commands.push(runnable(group, tag)) - 1;
          return (
            `<div class="run-cmd"><pre><code>${escape(group)}</code></pre>` +
            `<button type="button" class="btn btn-sm btn-quiet btn-run" data-run="${index}" ` +
            `title="Run this command on the server (read-only shell); the result is added to the chat">` +
            `${PLAY}<span>Run</span></button></div>`
          );
        });
        return `<div class="run-block">${parts.join("")}</div>`;
      },
    },
  });
  const html = DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
  return { html, commands };
}

/** An answer's Markdown, with a Run button on every command of its shell code blocks. */
export function RunnableMarkdown({ text, onRun }: { text: string; onRun?: (command: string) => Promise<void> }) {
  const { html, commands } = useMemo(() => render(text), [text]);
  const click = async (event: MouseEvent<HTMLDivElement>) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-run]");
    if (!button || !onRun || button.disabled) return;
    const command = commands[Number(button.dataset.run)];
    if (command === undefined) return;
    const label = button.querySelector("span");
    button.disabled = true;
    button.classList.add("running");
    if (label) label.textContent = "Running…";
    try {
      await onRun(command);
    } finally {
      button.disabled = false;
      button.classList.remove("running");
      if (label) label.textContent = "Run";
    }
  };
  return <div className={`markdown${onRun ? "" : " no-run"}`} onClick={click} dangerouslySetInnerHTML={{ __html: html }} />;
}
