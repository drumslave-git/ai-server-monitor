import { describe, expect, it } from "vitest";
import { ago, bytes, clock, duration, until } from "../../src/fmt";

describe("bytes", () => {
  it.each([
    [0, "0 B"],
    [999, "999 B"],
    [1000, "1.0 KB"],
    [1536, "1.5 KB"],
    [999_600, "1.0 MB"],
    [99_960_000, "100 MB"],
    [5_000_000_000, "5.0 GB"],
    [-2000, "-2.0 KB"],
    [2e15, "2000 TB"],
  ])("%s → %s", (n, text) => expect(bytes(n)).toBe(text));

  it.each([null, undefined, Number.NaN])("%s → dash", (n) => expect(bytes(n)).toBe("—"));
});

describe("ago", () => {
  const now = 1_000_000;
  it.each([
    [now - 3, "just now"],
    [now - 30, "30s ago"],
    [now - 90, "2m ago"],
    [now - 7200, "2h ago"],
    [now - 3 * 86400, "3d ago"],
    [now + 500, "just now"],
  ])("%s → %s", (ts, text) => expect(ago(ts, now)).toBe(text));

  it.each([null, undefined, 0])("%s → dash", (ts) => expect(ago(ts, now)).toBe("—"));
});

describe("until", () => {
  const now = 1_000_000;
  it.each([
    [now - 1, "expired"],
    [now, "expired"],
    [now + 20, "in 1m"],
    [now + 600, "in 10m"],
    [now + 7200, "in 2h"],
    [now + 3 * 86400, "in 3d"],
  ])("%s → %s", (ts, text) => expect(until(ts, now)).toBe(text));

  it("null → dash", () => expect(until(null, now)).toBe("—"));
});

describe("duration", () => {
  it.each([
    [0, "0m"],
    [59, "0m"],
    [600, "10m"],
    [3 * 3600 + 5 * 60, "3h 5m"],
    [2 * 86400 + 4 * 3600 + 59, "2d 4h"],
  ])("%s → %s", (s, text) => expect(duration(s)).toBe(text));

  it.each([null, undefined])("%s → dash", (s) => expect(duration(s)).toBe("—"));
});

describe("clock", () => {
  it("shows month, day and time of a unix timestamp in the local time zone", () => {
    const ts = new Date(2026, 4, 3, 14, 5).getTime() / 1000;
    const text = clock(ts);
    expect(text).toContain("3");
    expect(text).toMatch(/May/);
    expect(text).toMatch(/0?5/);
  });
});
