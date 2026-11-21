import { act, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TrimTips } from "../../../src/kit/trimtips";

// happy-dom does no layout, no popovers and no :focus-visible, and has no canvas text measuring. Each of them
// is stubbed here as a browser answers it, so TrimTips' own decisions (what is cut, when to show, where) are tested.
/** Says how wide an element's text is (scroll) and how wide its box is (client): scroll > client means cut.
 *  The numbers live in attributes: happy-dom hands a <select> out through a proxy, so a WeakMap keyed by the element would miss it. */
const measure = (el: Element, scroll: number, client: number) => {
  el.setAttribute("data-scroll", String(scroll));
  el.setAttribute("data-client", String(client));
};
const shown = new WeakSet<Element>();
let showPopover: ReturnType<typeof vi.fn>;
let hidePopover: ReturnType<typeof vi.fn>;
const saved: Record<string, PropertyDescriptor | undefined> = {};
let originalMatches: typeof Element.prototype.matches;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  for (const key of ["scrollWidth", "clientWidth"] as const) {
    saved[key] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, key);
    Object.defineProperty(HTMLElement.prototype, key, {
      configurable: true,
      get(this: Element) {
        return Number(this.getAttribute(key === "scrollWidth" ? "data-scroll" : "data-client") ?? 0);
      },
    });
  }
  showPopover = vi.fn(function (this: Element) {
    shown.add(this);
  });
  hidePopover = vi.fn(function (this: Element) {
    shown.delete(this);
  });
  Object.defineProperty(HTMLElement.prototype, "showPopover", { configurable: true, value: showPopover });
  Object.defineProperty(HTMLElement.prototype, "hidePopover", { configurable: true, value: hidePopover });
  originalMatches = Element.prototype.matches;
  vi.spyOn(Element.prototype, "matches").mockImplementation(function (this: Element, selector: string) {
    if (selector === ":popover-open") return shown.has(this);
    if (selector === ":focus-visible") return document.activeElement === this;
    return originalMatches.call(this, selector);
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  for (const key of ["scrollWidth", "clientWidth"] as const) {
    if (saved[key]) Object.defineProperty(HTMLElement.prototype, key, saved[key]!);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[key];
  }
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).showPopover;
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).hidePopover;
});

const tipBox = () => document.querySelector<HTMLElement>("[popover]")!;
const over = (el: Element, init: PointerEventInit = { pointerType: "mouse" }) => fireEvent.pointerOver(el, init);
const rest = () => act(() => void vi.advanceTimersByTime(500));

/** A cut line: its text is wider than its box and the CSS says ellipsis. */
function Cut({ text, id = "cut", ellipsis = true, fits = false }: { text: string; id?: string; ellipsis?: boolean; fits?: boolean }) {
  return (
    <span
      data-testid={id}
      style={ellipsis ? { textOverflow: "ellipsis" } : undefined}
      ref={(el) => {
        if (el) measure(el, fits ? 50 : 300, 100);
      }}
    >
      {text}
    </span>
  );
}

describe("TrimTips: the pointer", () => {
  it("is an empty popover box hidden from assistive tech", () => {
    render(<TrimTips />);
    expect(tipBox()).toHaveAttribute("popover", "manual");
    expect(tipBox()).toHaveAttribute("aria-hidden", "true");
    expect(tipBox()).toHaveTextContent("");
  });

  it("shows the whole text of a cut element after the pointer rested on it for half a second", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="A very long job name that does not fit its row" />
      </>,
    );
    over(getByTestId("cut"));
    act(() => void vi.advanceTimersByTime(499));
    expect(showPopover).not.toHaveBeenCalled();
    act(() => void vi.advanceTimersByTime(1));
    expect(showPopover).toHaveBeenCalledTimes(1);
    expect(tipBox()).toHaveTextContent("A very long job name that does not fit its row");
  });

  it("shows nothing for a text that fits", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="short" fits />
      </>,
    );
    over(getByTestId("cut"));
    rest();
        expect(showPopover).not.toHaveBeenCalled();
  });

  it("shows nothing for a wider-than-box text that has no ellipsis (it is not cut by the kit's rule)", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="wraps instead" ellipsis={false} />
      </>,
    );
    over(getByTestId("cut"));
    rest();
    expect(showPopover).not.toHaveBeenCalled();
  });

  it("a touch screen gets no tooltip", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    over(getByTestId("cut"), { pointerType: "touch" });
    rest();
    expect(showPopover).not.toHaveBeenCalled();
  });

  it("a pen gets the tooltip like a mouse", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    over(getByTestId("cut"), { pointerType: "pen" });
    rest();
    expect(tipBox()).toHaveTextContent("long text");
  });

  it("the pointer over an element inside a cut one gets the cut one's tooltip", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <a
          data-testid="link"
          href="#/x"
          style={{ textOverflow: "ellipsis" }}
          ref={(el) => {
            if (el) measure(el, 400, 100);
          }}
        >
          <b data-testid="inner">Server name</b> with a long tail
        </a>
      </>,
    );
    over(getByTestId("inner"));
    rest();
    expect(tipBox()).toHaveTextContent("Server name with a long tail");
  });

  it("moving off before the rest is over cancels the tooltip", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
        <p data-testid="plain">plain</p>
      </>,
    );
    over(getByTestId("cut"));
    act(() => void vi.advanceTimersByTime(300));
    over(getByTestId("plain"));
    act(() => void vi.advanceTimersByTime(1000));
    expect(showPopover).not.toHaveBeenCalled();
  });

  it("moving to another cut element shows that element's text instead", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut id="one" text="first long text" />
        <Cut id="two" text="second long text" />
      </>,
    );
    over(getByTestId("one"));
    rest();
    expect(tipBox()).toHaveTextContent("first long text");
    over(getByTestId("two"));
    expect(hidePopover).toHaveBeenCalled();
    rest();
    expect(tipBox()).toHaveTextContent("second long text");
  });

  it("moving over the same cut element again does not restart or repeat the tooltip", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    over(getByTestId("cut"));
    rest();
    over(getByTestId("cut"));
    act(() => void vi.advanceTimersByTime(1000));
    expect(showPopover).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["Escape", () => fireEvent.keyDown(document, { key: "Escape" })],
    ["a click (pointer down)", () => fireEvent.pointerDown(document.body)],
    ["scrolling", () => fireEvent.scroll(document)],
    ["the pointer leaving to a plain element", () => over(document.body)],
    ["the window losing focus", () => fireEvent.blur(window)],
  ])("%s closes a shown tooltip", (_name, close) => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    over(getByTestId("cut"));
    rest();
    expect(shown.has(tipBox())).toBe(true);
    close();
    expect(shown.has(tipBox())).toBe(false);
  });

  it("another key does not close it", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    over(getByTestId("cut"));
    rest();
    fireEvent.keyDown(document, { key: "a" });
    expect(shown.has(tipBox())).toBe(true);
  });

  it("a click while still waiting cancels the pending tooltip", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    over(getByTestId("cut"));
    fireEvent.pointerDown(getByTestId("cut"));
    act(() => void vi.advanceTimersByTime(1000));
    expect(showPopover).not.toHaveBeenCalled();
  });

  it("after unmounting it no longer reacts and leaves nothing shown", () => {
    const { getByTestId, rerender } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    over(getByTestId("cut"));
    rerender(<Cut text="long text" />);
    act(() => void vi.advanceTimersByTime(1000));
    expect(showPopover).not.toHaveBeenCalled();
    over(getByTestId("cut"));
    act(() => void vi.advanceTimersByTime(1000));
    expect(showPopover).not.toHaveBeenCalled();
  });
});

describe("TrimTips: the keyboard", () => {
  it("shows the tooltip at once when focus lands on a cut control", () => {
    const { getByRole } = render(
      <>
        <TrimTips />
        <button
          ref={(el) => {
            if (el) {
              measure(el, 300, 100);
              el.style.textOverflow = "ellipsis";
            }
          }}
        >
          A button with long words
        </button>
      </>,
    );
    act(() => getByRole("button").focus());
    expect(showPopover).toHaveBeenCalledTimes(1);
    expect(tipBox()).toHaveTextContent("A button with long words");
  });

  it("shows the cut text inside a focused control (a link whose title is cut)", () => {
    const { getByRole } = render(
      <>
        <TrimTips />
        <a href="#/chat/1">
          <span>icon</span>
          <Cut text="A conversation title that is cut" />
        </a>
      </>,
    );
    act(() => getByRole("link").focus());
    expect(tipBox()).toHaveTextContent("A conversation title that is cut");
  });

  it("shows nothing when the focused control has no cut text", () => {
    const { getByRole } = render(
      <>
        <TrimTips />
        <a href="#/x">
          <Cut text="fits" fits />
        </a>
      </>,
    );
    act(() => getByRole("link").focus());
    expect(showPopover).not.toHaveBeenCalled();
  });

  it("shows nothing when focus came from a click (not focus-visible)", () => {
    vi.mocked(Element.prototype.matches).mockImplementation(function (this: Element, selector: string) {
      if (selector === ":focus-visible") return false;
      if (selector === ":popover-open") return shown.has(this);
      return originalMatches.call(this, selector);
    });
    const { getByRole } = render(
      <>
        <TrimTips />
        <a href="#/x">
          <Cut text="long text" />
        </a>
      </>,
    );
    act(() => getByRole("link").focus());
    expect(showPopover).not.toHaveBeenCalled();
  });

  it("closes when focus leaves", () => {
    const { getByRole } = render(
      <>
        <TrimTips />
        <a href="#/x">
          <Cut text="long text" />
        </a>
      </>,
    );
    act(() => getByRole("link").focus());
    expect(shown.has(tipBox())).toBe(true);
    act(() => getByRole("link").blur());
    expect(shown.has(tipBox())).toBe(false);
  });
});

describe("TrimTips: where it shows", () => {
  const rect = (left: number, top: number, bottom: number) => ({ left, top, bottom, right: left + 50, width: 50, height: bottom - top, x: left, y: top, toJSON() {} }) as DOMRect;

  it("under the text, from its start", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    getByTestId("cut").getBoundingClientRect = () => rect(120, 40, 60);
    tipBox().getBoundingClientRect = () => rect(0, 0, 30) as DOMRect;
    over(getByTestId("cut"));
    rest();
    expect(tipBox().style.top).toBe("68px");
    expect(tipBox().style.left).toBe("120px");
  });

  it("above the text where there is no room below", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    getByTestId("cut").getBoundingClientRect = () => rect(120, window.innerHeight - 20, window.innerHeight - 4);
    tipBox().getBoundingClientRect = () => rect(0, 0, 30);
    over(getByTestId("cut"));
    rest();
    expect(tipBox().style.top).toBe(`${window.innerHeight - 20 - 8 - 30}px`);
  });

  it("inside the window: shifted left when the text starts near the right edge", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    getByTestId("cut").getBoundingClientRect = () => rect(window.innerWidth - 10, 40, 60);
    tipBox().getBoundingClientRect = () => ({ ...rect(0, 0, 30), width: 200 }) as DOMRect;
    over(getByTestId("cut"));
    rest();
    expect(tipBox().style.left).toBe(`${window.innerWidth - 200 - 8}px`);
  });

  it("never above the window's top edge", () => {
    const { getByTestId } = render(
      <>
        <TrimTips />
        <Cut text="long text" />
      </>,
    );
    // no room below (a tall tip) and not enough above either
    getByTestId("cut").getBoundingClientRect = () => rect(10, 2, window.innerHeight - 2);
    tipBox().getBoundingClientRect = () => rect(0, 0, 400);
    over(getByTestId("cut"));
    rest();
    expect(tipBox().style.top).toBe("8px");
    expect(tipBox().style.left).toBe("10px");
  });
});

describe("TrimTips: form fields", () => {
  // the browser clips a placeholder or a chosen option without an ellipsis the page can ask about, so the field's
  // text is measured on a canvas: here 10px per character
  beforeEach(() => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
      () => ({ font: "", measureText: (text: string) => ({ width: text.length * 10 }) }) as unknown as RenderingContext,
    );
  });

  it("an empty input whose placeholder is wider than its room shows the placeholder", () => {
    const { getByRole } = render(
      <>
        <TrimTips />
        <input
          placeholder="Search by name or by address"
          style={{ paddingInlineStart: "4px", paddingInlineEnd: "4px" }}
          ref={(el) => {
            if (el) measure(el, 0, 100);
          }}
        />
      </>,
    );
    over(getByRole("textbox"));
    rest();
    expect(tipBox()).toHaveTextContent("Search by name or by address");
  });

  it("an input whose placeholder fits shows nothing", () => {
    const { getByRole } = render(
      <>
        <TrimTips />
        <input
          placeholder="search"
          style={{ paddingInlineStart: "4px", paddingInlineEnd: "4px" }}
          ref={(el) => {
            if (el) measure(el, 0, 100);
          }}
        />
      </>,
    );
    over(getByRole("textbox"));
    rest();
    expect(showPopover).not.toHaveBeenCalled();
  });

  it("an input with a value shows nothing, whatever its placeholder", () => {
    const { getByRole } = render(
      <>
        <TrimTips />
        <input
          defaultValue="typed"
          placeholder="Search by name or by address"
          ref={(el) => {
            if (el) measure(el, 0, 100);
          }}
        />
      </>,
    );
    over(getByRole("textbox"));
    rest();
    expect(showPopover).not.toHaveBeenCalled();
  });

  it("a select whose chosen option is wider than its room (less its arrow) shows that option's text", () => {
    const { getByRole } = render(
      <>
        <TrimTips />
        <select
          defaultValue="b"
          style={{ paddingInlineStart: "4px", paddingInlineEnd: "4px" }}
          ref={(el) => {
            if (el) measure(el, 0, 100);
          }}
        >
          <option value="a">short</option>
          <option value="b">Eight ch</option>
        </select>
      </>,
    );
    // 8 characters = 80px; the room is 100 - 4 - 4 - 24 (the arrow) = 68px
    over(getByRole("combobox"));
    rest();
    expect(tipBox()).toHaveTextContent("Eight ch");
  });

  it("a select whose chosen option fits shows nothing", () => {
    const { getByRole } = render(
      <>
        <TrimTips />
        <select
          defaultValue="a"
          style={{ paddingInlineStart: "4px", paddingInlineEnd: "4px" }}
          ref={(el) => {
            if (el) measure(el, 0, 100);
          }}
        >
          <option value="a">short</option>
          <option value="b">Eight ch</option>
        </select>
      </>,
    );
    over(getByRole("combobox"));
    rest();
        expect(showPopover).not.toHaveBeenCalled();
  });
});
