// The tooltip of trimmed text: where a text is cut by its width (text-overflow: ellipsis), pointing at it
// (mouse or pen, after a short rest) or moving the keyboard focus onto it shows the whole text.
// One controller for the whole page, mounted by Shell: a part that trims its text needs nothing but the
// CSS ellipsis, and nothing is listed here, so a text added later is covered the moment it is cut.
// A touch screen gets no tooltip: there is no pointer to rest on it, and a long press is already the
// browser's text selection.
// A form field is covered too: an empty input's placeholder and a select's chosen option (see fieldText).
import { useEffect, useRef } from "react";

/** How long the pointer rests on a cut text before its tooltip shows. */
const REST_MS = 500;
/** The space kept between the tooltip and the window's edge, and between it and its text. */
const GAP = 8;

/** The space a select's own arrow takes at its end (the browser draws it inside the box, after the padding). */
const SELECT_ARROW = 24;

/** The text a form field shows that is no text of the page to measure: the placeholder of an empty input, the
 *  chosen option of a select. The browser draws a placeholder with an ellipsis and clips an option, so these are
 *  measured as the browser sets them: the canvas with the field's own font against the room inside it. */
function fieldText(el: Element): { text: string; arrow: number } | null {
  if (el instanceof HTMLInputElement) return !el.value && el.placeholder ? { text: el.placeholder, arrow: 0 } : null;
  if (el instanceof HTMLSelectElement) return el.selectedOptions[0] ? { text: el.selectedOptions[0].text, arrow: SELECT_ARROW } : null;
  return null;
}

function fieldCut(el: Element): boolean {
  const field = fieldText(el);
  const ctx = field && document.createElement("canvas").getContext("2d");
  if (!field || !ctx) return false;
  const style = getComputedStyle(el);
  ctx.font = style.font;
  const room = el.clientWidth - parseFloat(style.paddingInlineStart) - parseFloat(style.paddingInlineEnd) - field.arrow;
  return ctx.measureText(field.text).width > room;
}

/** Is this element's text cut at its end? */
function isCut(el: Element): boolean {
  if (fieldCut(el)) return true;
  return getComputedStyle(el).textOverflow === "ellipsis" && el.scrollWidth > el.clientWidth;
}

/** The whole text of a cut element: the placeholder of an empty field, the chosen option of a select, else its text. */
function wholeText(el: HTMLElement): string {
  return fieldText(el)?.text ?? el.textContent ?? "";
}

/** The cut text under the pointer: the nearest element above the target (itself included) that is cut. */
function cutAbove(target: EventTarget | null): HTMLElement | null {
  for (let el = target instanceof Element ? target : null; el && el !== document.body; el = el.parentElement) {
    if (isCut(el)) return el as HTMLElement;
  }
  return null;
}

/** The cut text of a focused control: the control itself, or the first cut text inside it. */
function cutWithin(focused: EventTarget | null): HTMLElement | null {
  if (!(focused instanceof Element)) return null;
  if (isCut(focused)) return focused as HTMLElement;
  for (const el of focused.querySelectorAll("*")) if (isCut(el)) return el as HTMLElement;
  return null;
}

export function TrimTips() {
  const tip = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const box = tip.current;
    if (!box) return;
    let timer = 0;
    // the text the tooltip is for, shown or still waiting for the pointer's rest
    let current: HTMLElement | null = null;

    const hide = () => {
      window.clearTimeout(timer);
      current = null;
      if (box.matches(":popover-open")) box.hidePopover();
    };

    const show = (el: HTMLElement) => {
      current = el;
      box.textContent = wholeText(el);
      box.showPopover();
      // under the text, from its start; above it where there is no room below; always inside the window
      const at = el.getBoundingClientRect();
      const size = box.getBoundingClientRect();
      const below = at.bottom + GAP + size.height <= window.innerHeight;
      box.style.top = `${Math.max(GAP, below ? at.bottom + GAP : at.top - GAP - size.height)}px`;
      box.style.left = `${Math.max(GAP, Math.min(at.left, window.innerWidth - size.width - GAP))}px`;
    };

    const onOver = (e: PointerEvent) => {
      if (e.pointerType === "touch") return;
      const el = cutAbove(e.target);
      if (el === current) return;
      hide();
      current = el;
      if (el) timer = window.setTimeout(() => show(el), REST_MS);
    };
    // keyboard focus only: a click that focuses a link is not a reason to cover its neighbours
    const onFocus = (e: FocusEvent) => {
      hide();
      const target = e.target instanceof Element && e.target.matches(":focus-visible") ? e.target : null;
      const el = cutWithin(target);
      if (el) show(el);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") hide();
    };

    document.addEventListener("pointerover", onOver);
    document.addEventListener("pointerdown", hide, true);
    document.addEventListener("scroll", hide, true);
    document.addEventListener("focusin", onFocus);
    document.addEventListener("focusout", hide);
    document.addEventListener("keydown", onKey);
    window.addEventListener("blur", hide);
    return () => {
      hide();
      document.removeEventListener("pointerover", onOver);
      document.removeEventListener("pointerdown", hide, true);
      document.removeEventListener("scroll", hide, true);
      document.removeEventListener("focusin", onFocus);
      document.removeEventListener("focusout", hide);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", hide);
    };
  }, []);

  // the text is in the page already (the cut is only the look), so it stays out of the accessibility tree
  return <div ref={tip} popover="manual" className="fd-trim-tip" aria-hidden="true" />;
}
