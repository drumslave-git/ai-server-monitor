// useMatchMedia: the answer of the query now, and its changes while the component is mounted.
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useMatchMedia } from "../../../src/ui";

afterEach(() => vi.unstubAllGlobals());

function fakeMedia(initial: boolean) {
  const listeners = new Set<() => void>();
  const list = {
    matches: initial,
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  vi.stubGlobal("matchMedia", () => list);
  return {
    listeners,
    set(value: boolean) {
      list.matches = value;
      for (const fn of listeners) fn();
    },
  };
}

describe("useMatchMedia", () => {
  it("starts with the answer of the query and follows its changes", () => {
    const media = fakeMedia(false);
    const { result } = renderHook(() => useMatchMedia("(max-width: 760px)"));
    expect(result.current).toBe(false);
    act(() => media.set(true));
    expect(result.current).toBe(true);
    act(() => media.set(false));
    expect(result.current).toBe(false);
  });

  it("stops listening when the component goes away", () => {
    const media = fakeMedia(true);
    const { result, unmount } = renderHook(() => useMatchMedia("(max-width: 760px)"));
    expect(result.current).toBe(true);
    expect(media.listeners.size).toBe(1);
    unmount();
    expect(media.listeners.size).toBe(0);
  });
});
