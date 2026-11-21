// The hardware meters in the rail: what they show, and when they ask.
import { act, render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HardwareMeters } from "../../../src/hardware";
import { hardware } from "../../mocks/fixtures/shell";
import type { ApiResponse } from "../../mocks/types";
import { server } from "../../mocks/server";

type Hardware = ApiResponse<"/api/hardware">;
const GIB = 1024 ** 3;

// The test fakes setTimeout (the meters poll with it), the mock server answers in real time.
const realSetTimeout = globalThis.setTimeout;
const settle = () => act(() => new Promise<void>((resolve) => realSetTimeout(resolve, 60)));
/** Move the fake clock, then let the answers come in. */
const tick = async (ms: number) => {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
  await settle();
};

let visibility: DocumentVisibilityState = "visible";
const setVisibility = (state: DocumentVisibilityState) => {
  visibility = state;
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
};

/** Count the requests and answer with whatever `answer()` says at that moment. */
function serve(answer: () => Hardware | "fail" | Promise<Hardware>) {
  const log = { asked: 0 };
  server.use(
    http.get("/api/hardware", async () => {
      log.asked += 1;
      const result = await answer();
      return result === "fail" ? HttpResponse.json({ detail: "no sensors" }, { status: 500 }) : HttpResponse.json(result);
    }),
  );
  return log;
}

const reading = (over: Partial<Hardware> = {}): Hardware => ({ ...hardware, ...over });
const meter = (name: string) => screen.getByRole("meter", { name });
const meters = () => screen.queryAllByRole("meter").map((m) => m.getAttribute("aria-label"));

beforeEach(() => {
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
});

afterEach(() => {
  vi.useRealTimers();
  delete (document as unknown as Record<string, unknown>).visibilityState;
});

describe("what the meters show", () => {
  it("shows nothing until the first answer", () => {
    const { container } = render(<HardwareMeters />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows CPU, RAM, GPU and VRAM, each as a meter with its reading as text", async () => {
    render(<HardwareMeters />);
    await screen.findByRole("meter", { name: "CPU" });
    expect(meters()).toEqual(["CPU", "RAM", "GPU, Test GPU 1000", "VRAM, Test GPU 1000"]);
    expect(meter("CPU")).toHaveAttribute("aria-valuetext", "12%");
    expect(meter("CPU")).toHaveAttribute("aria-valuenow", "12.4");
    expect(meter("CPU")).toHaveAttribute("aria-valuemax", "100");
    expect(meter("RAM")).toHaveAttribute("aria-valuetext", "8.0/32.0 GiB");
    expect(meter("RAM")).toHaveAttribute("aria-valuenow", String(8 * GIB));
    expect(meter("RAM")).toHaveAttribute("aria-valuemax", String(32 * GIB));
    expect(meter("GPU, Test GPU 1000")).toHaveAttribute("aria-valuetext", "40%");
    expect(meter("VRAM, Test GPU 1000")).toHaveAttribute("aria-valuetext", "3.0/12.0 GiB");
    expect(meter("VRAM, Test GPU 1000")).toHaveAttribute("aria-valuemax", String(12 * GIB));
  });

  it("rounds percentages to whole numbers and sizes to one decimal", async () => {
    serve(() => reading({ cpu_percent: 99.5, ram_total: 16 * GIB, ram_used: 1.55 * GIB, gpus: [{ name: "G", util_percent: 0.4, vram_total: 8 * GIB, vram_used: 0.25 * GIB }] }));
    render(<HardwareMeters />);
    await screen.findByRole("meter", { name: "CPU" });
    expect(meter("CPU")).toHaveAttribute("aria-valuetext", "100%");
    expect(meter("RAM")).toHaveAttribute("aria-valuetext", "1.6/16.0 GiB");
    expect(meter("GPU, G")).toHaveAttribute("aria-valuetext", "0%");
    expect(meter("VRAM, G")).toHaveAttribute("aria-valuetext", "0.3/8.0 GiB");
  });

  it("numbers the GPUs from 0 when there are several, each with its own name", async () => {
    serve(() =>
      reading({
        gpus: [
          { name: "Alpha 1", util_percent: 10, vram_total: 8 * GIB, vram_used: 2 * GIB },
          { name: "Beta 2", util_percent: 90, vram_total: 24 * GIB, vram_used: 12 * GIB },
        ],
      }),
    );
    render(<HardwareMeters />);
    await screen.findByRole("meter", { name: "CPU" });
    expect(meters()).toEqual(["CPU", "RAM", "GPU 0, Alpha 1", "VRAM 0, Alpha 1", "GPU 1, Beta 2", "VRAM 1, Beta 2"]);
    expect(meter("GPU 1, Beta 2")).toHaveAttribute("aria-valuetext", "90%");
    expect(meter("VRAM 1, Beta 2")).toHaveAttribute("aria-valuetext", "12.0/24.0 GiB");
  });

  it("shows only CPU and RAM on a machine without a GPU", async () => {
    serve(() => reading({ gpus: [] }));
    render(<HardwareMeters />);
    await screen.findByRole("meter", { name: "CPU" });
    expect(meters()).toEqual(["CPU", "RAM"]);
  });

  it("shows nothing when the server cannot read the hardware", async () => {
    const log = serve(() => "fail");
    const { container } = render(<HardwareMeters />);
    await settle();
    expect(log.asked).toBe(1);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("when they ask", () => {
  it("asks again 5 seconds after the answer, not sooner, and shows the new reading", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let cpu = 10;
    const log = serve(() => reading({ cpu_percent: cpu }));
    render(<HardwareMeters />);
    await settle();
    expect(log.asked).toBe(1);
    expect(meter("CPU")).toHaveAttribute("aria-valuetext", "10%");

    await tick(4_999);
    expect(log.asked).toBe(1);

    cpu = 55;
    await tick(1);
    expect(log.asked).toBe(2);
    expect(meter("CPU")).toHaveAttribute("aria-valuetext", "55%");

    cpu = 70;
    await tick(5_000);
    expect(log.asked).toBe(3);
    expect(meter("CPU")).toHaveAttribute("aria-valuetext", "70%");
  });

  it("never has a second request beside a slow one", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    let first = true;
    const log = serve(async () => {
      if (first) {
        first = false;
        await gate;
      }
      return reading();
    });
    render(<HardwareMeters />);
    await settle();
    await tick(60_000);
    expect(log.asked).toBe(1);
    expect(screen.queryAllByRole("meter")).toHaveLength(0);

    release();
    await settle();
    expect(screen.getByRole("meter", { name: "CPU" })).toBeInTheDocument();
    await tick(4_999);
    expect(log.asked).toBe(1);
    await tick(1);
    expect(log.asked).toBe(2);
  });

  it("drops the meters when a later request fails, and brings them back when one works again", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let mode: "ok" | "fail" = "ok";
    const log = serve(() => (mode === "ok" ? reading() : "fail"));
    render(<HardwareMeters />);
    await settle();
    expect(meters()).toContain("CPU");

    mode = "fail";
    await tick(5_000);
    expect(log.asked).toBe(2);
    expect(meters()).toEqual([]);

    mode = "ok";
    await tick(5_000);
    expect(log.asked).toBe(3);
    expect(meters()).toContain("CPU");
  });

  it("stops asking when it is closed", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const log = serve(() => reading());
    const { unmount } = render(<HardwareMeters />);
    await settle();
    unmount();
    await tick(60_000);
    expect(log.asked).toBe(1);
  });

  it("does not ask while the tab is hidden, and asks at once when it comes back", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    visibility = "hidden";
    const log = serve(() => reading());
    render(<HardwareMeters />);
    await settle();
    await tick(60_000);
    expect(log.asked).toBe(0);
    expect(meters()).toEqual([]);

    setVisibility("visible");
    await settle();
    expect(log.asked).toBe(1);
    expect(meters()).toContain("CPU");
  });

  it("stops the 5 second clock when the tab is hidden, and starts it again when it is shown", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const log = serve(() => reading());
    render(<HardwareMeters />);
    await settle();
    expect(log.asked).toBe(1);

    setVisibility("hidden");
    await tick(60_000);
    expect(log.asked).toBe(1);
    // what it showed stays on screen while hidden
    expect(meters()).toContain("CPU");

    setVisibility("visible");
    await settle();
    expect(log.asked).toBe(2);
    await tick(5_000);
    expect(log.asked).toBe(3);
  });

  it("does not ask when the tab is hidden by the time the answer comes in", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const log = serve(async () => {
      await gate;
      return reading();
    });
    render(<HardwareMeters />);
    await settle();
    visibility = "hidden";
    release();
    await settle();
    await tick(60_000);
    expect(log.asked).toBe(1);
  });
});
