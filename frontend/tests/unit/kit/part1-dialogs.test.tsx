import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Confirm, Dialog, Picker, type PickerGroup } from "../../../src/kit";
import { installDialogBehaviour } from "./part1-support";

// happy-dom's <dialog> does not know showModal's top layer, <form method="dialog"> or Escape; part1-support adds
// those as a browser does them, so the tests exercise the kit's own logic around them.
let dialogs: ReturnType<typeof installDialogBehaviour>;
beforeEach(() => {
  dialogs = installDialogBehaviour();
});
afterEach(() => {
  dialogs.remove();
  vi.restoreAllMocks();
});

describe("Dialog", () => {
  it("opens as a modal dialog on mount, named by its title, with its children", () => {
    render(
      <Dialog title="Rename chat" onClose={() => {}}>
        <input aria-label="new name" />
      </Dialog>,
    );
    const dialog = screen.getByRole("dialog", { name: "Rename chat" });
    expect(dialog).toHaveAttribute("open");
    expect(dialogs.showModal).toHaveBeenCalledTimes(1);
    expect(dialog.dataset.modal).toBe("true");
    expect(screen.getByRole("heading", { level: 2, name: "Rename chat" })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("new name")).toBeInTheDocument();
  });

  it("does not open it a second time when it re-renders", () => {
    const { rerender } = render(
      <Dialog title="Rename chat" onClose={() => {}}>
        x
      </Dialog>,
    );
    rerender(
      <Dialog title="Rename chat" onClose={() => {}}>
        y
      </Dialog>,
    );
    expect(dialogs.showModal).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when Esc closes the dialog", async () => {
    const onClose = vi.fn();
    render(
      <Dialog title="Rename chat" onClose={onClose}>
        x
      </Dialog>,
    );
    await userEvent.setup().keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when a child closes it with its button", async () => {
    const onClose = vi.fn();
    render(
      <Dialog title="Rename chat" onClose={onClose}>
        <button type="button" onClick={(e) => e.currentTarget.closest("dialog")!.close()}>
          done
        </button>
      </Dialog>,
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "done" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("Confirm", () => {
  const setup = (props: Partial<React.ComponentProps<typeof Confirm>> = {}) => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    const calls: string[] = [];
    onConfirm.mockImplementation(() => calls.push("confirm"));
    onClose.mockImplementation(() => calls.push("close"));
    render(
      <Confirm title="Delete job?" confirm="delete" onConfirm={onConfirm} onClose={onClose} {...props}>
        The job and its runs are removed.
      </Confirm>,
    );
    return { onConfirm, onClose, calls, user: userEvent.setup() };
  };

  it("opens as a modal dialog named by its title and described by its text", () => {
    setup();
    const dialog = screen.getByRole("dialog", { name: "Delete job?" });
    expect(dialog).toHaveAttribute("open");
    expect(dialog.dataset.modal).toBe("true");
    expect(dialog).toHaveAccessibleDescription("The job and its runs are removed.");
  });

  it("has no description when it has no text", () => {
    render(<Confirm title="Reset?" confirm="reset" onConfirm={() => {}} onClose={() => {}} />);
    expect(screen.getByRole("dialog", { name: "Reset?" })).not.toHaveAttribute("aria-describedby");
  });

  it("offers cancel and the confirm button's own word, and the safe answer (cancel) has the focus", () => {
    setup();
    expect(screen.getByRole("button", { name: "delete" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "cancel" })).toHaveFocus();
  });

  it("confirm calls onConfirm and then onClose", async () => {
    const { onConfirm, onClose, calls, user } = setup();
    await user.click(screen.getByRole("button", { name: "delete" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(calls).toEqual(["confirm", "close"]);
  });

  it("cancel calls only onClose", async () => {
    const { onConfirm, onClose, user } = setup();
    await user.click(screen.getByRole("button", { name: "cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Esc is the same as cancel", async () => {
    const { onConfirm, onClose, user } = setup();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("a confirm answer does not leak into the next closing: Esc after a reopen is still a cancel", async () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(<Confirm title="Delete job?" confirm="delete" onConfirm={onConfirm} onClose={onClose} />);
    const dialog = screen.getByRole("dialog");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "delete" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    // the same element is shown again and dismissed with Esc
    dialog.setAttribute("open", "");
    await user.keyboard("{Escape}");
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("the confirm button's look follows variant (danger by default)", () => {
    const { rerender } = render(<Confirm title="t" confirm="go" onConfirm={() => {}} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: "go" }).className).toMatch(/danger/);
    rerender(<Confirm title="t" confirm="go" variant="warn" onConfirm={() => {}} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: "go" }).className).toMatch(/warn/);
    expect(screen.getByRole("button", { name: "go" }).className).not.toMatch(/danger/);
  });
});

describe("Picker", () => {
  const groups: PickerGroup[] = [
    {
      label: "Chats",
      items: [
        { value: "chat-1", title: "Disk space check", meta: "yesterday", text: "disk space check" },
        { value: "chat-2", title: "Network review", text: "network review" },
      ],
    },
    { label: "Jobs", items: [{ value: "job-1", title: "Daily docker status", meta: "daily", text: "daily docker status" }] },
  ];

  const setup = (props: Partial<React.ComponentProps<typeof Picker>> = {}) => {
    const onPick = vi.fn();
    const onClose = vi.fn();
    render(<Picker title="Attach a thing" groups={groups} onPick={onPick} onClose={onClose} {...props} />);
    return { onPick, onClose, user: userEvent.setup() };
  };

  it("opens as a modal dialog named by its title, with a focused filter and the groups", () => {
    setup();
    const dialog = screen.getByRole("dialog", { name: "Attach a thing" });
    expect(dialog.dataset.modal).toBe("true");
    expect(screen.getByRole("searchbox", { name: "filter" })).toHaveFocus();
    expect(within(screen.getByRole("region", { name: "Chats" })).getAllByRole("button")).toHaveLength(2);
    expect(within(screen.getByRole("region", { name: "Jobs" })).getByRole("button", { name: /Daily docker status/ })).toBeInTheDocument();
    expect(screen.getByText("yesterday")).toBeInTheDocument();
  });

  it("uses the filter label it is given", () => {
    setup({ filterLabel: "search chats" });
    expect(screen.getByRole("searchbox", { name: "search chats" })).toBeInTheDocument();
  });

  it("filters by every word, case-insensitively, in the item's text, and drops groups with no match", async () => {
    const { user } = setup();
    await user.type(screen.getByRole("searchbox"), "  DISK check ");
    expect(screen.getByRole("button", { name: /Disk space check/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Network review/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Jobs" })).not.toBeInTheDocument();
  });

  it("a filter that needs all words does not match when one is missing", async () => {
    const { user } = setup();
    await user.type(screen.getByRole("searchbox"), "disk docker");
    expect(screen.queryAllByRole("button", { name: /Disk|Daily/ })).toHaveLength(0);
  });

  it("reads out how many things were found, and nothing while the filter is empty", async () => {
    const { user } = setup();
    expect(screen.getByRole("status")).toHaveTextContent("");
    await user.type(screen.getByRole("searchbox"), "e");
    expect(screen.getByRole("status")).toHaveTextContent("3 found");
    await user.clear(screen.getByRole("searchbox"));
    await user.type(screen.getByRole("searchbox"), "network");
    expect(screen.getByRole("status")).toHaveTextContent("1 found");
  });

  it("says nothing matches when the filter finds nothing", async () => {
    const { user } = setup();
    await user.type(screen.getByRole("searchbox"), "zzz");
    expect(screen.getByText("Nothing matches.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("0 found");
  });

  it("shows the empty text when there is nothing to pick at all", () => {
    setup({ groups: [{ label: "Chats", items: [] }], empty: "No chats yet." });
    expect(screen.getByText("No chats yet.")).toBeInTheDocument();
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
  });

  it("has a default empty text", () => {
    setup({ groups: [] });
    expect(screen.getByText("Nothing to pick.")).toBeInTheDocument();
  });

  it("picking calls onPick with the value, then closes (onClose)", async () => {
    const { onPick, onClose, user } = setup();
    await user.click(screen.getByRole("button", { name: /Network review/ }));
    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick).toHaveBeenCalledWith("chat-2");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog", { hidden: true })).not.toHaveAttribute("open");
  });

  it("close shuts it without picking", async () => {
    const { onPick, onClose, user } = setup();
    await user.click(screen.getByRole("button", { name: "close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onPick).not.toHaveBeenCalled();
  });

  it("Esc shuts it without picking", async () => {
    const { onPick, onClose, user } = setup();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onPick).not.toHaveBeenCalled();
  });
});
