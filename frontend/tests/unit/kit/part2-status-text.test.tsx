import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  Count,
  Counter,
  Counters,
  EntityLink,
  EntityList,
  EntityTail,
  ErrorText,
  Heading,
  Lamp,
  Meta,
  Status,
  Tag,
  Text,
  TitleLink,
  type EntityKind,
} from "../../../src/kit";

describe("Lamp", () => {
  it("without a title it is decoration, hidden from assistive tech", () => {
    const { container } = render(<Lamp tone="ok" />);
    const lamp = container.querySelector(".fd-lamp") as HTMLElement;
    expect(lamp).toHaveAttribute("aria-hidden", "true");
    expect(lamp).not.toHaveAttribute("title");
  });

  it("with a title it has the title and is not hidden", () => {
    const { container } = render(<Lamp tone="crit" title="last run failed" />);
    const lamp = container.querySelector(".fd-lamp") as HTMLElement;
    expect(lamp).toHaveAttribute("title", "last run failed");
    expect(lamp).not.toHaveAttribute("aria-hidden");
  });

  it("tone, hollow and blink go in the class; off has no tone class", () => {
    const { container } = render(
      <>
        <Lamp tone="warn" hollow blink />
        <Lamp tone="off" />
        <Lamp />
      </>,
    );
    const [a, b, c] = [...container.querySelectorAll(".fd-lamp")];
    expect(a).toHaveClass("fd-lamp-warn", "fd-lamp-hollow", "fd-spin");
    expect(b.className).toBe("fd-lamp");
    expect(c.className).toBe("fd-lamp");
  });
});

describe("Status", () => {
  it("shows the word next to a lamp that is hidden from assistive tech", () => {
    const { container } = render(<Status tone="ok">on</Status>);
    expect(screen.getByText("on")).toBeInTheDocument();
    expect(container.querySelector(".fd-lamp")).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelector(".fd-status")).toHaveTextContent("on");
  });

  it("colours the word by tone, unless plain", () => {
    const { container, rerender } = render(<Status tone="crit">failed</Status>);
    expect(container.querySelector(".fd-status")).toHaveClass("fd-t-crit");
    expect(container.querySelector(".fd-lamp")).toHaveClass("fd-lamp-crit");
    rerender(
      <Status tone="crit" plain>
        failed
      </Status>,
    );
    expect(container.querySelector(".fd-status")).not.toHaveClass("fd-t-crit");
    // the lamp keeps its tone
    expect(container.querySelector(".fd-lamp")).toHaveClass("fd-lamp-crit");
  });

  it("text and next tones colour the lamp only, not the word", () => {
    const { container } = render(<Status tone="text">idle</Status>);
    expect(container.querySelector(".fd-status")).not.toHaveClass("fd-t-text");
    expect(container.querySelector(".fd-lamp")).toHaveClass("fd-lamp-text");
  });

  it("hollow and blink pass to the lamp", () => {
    const { container } = render(
      <Status tone="info" hollow blink>
        running
      </Status>,
    );
    expect(container.querySelector(".fd-lamp")).toHaveClass("fd-lamp-hollow", "fd-spin");
  });
});

describe("Tag and Count", () => {
  it("Tag shows its text with the tone class", () => {
    render(<Tag tone="warn">waiting</Tag>);
    expect(screen.getByText("waiting")).toHaveClass("fd-tag", "fd-tag-warn");
  });

  it("Tag without tone has only the base class", () => {
    render(<Tag>plain</Tag>);
    expect(screen.getByText("plain").className).toBe("fd-tag");
  });

  it("Count shows its number; crit is marked, warn is the base look", () => {
    render(
      <>
        <Count>3</Count>
        <Count tone="crit">7</Count>
        <Count tone="warn">2</Count>
      </>,
    );
    expect(screen.getByText("3")).toHaveClass("fd-count");
    expect(screen.getByText("3")).not.toHaveClass("fd-count-crit");
    expect(screen.getByText("7")).toHaveClass("fd-count-crit");
    expect(screen.getByText("2")).not.toHaveClass("fd-count-crit");
  });
});

describe("TitleLink", () => {
  it("is a link with the right href and name", () => {
    render(<TitleLink href="#/jobs/example-job">Example job</TitleLink>);
    const link = screen.getByRole("link", { name: "Example job" });
    expect(link).toHaveAttribute("href", "#/jobs/example-job");
    expect(link).toHaveClass("fd-title-link");
  });

  it("mono and truncate go in the class", () => {
    render(
      <TitleLink href="#/a" mono truncate>
        host-a
      </TitleLink>,
    );
    expect(screen.getByRole("link", { name: "host-a" })).toHaveClass("fd-mono", "fd-truncate");
  });

  it("passes other attributes (target, rel, title)", () => {
    render(
      <TitleLink href="https://example.invalid/x" target="_blank" rel="noreferrer" title="full name">
        x
      </TitleLink>,
    );
    const link = screen.getByRole("link", { name: "x" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link).toHaveAttribute("title", "full name");
  });
});

describe("EntityLink", () => {
  const kinds: EntityKind[] = ["chat", "job", "summary", "run", "approval", "call", "server", "model", "worker"];

  it.each(kinds)("a %s link goes to its href and is named by its text alone (the icon is decoration)", (kind) => {
    render(
      <EntityLink kind={kind} href={`#/somewhere/${kind}`}>
        test-name
      </EntityLink>,
    );
    const link = screen.getByRole("link", { name: "test-name" });
    expect(link).toHaveAttribute("href", `#/somewhere/${kind}`);
    const icon = link.querySelector("svg");
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute("aria-hidden", "true");
  });

  it("every kind draws an icon of its own: kinds that name different things differ", () => {
    const html = (kind: EntityKind) => {
      const { container, unmount } = render(
        <EntityLink kind={kind} href="#/x">
          n
        </EntityLink>,
      );
      const svg = container.querySelector("svg")!.innerHTML;
      unmount();
      return svg;
    };
    const chat = html("chat");
    const run = html("run");
    const server = html("server");
    const approval = html("approval");
    expect(new Set([chat, run, server, approval]).size).toBe(4);
    // a job and a summary share the icon of the jobs page
    expect(html("job")).toBe(html("summary"));
  });

  it("the name of a job or summary wraps; other names are cut", () => {
    render(
      <>
        <EntityLink kind="job" href="#/j">
          a job
        </EntityLink>
        <EntityLink kind="summary" href="#/s">
          a summary
        </EntityLink>
        <EntityLink kind="chat" href="#/c">
          a chat
        </EntityLink>
      </>,
    );
    expect(screen.getByRole("link", { name: "a job" })).toHaveClass("fd-name");
    expect(screen.getByRole("link", { name: "a summary" })).toHaveClass("fd-name");
    expect(screen.getByRole("link", { name: "a chat" })).not.toHaveClass("fd-name");
    expect(screen.getByText("a chat")).toHaveClass("fd-truncate");
    expect(screen.getByText("a job")).not.toHaveClass("fd-truncate");
  });

  it("mono is for ids", () => {
    render(
      <EntityLink kind="run" href="#/runs/abc123" mono>
        abc123
      </EntityLink>,
    );
    expect(screen.getByRole("link", { name: "abc123" })).toHaveClass("fd-mono");
  });

  it("passes other attributes through", () => {
    render(
      <EntityLink kind="server" href="#/settings/mcp/test-main" title="the server">
        test-main
      </EntityLink>,
    );
    expect(screen.getByRole("link", { name: "test-main" })).toHaveAttribute("title", "the server");
  });
});

describe("EntityList and EntityTail", () => {
  it("EntityList puts the links in one line, each one in its own unit, the last without the comma mark", () => {
    const { container } = render(
      <EntityList
        items={[
          <EntityLink key="a" kind="chat" href="#/chat/a">
            chat a
          </EntityLink>,
          <EntityLink key="b" kind="chat" href="#/chat/b">
            chat b
          </EntityLink>,
          <EntityLink key="c" kind="chat" href="#/chat/c">
            chat c
          </EntityLink>,
        ]}
      />,
    );
    const links = screen.getAllByRole("link");
    expect(links.map((l) => l.getAttribute("href"))).toEqual(["#/chat/a", "#/chat/b", "#/chat/c"]);
    const units = [...container.querySelectorAll(".fd-entity-item")];
    expect(units).toHaveLength(3);
    // all but the last carry the comma
    expect(units.map((u) => u.classList.contains("fd-entity-item-more"))).toEqual([true, true, false]);
  });

  it("EntityList of one has no comma mark and of none is empty", () => {
    const { container, rerender } = render(<EntityList items={[<span key="a">only</span>]} />);
    expect(container.querySelector(".fd-entity-item-more")).toBeNull();
    rerender(<EntityList items={[]} />);
    expect(container.textContent).toBe("");
  });

  it("EntityTail keeps the link and its text as one unit", () => {
    const { container } = render(
      <EntityTail
        link={
          <EntityLink kind="server" href="#/settings/mcp/test-main">
            test-main
          </EntityLink>
        }
      >
        · read_file
      </EntityTail>,
    );
    const unit = container.querySelector(".fd-entity-tail") as HTMLElement;
    expect(within(unit).getByRole("link", { name: "test-main" })).toHaveAttribute("href", "#/settings/mcp/test-main");
    expect(within(unit).getByText("· read_file")).toHaveClass("fd-entity-tail-text");
  });
});

describe("Counter and Counters", () => {
  it("Counter shows its value and label", () => {
    const { container } = render(<Counter value={12} label="runs today" />);
    expect(container.querySelector(".fd-counter-value")).toHaveTextContent("12");
    expect(container.querySelector(".fd-counter-label")).toHaveTextContent("runs today");
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("title is a heading between the numeral and the label", () => {
    render(<Counter value={3} label="waiting" title="Approvals" />);
    expect(screen.getByRole("heading", { name: "Approvals", level: 2 })).toBeInTheDocument();
  });

  it("tone colours the value; size and plain go in the class", () => {
    const { container } = render(<Counter value={1} label="x" tone="crit" size="xl" plain />);
    expect(container.querySelector(".fd-counter-value")).toHaveClass("fd-t-crit");
    expect(container.querySelector(".fd-counter")).toHaveClass("fd-counter-xl", "fd-counter-plain");
  });

  it("Counters holds counters in a row; flush is marked", () => {
    const { container } = render(
      <Counters flush>
        <Counter value={1} label="one" />
        <Counter value={2} label="two" />
      </Counters>,
    );
    const row = container.querySelector(".fd-counters") as HTMLElement;
    expect(row).toHaveClass("fd-counters-flush");
    expect(row.querySelectorAll(".fd-counter")).toHaveLength(2);
  });
});

describe("Meta, Text, Heading, ErrorText", () => {
  it("Meta is an inline mono line; block makes it a paragraph", () => {
    const { rerender } = render(<Meta>3 min ago</Meta>);
    expect(screen.getByText("3 min ago").tagName).toBe("SPAN");
    expect(screen.getByText("3 min ago")).toHaveClass("fd-meta");
    rerender(<Meta block>3 min ago</Meta>);
    expect(screen.getByText("3 min ago").tagName).toBe("P");
  });

  it("Meta takes a tone and a title", () => {
    render(
      <Meta tone="warn" title="exact time">
        late
      </Meta>,
    );
    const meta = screen.getByText("late");
    expect(meta).toHaveClass("fd-t-warn");
    expect(meta).toHaveAttribute("title", "exact time");
  });

  it("Text maps its options to classes", () => {
    render(
      <Text kind="data" dim tone="crit" wrap nowrap keep>
        output
      </Text>,
    );
    expect(screen.getByText("output")).toHaveClass("fd-data", "fd-dim", "fd-t-crit", "fd-wrap", "fd-nowrap", "fd-keep");
  });

  it("Text label kind, and no classes at all without options", () => {
    render(
      <>
        <Text kind="label">named</Text>
        <Text>bare</Text>
      </>,
    );
    expect(screen.getByText("named")).toHaveClass("fd-label");
    expect(screen.getByText("bare")).not.toHaveAttribute("class");
  });

  it("Text block is a paragraph and keeps its title", () => {
    render(
      <Text block title="whole text">
        some words
      </Text>,
    );
    const p = screen.getByText("some words");
    expect(p.tagName).toBe("P");
    expect(p).toHaveAttribute("title", "whole text");
  });

  it("Text takes placement", () => {
    render(<Text toEnd>right</Text>);
    expect(screen.getByText("right")).toHaveStyle({ marginLeft: "auto" });
  });

  it("Heading is a level 3 heading", () => {
    render(<Heading>Approval</Heading>);
    expect(screen.getByRole("heading", { name: "Approval", level: 3 })).toBeInTheDocument();
  });

  it("ErrorText is an alert with the message", () => {
    render(<ErrorText>Could not save: 500</ErrorText>);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not save: 500");
  });
});
