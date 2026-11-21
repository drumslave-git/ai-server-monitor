import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  Approval,
  Axis,
  Bar,
  Button,
  Cells,
  Code,
  Command,
  Finding,
  Fold,
  Gauge,
  IconButton,
  KeyValue,
  Lane,
  Message,
  Meter,
  Schedule,
  Stack,
  Table,
  Td,
  TestLine,
  Tr,
  type Cell,
} from "../../../src/kit";

const cellClasses = (container: HTMLElement) =>
  [...container.querySelectorAll(".fd-cell")].map((c) => [...c.classList].find((n) => n.startsWith("fd-cell-"))?.slice("fd-cell-".length) ?? "");

describe("Cells", () => {
  it("draws one cell per entry with its tone class", () => {
    const cells: Cell[] = ["ok", "warn", "", "crit", "info", "text", "next", "now"];
    const { container } = render(<Cells cells={cells} />);
    expect(cellClasses(container)).toEqual(cells);
  });

  it("with a label it is an image of that name", () => {
    render(<Cells cells={["ok"]} label="last 24 runs" />);
    expect(screen.getByRole("img", { name: "last 24 runs" })).toBeInTheDocument();
  });

  it("without a label it is hidden from assistive tech", () => {
    const { container } = render(<Cells cells={["ok"]} />);
    expect(container.querySelector(".fd-cells")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("size and fixed go in the class", () => {
    const { container } = render(<Cells cells={[]} size="lg" fixed />);
    expect(container.querySelector(".fd-cells")).toHaveClass("fd-cells-lg", "fd-cells-fixed");
  });
});

describe("Meter", () => {
  it("fills a share of the cells in its tone", () => {
    const { container } = render(<Meter value={5} max={10} count={10} tone="warn" />);
    expect(cellClasses(container)).toEqual([...Array(5).fill("warn"), ...Array(5).fill("")]);
  });

  it("an amount above zero fills at least one cell; zero fills none", () => {
    const { container, rerender } = render(<Meter value={1} max={1000} count={10} />);
    expect(cellClasses(container).filter(Boolean)).toHaveLength(1);
    rerender(<Meter value={0} max={1000} count={10} />);
    expect(cellClasses(container).filter(Boolean)).toHaveLength(0);
  });

  it("an amount above max fills all cells and no more", () => {
    const { container } = render(<Meter value={50} max={10} count={10} />);
    expect(cellClasses(container)).toEqual(Array(10).fill("ok"));
  });

  it("a max of zero fills none", () => {
    const { container } = render(<Meter value={5} max={0} count={4} />);
    expect(cellClasses(container)).toEqual(["", "", "", ""]);
  });

  it("inner is drawn first, in its tone, then the rest of the value", () => {
    const { container } = render(<Meter value={8} inner={3} max={10} count={10} tone="ok" innerTone="info" />);
    expect(cellClasses(container)).toEqual([...Array(3).fill("info"), ...Array(5).fill("ok"), "", ""]);
  });

  it("tone off draws the amount as empty cells", () => {
    const { container } = render(<Meter value={5} max={10} count={10} tone="off" />);
    expect(cellClasses(container)).toEqual(Array(10).fill(""));
  });

  it("has 20 cells by default and a label as its name", () => {
    const { container } = render(<Meter value={1} max={2} label="disk use" />);
    expect(container.querySelectorAll(".fd-cell")).toHaveLength(20);
    expect(screen.getByRole("img", { name: "disk use" })).toBeInTheDocument();
  });

  it("fluid cells share the width instead of keeping their own", () => {
    const { container, rerender } = render(<Meter value={1} max={2} />);
    expect(container.querySelector(".fd-cells")).toHaveClass("fd-cells-fixed");
    rerender(<Meter value={1} max={2} fluid />);
    expect(container.querySelector(".fd-cells")).not.toHaveClass("fd-cells-fixed");
  });
});

describe("Gauge", () => {
  it("is a meter with its range, value and text", () => {
    render(<Gauge label="gpu" value={30} max={100} text="30 %" />);
    const gauge = screen.getByRole("meter", { name: "gpu" });
    expect(gauge).toHaveAttribute("aria-valuemin", "0");
    expect(gauge).toHaveAttribute("aria-valuemax", "100");
    expect(gauge).toHaveAttribute("aria-valuenow", "30");
    expect(gauge).toHaveAttribute("aria-valuetext", "30 %");
  });

  it("shows the label and the text, hidden from assistive tech (the meter says them)", () => {
    const { container } = render(<Gauge label="gpu" value={30} max={100} text="30 %" />);
    const head = container.querySelector(".fd-gauge-head") as HTMLElement;
    expect(head).toHaveAttribute("aria-hidden", "true");
    expect(head).toHaveTextContent("gpu");
    expect(head).toHaveTextContent("30 %");
  });

  it("clamps the value into the range", () => {
    const { rerender } = render(<Gauge label="gpu" value={150} max={100} text="150" />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "100");
    rerender(<Gauge label="gpu" value={-5} max={100} text="-5" />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "0");
  });

  it("a title is part of its name and a tooltip", () => {
    render(<Gauge label="ram" value={4} max={16} text="4 GB" title="host-a, 16 GB installed" />);
    const gauge = screen.getByRole("meter", { name: "ram, host-a, 16 GB installed" });
    expect(gauge).toHaveAttribute("title", "host-a, 16 GB installed");
  });

  it("fills its cells with the share", () => {
    const { container } = render(<Gauge label="ram" value={8} max={16} text="8" />);
    const filled = cellClasses(container).filter(Boolean);
    expect(filled).toHaveLength(10);
    expect(new Set(filled)).toEqual(new Set(["text"]));
  });
});

describe("Schedule", () => {
  const now = 1_000_000;
  const slot = 1800;

  it("has the past slots, the now mark and the future slots", () => {
    const { container } = render(<Schedule runs={[]} now={now} />);
    const cells = cellClasses(container);
    expect(cells).toHaveLength(24 + 1 + 6);
    expect(cells[24]).toBe("now");
    expect(cells.filter((c) => c === "")).toHaveLength(30);
  });

  it("puts a run in its slot: the latest slot holds the newest run", () => {
    const { container } = render(<Schedule runs={[{ at: now - 60, tone: "ok" }, { at: now - 24 * slot + 10, tone: "crit" }]} now={now} />);
    const cells = cellClasses(container);
    expect(cells[23]).toBe("ok");
    expect(cells[0]).toBe("crit");
    expect(cells.slice(1, 23).every((c) => c === "")).toBe(true);
  });

  it("a slot shows the worst of its runs", () => {
    const { container } = render(
      <Schedule
        runs={[
          { at: now - 100, tone: "ok" },
          { at: now - 200, tone: "warn" },
          { at: now - 300, tone: "info" },
        ]}
        now={now}
      />,
    );
    expect(cellClasses(container)[23]).toBe("warn");
    const crit = render(
      <Schedule
        runs={[
          { at: now - 100, tone: "crit" },
          { at: now - 200, tone: "warn" },
        ]}
        now={now}
      />,
    );
    expect(cellClasses(crit.container)[23]).toBe("crit");
  });

  it("runs outside the window are not drawn", () => {
    const { container } = render(
      <Schedule
        runs={[
          { at: now - 25 * slot, tone: "crit" },
          { at: now + 10, tone: "crit" },
        ]}
        now={now}
      />,
    );
    expect(cellClasses(container).slice(0, 24).every((c) => c === "")).toBe(true);
  });

  it("marks the next run in the future part", () => {
    const { container } = render(<Schedule runs={[]} now={now} next={now + 2 * slot + 5} />);
    const future = cellClasses(container).slice(25);
    expect(future).toEqual(["", "", "next", "", "", ""]);
  });

  it("an interval job repeats the next marks every interval", () => {
    const { container } = render(<Schedule runs={[]} now={now} next={now + slot} every={2 * slot} />);
    expect(cellClasses(container).slice(25)).toEqual(["", "next", "", "next", "", "next"]);
  });

  it("a next run in the past is not drawn", () => {
    const { container } = render(<Schedule runs={[]} now={now} next={now - 10} />);
    expect(cellClasses(container).slice(25)).toEqual(["", "", "", "", "", ""]);
  });

  it("past, future and slot can be changed", () => {
    const { container } = render(<Schedule runs={[{ at: now - 5, tone: "ok" }]} now={now} slot={60} past={3} future={2} next={now + 70} />);
    expect(cellClasses(container)).toEqual(["", "", "ok", "now", "", "next"]);
  });

  it("without a future there is no now mark", () => {
    const { container } = render(<Schedule runs={[]} now={now} past={4} future={0} />);
    expect(cellClasses(container)).toEqual(["", "", "", ""]);
  });

  it("a label makes it an image", () => {
    render(<Schedule runs={[]} now={now} label="runs of Example job" />);
    expect(screen.getByRole("img", { name: "runs of Example job" })).toBeInTheDocument();
  });
});

describe("Lane", () => {
  it("shows the job as a link with its lamp, the schedule, the line and the time", () => {
    const { container } = render(
      <Lane name="Example job" href="#/jobs/example" tone="ok" line="3 findings" when="5 min ago">
        <Cells cells={["ok"]} label="runs" />
      </Lane>,
    );
    const link = screen.getByRole("link", { name: "Example job" });
    expect(link).toHaveAttribute("href", "#/jobs/example");
    expect(link.querySelector(".fd-lamp")).toHaveClass("fd-lamp-ok");
    expect(link.querySelector(".fd-lamp")).not.toHaveClass("fd-lamp-hollow");
    expect(screen.getByRole("img", { name: "runs" })).toBeInTheDocument();
    expect(screen.getByText("3 findings")).toBeInTheDocument();
    expect(screen.getByText("5 min ago")).toBeInTheDocument();
    expect(container.querySelector(".fd-lane")).not.toHaveClass("fd-lane-stack");
  });

  it("a job without a result has a hollow lamp", () => {
    render(
      <Lane name="Never ran" href="#/jobs/x">
        <Cells cells={[]} />
      </Lane>,
    );
    expect(screen.getByRole("link", { name: "Never ran" }).querySelector(".fd-lamp")).toHaveClass("fd-lamp-hollow");
  });

  it("a tone of off is hollow too", () => {
    render(
      <Lane name="Off job" href="#/jobs/x" tone="off">
        <Cells cells={[]} />
      </Lane>,
    );
    expect(screen.getByRole("link", { name: "Off job" }).querySelector(".fd-lamp")).toHaveClass("fd-lamp-hollow");
  });

  it("stack puts the time up by the name and the line under the strip", () => {
    const { container } = render(
      <Lane name="Example job" href="#/jobs/example" stack line="all fine" when="now">
        <Cells cells={[]} />
      </Lane>,
    );
    const lane = container.querySelector(".fd-lane") as HTMLElement;
    expect(lane).toHaveClass("fd-lane-stack");
    // the time is a direct child (next to the name); in the note only the line is left
    const note = lane.querySelector(".fd-lane-note") as HTMLElement;
    expect(within(note).queryByText("now")).toBeNull();
    expect(within(note).getByText("all fine")).toBeInTheDocument();
    expect(screen.getByText("now")).toBeInTheDocument();
  });

  it("blink makes the lamp blink", () => {
    render(
      <Lane name="Busy" href="#/jobs/x" tone="info" blink>
        <Cells cells={[]} />
      </Lane>,
    );
    expect(screen.getByRole("link", { name: "Busy" }).querySelector(".fd-lamp")).toHaveClass("fd-spin");
  });
});

describe("Bar", () => {
  const fills = (container: HTMLElement) => [...container.querySelectorAll<HTMLElement>(".fd-bar-fill")];

  it("draws one span as a share of the axis", () => {
    const { container } = render(<Bar start={20} end={60} total={100} />);
    const [fill] = fills(container);
    expect(fills(container)).toHaveLength(1);
    expect(fill).toHaveClass("fd-cell-ok");
    expect(fill.style.left).toBe("20%");
  });

  it("queued is drawn hollow before the start", () => {
    const { container } = render(<Bar start={30} end={50} total={100} queued={10} />);
    const parts = fills(container);
    expect(parts).toHaveLength(2);
    expect(parts[0]).toHaveClass("fd-bar-queued");
    expect(parts[0].style.left).toBe("10%");
    expect(parts[0].style.width).toBe("20%");
  });

  it("inner splits the span: before it in innerTone, the rest in tone", () => {
    const { container } = render(<Bar start={10} end={50} total={100} inner={30} tone="ok" innerTone="info" />);
    const parts = fills(container);
    expect(parts).toHaveLength(2);
    expect(parts[0]).toHaveClass("fd-cell-info");
    expect(parts[0].style.left).toBe("10%");
    expect(parts[0].style.width).toBe("20%");
    expect(parts[1]).toHaveClass("fd-cell-ok");
    expect(parts[1].style.left).toBe("30%");
  });

  it("is clamped to the axis", () => {
    const { container } = render(<Bar start={-20} end={500} total={100} />);
    expect(fills(container)[0].style.left).toBe("0%");
  });

  it("a total of zero does not divide by zero", () => {
    const { container } = render(<Bar start={0} end={0} total={0} />);
    expect(fills(container)).toHaveLength(1);
    expect(fills(container)[0].style.left).not.toContain("NaN");
    expect(fills(container)[0].style.left).not.toContain("Infinity");
  });

  it("label makes it an image; without one it is hidden", () => {
    const { container, rerender } = render(<Bar start={0} end={10} total={100} label="took 10 s" />);
    expect(screen.getByRole("img", { name: "took 10 s" })).toBeInTheDocument();
    rerender(<Bar start={0} end={10} total={100} />);
    expect(container.querySelector(".fd-bar")).toHaveAttribute("aria-hidden", "true");
  });

  it("size sm is marked", () => {
    const { container } = render(<Bar start={0} end={1} total={2} size="sm" />);
    expect(container.querySelector(".fd-bar")).toHaveClass("fd-bar-sm");
  });
});

describe("Axis", () => {
  it("shows its tick labels in order, hidden from assistive tech", () => {
    const { container } = render(<Axis labels={["0 s", "5 s", "10 s"]} />);
    const axis = container.querySelector(".fd-axis") as HTMLElement;
    expect(axis).toHaveAttribute("aria-hidden", "true");
    expect([...axis.children].map((c) => c.textContent)).toEqual(["0 s", "5 s", "10 s"]);
  });
});

describe("Code and Command", () => {
  it("Code is a pre with its text exactly as given", () => {
    render(<Code>{"line 1\n  line 2"}</Code>);
    const pre = screen.getByText(/line 1/);
    expect(pre.tagName).toBe("PRE");
    expect(pre.textContent).toBe("line 1\n  line 2");
  });

  it("inline Code is a code element", () => {
    render(<Code inline>x = 1</Code>);
    const code = screen.getByText("x = 1");
    expect(code.tagName).toBe("CODE");
    expect(code).toHaveClass("fd-code-inline");
  });

  it("a comment is shown above the text, on a line of its own", () => {
    render(<Code comment="# the answer">42</Code>);
    const pre = screen.getByText("42", { exact: false });
    expect(pre.textContent).toBe("# the answer\n42");
    expect(within(pre).getByText("# the answer")).toHaveClass("fd-dim");
  });

  it("maxHeight scrolls inside that height", () => {
    const { container } = render(<Code maxHeight={120}>long</Code>);
    const pre = container.querySelector("pre") as HTMLElement;
    expect(pre).toHaveClass("fd-code-scroll");
    expect(pre.style.maxHeight).toBe("120px");
  });

  it("wrap is marked", () => {
    const { container } = render(<Code wrap>x</Code>);
    expect(container.querySelector("pre")).toHaveClass("fd-code-wrap");
  });

  it("Command shows the command, its comment and its action buttons, which work", async () => {
    const user = userEvent.setup();
    const onCopy = vi.fn();
    const { container } = render(
      <Command comment="# list files" action={<IconButton icon="copy" label="copy" small onClick={onCopy} />}>
        ls -la
      </Command>,
    );
    const pre = container.querySelector("pre") as HTMLElement;
    expect(pre).toHaveTextContent("# list files");
    expect(pre).toHaveTextContent("ls -la");
    // with buttons the code wraps before them
    expect(pre).toHaveClass("fd-code-wrap");
    expect(container.querySelector(".fd-command-actions")).toContainElement(screen.getByRole("button", { name: "copy" }));
    await user.click(screen.getByRole("button", { name: "copy" }));
    expect(onCopy).toHaveBeenCalledTimes(1);
  });

  it("Command without action has no actions box and does not wrap unless asked", () => {
    const { container, rerender } = render(<Command>ls</Command>);
    expect(container.querySelector(".fd-command-actions")).toBeNull();
    expect(container.querySelector("pre")).not.toHaveClass("fd-code-wrap");
    rerender(<Command wrap>ls</Command>);
    expect(container.querySelector("pre")).toHaveClass("fd-code-wrap");
  });
});

describe("Table", () => {
  it("names its columns as column headers", () => {
    render(
      <Table head={["name", { label: "state", head: <em>state!</em> }]}>
        <tbody>
          <Tr>
            <Td>job a</Td>
            <Td>ok</Td>
          </Tr>
        </tbody>
      </Table>,
    );
    const headers = screen.getAllByRole("columnheader");
    expect(headers.map((h) => h.textContent)).toEqual(["name", "state!"]);
    expect(headers[0]).toHaveAttribute("scope", "col");
  });

  it("body cells belong to rows; a cell is named by its column in reading order", () => {
    render(
      <Table head={["name", "state"]}>
        <tbody>
          <Tr>
            <Td>job a</Td>
            <Td>ok</Td>
          </Tr>
          <Tr>
            <Td>job b</Td>
            <Td>failed</Td>
          </Tr>
        </tbody>
      </Table>,
    );
    const rows = screen.getAllByRole("row");
    // header row plus two
    expect(rows).toHaveLength(3);
    expect(within(rows[2]).getAllByRole("cell").map((c) => c.textContent)).toEqual(["job b", "failed"]);
  });

  it("a cut cell stays on one line (the trim tooltip shows the whole text); nowrap and fill have their own classes", () => {
    render(
      <Table>
        <tbody>
          <Tr>
            <Td cut>a very long detail</Td>
            <Td nowrap>kept</Td>
            <Td fill>rest</Td>
          </Tr>
        </tbody>
      </Table>,
    );
    expect(screen.getByText("a very long detail")).toHaveClass("fd-cut");
    expect(screen.getByText("a very long detail")).not.toHaveClass("fd-fill");
    expect(screen.getByText("kept")).toHaveClass("fd-nowrap");
    expect(screen.getByText("rest")).toHaveClass("fd-fill");
  });

  it("without head there is no header row", () => {
    render(
      <Table>
        <tbody>
          <Tr>
            <Td>only</Td>
          </Tr>
        </tbody>
      </Table>,
    );
    expect(screen.queryByRole("columnheader")).toBeNull();
    expect(screen.getAllByRole("row")).toHaveLength(1);
  });

  it("column width, minWidth and wrap are put on its header", () => {
    render(
      <Table head={[{ label: "name", width: "12rem", minWidth: "6rem", wrap: true }, "state"]}>
        <tbody />
      </Table>,
    );
    const [name, state] = screen.getAllByRole("columnheader");
    expect(name).toHaveClass("fd-th-wrap");
    expect(name.style.width).toBe("12rem");
    expect(name.style.minWidth).toBe("6rem");
    expect(state).not.toHaveClass("fd-th-wrap");
  });

  it("stack, dense and stickyHead are marked, and the column names are kept for the stacked rows", () => {
    const { container } = render(
      <Table head={["name", "state"]} stack dense stickyHead minWidth="40rem">
        <tbody />
      </Table>,
    );
    const table = container.querySelector("table") as HTMLElement;
    expect(table).toHaveClass("fd-table-stack", "fd-table-dense");
    expect(container.querySelector(".fd-table-wrap")).toHaveClass("fd-table-wrap-sticky");
    expect(table.style.getPropertyValue("--fd-l1")).toBe('"name"');
    expect(table.style.getPropertyValue("--fd-l2")).toBe('"state"');
    expect(table.style.minWidth).toBe("40rem");
  });

  it("a column name with quotes is escaped for the stacked rows", () => {
    const { container } = render(
      <Table head={['the "big" one']} stack>
        <tbody />
      </Table>,
    );
    expect((container.querySelector("table") as HTMLElement).style.getPropertyValue("--fd-l1")).toBe('"the \\"big\\" one"');
  });

  it("Tr: off and group are marked, selected is aria-selected", () => {
    render(
      <Table>
        <tbody>
          <Tr off>
            <Td>off row</Td>
          </Tr>
          <Tr group>
            <Td colSpan={3}>group row</Td>
          </Tr>
          <Tr selected>
            <Td>chosen row</Td>
          </Tr>
          <Tr>
            <Td>plain row</Td>
          </Tr>
        </tbody>
      </Table>,
    );
    expect(screen.getByText("off row").closest("tr")).toHaveClass("fd-off");
    expect(screen.getByText("group row").closest("tr")).toHaveClass("fd-group");
    expect(screen.getByText("chosen row").closest("tr")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("plain row").closest("tr")).not.toHaveAttribute("aria-selected");
    expect(screen.getByText("plain row").closest("tr")).not.toHaveAttribute("class");
  });

  it("Td: options map to classes, tone colours the text, colSpan spans", () => {
    render(
      <Table>
        <tbody>
          <Tr>
            <Td nowrap wrap right fill tone="crit" minWidth="5rem" colSpan={2}>
              cell
            </Td>
            <Td>bare</Td>
          </Tr>
        </tbody>
      </Table>,
    );
    const cell = screen.getByRole("cell", { name: "cell" });
    expect(cell).toHaveClass("fd-nowrap", "fd-wrap", "fd-right", "fd-fill", "fd-t-crit");
    expect(cell).toHaveAttribute("colspan", "2");
    expect(cell.style.minWidth).toBe("5rem");
    expect(screen.getByRole("cell", { name: "bare" })).not.toHaveAttribute("class");
  });

  it("controls in cells work", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Table head={["name", "action"]}>
        <tbody>
          <Tr>
            <Td>job a</Td>
            <Td>
              <Button onClick={onClick}>run</Button>
            </Td>
          </Tr>
        </tbody>
      </Table>,
    );
    await user.click(screen.getByRole("button", { name: "run" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe("Stack and KeyValue", () => {
  it("Stack keeps its children in order", () => {
    const { container } = render(
      <Stack gap={1}>
        <span>first</span>
        <span>second</span>
      </Stack>,
    );
    const stack = container.querySelector(".fd-stack") as HTMLElement;
    expect(stack).toHaveClass("fd-gap-1");
    expect([...stack.children].map((c) => c.textContent)).toEqual(["first", "second"]);
  });

  it("KeyValue pairs each name (term) with its value (definition)", () => {
    const { container } = render(
      <KeyValue
        items={[
          ["host", "host-a"],
          ["port", <b key="p">8080</b>],
        ]}
      />,
    );
    const terms = [...container.querySelectorAll("dt")].map((t) => t.textContent);
    const values = [...container.querySelectorAll("dd")].map((d) => d.textContent);
    expect(terms).toEqual(["host", "port"]);
    expect(values).toEqual(["host-a", "8080"]);
    expect(container.querySelector("dd b")).not.toBeNull();
  });

  it("KeyValue with no items is empty", () => {
    const { container } = render(<KeyValue items={[]} />);
    expect(container.querySelectorAll("dt")).toHaveLength(0);
  });
});

describe("TestLine", () => {
  it("ok shows the word ok and what was said", () => {
    render(<TestLine ok>Connected in 20 ms</TestLine>);
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.getByText("Connected in 20 ms")).toBeInTheDocument();
  });

  it("not ok shows failed in the critical tone", () => {
    const { container } = render(<TestLine>Connection refused</TestLine>);
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("Connection refused")).toBeInTheDocument();
    expect(container.querySelector(".fd-status")).toHaveClass("fd-t-crit");
  });

  it("running shows the awaiting line and ignores the text", () => {
    const { container } = render(<TestLine running>never shown</TestLine>);
    expect(screen.getByText("testing…")).toBeInTheDocument();
    expect(screen.queryByText("never shown")).toBeNull();
    expect(container.querySelector(".fd-lamp")).toHaveClass("fd-spin");
  });
});

describe("Fold", () => {
  it("shows its title in a summary and its body", () => {
    const { container } = render(
      <Fold title="Details" meta="3 lines" tag="warn" tagTone="warn">
        <p>the body</p>
      </Fold>,
    );
    const summary = container.querySelector("summary") as HTMLElement;
    expect(summary).toHaveTextContent("Details");
    expect(summary).toHaveTextContent("3 lines");
    expect(within(summary).getByText("warn")).toHaveClass("fd-tag-warn");
    expect(screen.getByText("the body")).toBeInTheDocument();
    expect(container.querySelector("details")).not.toHaveAttribute("open");
  });

  it("open shows it opened; inline is marked", () => {
    const { container } = render(
      <Fold title="Details" open inline>
        body
      </Fold>,
    );
    const details = container.querySelector("details") as HTMLDetailsElement;
    expect(details.open).toBe(true);
    expect(details).toHaveClass("fd-fold-inline");
  });

  it("onToggle gets the new state when it opens and closes", () => {
    const onToggle = vi.fn();
    const { container } = render(
      <Fold title="Details" onToggle={onToggle}>
        body
      </Fold>,
    );
    const details = container.querySelector("details") as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    expect(onToggle).toHaveBeenLastCalledWith(true);
    details.open = false;
    fireEvent(details, new Event("toggle"));
    expect(onToggle).toHaveBeenLastCalledWith(false);
  });

  it("works without onToggle", () => {
    const { container } = render(<Fold title="Details">body</Fold>);
    const details = container.querySelector("details") as HTMLDetailsElement;
    expect(() => fireEvent(details, new Event("toggle"))).not.toThrow();
  });
});

describe("Finding", () => {
  it("shows its severity word, source, title, detail and evidence", () => {
    render(
      <Finding tone="crit" source="docker:status" title="Container restarting" evidence="exit code 137">
        It restarted five times.
      </Finding>,
    );
    const article = screen.getByRole("article");
    expect(within(article).getByRole("heading", { name: "Container restarting", level: 3 })).toBeInTheDocument();
    expect(article).toHaveTextContent("critical · docker:status");
    expect(within(article).getByText("It restarted five times.")).toBeInTheDocument();
    expect(within(article).getByText("exit code 137").tagName).toBe("PRE");
  });

  it("each tone has its word", () => {
    render(
      <>
        <Finding tone="warn" title="a" />
        <Finding tone="info" title="b" />
      </>,
    );
    expect(screen.getByText("warning")).toBeInTheDocument();
    expect(screen.getByText("info")).toBeInTheDocument();
  });

  it("without source, detail and evidence only the severity and the title are shown", () => {
    const { container } = render(<Finding tone="warn" title="Disk filling up" />);
    expect(container.querySelector(".fd-status")).toHaveTextContent(/^warning$/);
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelector(".fd-dim")).toBeNull();
  });

  it("shows its action, which works", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Finding tone="warn" title="x" action={<Button onClick={onClick}>fix</Button>} />);
    await user.click(screen.getByRole("button", { name: "fix" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe("Message", () => {
  it("the owner's turn is marked 'you' in plain meta, with no status lamp", () => {
    const { container } = render(<Message who="you">How is the disk?</Message>);
    expect(screen.getByText("you")).toHaveClass("fd-meta");
    expect(container.querySelector(".fd-lamp")).toBeNull();
    expect(container.querySelector(".fd-msg")).toHaveClass("fd-msg-you");
    expect(screen.getByText("How is the disk?")).toBeInTheDocument();
  });

  it("another speaker is named in a status", () => {
    const { container } = render(<Message who="test-model">All fine.</Message>);
    expect(screen.getByText("test-model")).toBeInTheDocument();
    expect(container.querySelector(".fd-status")).toHaveClass("fd-t-ok");
    expect(container.querySelector(".fd-msg")).not.toHaveClass("fd-msg-you");
  });

  it("says replaces the name with what the speaker did", () => {
    render(
      <Message who="you" says="you ran this command">
        ls
      </Message>,
    );
    expect(screen.getByText("you ran this command")).toBeInTheDocument();
    expect(screen.queryByText(/^you$/)).toBeNull();
  });

  it("running makes the lamp blink", () => {
    const { container } = render(
      <Message who="test-model" running>
        …
      </Message>,
    );
    expect(container.querySelector(".fd-lamp")).toHaveClass("fd-spin");
    expect(container.querySelector(".fd-status")).toHaveClass("fd-t-info");
  });

  it("meta is shown beside the speaker; typed keeps line breaks; wide is marked", () => {
    const { container } = render(
      <Message who="you" meta="12:00" typed wide>
        {"a\nb"}
      </Message>,
    );
    expect(screen.getByText("12:00")).toHaveClass("fd-meta");
    expect(container.querySelector(".fd-msg-body")).toHaveClass("fd-keep");
    expect(container.querySelector(".fd-msg")).toHaveClass("fd-msg-wide");
  });
});

describe("Approval", () => {
  it("while waiting it has the hazard stripe, the title, the command and the note", () => {
    const { container } = render(
      <Approval title="Restart test-main" command="docker restart test-main" note="It will be down for a few seconds.">
        <p>extra item</p>
      </Approval>,
    );
    expect(container.querySelector(".fd-hazard")).not.toBeNull();
    expect(screen.getByText("Restart test-main").tagName).toBe("STRONG");
    expect(screen.getByText("docker restart test-main").tagName).toBe("PRE");
    expect(screen.getByText("It will be down for a few seconds.")).toBeInTheDocument();
    expect(screen.getByText("extra item")).toBeInTheDocument();
  });

  it("decided removes the stripe", () => {
    const { container } = render(<Approval title="Done" decided />);
    expect(container.querySelector(".fd-hazard")).toBeNull();
  });

  it("its action buttons work", async () => {
    const user = userEvent.setup();
    const onApprove = vi.fn();
    const onDeny = vi.fn();
    render(
      <Approval
        title="Restart"
        actions={
          <>
            <Button variant="primary" onClick={onApprove}>
              approve
            </Button>
            <Button onClick={onDeny}>deny</Button>
          </>
        }
      />,
    );
    await user.click(screen.getByRole("button", { name: "approve" }));
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onDeny).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "deny" }));
    expect(onDeny).toHaveBeenCalledTimes(1);
  });

  it("without command, note and actions only the title is there", () => {
    const { container } = render(<Approval title="Only a title" />);
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelector(".fd-approval-actions")).toBeNull();
    expect(container.querySelector("p")).toBeNull();
  });
});
