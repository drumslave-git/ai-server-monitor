// src/ui.tsx: useNow and usePoll. (Markdown from the same file is tested in tests/unit/kit/part2-prose.test.tsx,
// which repairs DOMPurify for happy-dom; without that repair it strips every tag and keeps scripts.)
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useNow, usePoll } from "../../../src/ui";

afterEach(() => {
  vi.useRealTimers();
});

/** Let the promises of a load settle (timers are fake, no real wait is needed). */
const flush = () => act(async () => void (await Promise.resolve()));

describe("useNow", () => {
  it("starts at the current time in seconds", () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(new Date("2026-01-01T00:00:10.500Z"));
    const { result } = renderHook(() => useNow());
    expect(result.current).toBe(new Date("2026-01-01T00:00:10.500Z").getTime() / 1000);
  });

  it("moves on every 15 seconds by default, and not in between", () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(1_000_000);
    const { result } = renderHook(() => useNow());
    const start = result.current;
    act(() => vi.advanceTimersByTime(14_999));
    expect(result.current).toBe(start);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe(start + 15);
    act(() => vi.advanceTimersByTime(15_000));
    expect(result.current).toBe(start + 30);
  });

  it("takes its own interval", () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(1_000_000);
    const { result } = renderHook(() => useNow(1_000));
    const start = result.current;
    act(() => vi.advanceTimersByTime(3_000));
    expect(result.current).toBe(start + 3);
  });

  it("stops its timer when it is closed", () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    const { unmount } = renderHook(() => useNow());
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("usePoll", () => {
  it("loads at once and gives the data, with no error", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const load = vi.fn().mockResolvedValue({ n: 1 });
    const { result } = renderHook(() => usePoll(load, 10_000));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
    await flush();
    expect(load).toHaveBeenCalledTimes(1);
    expect(result.current.data).toEqual({ n: 1 });
    expect(result.current.error).toBeNull();
  });

  it("loads again every interval, not sooner", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    let n = 0;
    const load = vi.fn(() => Promise.resolve(++n));
    const { result } = renderHook(() => usePoll(load, 10_000));
    await flush();
    expect(result.current.data).toBe(1);

    act(() => vi.advanceTimersByTime(9_999));
    await flush();
    expect(load).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(1));
    await flush();
    expect(load).toHaveBeenCalledTimes(2);
    expect(result.current.data).toBe(2);
  });

  it("gives the message of a failed load, keeps the earlier data, and clears the message on the next good load", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const answers: (() => Promise<string>)[] = [() => Promise.resolve("first"), () => Promise.reject(new Error("backend down")), () => Promise.resolve("third")];
    const load = vi.fn(() => answers.shift()!());
    const { result } = renderHook(() => usePoll(load, 5_000));
    await flush();
    expect(result.current).toMatchObject({ data: "first", error: null });

    act(() => vi.advanceTimersByTime(5_000));
    await flush();
    expect(result.current).toMatchObject({ data: "first", error: "backend down" });

    act(() => vi.advanceTimersByTime(5_000));
    await flush();
    expect(result.current).toMatchObject({ data: "third", error: null });
  });

  it("reload loads at once and restarts the clock", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    let n = 0;
    const load = vi.fn(() => Promise.resolve(++n));
    const { result } = renderHook(() => usePoll(load, 10_000));
    await flush();

    act(() => vi.advanceTimersByTime(8_000));
    act(() => result.current.reload());
    await flush();
    expect(load).toHaveBeenCalledTimes(2);
    expect(result.current.data).toBe(2);

    // the clock began again at the reload: 8 s later is not yet a tick
    act(() => vi.advanceTimersByTime(8_000));
    await flush();
    expect(load).toHaveBeenCalledTimes(2);
    act(() => vi.advanceTimersByTime(2_000));
    await flush();
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("follows a change of the interval", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const load = vi.fn().mockResolvedValue(1);
    const { rerender } = renderHook(({ ms }) => usePoll(load, ms), { initialProps: { ms: 10_000 } });
    await flush();
    rerender({ ms: 1_000 });
    await flush();
    const before = load.mock.calls.length;
    act(() => vi.advanceTimersByTime(3_000));
    await flush();
    expect(load.mock.calls.length - before).toBe(3);
  });

  it("loads again, with the new load, when a value named in deps changes, and not when it stays", async () => {
    const load = vi.fn((q: string) => Promise.resolve(q));
    const { result, rerender } = renderHook(({ q }) => usePoll(() => load(q), 60_000, [q]), { initialProps: { q: "a" } });
    await flush();
    expect(load).toHaveBeenLastCalledWith("a");
    rerender({ q: "a" });
    await flush();
    expect(load).toHaveBeenCalledTimes(1);
    rerender({ q: "b" });
    await flush();
    expect(load).toHaveBeenCalledTimes(2);
    expect(load).toHaveBeenLastCalledWith("b");
    expect(result.current.data).toBe("b");
  });

  it("stops loading when it is closed", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const load = vi.fn().mockResolvedValue(1);
    const { unmount } = renderHook(() => usePoll(load, 5_000));
    await flush();
    unmount();
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(60_000));
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("drops an answer that comes in after it was closed", async () => {
    let answer!: (n: number) => void;
    const load = vi.fn(() => new Promise<number>((resolve) => (answer = resolve)));
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { result, unmount } = renderHook(() => usePoll(load, 60_000));
    unmount();
    answer(5);
    await flush();
    expect(result.current.data).toBeNull();
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });
});
