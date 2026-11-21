// The card a link points at (Card focus) carries fd-card-focus: on a phone kit.css gives that class a scroll margin
// as tall as the compact page head, so the card comes to rest with its title below the head. (happy-dom applies no
// layout or media query, so the resting place itself is measured in a browser, not here.)
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Card, Cards } from "../../../src/kit";

beforeEach(() => {
  Element.prototype.scrollIntoView = () => {};
});
afterEach(() => {
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

describe("Card focus", () => {
  it("marks the card a link points at for the scroll margin, and no other card", () => {
    render(
      <Cards>
        <Card title="server-a">
          <p>form a</p>
        </Card>
        <Card title="server-b" focus>
          <p>form b</p>
        </Card>
        <Card title="server-c" reveal>
          <p>form c</p>
        </Card>
      </Cards>,
    );
    const card = (name: string) => screen.getByRole("button", { name }).closest("section")!;
    expect(card("server-b")).toHaveClass("fd-card", "fd-card-focus");
    expect(card("server-a")).not.toHaveClass("fd-card-focus");
    expect(card("server-c")).not.toHaveClass("fd-card-focus");
  });

  it("opens a shut card that becomes the focused one later (a link followed on the page) and brings it into view", () => {
    const scrolled: Element[] = [];
    Element.prototype.scrollIntoView = function () {
      scrolled.push(this);
    };
    const view = (focus: boolean) => (
      <Cards>
        <Card title="server-a">
          <p>form a</p>
        </Card>
        <Card title="server-b" focus={focus}>
          <p>form b</p>
        </Card>
      </Cards>
    );
    const { rerender } = render(view(false));
    const toggle = screen.getByRole("button", { name: "server-b" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(scrolled).toHaveLength(0);
    rerender(view(true));
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(scrolled).toEqual([toggle.closest("section")]);
  });
});
