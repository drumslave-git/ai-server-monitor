import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { commandGroups, commandMode, RunnableMarkdown } from "../../../src/components/RunnableMarkdown";

afterEach(() => vi.unstubAllGlobals());

/** The clipboard the test reads: set after userEvent.setup(), which installs its own. */
function clipboard(write: (text: string) => Promise<void> = async () => {}) {
  const writeText = vi.fn(write);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  return writeText;
}

const block = (lang: string, body: string) => "```" + lang + "\n" + body + "\n```";

describe("commandGroups", () => {
  it("splits a block at blank lines", () => {
    expect(commandGroups("ls\n\npwd")).toEqual(["ls", "pwd"]);
  });

  it("splits before a comment that follows a command, and keeps leading comments with their command", () => {
    expect(commandGroups("# first\nls\n# second\npwd")).toEqual(["# first\nls", "# second\npwd"]);
  });

  it("keeps a command that continues on the next line (trailing backslash) together, blank line included", () => {
    expect(commandGroups("docker run \\\n  --rm \\\n\n  test-image\nls")).toEqual(["docker run \\\n  --rm \\\n\n  test-image\nls"]);
  });

  it("ignores trailing whitespace and gives nothing for empty text", () => {
    expect(commandGroups("ls\n\n\n")).toEqual(["ls"]);
    expect(commandGroups("")).toEqual([]);
  });
});

describe("commandMode", () => {
  it("reads the mark above a command", () => {
    expect(commandMode("# [read-only] list\nls")).toBe("read-only");
    expect(commandMode("# [write] clean\nrm -rf /tmp/x")).toBe("write");
    expect(commandMode("ls")).toBe("unmarked");
  });

  it("any write mark wins over read-only marks, and a mark is case-insensitive", () => {
    expect(commandMode("# [read-only] a\nls\n# [write] b\nrm x")).toBe("write");
    expect(commandMode("# [READ-ONLY] a\nls\n# [read-only] b\npwd")).toBe("read-only");
  });
});

describe("RunnableMarkdown", () => {
  it("renders the Markdown", () => {
    render(<RunnableMarkdown text={"A **bold** word and `code`.\n\n- one\n- two"} />);
    expect(screen.getByText("bold", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("code", { selector: "code" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("gives every command of a shell block its own run button, named by its mark", () => {
    const text = block("bash", "# [read-only] look\nls\n\n# [write] change\nrm x\n\npwd");
    render(<RunnableMarkdown text={text} onRun={async () => {}} />);
    expect(screen.getByRole("button", { name: "run" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "run (changes the server)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "run (may change the server)" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "copy" })).toHaveLength(3);
  });

  it.each(["bash", "sh", "shell", "zsh", "console", "BASH"])("a %s block is runnable", (lang) => {
    render(<RunnableMarkdown text={block(lang, "ls")} onRun={async () => {}} />);
    expect(screen.getByRole("button", { name: /^run/ })).toBeInTheDocument();
  });

  it("clicking run sends that command, as written with its comment, to onRun", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn(async () => {});
    render(<RunnableMarkdown text={block("bash", "# [read-only] look\nls -la\n\ndf -h")} onRun={onRun} />);
    await user.click(screen.getByRole("button", { name: "run" }));
    expect(onRun).toHaveBeenCalledExactlyOnceWith("# [read-only] look\nls -la");
    await user.click(screen.getByRole("button", { name: "run (may change the server)" }));
    expect(onRun).toHaveBeenLastCalledWith("df -h");
  });

  it("in a console block only the $ lines run, without the prompt", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn(async () => {});
    render(<RunnableMarkdown text={block("console", "$ uptime\n 10:00 up 3 days\n$ whoami\nroot")} onRun={onRun} />);
    await user.click(screen.getByRole("button", { name: /^run/ }));
    expect(onRun).toHaveBeenCalledExactlyOnceWith("uptime\nwhoami");
  });

  it("shows the run button as running and refuses a second click until the command is done", async () => {
    const user = userEvent.setup();
    let finish!: () => void;
    const onRun = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    render(<RunnableMarkdown text={block("bash", "# [read-only] look\nls")} onRun={onRun} />);
    const button = screen.getByRole("button", { name: "run" });
    await user.click(button);
    expect(await screen.findByRole("button", { name: "running…" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "running…" }));
    expect(onRun).toHaveBeenCalledTimes(1);
    finish();
    const back = await screen.findByRole("button", { name: "run" });
    expect(back).toBeEnabled();
    await user.click(back);
    expect(onRun).toHaveBeenCalledTimes(2);
    finish();
  });

  it("has no run buttons without onRun (the answer is still written), but keeps copy buttons", () => {
    render(<RunnableMarkdown text={block("bash", "ls")} />);
    expect(screen.queryByRole("button", { name: /^run/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "copy" })).toBeInTheDocument();
  });

  it("a block of another language has a copy button but no run button, and its text is shown as text", () => {
    render(<RunnableMarkdown text={block("html", "<b>x</b> & <i>y</i>")} onRun={async () => {}} />);
    expect(screen.queryByRole("button", { name: /^run/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "copy" })).toBeInTheDocument();
    expect(screen.getByText("<b>x</b> & <i>y</i>", { exact: false })).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });

  it("a block without a language is not runnable", () => {
    render(<RunnableMarkdown text={"```\nls\n```"} onRun={async () => {}} />);
    expect(screen.queryByRole("button", { name: /^run/ })).not.toBeInTheDocument();
  });
});

describe("copy buttons", () => {
  it("copy the command exactly as run would send it, then say copied and go back", async () => {
    const user = userEvent.setup();
    const writeText = clipboard();
    render(<RunnableMarkdown text={block("console", "$ uptime\n 10:00 up 3 days")} onRun={async () => {}} />);
    await user.click(screen.getByRole("button", { name: "copy" }));
    expect(writeText).toHaveBeenCalledExactlyOnceWith("uptime");
    expect(await screen.findByRole("button", { name: "copied" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "copy" })).toBeInTheDocument(), { timeout: 3000 });
  });

  it("copy a block of another language as its whole text", async () => {
    const user = userEvent.setup();
    const writeText = clipboard();
    render(<RunnableMarkdown text={block("json", '{"free": "40 GB"}')} />);
    await user.click(screen.getByRole("button", { name: "copy" }));
    expect(writeText).toHaveBeenCalledExactlyOnceWith('{"free": "40 GB"}');
  });

  it("each block copies its own text", async () => {
    const user = userEvent.setup();
    const writeText = clipboard();
    render(<RunnableMarkdown text={[block("bash", "ls\n\npwd"), block("json", "{}")].join("\n\n")} />);
    const copies = screen.getAllByRole("button", { name: "copy" });
    expect(copies).toHaveLength(3);
    await user.click(copies[1]);
    await user.click(copies[2]);
    expect(writeText.mock.calls.map((c) => c[0])).toEqual(["pwd", "{}"]);
  });

  it("says failed when the browser refuses the copy", async () => {
    const user = userEvent.setup();
    clipboard(async () => {
      throw new Error("denied");
    });
    render(<RunnableMarkdown text={block("bash", "ls")} />);
    await user.click(screen.getByRole("button", { name: "copy" }));
    expect(await screen.findByRole("button", { name: "failed" })).toBeInTheDocument();
  });

  it("copying does not run the command", async () => {
    const user = userEvent.setup();
    clipboard();
    const onRun = vi.fn(async () => {});
    render(<RunnableMarkdown text={block("bash", "ls")} onRun={onRun} />);
    await user.click(screen.getByRole("button", { name: "copy" }));
    expect(onRun).not.toHaveBeenCalled();
  });
});
