// Chat on a narrow window: one pane at a time (the conversations or the thread), the width where it switches is
// 820 px, and the page follows the window while it is open (the shared useMatchMedia hook).
import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderChat } from "./render";

/** A window whose width answers the one media query the page asks; `resize` changes the answer and tells the listeners. */
function fakeWindow(narrow: boolean) {
  const asked: string[] = [];
  const listeners = new Set<() => void>();
  const list = {
    matches: narrow,
    media: "",
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  vi.stubGlobal("matchMedia", (query: string) => {
    asked.push(query);
    return list;
  });
  return {
    asked,
    listeners,
    resize(to: boolean) {
      list.matches = to;
      for (const fn of [...listeners]) fn();
    },
  };
}

beforeEach(() => {
  vi.stubGlobal("confirm", vi.fn(() => true));
});
afterEach(() => {
  vi.unstubAllGlobals();
  history.replaceState(null, "", "#/");
});

const list = () => screen.queryByRole("navigation", { name: "Conversations" });
const thread = () => screen.queryByRole("textbox", { name: "Message" });
const back = () => screen.queryByRole("link", { name: /all conversations/ });

describe("Chat on a narrow window", () => {
  it("asks for 820 px, and shows the conversations alone while none is open", async () => {
    const win = fakeWindow(true);
    renderChat("#/chat");
    expect(await screen.findByRole("link", { name: /Example disk usage/ })).toBeInTheDocument();
    expect(win.asked).toContain("(max-width: 820px)");
    expect(thread()).not.toBeInTheDocument();
    expect(back()).not.toBeInTheDocument();
  });

  it("shows the thread alone under a way back to the conversations once one is open", async () => {
    fakeWindow(true);
    renderChat("#/chat/new");
    expect(await screen.findByRole("textbox", { name: "Message" })).toBeInTheDocument();
    expect(list()).not.toBeInTheDocument();
    expect(back()).toHaveAttribute("href", "#/chat");
  });

  it("shows both beside each other when the window is wide, with no way back", async () => {
    fakeWindow(false);
    renderChat("#/chat/new");
    expect(await screen.findByRole("textbox", { name: "Message" })).toBeInTheDocument();
    expect(list()).toBeInTheDocument();
    expect(back()).not.toBeInTheDocument();
  });

  it("follows the window while it is open, and stops listening when it goes away", async () => {
    const win = fakeWindow(false);
    const view = renderChat("#/chat/new");
    expect(await screen.findByRole("textbox", { name: "Message" })).toBeInTheDocument();
    expect(list()).toBeInTheDocument();
    act(() => win.resize(true));
    expect(list()).not.toBeInTheDocument();
    expect(back()).toBeInTheDocument();
    act(() => win.resize(false));
    expect(list()).toBeInTheDocument();
    expect(back()).not.toBeInTheDocument();
    view.unmount();
    expect(win.listeners.size).toBe(0);
  });
});
