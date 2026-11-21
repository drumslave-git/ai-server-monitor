import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Button, IconButton, IconLink, LinkButton } from "../../../src/kit";

describe("Button", () => {
  it("is a button named by its words and calls onClick", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Button onClick={onClick}>save</Button>);
    await user.click(screen.getByRole("button", { name: "save" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("is type=button unless told otherwise, so it does not submit a form by accident", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <Button>plain</Button>
        <Button type="submit">send</Button>
      </form>,
    );
    expect(screen.getByRole("button", { name: "plain" })).toHaveAttribute("type", "button");
    await user.click(screen.getByRole("button", { name: "plain" }));
    expect(onSubmit).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "send" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("a disabled button does not call onClick", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        delete
      </Button>,
    );
    const button = screen.getByRole("button", { name: "delete" });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("puts the variant and block in its class", () => {
    render(
      <>
        <Button variant="primary">a</Button>
        <Button variant="warn">b</Button>
        <Button variant="danger" block>
          c
        </Button>
        <Button variant="link">d</Button>
        <Button>e</Button>
      </>,
    );
    expect(screen.getByRole("button", { name: "a" })).toHaveClass("fd-btn", "fd-btn-primary");
    expect(screen.getByRole("button", { name: "b" })).toHaveClass("fd-btn-warn");
    expect(screen.getByRole("button", { name: "c" })).toHaveClass("fd-btn-danger", "fd-btn-block");
    expect(screen.getByRole("button", { name: "d" })).toHaveClass("fd-btn-link");
    expect(screen.getByRole("button", { name: "e" }).className).toBe("fd-btn");
  });

  it("an icon is drawn before the word and does not change the name", () => {
    render(<Button icon="save">save</Button>);
    const button = screen.getByRole("button", { name: "save" });
    const icon = button.querySelector("svg");
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute("aria-hidden", "true");
    // the icon comes first
    expect(button.firstElementChild).toBe(icon);
  });

  it("passes other attributes (aria-expanded, form) to the element", () => {
    render(
      <Button aria-expanded={true} form="f1">
        more
      </Button>,
    );
    const button = screen.getByRole("button", { name: "more" });
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button).toHaveAttribute("form", "f1");
  });

  it("toEnd pushes it to the far end of its row", () => {
    render(<Button toEnd>later</Button>);
    expect(screen.getByRole("button", { name: "later" })).toHaveStyle({ marginLeft: "auto" });
  });
});

describe("LinkButton", () => {
  it("is a link with the right href and the button look", () => {
    render(
      <LinkButton href="#/jobs/new" variant="primary">
        new job
      </LinkButton>,
    );
    const link = screen.getByRole("link", { name: "new job" });
    expect(link).toHaveAttribute("href", "#/jobs/new");
    expect(link).toHaveClass("fd-btn", "fd-btn-primary");
  });

  it("takes an icon and keeps the word as the name", () => {
    render(
      <LinkButton href="#/x" icon="add">
        add
      </LinkButton>,
    );
    const link = screen.getByRole("link", { name: "add" });
    expect(link.querySelector("svg")).not.toBeNull();
  });

  it("passes download and target to the anchor", () => {
    render(
      <LinkButton href="/api/export" download="export.json">
        export
      </LinkButton>,
    );
    expect(screen.getByRole("link", { name: "export" })).toHaveAttribute("download", "export.json");
  });

  it("calls onClick", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn((e: React.MouseEvent) => e.preventDefault());
    render(
      <LinkButton href="#/a" onClick={onClick}>
        go
      </LinkButton>,
    );
    await user.click(screen.getByRole("link", { name: "go" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe("IconButton", () => {
  it("is a button whose accessible name is its label", () => {
    render(<IconButton icon="copy" label="copy" />);
    const button = screen.getByRole("button", { name: "copy" });
    expect(button).toHaveAttribute("aria-label", "copy");
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveClass("fd-btn", "fd-btn-icon");
    expect(button.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("calls onClick", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<IconButton icon="run" label="run" onClick={onClick} />);
    await user.click(screen.getByRole("button", { name: "run" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("is operated with Enter and Space from the keyboard", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<IconButton icon="run" label="run" onClick={onClick} />);
    await user.tab();
    expect(screen.getByRole("button", { name: "run" })).toHaveFocus();
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    expect(onClick).toHaveBeenCalledTimes(2);
  });

  it("shows its label as the hint when no hint is given", () => {
    render(<IconButton icon="copy" label="copy" />);
    const button = screen.getByRole("button", { name: "copy" });
    const hint = document.querySelector(".fd-hint") as HTMLElement;
    expect(hint).toHaveTextContent("copy");
    // the hint repeats the name, so it is not a second description
    expect(button).not.toHaveAttribute("aria-describedby");
    expect(hint).not.toHaveAttribute("role");
    // the button points at its hint for the interest invoker
    expect(button.getAttribute("interestfor")).toBe(hint.id);
    expect(hint).toHaveAttribute("popover", "hint");
  });

  it("a longer hint is the description and stays out of the name", () => {
    render(<IconButton icon="run" label="run" hint="Run the command as root" />);
    const button = screen.getByRole("button", { name: "run" });
    const hint = screen.getByRole("tooltip", { hidden: true });
    expect(hint).toHaveTextContent("Run the command as root");
    expect(button.getAttribute("aria-describedby")).toBe(hint.id);
    expect(button.getAttribute("interestfor")).toBe(hint.id);
  });

  it("a hint that only differs by case from the label is not a description", () => {
    render(<IconButton icon="copy" label="copy" hint="Copy" />);
    const button = screen.getByRole("button", { name: "copy" });
    expect(button).not.toHaveAttribute("aria-describedby");
    expect(document.querySelector(".fd-hint")).toHaveTextContent("copy");
  });

  it("count is added to the name as (n) and shown beside the icon, hidden from assistive tech", () => {
    render(<IconButton icon="approve" label="approve all" count={3} />);
    const button = screen.getByRole("button", { name: "approve all (3)" });
    const count = button.querySelector(".fd-btn-count") as HTMLElement;
    expect(count).toHaveTextContent("3");
    expect(count).toHaveAttribute("aria-hidden", "true");
    // the hint carries the same name
    expect(document.querySelector(".fd-hint")).toHaveTextContent("approve all (3)");
  });

  it("count of zero is still shown, as (0)", () => {
    render(<IconButton icon="approve" label="approve all" count={0} />);
    const button = screen.getByRole("button", { name: "approve all (0)" });
    expect(button.querySelector(".fd-btn-count")).toHaveTextContent("0");
  });

  it("without count there is no count mark", () => {
    render(<IconButton icon="approve" label="approve" />);
    expect(screen.getByRole("button", { name: "approve" }).querySelector(".fd-btn-count")).toBeNull();
  });

  it("a longer hint with a count: name has the count, hint is the long text", () => {
    render(<IconButton icon="approve" label="approve all" count={2} hint="Approve every waiting action" />);
    const button = screen.getByRole("button", { name: "approve all (2)" });
    expect(screen.getByRole("tooltip", { hidden: true })).toHaveTextContent("Approve every waiting action");
    expect(button).toHaveAttribute("aria-describedby");
  });

  it("disabled is aria-disabled, stays focusable and does not call onClick", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<IconButton icon="remove" label="delete" disabled onClick={onClick} />);
    const button = screen.getByRole("button", { name: "delete" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    // not the native attribute: a native disabled button gets no focus, so its hint would never show
    expect(button).not.toHaveAttribute("disabled");
    await user.click(button);
    button.focus();
    expect(button).toHaveFocus();
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    expect(onClick).not.toHaveBeenCalled();
  });

  it("a disabled one still has its hint", () => {
    render(<IconButton icon="remove" label="delete" disabled />);
    expect(document.querySelector(".fd-hint")).toHaveTextContent("delete");
  });

  it("a disabled submit button does not submit its form", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <IconButton icon="save" label="save" type="submit" disabled />
      </form>,
    );
    expect(screen.getByRole("button", { name: "save" })).toHaveAttribute("type", "button");
    await user.click(screen.getByRole("button", { name: "save" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("an enabled submit button submits its form", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <IconButton icon="save" label="save" type="submit" />
      </form>,
    );
    await user.click(screen.getByRole("button", { name: "save" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("variant and small go in the class", () => {
    render(
      <>
        <IconButton icon="remove" label="delete" variant="danger" small />
        <IconButton icon="run" label="run" variant="warn" />
        <IconButton icon="save" label="save" variant="primary" />
      </>,
    );
    expect(screen.getByRole("button", { name: "delete" })).toHaveClass("fd-btn-danger", "fd-btn-small");
    expect(screen.getByRole("button", { name: "run" })).toHaveClass("fd-btn-warn");
    expect(screen.getByRole("button", { name: "run" })).not.toHaveClass("fd-btn-small");
    expect(screen.getByRole("button", { name: "save" })).toHaveClass("fd-btn-primary");
  });

  it("passes data attributes through and each button has its own hint element", () => {
    render(
      <>
        <IconButton icon="run" label="run a" data-step="up" />
        <IconButton icon="run" label="run b" />
      </>,
    );
    expect(screen.getByRole("button", { name: "run a" })).toHaveAttribute("data-step", "up");
    const hints = [...document.querySelectorAll(".fd-hint")];
    expect(hints).toHaveLength(2);
    expect(hints[0].id).not.toBe(hints[1].id);
    expect(screen.getByRole("button", { name: "run a" }).getAttribute("interestfor")).toBe(hints[0].id);
    expect(screen.getByRole("button", { name: "run b" }).getAttribute("interestfor")).toBe(hints[1].id);
  });

  it("the hint is not part of any button's name", () => {
    const { container } = render(<IconButton icon="run" label="run" hint="A long explanation" />);
    expect(within(container).getByRole("button", { name: "run" })).toBeInTheDocument();
    expect(within(container).queryByRole("button", { name: /explanation/ })).toBeNull();
  });
});

describe("IconLink", () => {
  it("is a link named by its label with the right href", () => {
    render(<IconLink icon="download" label="download log" href="/api/logs/1/download" download="log.txt" />);
    const link = screen.getByRole("link", { name: "download log" });
    expect(link).toHaveAttribute("href", "/api/logs/1/download");
    expect(link).toHaveAttribute("download", "log.txt");
    expect(link).toHaveClass("fd-btn", "fd-btn-icon");
    expect(link.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("shows its label as the hint, or a longer hint as a description", () => {
    render(
      <>
        <IconLink icon="explain" label="open" href="#/a" />
        <IconLink icon="explain" label="open job" hint="Open the job in its page" href="#/b" />
      </>,
    );
    const plain = screen.getByRole("link", { name: "open" });
    const long = screen.getByRole("link", { name: "open job" });
    expect(plain).not.toHaveAttribute("aria-describedby");
    const tooltip = screen.getByRole("tooltip", { hidden: true });
    expect(tooltip).toHaveTextContent("Open the job in its page");
    expect(long.getAttribute("aria-describedby")).toBe(tooltip.id);
    expect(long.getAttribute("interestfor")).toBe(tooltip.id);
    expect([...document.querySelectorAll(".fd-hint")].map((h) => h.textContent)).toEqual(["open", "Open the job in its page"]);
  });

  it("small and variant go in the class", () => {
    render(<IconLink icon="explain" label="open" href="#/a" small variant="warn" />);
    expect(screen.getByRole("link", { name: "open" })).toHaveClass("fd-btn-small", "fd-btn-warn");
  });

  it("calls onClick", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn((e: React.MouseEvent) => e.preventDefault());
    render(<IconLink icon="explain" label="open" href="#/a" onClick={onClick} />);
    await user.click(screen.getByRole("link", { name: "open" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
