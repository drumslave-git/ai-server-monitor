import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Icon, Shell } from "../../../src/kit";
import { GLYPHS, SPRITE, type IconName } from "../../../src/kit/icons";

const names = Object.keys(GLYPHS) as IconName[];

describe("icons", () => {
  it("every icon name resolves to a component", () => {
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) expect(typeof GLYPHS[name], name).toMatch(/function|object/);
  });

  it("names the icons the README promises: every page, every settings section, the actions", () => {
    const needed: IconName[] = ["dashboard", "jobs", "approvals", "chat", "mcp", "logs", "settings", "general", "model", "worker", "storage", "outputs", "run", "copy", "save", "discard", "close", "moveUp", "moveDown", "filter", "send", "stop"];
    for (const name of needed) expect(names, name).toContain(name);
  });

  it.each(names)("Icon %s renders one svg hidden from assistive tech and not focusable", (name) => {
    const { container } = render(<Icon name={name} />);
    const svgs = container.querySelectorAll("svg");
    expect(svgs).toHaveLength(1);
    expect(svgs[0]).toHaveAttribute("aria-hidden", "true");
    expect(svgs[0]).toHaveAttribute("focusable", "false");
    expect(svgs[0].querySelector("path, circle, rect, polygon, polyline")).not.toBeNull();
  });

  it("Icon is 16px by default and takes the sizes 20 and 24", () => {
    const { container, rerender } = render(<Icon name="save" />);
    expect(container.querySelector("svg")).toHaveAttribute("width", "16");
    rerender(<Icon name="save" size={20} />);
    expect(container.querySelector("svg")).toHaveAttribute("width", "20");
    rerender(<Icon name="save" size={24} />);
    expect(container.querySelector("svg")).toHaveAttribute("height", "24");
  });

  it("different names draw different shapes", () => {
    const shape = (name: IconName) => render(<Icon name={name} />).container.querySelector("svg")!.innerHTML;
    expect(shape("moveUp")).not.toBe(shape("moveDown"));
    expect(shape("save")).not.toBe(shape("close"));
  });

  it("the sprite icons (for HTML written as a string) are all real icon names", () => {
    expect(SPRITE.length).toBeGreaterThan(0);
    for (const name of SPRITE) expect(names, name).toContain(name);
  });

  it("Shell draws each sprite icon once as a symbol with the id that iconButtonHtml's markup refers to", () => {
    const { container } = render(<Shell>x</Shell>);
    const symbols = [...container.querySelectorAll("symbol")];
    expect(symbols.map((s) => s.id)).toEqual(SPRITE.map((n) => `fd-icon-${n}`));
    for (const symbol of symbols) expect(symbol.querySelector("path")).not.toBeNull();
  });
});
