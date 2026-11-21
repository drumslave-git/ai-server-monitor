import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { SortableList, type SortableEntry } from "../../../src/kit";

const entries: SortableEntry[] = [
  { id: "a", name: "Disk check", title: "Disk check", meta: "docker:status" },
  { id: "b", name: "Network review", title: "Network review", trailing: <span>weekly</span> },
  { id: "c", name: "Backup test", title: "Backup test" },
];

// happy-dom has no ARIA reflection (element.ariaDisabled); the focus hand-over after a move reads it, as a browser has it.
beforeAll(() => {
  Object.defineProperty(Element.prototype, "ariaDisabled", { configurable: true, get: function (this: Element) { return this.getAttribute("aria-disabled"); } });
});
afterAll(() => {
  delete (Element.prototype as unknown as Record<string, unknown>).ariaDisabled;
});
afterEach(() => {
  vi.useRealTimers();
});

const setup = (props: Partial<React.ComponentProps<typeof SortableList>> = {}) => {
  const onMove = vi.fn();
  const view = render(<SortableList label="jobs of Health" items={entries} onMove={onMove} {...props} />);
  return { onMove, user: userEvent.setup(), ...view };
};

/** A parent that applies the moves, as a page does. */
function Stateful() {
  const [items, setItems] = useState(entries);
  return (
    <SortableList
      label="jobs"
      items={items}
      onMove={(from, to) =>
        setItems((list) => {
          const next = [...list];
          next.splice(to, 0, next.splice(from, 1)[0]);
          return next;
        })
      }
    />
  );
}
const setupStateful = () => ({ user: userEvent.setup(), ...render(<Stateful />) });
const rowNames = () => screen.getAllByRole("button", { name: /^Move .*, \d+ of \d+$/ }).map((b) => b.getAttribute("aria-label")!.replace(/^Move (.*), \d+ of \d+$/, "$1"));

describe("SortableList", () => {
  it("is a list named by its label with one item per entry, in order", () => {
    setup();
    const list = screen.getByRole("list", { name: "jobs of Health" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("Disk check");
    expect(items[1]).toHaveTextContent("Network review");
    expect(items[2]).toHaveTextContent("Backup test");
  });

  it("shows each entry's meta and trailing parts", () => {
    setup();
    expect(screen.getByText("docker:status")).toBeInTheDocument();
    expect(screen.getByText("weekly")).toBeInTheDocument();
  });

  it("each row has a handle named with its position, showing its number", () => {
    setup();
    const first = screen.getByRole("button", { name: "Move Disk check, 1 of 3" });
    expect(first).toHaveTextContent("01");
    expect(screen.getByRole("button", { name: "Move Network review, 2 of 3" })).toHaveTextContent("02");
    expect(screen.getByRole("button", { name: "Move Backup test, 3 of 3" })).toHaveTextContent("03");
  });

  it("the handle has instructions for the keyboard", () => {
    setup();
    const handle = screen.getByRole("button", { name: "Move Disk check, 1 of 3" });
    expect(handle).toHaveAttribute("aria-roledescription", "sortable");
    expect(handle).toHaveAccessibleDescription(/press space, then the up and down arrows, then space to drop it/);
  });

  it("the first row's up button and the last row's down button are disabled, the others are not", () => {
    setup();
    expect(screen.getByRole("button", { name: "move Disk check up" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "move Disk check down" })).not.toHaveAttribute("aria-disabled");
    expect(screen.getByRole("button", { name: "move Network review up" })).not.toHaveAttribute("aria-disabled");
    expect(screen.getByRole("button", { name: "move Network review down" })).not.toHaveAttribute("aria-disabled");
    expect(screen.getByRole("button", { name: "move Backup test up" })).not.toHaveAttribute("aria-disabled");
    expect(screen.getByRole("button", { name: "move Backup test down" })).toHaveAttribute("aria-disabled", "true");
  });

  it("down calls onMove with the old and the new index", async () => {
    const { onMove, user } = setup();
    await user.click(screen.getByRole("button", { name: "move Disk check down" }));
    expect(onMove).toHaveBeenCalledTimes(1);
    expect(onMove).toHaveBeenCalledWith(0, 1);
  });

  it("up calls onMove with the old and the new index", async () => {
    const { onMove, user } = setup();
    await user.click(screen.getByRole("button", { name: "move Backup test up" }));
    expect(onMove).toHaveBeenCalledWith(2, 1);
  });

  it("a disabled end button does nothing when pressed", async () => {
    const { onMove, user } = setup();
    await user.click(screen.getByRole("button", { name: "move Disk check up" }));
    await user.click(screen.getByRole("button", { name: "move Backup test down" }));
    expect(onMove).not.toHaveBeenCalled();
  });

  it("has no remove buttons unless onRemove is given", () => {
    setup();
    expect(screen.queryByRole("button", { name: /^remove / })).not.toBeInTheDocument();
  });

  it("onRemove adds a remove button per row that calls it with the entry's id", async () => {
    const onRemove = vi.fn();
    const { user } = setup({ onRemove });
    expect(screen.getAllByRole("button", { name: /^remove / })).toHaveLength(3);
    await user.click(screen.getByRole("button", { name: "remove Network review" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(onRemove).toHaveBeenCalledWith("b");
  });

  it("reads out the new position half a second after a move button press", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { onMove } = setup();
    const live = screen.getAllByRole("status").find((el) => !el.id.startsWith("DndLiveRegion"))!;
    expect(live).toHaveTextContent("");
    act(() => {
      screen.getByRole("button", { name: "move Disk check down" }).click();
    });
    expect(onMove).toHaveBeenCalledWith(0, 1);
    expect(live).toHaveTextContent("");
    act(() => {
      vi.advanceTimersByTime(499);
    });
    expect(live).toHaveTextContent("");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(live).toHaveTextContent("Disk check is now at position 2 of 3.");
  });

  it("after a move, focus goes back to the same kind of button on the moved row", async () => {
    const { user } = setupStateful();
    await user.click(screen.getByRole("button", { name: "move Disk check down" }));
    expect(rowNames()).toEqual(["Network review", "Disk check", "Backup test"]);
    expect(screen.getByRole("button", { name: "move Disk check down" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(rowNames()).toEqual(["Network review", "Backup test", "Disk check"]);
  });

  it("when the moved row reached an end, focus goes to its other move button", async () => {
    const { user } = setupStateful();
    await user.click(screen.getByRole("button", { name: "move Network review up" }));
    expect(rowNames()).toEqual(["Network review", "Disk check", "Backup test"]);
    // Network review is now first: its "up" button is disabled, so "down" takes the focus
    expect(screen.getByRole("button", { name: "move Network review up" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "move Network review down" })).toHaveFocus();
  });

  it("renumbers its handles when the list changes", () => {
    const { rerender } = render(<SortableList label="jobs" items={entries} onMove={() => {}} />);
    rerender(<SortableList label="jobs" items={[entries[2], entries[0]]} onMove={() => {}} />);
    expect(screen.getByRole("button", { name: "Move Backup test, 1 of 2" })).toHaveTextContent("01");
    expect(screen.getByRole("button", { name: "Move Disk check, 2 of 2" })).toHaveTextContent("02");
  });

  it("shows an empty list without rows", () => {
    render(<SortableList label="jobs" items={[]} onMove={() => {}} />);
    expect(within(screen.getByRole("list", { name: "jobs" })).queryAllByRole("listitem")).toHaveLength(0);
  });

  it("picks a row up with the keyboard and announces it, then Esc cancels without a move", async () => {
    const { onMove, user } = setup();
    screen.getByRole("button", { name: "Move Disk check, 1 of 3" }).focus();
    await user.keyboard(" ");
    const dndLive = document.querySelector('[id^="DndLiveRegion"]')!;
    expect(dndLive.textContent).toMatch(/Disk check/);
    expect(dndLive.textContent).toMatch(/position 1 of 3/);
    await user.keyboard("{Escape}");
    expect(dndLive).toHaveTextContent("Moving Disk check was cancelled; it stays at position 1 of 3.");
    expect(onMove).not.toHaveBeenCalled();
  });

  it("moves a row with the keyboard (space, arrow, space) and announces the drop", async () => {
    const { onMove, user } = setup();
    // happy-dom does no layout: give the rows places one under the other so the drag has somewhere to go
    document.querySelectorAll<HTMLElement>("[data-sort-id]").forEach((row, i) => {
      row.getBoundingClientRect = () => ({ x: 0, y: i * 40, top: i * 40, bottom: i * 40 + 40, left: 0, right: 100, width: 100, height: 40, toJSON() {} }) as DOMRect;
    });
    screen.getByRole("button", { name: "Move Disk check, 1 of 3" }).focus();
    await user.keyboard(" ");
    await user.keyboard("{ArrowDown}");
    await user.keyboard(" ");
    expect(onMove).toHaveBeenCalledTimes(1);
    expect(onMove).toHaveBeenCalledWith(0, 1);
    expect(document.querySelector('[id^="DndLiveRegion"]')).toHaveTextContent("Disk check dropped at position 2 of 3.");
  });
});
