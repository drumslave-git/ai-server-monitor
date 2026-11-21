import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  Button,
  Card,
  Cards,
  Clamp,
  Composer,
  IconButton,
  List,
  ListItem,
  Picker,
  SortableList,
  Status,
  type PickerGroup,
  type SortableEntry,
} from "../../../src/kit";

describe("List and ListItem", () => {
  it("a List with a label is a navigation landmark of that name; without one it is not", () => {
    const { rerender } = render(
      <List label="chats">
        <ListItem title="Chat one" href="#/chat/1" />
      </List>,
    );
    expect(screen.getByRole("navigation", { name: "chats" })).toBeInTheDocument();
    rerender(
      <List>
        <ListItem title="Chat one" href="#/chat/1" />
      </List>,
    );
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("with href the row is a link to it, named by its title", () => {
    render(
      <List>
        <ListItem title="Example job" href="#/jobs/example" meta="every hour" />
      </List>,
    );
    const link = screen.getByRole("link", { name: /Example job/ });
    expect(link).toHaveAttribute("href", "#/jobs/example");
    expect(link).toHaveTextContent("every hour");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("selected marks the link as current", () => {
    render(
      <List>
        <ListItem title="One" href="#/a" selected />
        <ListItem title="Two" href="#/b" />
      </List>,
    );
    expect(screen.getByRole("link", { name: /One/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("link", { name: /Two/ })).not.toHaveAttribute("aria-current");
  });

  it("with onClick alone it is a button; expanded is aria-expanded", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const { rerender } = render(<ListItem title="Domain" onClick={onClick} expanded={false} />);
    const button = screen.getByRole("button", { name: /Domain/ });
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveAttribute("aria-expanded", "false");
    await user.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
    rerender(<ListItem title="Domain" onClick={onClick} expanded />);
    expect(screen.getByRole("button", { name: /Domain/ })).toHaveAttribute("aria-expanded", "true");
  });

  it("a button row that does not open anything has no aria-expanded", () => {
    render(<ListItem title="Pick me" onClick={() => {}} />);
    expect(screen.getByRole("button", { name: /Pick me/ })).not.toHaveAttribute("aria-expanded");
  });

  it("a link row calls onClick too (the page may take the click over)", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn((e: React.MouseEvent) => e.preventDefault());
    render(<ListItem title="Chat" href="#/chat/1" onClick={onClick} />);
    await user.click(screen.getByRole("link", { name: /Chat/ }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("an href of empty string is still an anchor, not a button", () => {
    const { container } = render(<ListItem title="Home" href="" />);
    expect(container.querySelector("a.fd-item-main")).not.toBeNull();
    expect(container.querySelector("button.fd-item-main")).toBeNull();
  });

  it("selected and expanded rows share the chosen look", () => {
    const { container } = render(
      <>
        <ListItem title="a" href="#/a" selected />
        <ListItem title="b" onClick={() => {}} expanded />
        <ListItem title="c" href="#/c" />
      </>,
    );
    const items = [...container.querySelectorAll(".fd-item")];
    expect(items.map((i) => i.classList.contains("fd-item-on"))).toEqual([true, true, false]);
  });

  it("shows the index, the icon, the trailing text and the marks inside the row", () => {
    const { container } = render(
      <ListItem title="General" href="#/settings/general" index="01" icon="settings" trailing="3 jobs">
        <Status tone="ok">on</Status>
      </ListItem>,
    );
    const link = screen.getByRole("link", { name: /General/ });
    expect(within(link).getByText("01")).toHaveClass("fd-item-index");
    expect(container.querySelector(".fd-item-icon svg")).not.toBeNull();
    expect(within(link).getByText("3 jobs")).toBeInTheDocument();
    expect(within(link).getByText("on")).toBeInTheDocument();
  });

  it("mono is marked", () => {
    const { container } = render(<ListItem title="docker" href="#/d" mono />);
    expect(container.querySelector(".fd-item")).toHaveClass("fd-item-mono");
  });

  it("action is a control of its own beside the row, and clicking it does not click the row", async () => {
    const user = userEvent.setup();
    const onRow = vi.fn();
    const onDelete = vi.fn();
    render(<ListItem title="Chat" onClick={onRow} action={<IconButton icon="remove" label="delete chat" small onClick={onDelete} />} />);
    await user.click(screen.getByRole("button", { name: "delete chat" }));
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(onRow).not.toHaveBeenCalled();
    // the action is not inside the row's own button
    expect(screen.getByRole("button", { name: /^Chat$/ })).not.toContainElement(screen.getByRole("button", { name: "delete chat" }));
  });

  it("without action there is no action box", () => {
    const { container } = render(<ListItem title="Chat" href="#/c" />);
    expect(container.querySelector(".fd-item-action")).toBeNull();
  });
});

describe("Card", () => {
  const scrollIntoView = vi.fn();
  beforeEach(() => {
    scrollIntoView.mockReset();
    vi.stubGlobal("Element", Element);
    Element.prototype.scrollIntoView = scrollIntoView;
  });
  afterEach(() => {
    // @ts-expect-error removing the stub again
    delete Element.prototype.scrollIntoView;
  });

  const body = () => screen.getByText("the form").parentElement as HTMLElement;

  it("is closed by default: the toggle says so and the body is hidden", () => {
    render(
      <Card title="test-main">
        <p>the form</p>
      </Card>,
    );
    const toggle = screen.getByRole("button", { name: "test-main" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(body()).toHaveAttribute("hidden", "until-found");
    expect(toggle.getAttribute("aria-controls")).toBe(body().id);
  });

  it("the toggle opens and closes the card", async () => {
    const user = userEvent.setup();
    render(
      <Card title="test-main">
        <p>the form</p>
      </Card>,
    );
    const toggle = screen.getByRole("button", { name: "test-main" });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(body()).not.toHaveAttribute("hidden");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(body()).toHaveAttribute("hidden", "until-found");
  });

  it("the title is a heading holding the toggle", () => {
    render(
      <Card title="test-main">
        <p>x</p>
      </Card>,
    );
    expect(screen.getByRole("heading", { level: 3 })).toContainElement(screen.getByRole("button", { name: "test-main" }));
  });

  it("defaultOpen starts it open", () => {
    render(
      <Card title="a" defaultOpen>
        <p>the form</p>
      </Card>,
    );
    expect(screen.getByRole("button", { name: "a" })).toHaveAttribute("aria-expanded", "true");
    expect(body()).not.toHaveAttribute("hidden");
  });

  it("reveal opens it, also when it turns true later; turning it off does not close it", () => {
    const { rerender } = render(
      <Card title="a" reveal={false}>
        <p>the form</p>
      </Card>,
    );
    expect(screen.getByRole("button", { name: "a" })).toHaveAttribute("aria-expanded", "false");
    rerender(
      <Card title="a" reveal>
        <p>the form</p>
      </Card>,
    );
    expect(screen.getByRole("button", { name: "a" })).toHaveAttribute("aria-expanded", "true");
    rerender(
      <Card title="a" reveal={false}>
        <p>the form</p>
      </Card>,
    );
    expect(screen.getByRole("button", { name: "a" })).toHaveAttribute("aria-expanded", "true");
  });

  it("focus opens it and scrolls it into view", () => {
    render(
      <Card title="a" focus>
        <p>the form</p>
      </Card>,
    );
    expect(screen.getByRole("button", { name: "a" })).toHaveAttribute("aria-expanded", "true");
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
  });

  it("without focus it does not scroll", () => {
    render(
      <Card title="a">
        <p>the form</p>
      </Card>,
    );
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("a find-in-page match inside the closed body opens the card (beforematch)", () => {
    render(
      <Card title="a">
        <p>the form</p>
      </Card>,
    );
    fireEvent(body(), new Event("beforematch"));
    expect(screen.getByRole("button", { name: "a" })).toHaveAttribute("aria-expanded", "true");
    expect(body()).not.toHaveAttribute("hidden");
  });

  it("status, meta, aside and below are shown whether the card is open or closed", () => {
    render(
      <Card title="a" status={<Status tone="ok">on</Status>} meta="3 tools" aside={<Button>test</Button>} below={<p>test passed</p>}>
        <p>the form</p>
      </Card>,
    );
    expect(screen.getByText("on")).toBeInTheDocument();
    expect(screen.getByText("3 tools")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "test" })).toBeInTheDocument();
    expect(screen.getByText("test passed")).toBeInTheDocument();
    // none of them is inside the closed body
    expect(body()).not.toContainElement(screen.getByRole("button", { name: "test" }));
    expect(body()).not.toContainElement(screen.getByText("test passed"));
  });

  it("the aside buttons work without toggling the card", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Card title="a" aside={<Button onClick={onClick}>test</Button>}>
        <p>the form</p>
      </Card>,
    );
    await user.click(screen.getByRole("button", { name: "test" }));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "a" })).toHaveAttribute("aria-expanded", "false");
  });

  it("each card of Cards opens on its own", async () => {
    const user = userEvent.setup();
    render(
      <Cards>
        <Card title="one">
          <p>form one</p>
        </Card>
        <Card title="two">
          <p>form two</p>
        </Card>
      </Cards>,
    );
    await user.click(screen.getByRole("button", { name: "two" }));
    expect(screen.getByRole("button", { name: "two" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "one" })).toHaveAttribute("aria-expanded", "false");
  });

  it("the form inside a closed card keeps its state while it is closed", async () => {
    const user = userEvent.setup();
    render(
      <Card title="a" defaultOpen>
        <input aria-label="name" defaultValue="" />
      </Card>,
    );
    await user.type(screen.getByLabelText("name", { selector: "input" }), "host-a");
    await user.click(screen.getByRole("button", { name: "a" }));
    await user.click(screen.getByRole("button", { name: "a" }));
    expect(screen.getByLabelText("name")).toHaveValue("host-a");
  });
});

describe("Composer", () => {
  // happy-dom has no layout and no ResizeObserver
  beforeEach(() => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
        unobserve() {}
      },
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    // @ts-expect-error removing the layout stub again
    delete HTMLTextAreaElement.prototype.scrollHeight;
    // @ts-expect-error removing the layout stub again
    delete HTMLTextAreaElement.prototype.clientHeight;
  });

  it("is a form with a one-line textarea, the leading buttons and the actions", () => {
    const { container } = render(
      <Composer aria-label="message" value="" onChange={() => {}} leading={<Button>attach</Button>} actions={<Button variant="primary">send</Button>} />,
    );
    expect(container.querySelector("form")).toHaveClass("fd-composer");
    const text = screen.getByRole("textbox", { name: "message" });
    expect(text).toHaveAttribute("rows", "1");
    expect(screen.getByRole("button", { name: "attach" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "send" })).toBeInTheDocument();
  });

  it("typing calls onChange with the text", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    function Parent() {
      const [value, setValue] = useState("");
      return (
        <Composer
          aria-label="message"
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            onChange(e.target.value);
          }}
          actions={<Button>send</Button>}
        />
      );
    }
    render(<Parent />);
    await user.type(screen.getByRole("textbox", { name: "message" }), "hello");
    expect(onChange).toHaveBeenLastCalledWith("hello");
    expect(screen.getByRole("textbox", { name: "message" })).toHaveValue("hello");
  });

  it("the send button submits the form", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(<Composer aria-label="message" value="hi" onChange={() => {}} onSubmit={onSubmit} actions={<Button type="submit">send</Button>} />);
    await user.click(screen.getByRole("button", { name: "send" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("the page handles the keys: onKeyDown is the textarea's", async () => {
    const user = userEvent.setup();
    const onKeyDown = vi.fn();
    render(<Composer aria-label="message" value="" onChange={() => {}} onKeyDown={onKeyDown} actions={<Button>send</Button>} />);
    await user.type(screen.getByRole("textbox", { name: "message" }), "{Enter}");
    expect(onKeyDown).toHaveBeenCalled();
    expect(onKeyDown.mock.calls[0][0].key).toBe("Enter");
  });

  it("a short text has no expand button", () => {
    render(<Composer aria-label="message" value="hi" onChange={() => {}} actions={<Button>send</Button>} />);
    expect(screen.queryByRole("button", { name: "expand" })).toBeNull();
  });

  it("a text that scrolls gets an expand button that expands and collapses the box", async () => {
    const user = userEvent.setup();
    Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", { configurable: true, get: () => 500 });
    Object.defineProperty(HTMLTextAreaElement.prototype, "clientHeight", { configurable: true, get: () => 100 });
    const { container } = render(<Composer aria-label="message" value={"long\n".repeat(30)} onChange={() => {}} actions={<Button>send</Button>} />);
    const expand = await screen.findByRole("button", { name: "expand" });
    expect(container.querySelector(".fd-composer-box")).toHaveClass("fd-composer-scrolls");
    expect(expand).toHaveAttribute("aria-expanded", "false");
    await user.click(expand);
    expect(container.querySelector("form")).toHaveClass("fd-composer-expanded");
    expect(screen.getByRole("button", { name: "collapse" })).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByRole("button", { name: "collapse" }));
    expect(container.querySelector("form")).not.toHaveClass("fd-composer-expanded");
    expect(screen.getByRole("button", { name: "expand" })).toBeInTheDocument();
  });

  it("the expand button does not submit the form", async () => {
    const user = userEvent.setup();
    Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", { configurable: true, get: () => 500 });
    Object.defineProperty(HTMLTextAreaElement.prototype, "clientHeight", { configurable: true, get: () => 100 });
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(<Composer aria-label="message" value="x" onChange={() => {}} onSubmit={onSubmit} actions={<Button>send</Button>} />);
    await user.click(await screen.findByRole("button", { name: "expand" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("sending (the text becomes empty) takes the expanded box back to one line", async () => {
    const user = userEvent.setup();
    Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", { configurable: true, get: () => 500 });
    Object.defineProperty(HTMLTextAreaElement.prototype, "clientHeight", { configurable: true, get: () => 100 });
    function Parent() {
      const [value, setValue] = useState("long text");
      return <Composer aria-label="message" value={value} onChange={(e) => setValue(e.target.value)} actions={<Button onClick={() => setValue("")}>send</Button>} />;
    }
    const { container } = render(<Parent />);
    await user.click(await screen.findByRole("button", { name: "expand" }));
    expect(container.querySelector("form")).toHaveClass("fd-composer-expanded");
    await user.click(screen.getByRole("button", { name: "send" }));
    await waitFor(() => expect(container.querySelector("form")).not.toHaveClass("fd-composer-expanded"));
  });
});

describe("Clamp", () => {
  afterEach(() => {
    // @ts-expect-error removing the layout stub again
    delete HTMLParagraphElement.prototype.scrollHeight;
    // @ts-expect-error removing the layout stub again
    delete HTMLParagraphElement.prototype.clientHeight;
  });

  it("shows the text and no button while it fits", () => {
    render(<Clamp>A short text.</Clamp>);
    expect(screen.getByText("A short text.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders nothing for an empty text", () => {
    const { container } = render(<Clamp>{""}</Clamp>);
    expect(container).toBeEmptyDOMElement();
  });

  it("a text longer than three lines gets 'show more', which shows all and then offers 'show less'", async () => {
    const user = userEvent.setup();
    Object.defineProperty(HTMLParagraphElement.prototype, "scrollHeight", { configurable: true, get: () => 200 });
    Object.defineProperty(HTMLParagraphElement.prototype, "clientHeight", { configurable: true, get: () => 60 });
    render(<Clamp>{"one\ntwo\nthree\nfour\nfive"}</Clamp>);
    const more = await screen.findByRole("button", { name: "show more" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(document.querySelector("p")).toHaveClass("fd-clamp");
    await user.click(more);
    expect(document.querySelector("p")).toHaveClass("fd-pre-line");
    expect(document.querySelector("p")).not.toHaveClass("fd-clamp");
    const less = screen.getByRole("button", { name: "show less" });
    expect(less).toHaveAttribute("aria-expanded", "true");
    await user.click(less);
    expect(screen.getByRole("button", { name: "show more" })).toBeInTheDocument();
  });
});

describe("SortableList", () => {
  const entries: SortableEntry[] = [
    { id: "a", name: "job a", title: "Job A", meta: "hourly" },
    { id: "b", name: "job b", title: "Job B", trailing: "idle" },
    { id: "c", name: "job c", title: "Job C" },
  ];

  it("is a list named by label with a listitem per entry, in order", () => {
    render(<SortableList label="jobs of Health" items={entries} onMove={() => {}} />);
    const list = screen.getByRole("list", { name: "jobs of Health" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("Job A");
    expect(items[0]).toHaveTextContent("hourly");
    expect(items[1]).toHaveTextContent("idle");
    expect(items[2]).toHaveTextContent("Job C");
  });

  it("each handle says which row it moves and where it stands", () => {
    render(<SortableList label="jobs" items={entries} onMove={() => {}} />);
    expect(screen.getByRole("button", { name: "Move job a, 1 of 3" })).toHaveTextContent("01");
    expect(screen.getByRole("button", { name: "Move job b, 2 of 3" })).toHaveTextContent("02");
    expect(screen.getByRole("button", { name: "Move job c, 3 of 3" })).toHaveTextContent("03");
  });

  it("the down button moves a row one place down: onMove(from, to)", async () => {
    const user = userEvent.setup();
    const onMove = vi.fn();
    render(<SortableList label="jobs" items={entries} onMove={onMove} />);
    await user.click(screen.getByRole("button", { name: "move job a down" }));
    expect(onMove).toHaveBeenCalledWith(0, 1);
  });

  it("the up button moves a row one place up", async () => {
    const user = userEvent.setup();
    const onMove = vi.fn();
    render(<SortableList label="jobs" items={entries} onMove={onMove} />);
    await user.click(screen.getByRole("button", { name: "move job c up" }));
    expect(onMove).toHaveBeenCalledWith(2, 1);
  });

  it("the first row cannot move up and the last cannot move down (aria-disabled, no call)", async () => {
    const user = userEvent.setup();
    const onMove = vi.fn();
    render(<SortableList label="jobs" items={entries} onMove={onMove} />);
    const first = screen.getByRole("button", { name: "move job a up" });
    const last = screen.getByRole("button", { name: "move job c down" });
    expect(first).toHaveAttribute("aria-disabled", "true");
    expect(last).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "move job b up" })).not.toHaveAttribute("aria-disabled");
    await user.click(first);
    await user.click(last);
    expect(onMove).not.toHaveBeenCalled();
  });

  it("the buttons have hints: move up, move down", () => {
    render(<SortableList label="jobs" items={entries} onMove={() => {}} />);
    const hints = [...document.querySelectorAll(".fd-hint")].map((h) => h.textContent);
    expect(hints).toContain("Move up");
    expect(hints).toContain("Move down");
  });

  it("there is no remove button unless onRemove is given", () => {
    render(<SortableList label="jobs" items={entries} onMove={() => {}} />);
    expect(screen.queryByRole("button", { name: /remove/ })).toBeNull();
  });

  it("onRemove adds a remove button per row, which calls it with the row's id", async () => {
    const user = userEvent.setup();
    const onRemove = vi.fn();
    render(<SortableList label="jobs" items={entries} onMove={() => {}} onRemove={onRemove} />);
    expect(screen.getAllByRole("button", { name: /^remove job/ })).toHaveLength(3);
    await user.click(screen.getByRole("button", { name: "remove job b" }));
    expect(onRemove).toHaveBeenCalledWith("b");
  });

  it("after a step the focus stays on the same direction button of the moved row, and the move is announced", async () => {
    const user = userEvent.setup();
    function Parent() {
      const [items, setItems] = useState(entries);
      return (
        <SortableList
          label="jobs"
          items={items}
          onMove={(from, to) => {
            const next = [...items];
            const [moved] = next.splice(from, 1);
            next.splice(to, 0, moved);
            setItems(next);
          }}
        />
      );
    }
    render(<Parent />);
    await user.click(screen.getByRole("button", { name: "move job a down" }));
    // the row moved to second place and its down button has the focus again
    const rows = within(screen.getByRole("list", { name: "jobs" })).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual([expect.stringContaining("Job B"), expect.stringContaining("Job A"), expect.stringContaining("Job C")]);
    expect(screen.getByRole("button", { name: "move job a down" })).toHaveFocus();
    // the number follows the row
    expect(screen.getByRole("button", { name: "Move job a, 2 of 3" })).toHaveTextContent("02");
    await waitFor(() => expect(screen.getByText("job a is now at position 2 of 3.")).toBeInTheDocument(), { timeout: 2000 });
  });

  it("when the row reaches the end, the focus goes to its other button", async () => {
    const user = userEvent.setup();
    function Parent() {
      const [items, setItems] = useState(entries);
      return (
        <SortableList
          label="jobs"
          items={items}
          onMove={(from, to) => {
            const next = [...items];
            const [moved] = next.splice(from, 1);
            next.splice(to, 0, moved);
            setItems(next);
          }}
        />
      );
    }
    render(<Parent />);
    // happy-dom has no ARIA reflection (element.ariaDisabled); browsers do, and the kit reads it
    Object.defineProperty(Element.prototype, "ariaDisabled", { configurable: true, get() { return this.getAttribute("aria-disabled"); } });
    try {
      await user.click(screen.getByRole("button", { name: "move job b down" }));
    } finally {
      // @ts-expect-error removing the stub again
      delete Element.prototype.ariaDisabled;
    }
    // b is last now: its down button is disabled, so the up button takes the focus
    expect(screen.getByRole("button", { name: "move job b up" })).toHaveFocus();
  });

  it("the handle is reachable and carries the keyboard instructions", () => {
    render(<SortableList label="jobs" items={entries} onMove={() => {}} />);
    const handle = screen.getByRole("button", { name: "Move job a, 1 of 3" });
    expect(handle).toHaveAttribute("aria-roledescription", "sortable");
    expect(handle).toHaveAttribute("tabindex", "0");
    const instructions = document.getElementById(handle.getAttribute("aria-describedby")!);
    expect(instructions).toHaveTextContent("press space");
  });
});

describe("Picker", () => {
  const groups: PickerGroup[] = [
    {
      label: "Docker",
      items: [
        { value: "docker:status", title: "docker:status", meta: "containers", text: "docker status containers" },
        { value: "docker:resources", title: "docker:resources", text: "docker resources" },
      ],
    },
    { label: "System", items: [{ value: "system:disk", title: "system:disk", text: "system disk" }] },
  ];

  it("is a dialog named by its title with a filter and the items in their groups", () => {
    render(<Picker title="Add a topic" groups={groups} onPick={() => {}} onClose={() => {}} />);
    const dialog = screen.getByRole("dialog", { name: "Add a topic", hidden: true });
    expect(dialog).toHaveAttribute("open");
    expect(within(dialog).getByLabelText("filter")).toBeInTheDocument();
    const docker = within(dialog).getByRole("region", { name: "Docker", hidden: true });
    expect(within(docker).getAllByRole("button", { hidden: true }).map((b) => b.textContent)).toEqual(["docker:statuscontainers", "docker:resources"]);
    expect(within(dialog).getByRole("region", { name: "System", hidden: true })).toBeInTheDocument();
  });

  it("the filter looks at every word in the item's text, hides empty groups and counts what it found", async () => {
    const user = userEvent.setup();
    render(<Picker title="Add" groups={groups} onPick={() => {}} onClose={() => {}} />);
    await user.type(screen.getByLabelText("filter"), "docker RESOURCES");
    expect(screen.getByRole("button", { name: "docker:resources", hidden: true })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /docker:status/, hidden: true })).toBeNull();
    expect(screen.queryByRole("region", { name: "System", hidden: true })).toBeNull();
    expect(screen.getByRole("status", { hidden: true })).toHaveTextContent("1 found");
  });

  it("says so when nothing matches, and shows the empty text when there was nothing to pick", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Picker title="Add" groups={groups} onPick={() => {}} onClose={() => {}} />);
    await user.type(screen.getByLabelText("filter"), "zzz");
    expect(screen.getByText("Nothing matches.")).toBeInTheDocument();
    rerender(<Picker title="Add" groups={[]} empty="No topics left." onPick={() => {}} onClose={() => {}} />);
    expect(screen.getByText("No topics left.")).toBeInTheDocument();
  });

  it("with no items at all it shows the empty text", () => {
    render(<Picker title="Add" groups={[]} empty="No topics left." onPick={() => {}} onClose={() => {}} />);
    expect(screen.getByText("No topics left.")).toBeInTheDocument();
  });

  it("the filter label can be renamed", () => {
    render(<Picker title="Add" groups={groups} filterLabel="find a topic" onPick={() => {}} onClose={() => {}} />);
    expect(screen.getByLabelText("find a topic")).toBeInTheDocument();
  });

  it("picking an item calls onPick with its value and closes the dialog", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    const onClose = vi.fn();
    render(<Picker title="Add" groups={groups} onPick={onPick} onClose={onClose} />);
    await user.click(screen.getByRole("button", { name: "system:disk", hidden: true }));
    expect(onPick).toHaveBeenCalledWith("system:disk");
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("dialog", { hidden: true })).not.toHaveAttribute("open");
  });

  it("the close button closes without picking", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    const onClose = vi.fn();
    render(<Picker title="Add" groups={groups} onPick={onPick} onClose={onClose} />);
    await user.click(screen.getByRole("button", { name: "close", hidden: true }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onPick).not.toHaveBeenCalled();
  });

  it("act is not needed for the dialog to open", async () => {
    render(<Picker title="Add" groups={groups} onPick={() => {}} onClose={() => {}} />);
    await act(async () => {});
    expect(screen.getByRole("dialog", { hidden: true })).toHaveAttribute("open");
  });
});
