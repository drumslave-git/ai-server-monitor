import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  Actions,
  Empty,
  FormGrid,
  FormGroup,
  FormGroups,
  FormWide,
  Hazard,
  Hero,
  Inline,
  Labeled,
  Legend,
  Live,
  Main,
  Notice,
  Pad,
  Pane,
  Panel,
  Panes,
  Row,
  Scroll,
  SectionHead,
  Shell,
  Split,
  Toolbar,
  setRootFontSize,
} from "../../../src/kit";

afterEach(() => {
  vi.restoreAllMocks();
  document.documentElement.style.fontSize = "";
});

describe("Shell", () => {
  it("renders its children beside the tooltip controller and an icon sprite hidden from assistive tech", () => {
    const { container } = render(
      <Shell>
        <p>page body</p>
      </Shell>,
    );
    expect(screen.getByText("page body")).toBeInTheDocument();
    const sprite = container.querySelector("svg");
    expect(sprite).toHaveAttribute("aria-hidden", "true");
    // the sprite holds one symbol per icon that HTML strings may use
    expect([...container.querySelectorAll("symbol")].map((s) => s.id)).toEqual(["fd-icon-run", "fd-icon-copy", "fd-icon-copied", "fd-icon-failed", "fd-icon-running"]);
    // the tooltip box stays out of the accessibility tree
    expect(container.querySelector("[popover]")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("setRootFontSize", () => {
  it("sets the root font size in pixels", () => {
    setRootFontSize(18);
    expect(document.documentElement.style.fontSize).toBe("18px");
    setRootFontSize(14);
    expect(document.documentElement.style.fontSize).toBe("14px");
  });
});

describe("Main", () => {
  it("is the main landmark and scrolls to the top when resetOn changes, not otherwise", () => {
    const scrollTo = vi.spyOn(HTMLElement.prototype, "scrollTo").mockImplementation(() => {});
    const { rerender } = render(<Main resetOn="/a">content</Main>);
    expect(screen.getByRole("main")).toHaveTextContent("content");
    expect(scrollTo).toHaveBeenCalledWith(0, 0);
    scrollTo.mockClear();

    rerender(<Main resetOn="/a">content</Main>);
    expect(scrollTo).not.toHaveBeenCalled();

    rerender(<Main resetOn="/b">content</Main>);
    expect(scrollTo).toHaveBeenCalledWith(0, 0);
  });
});

describe("Panes and Pane", () => {
  it("a pane with a label is a region of that name", () => {
    render(
      <Panes>
        <Pane label="Conversations">list</Pane>
        <Pane label="Thread">thread</Pane>
      </Panes>,
    );
    expect(screen.getByRole("region", { name: "Conversations" })).toHaveTextContent("list");
    expect(screen.getByRole("region", { name: "Thread" })).toHaveTextContent("thread");
  });

  it("stack marks the Panes that also stand under each other on a tablet", () => {
    const { container } = render(
      <>
        <Panes>
          <Pane label="One">a</Pane>
        </Panes>
        <Panes stack>
          <Pane label="Two">b</Pane>
        </Panes>
      </>,
    );
    const [plain, stacked] = Array.from(container.querySelectorAll(".fd-panes"));
    expect(plain).not.toHaveClass("fd-panes-stack");
    expect(stacked).toHaveClass("fd-panes-stack");
  });

  it("shows title, meta and aside in its head, and the foot after the body", () => {
    render(
      <Pane title="Runs" meta="3 total" aside={<button>refresh</button>} foot={<button>save all</button>}>
        body text
      </Pane>,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Runs" })).toBeInTheDocument();
    expect(screen.getByText("3 total")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "refresh" })).toBeInTheDocument();
    const body = screen.getByText("body text");
    const foot = screen.getByRole("button", { name: "save all" });
    expect(body.compareDocumentPosition(foot) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("has no head when it has neither title nor aside", () => {
    render(<Pane label="Plain">only body</Pane>);
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });

  it("with onSubmit it is a form that submits and is named by label", async () => {
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(
      <Pane label="Edit job" onSubmit={onSubmit} foot={<button type="submit">save</button>}>
        <input aria-label="name" />
      </Pane>,
    );
    const form = screen.getByRole("form", { name: "Edit job" });
    expect(within(form).getByLabelText("name")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "save" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("scrolls its body to the top when resetOn changes", () => {
    const scrollTo = vi.spyOn(HTMLElement.prototype, "scrollTo").mockImplementation(() => {});
    const { rerender } = render(<Pane resetOn={1}>x</Pane>);
    scrollTo.mockClear();
    rerender(<Pane resetOn={1}>x</Pane>);
    expect(scrollTo).not.toHaveBeenCalled();
    rerender(<Pane resetOn={2}>x</Pane>);
    expect(scrollTo).toHaveBeenCalledWith(0, 0);
  });

  it("scrolls its own body to the end when endOn changes and the pane scrolls itself", () => {
    const scrollTo = vi.spyOn(HTMLElement.prototype, "scrollTo").mockImplementation(() => {});
    vi.spyOn(window, "getComputedStyle").mockReturnValue({ overflowY: "auto" } as CSSStyleDeclaration);
    const { rerender } = render(<Pane endOn={1}>thread</Pane>);
    scrollTo.mockClear();
    rerender(<Pane endOn={2}>thread</Pane>);
    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect(scrollTo.mock.calls[0][0]).toBe(0);
  });

  it("scrolls the page to its end instead when the pane does not scroll (a phone)", () => {
    const pageScroll = vi.fn();
    const docScrollTo = vi.spyOn(HTMLElement.prototype, "scrollTo").mockImplementation(pageScroll);
    vi.spyOn(window, "getComputedStyle").mockReturnValue({ overflowY: "visible" } as CSSStyleDeclaration);
    const { rerender } = render(<Pane endOn={1}>thread</Pane>);
    docScrollTo.mockClear();
    rerender(<Pane endOn={2}>thread</Pane>);
    // the call goes to document.scrollingElement (the root element), with the end of the page as y
    expect(docScrollTo).toHaveBeenCalledTimes(1);
    expect(docScrollTo.mock.contexts[0]).toBe(document.scrollingElement);
  });

  it("does not scroll to the end when endOn is not given", () => {
    const scrollTo = vi.spyOn(HTMLElement.prototype, "scrollTo").mockImplementation(() => {});
    const { rerender } = render(<Pane>x</Pane>);
    scrollTo.mockClear();
    rerender(<Pane>y</Pane>);
    expect(scrollTo).not.toHaveBeenCalled();
  });
});

describe("SectionHead", () => {
  it("shows the title as a second-level heading, the note and the buttons", () => {
    render(
      <SectionHead title="Jobs" note="What runs daily">
        <button>add job</button>
      </SectionHead>,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Jobs" })).toBeInTheDocument();
    expect(screen.getByText("What runs daily")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "add job" })).toBeInTheDocument();
  });

  it("shows no note and no button group when none are given", () => {
    const { container } = render(<SectionHead title="Only title" />);
    expect(container.querySelector("p")).toBeNull();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("Panel", () => {
  it("is a section with title, meta and aside in its head", () => {
    render(
      <Panel title="Hosts" meta="2 online" aside={<button>add host</button>}>
        <p>rows</p>
      </Panel>,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Hosts" })).toBeInTheDocument();
    expect(screen.getByText("2 online")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "add host" })).toBeInTheDocument();
    expect(screen.getByText("rows")).toBeInTheDocument();
  });

  it("has no heading without a title and takes an id to scroll to", () => {
    const { container } = render(<Panel id="anchor">body</Panel>);
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
    expect(container.querySelector("section")).toHaveAttribute("id", "anchor");
  });

  it("draws the hazard stripe, hidden from assistive tech, only when asked", () => {
    const { container, rerender } = render(<Panel title="A">x</Panel>);
    expect(container.querySelector("[aria-hidden]")).toBeNull();
    rerender(
      <Panel title="A" hazard="crit">
        x
      </Panel>,
    );
    expect(container.querySelector("[aria-hidden]")).toBeInTheDocument();
  });

  it("pad wraps the whole body in one Pad", () => {
    const { container } = render(
      <Panel pad>
        <span>a</span>
        <span>b</span>
      </Panel>,
    );
    const pad = container.querySelector("section > div");
    expect(pad).toContainElement(screen.getByText("a"));
    expect(pad).toContainElement(screen.getByText("b"));
  });

  it("takes placement (flex, width, span, toEnd) as style", () => {
    const { container } = render(
      <Panel flex="1 1 20rem" width="30rem" span={2} toEnd>
        x
      </Panel>,
    );
    const section = container.querySelector("section")!;
    expect(section.style.flex).toContain("20rem");
    expect(section.style.width).toBe("30rem");
    expect(section.style.gridColumn).toBe("span 2");
    expect(section.style.marginLeft).toBe("auto");
  });

  it("span all takes the whole line, and no placement leaves no style", () => {
    const { container, rerender } = render(<Panel span="all">x</Panel>);
    expect(container.querySelector("section")!.style.gridColumn).toBe("1 / -1");
    rerender(<Panel>x</Panel>);
    expect(container.querySelector("section")).not.toHaveAttribute("style");
  });
});

describe("Pad", () => {
  it("holds its children and takes placement", () => {
    const { container } = render(
      <Pad gap={3} width="10rem">
        <span>inner</span>
      </Pad>,
    );
    const pad = container.firstElementChild as HTMLElement;
    expect(pad).toContainElement(screen.getByText("inner"));
    expect(pad.style.width).toBe("10rem");
  });
});

describe("Row", () => {
  it("is a plain line with its children", () => {
    render(
      <Row>
        <span>one</span>
        <span>two</span>
      </Row>,
    );
    expect(screen.getByText("one")).toBeInTheDocument();
    expect(screen.getByText("two")).toBeInTheDocument();
  });

  it("current marks it with aria-current", () => {
    const { container, rerender } = render(<Row current>x</Row>);
    expect(container.firstElementChild).toHaveAttribute("aria-current", "true");
    rerender(<Row>x</Row>);
    expect(container.firstElementChild).not.toHaveAttribute("aria-current");
  });

  it("with a label it is an article of that name and takes an id", () => {
    render(
      <Row label="Run 7" id="run-7">
        body
      </Row>,
    );
    expect(screen.getByRole("article", { name: "Run 7" })).toHaveAttribute("id", "run-7");
  });

  it("hands its ref to the element", () => {
    const ref = vi.fn();
    render(
      <Row label="Run" ref={ref}>
        body
      </Row>,
    );
    expect(ref).toHaveBeenCalledWith(screen.getByRole("article", { name: "Run" }));
  });

  it("with onSubmit it is a form that submits, with an id a button outside can target", async () => {
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(
      <>
        <Row id="composer" onSubmit={onSubmit}>
          <input aria-label="message" />
        </Row>
        <button type="submit" form="composer">
          send
        </button>
      </>,
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "send" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

describe("Hazard, Empty, Notice, Live", () => {
  it("Hazard is hidden from assistive tech", () => {
    const { container } = render(<Hazard />);
    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
  });

  it("Empty is a line of text", () => {
    render(<Empty>No jobs yet.</Empty>);
    expect(screen.getByText("No jobs yet.")).toBeInTheDocument();
  });

  it("Notice is a status with title, text and action", () => {
    render(
      <Notice title="Model unreachable" action={<button>retry</button>} tone="crit">
        The server did not answer.
      </Notice>,
    );
    const notice = screen.getByRole("status");
    expect(within(notice).getByText("Model unreachable")).toBeInTheDocument();
    expect(within(notice).getByText("The server did not answer.")).toBeInTheDocument();
    expect(within(notice).getByRole("button", { name: "retry" })).toBeInTheDocument();
  });

  it("Notice without title or text shows only what it is given", () => {
    render(<Notice action={<button>ok</button>} />);
    expect(screen.getByRole("status")).toHaveTextContent("ok");
    expect(screen.queryByRole("strong")).not.toBeInTheDocument();
  });

  it("Live is a polite live region", () => {
    render(<Live>Test passed</Live>);
    const live = screen.getByRole("status");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent("Test passed");
  });
});

describe("Hero", () => {
  it("shows the headline as the page's first-level heading with word, meta, children and aside", () => {
    render(
      <Hero meta="checked 5 min ago" word="fine" tone="ok" headline="All systems are fine" aside={<span>aside counters</span>}>
        <button>run now</button>
      </Hero>,
    );
    expect(screen.getByRole("heading", { level: 1, name: "All systems are fine" })).toBeInTheDocument();
    expect(screen.getByText("fine")).toBeInTheDocument();
    expect(screen.getByText("checked 5 min ago")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "run now" })).toBeInTheDocument();
    expect(screen.getByText("aside counters")).toBeInTheDocument();
  });

  it("compact makes the headline a second-level heading", () => {
    render(<Hero compact headline="Daily summary" status={<span>running</span>} />);
    expect(screen.getByRole("heading", { level: 2, name: "Daily summary" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
  });
});

describe("Actions, Legend, Split, Inline, Scroll, Labeled", () => {
  it("Actions holds its buttons", () => {
    render(
      <Actions>
        <button>one</button>
        <button>two</button>
      </Actions>,
    );
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["one", "two"]);
  });

  it("Legend holds its items and takes placement", () => {
    const { container } = render(
      <Legend toEnd>
        <span>ok</span>
      </Legend>,
    );
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect((container.firstElementChild as HTMLElement).style.marginLeft).toBe("auto");
  });

  it("Split is a plain block of its children", () => {
    const { container } = render(
      <Split id="two">
        <Panel title="A">a</Panel>
        <Panel title="B">b</Panel>
      </Split>,
    );
    expect(container.firstElementChild?.tagName).toBe("DIV");
    expect(container.firstElementChild).toHaveAttribute("id", "two");
    expect(screen.getAllByRole("heading").map((h) => h.textContent)).toEqual(["A", "B"]);
  });

  it("Split with onSubmit is a form", async () => {
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    const { container } = render(
      <Split onSubmit={onSubmit}>
        <button>go</button>
      </Split>,
    );
    expect(container.firstElementChild?.tagName).toBe("FORM");
    await userEvent.setup().click(screen.getByRole("button", { name: "go" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("Inline holds its children and takes placement", () => {
    const { container } = render(
      <Inline gap={2} align="center" between flex="1">
        <span>left</span>
        <span>right</span>
      </Inline>,
    );
    expect(screen.getByText("left")).toBeInTheDocument();
    expect(screen.getByText("right")).toBeInTheDocument();
    expect((container.firstElementChild as HTMLElement).style.flex).toContain("1");
  });

  it("Scroll sets the height limit it is given", () => {
    const { container } = render(<Scroll max="12rem">long content</Scroll>);
    expect((container.firstElementChild as HTMLElement).style.maxHeight).toBe("12rem");
    expect(container.firstElementChild).toHaveTextContent("long content");
  });

  it("Labeled puts a third-level heading over what it names", () => {
    render(
      <Labeled label="Schedule" width="8rem">
        <span>daily 06:00</span>
      </Labeled>,
    );
    expect(screen.getByRole("heading", { level: 3, name: "Schedule" })).toBeInTheDocument();
    expect(screen.getByText("daily 06:00")).toBeInTheDocument();
  });
});

describe("Toolbar", () => {
  it("is a row of its children", () => {
    render(
      <Toolbar>
        <input aria-label="search" />
      </Toolbar>,
    );
    expect(screen.getByLabelText("search")).toBeInTheDocument();
  });

  it("with side, the side part and the rest are both shown, side first", () => {
    render(
      <Toolbar side={<input aria-label="find chat" />}>
        <button>filter</button>
      </Toolbar>,
    );
    const side = screen.getByLabelText("find chat");
    const rest = screen.getByRole("button", { name: "filter" });
    expect(side.compareDocumentPosition(rest) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("FormGrid, FormGroups, FormGroup, FormWide", () => {
  it("FormGrid holds its fields", () => {
    render(
      <FormGrid>
        <input aria-label="a" />
        <input aria-label="b" />
      </FormGrid>,
    );
    expect(screen.getByLabelText("a")).toBeInTheDocument();
    expect(screen.getByLabelText("b")).toBeInTheDocument();
  });

  it("FormGroup is a region named by its title, with a third-level heading, its fields and notes", () => {
    render(
      <FormGroups>
        <FormGroup title="Network" notes={["Needs a restart.", "Port 1 to 65535."]}>
          <input aria-label="host" />
          <FormWide>
            <textarea aria-label="extra json" />
          </FormWide>
        </FormGroup>
        <FormGroup title="Limits">
          <input aria-label="max" />
        </FormGroup>
      </FormGroups>,
    );
    const network = screen.getByRole("region", { name: "Network" });
    expect(within(network).getByRole("heading", { level: 3, name: "Network" })).toBeInTheDocument();
    expect(within(network).getByLabelText("host")).toBeInTheDocument();
    expect(within(network).getByLabelText("extra json")).toBeInTheDocument();
    expect(within(network).getByText("Needs a restart.")).toBeInTheDocument();
    expect(within(network).getByText("Port 1 to 65535.")).toBeInTheDocument();
    const limits = screen.getByRole("region", { name: "Limits" });
    expect(within(limits).getByLabelText("max")).toBeInTheDocument();
    expect(within(limits).queryByText("Needs a restart.")).not.toBeInTheDocument();
  });
});
