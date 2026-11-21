// FINDING (test environment): DOMPurify does not sanitise under happy-dom 21 as it is. DOMPurify reads node names
// through Node.prototype.nodeName, which happy-dom answers with "" for elements (it defines nodeName on the subclasses),
// so every tag counts as unknown; and happy-dom's NodeIterator loses its place when a node is removed, so the walk
// ends after the first removal and later nodes (a script, an onerror) stay. Both are repaired here before DOMPurify
// is imported (vi.hoisted runs first), so that these tests check the sanitiser and not the fake DOM.
vi.hoisted(() => {
  const original = Object.getOwnPropertyDescriptor(Node.prototype, "nodeName")!;
  Object.defineProperty(Node.prototype, "nodeName", {
    configurable: true,
    get(this: Node) {
      let proto = Object.getPrototypeOf(this);
      while (proto && proto !== Node.prototype) {
        const own = Object.getOwnPropertyDescriptor(proto, "nodeName");
        if (own?.get) return own.get.call(this);
        proto = Object.getPrototypeOf(proto);
      }
      return original.get!.call(this);
    },
  });
  const iterator = Object.getPrototypeOf(document.createNodeIterator(document.body));
  const state = new WeakMap<object, { current: Node | null; anchor: Node | null; started: boolean }>();
  const following = (node: Node, root: Node): Node | null => {
    if (node.firstChild) return node.firstChild;
    let at: Node | null = node;
    while (at && at !== root) {
      if (at.nextSibling) return at.nextSibling;
      at = at.parentNode;
    }
    return null;
  };
  const preceding = (node: Node, root: Node): Node | null => {
    if (node === root) return null;
    let prev = node.previousSibling;
    if (!prev) return node.parentNode;
    while (prev.lastChild) prev = prev.lastChild;
    return prev;
  };
  // pre-order walk that survives the removal of the node it just returned (what the DOM standard specifies)
  iterator.nextNode = function (this: { root: Node }) {
    const s = state.get(this) ?? { current: null, anchor: null, started: false };
    state.set(this, s);
    let next: Node | null;
    if (!s.started) {
      s.started = true;
      next = this.root;
    } else if (!s.current) next = null;
    else if (this.root.contains(s.current)) next = following(s.current, this.root);
    else next = s.anchor ? following(s.anchor, this.root) : null;
    s.current = next;
    s.anchor = next ? preceding(next, this.root) : null;
    return next;
  };
});

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RunnableMarkdown } from "../../../src/components/RunnableMarkdown";
import { commandHtml, iconButtonHtml, Prose, setIconButton } from "../../../src/kit";
import { Markdown } from "../../../src/ui";

/** The HTML of a helper, parsed, so the tests read attributes and not strings. */
function parse(html: string): HTMLElement {
  const holder = document.createElement("div");
  holder.innerHTML = html;
  return holder;
}

describe("iconButtonHtml", () => {
  it("is an icon button: type button, the kit's classes, the label as its name, an icon slot", () => {
    const button = parse(iconButtonHtml({ icon: "copy", label: "copy" })).querySelector("button")!;
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveClass("fd-btn", "fd-btn-icon", "fd-btn-small");
    expect(button).not.toHaveClass("fd-btn-warn");
    expect(button).toHaveAttribute("aria-label", "copy");
    expect(button.querySelector(".fd-icon-slot")).toHaveAttribute("data-icon", "copy");
  });

  it("the hint defaults to the label", () => {
    const button = parse(iconButtonHtml({ icon: "run", label: "run" })).querySelector("button")!;
    expect(button).toHaveAttribute("data-hint", "run");
  });

  it("an own hint is kept apart from the name", () => {
    const button = parse(iconButtonHtml({ icon: "run", label: "run", hint: "Run (read-only)" })).querySelector("button")!;
    expect(button).toHaveAttribute("aria-label", "run");
    expect(button).toHaveAttribute("data-hint", "Run (read-only)");
  });

  it("warn adds the warn look", () => {
    const button = parse(iconButtonHtml({ icon: "run", label: "run", warn: true })).querySelector("button")!;
    expect(button).toHaveClass("fd-btn-warn");
  });

  it("data becomes data-* attributes a click handler can read", () => {
    const button = parse(iconButtonHtml({ icon: "run", label: "run", data: { run: "3", label: "run now" } })).querySelector("button")!;
    expect(button.dataset.run).toBe("3");
    expect(button.dataset.label).toBe("run now");
  });

  it("text with quotes, angle brackets and ampersands cannot break out of the attributes", () => {
    const evil = `x" onclick="alert(1)" y="<b>&`;
    const holder = parse(iconButtonHtml({ icon: "copy", label: evil, hint: evil, data: { copy: evil } }));
    const buttons = holder.querySelectorAll("button");
    expect(buttons).toHaveLength(1);
    const button = buttons[0];
    expect(button.getAttribute("aria-label")).toBe(evil);
    expect(button.getAttribute("data-hint")).toBe(evil);
    expect(button.dataset.copy).toBe(evil);
    expect(button.hasAttribute("onclick")).toBe(false);
    expect(holder.querySelector("b")).toBeNull();
  });
});

describe("commandHtml", () => {
  it("is the markup of the kit's Command: a pre with the actions first and then the code", () => {
    const holder = parse(commandHtml("ls -la", iconButtonHtml({ icon: "copy", label: "copy" })));
    const command = holder.firstElementChild as HTMLElement;
    expect(command).toHaveClass("fd-command");
    const pre = command.querySelector("pre")!;
    expect(pre).toHaveClass("fd-code", "fd-code-wrap");
    expect(pre.firstElementChild).toHaveClass("fd-command-actions");
    expect(pre.firstElementChild!.querySelector("button")).toHaveAttribute("aria-label", "copy");
    expect(pre.textContent).toBe("ls -la");
  });

  it("takes the code as it is: the caller has escaped it", () => {
    const holder = parse(commandHtml("echo &lt;b&gt;", ""));
    expect(holder.querySelector("pre")!.textContent).toBe("echo <b>");
    expect(holder.querySelector("b")).toBeNull();
  });
});

describe("Prose", () => {
  it("shows the HTML it is given inside the kit's text block", () => {
    const { container } = render(<Prose html="<p>Hello <strong>world</strong></p>" />);
    expect(container.firstElementChild).toHaveClass("fd-md");
    expect(screen.getByText("world", { selector: "strong" })).toBeInTheDocument();
  });

  it("onClick hears clicks on what is inside", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Prose html={'<p>text <a href="#/x">a link</a></p>'} onClick={onClick} />);
    await user.click(screen.getByText("a link"));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect((onClick.mock.calls[0][0].target as HTMLElement).textContent).toBe("a link");
  });

  it("an icon slot gets the icon: an svg that uses the Shell's symbol, hidden from readers", () => {
    render(<Prose html={iconButtonHtml({ icon: "run", label: "run" })} />);
    const button = screen.getByRole("button", { name: "run" });
    const svg = button.querySelector("svg")!;
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg.querySelector("use")).toHaveAttribute("href", "#fd-icon-run");
  });

  it("a button whose hint is its name gets a hint without a role and no description", () => {
    render(<Prose html={iconButtonHtml({ icon: "copy", label: "copy" })} />);
    const button = screen.getByRole("button", { name: "copy" });
    const hint = document.getElementById(button.getAttribute("interestfor")!)!;
    expect(hint).toHaveClass("fd-hint");
    expect(hint).toHaveAttribute("popover", "hint");
    expect(hint).toHaveTextContent("copy");
    expect(hint).not.toHaveAttribute("role");
    expect(button).not.toHaveAttribute("aria-describedby");
    // the hint follows its button
    expect(button.nextElementSibling).toBe(hint);
  });

  it("a hint that says more than the name is a tooltip and describes the button", () => {
    render(<Prose html={iconButtonHtml({ icon: "run", label: "run", hint: "Run (read-only)" })} />);
    const button = screen.getByRole("button", { name: "run" });
    const hint = document.getElementById(button.getAttribute("interestfor")!)!;
    expect(hint).toHaveTextContent("Run (read-only)");
    expect(hint).toHaveAttribute("role", "tooltip");
    expect(button).toHaveAttribute("aria-describedby", hint.id);
    expect(button).toHaveAccessibleDescription("Run (read-only)");
  });

  it("a hint that differs from the name only in case adds no description", () => {
    render(<Prose html={iconButtonHtml({ icon: "copy", label: "copy", hint: "Copy" })} />);
    const button = screen.getByRole("button", { name: "copy" });
    expect(button).not.toHaveAttribute("aria-describedby");
    expect(document.getElementById(button.getAttribute("interestfor")!)).toHaveTextContent("Copy");
  });

  it("each button has a hint of its own with its own id", () => {
    render(<Prose html={iconButtonHtml({ icon: "copy", label: "copy" }) + iconButtonHtml({ icon: "run", label: "run", hint: "Run" })} />);
    const ids = screen.getAllByRole("button").map((b) => b.getAttribute("interestfor"));
    expect(new Set(ids).size).toBe(2);
    expect(document.querySelectorAll(".fd-hint")).toHaveLength(2);
  });

  it("two Proses on a page do not share hint ids", () => {
    render(
      <>
        <Prose html={iconButtonHtml({ icon: "copy", label: "copy" })} />
        <Prose html={iconButtonHtml({ icon: "copy", label: "copy" })} />
      </>,
    );
    const ids = screen.getAllByRole("button", { name: "copy" }).map((b) => b.getAttribute("interestfor"));
    expect(ids[0]).not.toBe(ids[1]);
  });

  it("new HTML replaces the old one with fresh icons and hints, without leftovers", () => {
    const { rerender } = render(<Prose html={iconButtonHtml({ icon: "copy", label: "copy" })} />);
    rerender(<Prose html={iconButtonHtml({ icon: "run", label: "run", hint: "Run it" })} />);
    expect(screen.queryByRole("button", { name: "copy" })).toBeNull();
    const button = screen.getByRole("button", { name: "run" });
    expect(button.querySelector("use")).toHaveAttribute("href", "#fd-icon-run");
    expect(document.querySelectorAll(".fd-hint")).toHaveLength(1);
    expect(button).toHaveAccessibleDescription("Run it");
  });

  it("buttons without a data-hint are left alone", () => {
    render(<Prose html={'<button type="button">plain</button>'} />);
    expect(screen.getByRole("button", { name: "plain" })).not.toHaveAttribute("interestfor");
    expect(document.querySelector(".fd-hint")).toBeNull();
  });
});

describe("setIconButton", () => {
  it("changes the name and the icon of a button written by iconButtonHtml", () => {
    render(<Prose html={iconButtonHtml({ icon: "copy", label: "copy" })} />);
    const button = screen.getByRole("button", { name: "copy" });
    setIconButton(button, "copied", "copied");
    expect(button).toHaveAccessibleName("copied");
    expect(button.querySelector("use")).toHaveAttribute("href", "#fd-icon-copied");
    setIconButton(button, "copy", "copy");
    expect(button).toHaveAccessibleName("copy");
    expect(button.querySelector("use")).toHaveAttribute("href", "#fd-icon-copy");
  });

  it("the icon stays hidden from readers", () => {
    render(<Prose html={iconButtonHtml({ icon: "run", label: "run" })} />);
    const button = screen.getByRole("button", { name: "run" });
    setIconButton(button, "running", "running…");
    expect(button.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("a button without an icon slot only gets the new name", () => {
    const button = document.createElement("button");
    button.textContent = "x";
    setIconButton(button, "failed", "failed");
    expect(button).toHaveAttribute("aria-label", "failed");
    expect(button.querySelector("svg")).toBeNull();
  });
});

describe("Markdown (sanitised)", () => {
  it("renders the usual Markdown: headings, emphasis, lists, links, code, tables, strikethrough", () => {
    render(
      <Markdown
        text={["# Title", "", "Some *emphasis* and **bold** and ~~gone~~.", "", "- one", "- two", "", "[docs](https://example.test/docs)", "", "`inline`", "", "| a | b |", "|---|---|", "| 1 | 2 |"].join("\n")}
      />,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Title" })).toBeInTheDocument();
    expect(screen.getByText("emphasis", { selector: "em" })).toBeInTheDocument();
    expect(screen.getByText("bold", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("gone", { selector: "del" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute("href", "https://example.test/docs");
    expect(screen.getByText("inline", { selector: "code" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "a" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "2" })).toBeInTheDocument();
  });

  it("drops a script element, text and all effect", () => {
    const { container } = render(<Markdown text={'Before\n\n<script>window.__pwned = true</script>\n\nAfter'} />);
    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
    expect(screen.getByText("Before")).toBeInTheDocument();
    expect(screen.getByText("After")).toBeInTheDocument();
  });

  it("drops an onerror attribute and any other event handler", () => {
    const { container } = render(
      <Markdown text={'<img src="x" onerror="window.__pwned = true">\n\n<p onclick="alert(1)" onmouseover="alert(2)">hover</p>'} />,
    );
    const everything = [...container.querySelectorAll("*")];
    for (const element of everything) {
      for (const name of element.getAttributeNames()) expect(name.startsWith("on")).toBe(false);
    }
    expect(screen.getByText("hover")).toBeInTheDocument();
  });

  it("drops a javascript: link target but keeps the link text", () => {
    render(<Markdown text={"[click me](javascript:alert(1))"} />);
    const link = screen.queryByRole("link", { name: "click me" });
    // the text stays, and nothing that runs script is left on it
    expect(screen.getByText("click me")).toBeInTheDocument();
    if (link) expect(link.getAttribute("href") ?? "").not.toMatch(/^\s*javascript:/i);
  });

  it("drops objects and embeds", () => {
    const { container } = render(<Markdown text={'<object data="x"></object>\n\n<embed src="x">\n\nkept'} />);
    expect(container.querySelector("object, embed")).toBeNull();
    expect(screen.getByText("kept")).toBeInTheDocument();
  });

  it("a button an attacker writes into the text gets no hint or icon from Prose unless it asks with data-hint", () => {
    const { container } = render(<Markdown text={'<button onclick="alert(1)">fake</button>'} />);
    const button = container.querySelector("button");
    if (button) expect(button.hasAttribute("onclick")).toBe(false);
    expect(container.querySelector(".fd-hint")).toBeNull();
  });
});

describe("RunnableMarkdown (sanitised)", () => {
  it("drops script, event handlers and javascript: targets, and keeps the safe text", () => {
    const text = ["Safe text.", "", "<script>window.__pwned = true</script>", "", '<img src="x" onerror="window.__pwned = true">', "", "[bad](javascript:alert(1))"].join("\n");
    const { container } = render(<RunnableMarkdown text={text} onRun={async () => {}} />);
    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
    for (const element of container.querySelectorAll("*")) {
      for (const name of element.getAttributeNames()) expect(name.startsWith("on")).toBe(false);
    }
    for (const link of container.querySelectorAll("a")) expect(link.getAttribute("href") ?? "").not.toMatch(/^\s*javascript:/i);
    expect(screen.getByText("Safe text.")).toBeInTheDocument();
  });

  it("code in a block is shown as text: tags in it are not markup", () => {
    const text = "```bash\necho '<img src=x onerror=alert(1)>'\n```";
    const { container } = render(<RunnableMarkdown text={text} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("pre")).toHaveTextContent("echo '<img src=x onerror=alert(1)>'");
  });

  it("its buttons are named copy and run, with the kit's hints, and the run button does not exist without onRun", () => {
    const text = "```bash\nls\n```";
    const { rerender } = render(<RunnableMarkdown text={text} />);
    expect(screen.getByRole("button", { name: "copy" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "run" })).toBeNull();
    rerender(<RunnableMarkdown text={text} onRun={async () => {}} />);
    const run = screen.getByRole("button", { name: "run (may change the server)" });
    expect(run).toHaveAccessibleDescription("Run as root (may change the server)");
    expect(run).toHaveClass("fd-btn-warn");
  });

  it("clicking copy writes the text to the clipboard and shows 'copied' for a moment", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const writeText = vi.fn(async () => {});
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
      render(<RunnableMarkdown text={"```text\nplain output\n```"} />);
      await user.click(screen.getByRole("button", { name: "copy" }));
      expect(writeText).toHaveBeenCalledWith("plain output");
      expect(await screen.findByRole("button", { name: "copied" })).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(2000);
      expect(screen.getByRole("button", { name: "copy" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("clicking run calls onRun with the command and disables the button while it runs", async () => {
    const user = userEvent.setup();
    let finish: () => void = () => {};
    const onRun = vi.fn((_command: string) => new Promise<void>((resolve) => (finish = resolve)));
    render(<RunnableMarkdown text={"```bash\n# [read-only] look\nls\n```"} onRun={onRun} />);
    await user.click(screen.getByRole("button", { name: "run" }));
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onRun.mock.calls[0][0]).toContain("ls");
    const busy = await screen.findByRole("button", { name: "running…" });
    expect(busy).toBeDisabled();
    // a second click while it runs does nothing
    fireEvent.click(busy);
    expect(onRun).toHaveBeenCalledTimes(1);
    finish();
    const back = await screen.findByRole("button", { name: "run" });
    expect(back).toBeEnabled();
  });
});
