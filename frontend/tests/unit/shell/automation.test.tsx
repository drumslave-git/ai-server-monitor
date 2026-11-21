// The global pause switch: the switch in the rail, the notice while paused, the calls behind them.
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AutomationNotice, AutomationProvider, AutomationSwitch, sinceText, type AutomationState } from "../../../src/automation";
import { server } from "../../mocks/server";
import { FakeEventSource, installFakeEventSource } from "./fake-event-source";

installFakeEventSource();

const running: AutomationState = { paused: false, since: null, by: null, queued_actions: 0 };
const paused = (over: Partial<AutomationState> = {}): AutomationState => ({ paused: true, since: Date.now() / 1000 - 600, by: "ui", queued_actions: 0, ...over });

/** The real wait: the timers of a test may be fake, the answers of the mock server are not. */
const settle = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 60)));

const serve = (state: AutomationState) => server.use(http.get("/api/automation", () => HttpResponse.json(state)));

function renderAutomation() {
  return render(
    <AutomationProvider>
      <AutomationSwitch />
      <AutomationNotice />
    </AutomationProvider>,
  );
}

afterEach(() => {
  vi.useRealTimers();
});

describe("while automation runs", () => {
  it("shows that it runs, with the button that pauses it, and no notice", async () => {
    serve(running);
    renderAutomation();
    expect(await screen.findByText("automation running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "pause automation" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "resume automation" })).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText(/Automation paused/)).not.toBeInTheDocument();
  });

  it("pauses with a POST to /api/automation/pause and shows the paused state from the answer", async () => {
    serve(running);
    const posts: { path: string; body: unknown }[] = [];
    server.use(
      http.post("/api/automation/:what", async ({ request, params }) => {
        posts.push({ path: String(params.what), body: await request.json() });
        return HttpResponse.json(paused({ queued_actions: 0 }));
      }),
    );
    const user = userEvent.setup();
    renderAutomation();
    await user.click(await screen.findByRole("button", { name: "pause automation" }));
    expect(await screen.findByText("automation paused")).toBeInTheDocument();
    expect(posts).toEqual([{ path: "pause", body: {} }]);
    expect(screen.getByRole("status")).toHaveTextContent("Automation paused since");
    expect(screen.getByRole("button", { name: "resume automation" })).toBeInTheDocument();
  });

  it("disables the button while the request is on its way", async () => {
    serve(running);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.post("/api/automation/pause", async () => {
        await gate;
        return HttpResponse.json(paused());
      }),
    );
    const user = userEvent.setup();
    renderAutomation();
    await user.click(await screen.findByRole("button", { name: "pause automation" }));
    expect(screen.getByRole("button", { name: "pause automation" })).toBeDisabled();
    release();
    expect(await screen.findByText("automation paused")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "resume automation" })).toBeEnabled();
  });

  it("says why the pause failed, and stays running", async () => {
    serve(running);
    server.use(http.post("/api/automation/pause", () => HttpResponse.json({ detail: "Cannot pause now" }, { status: 409 })));
    const user = userEvent.setup();
    renderAutomation();
    await user.click(await screen.findByRole("button", { name: "pause automation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot pause now");
    expect(screen.getByText("automation running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "pause automation" })).toBeEnabled();
  });

  it("drops the error when the next try works", async () => {
    serve(running);
    let fail = true;
    server.use(
      http.post("/api/automation/pause", () => (fail ? HttpResponse.json({ detail: "Cannot pause now" }, { status: 409 }) : HttpResponse.json(paused()))),
    );
    const user = userEvent.setup();
    renderAutomation();
    await user.click(await screen.findByRole("button", { name: "pause automation" }));
    await screen.findByRole("alert");
    fail = false;
    await user.click(screen.getByRole("button", { name: "pause automation" }));
    expect(await screen.findByText("automation paused")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("while automation is paused", () => {
  it("shows that it is paused, the notice with the time, and both ways to resume", async () => {
    serve(paused());
    renderAutomation();
    expect(await screen.findByText("automation paused")).toBeInTheDocument();
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent(/Automation paused since \S/);
    expect(notice).toHaveTextContent("Scheduled jobs and automations are stopped");
    expect(notice).toHaveTextContent("Chat, approvals and the Telegram bot keep working.");
    expect(within(notice).getByRole("button", { name: "resume" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "resume automation" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "pause automation" })).not.toBeInTheDocument();
  });

  it("leaves the time out when the server does not know since when", async () => {
    serve(paused({ since: null }));
    renderAutomation();
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Automation paused");
    expect(notice).not.toHaveTextContent("since");
  });

  it("says nothing about queued actions when there are none", async () => {
    serve(paused({ queued_actions: 0 }));
    renderAutomation();
    expect(await screen.findByRole("status")).not.toHaveTextContent("will run after the resume");
  });

  it("says one queued action waits", async () => {
    serve(paused({ queued_actions: 1 }));
    renderAutomation();
    expect(await screen.findByRole("status")).toHaveTextContent("1 approved action waits and will run after the resume.");
  });

  it("says several queued actions wait", async () => {
    serve(paused({ queued_actions: 3 }));
    renderAutomation();
    expect(await screen.findByRole("status")).toHaveTextContent("3 approved actions wait and will run after the resume.");
  });

  it.each([
    ["resume automation", "the rail"],
    ["resume", "the notice"],
  ])("resumes with a POST to /api/automation/resume from %s (%s)", async (name) => {
    serve(paused());
    const posts: { path: string; body: unknown }[] = [];
    server.use(
      http.post("/api/automation/:what", async ({ request, params }) => {
        posts.push({ path: String(params.what), body: await request.json() });
        return HttpResponse.json(running);
      }),
    );
    const user = userEvent.setup();
    renderAutomation();
    await user.click(await screen.findByRole("button", { name }));
    expect(await screen.findByText("automation running")).toBeInTheDocument();
    expect(posts).toEqual([{ path: "resume", body: {} }]);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "pause automation" })).toBeInTheDocument();
  });

  it("disables both resume buttons while the request is on its way", async () => {
    serve(paused());
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.post("/api/automation/resume", async () => {
        await gate;
        return HttpResponse.json(running);
      }),
    );
    const user = userEvent.setup();
    renderAutomation();
    await user.click(await screen.findByRole("button", { name: "resume automation" }));
    expect(screen.getByRole("button", { name: "resume automation" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "resume" })).toBeDisabled();
    release();
    expect(await screen.findByText("automation running")).toBeInTheDocument();
  });

  it("shows the reason inside the notice when the resume failed, and stays paused", async () => {
    serve(paused());
    server.use(http.post("/api/automation/resume", () => HttpResponse.json({ detail: "Cannot resume now" }, { status: 500 })));
    const user = userEvent.setup();
    renderAutomation();
    await user.click(await screen.findByRole("button", { name: "resume" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Cannot resume now");
    expect(screen.getByRole("status")).toContainElement(alert);
    expect(screen.getByText("automation paused")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "resume" })).toBeEnabled();
  });
});

describe("loading", () => {
  it("shows nothing until the state is known", () => {
    serve(running);
    const { container } = renderAutomation();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows nothing when the state cannot be read", async () => {
    server.use(http.get("/api/automation", () => HttpResponse.json({ detail: "down" }, { status: 500 })));
    const { container } = renderAutomation();
    await settle();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows nothing outside a provider", () => {
    const { container } = render(<AutomationSwitch />);
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps the last known state when a later read fails", async () => {
    let fail = false;
    server.use(http.get("/api/automation", () => (fail ? HttpResponse.json({ detail: "down" }, { status: 500 }) : HttpResponse.json(paused()))));
    renderAutomation();
    await screen.findByText("automation paused");
    fail = true;
    act(() => FakeEventSource.current().emit("automation.changed", {}));
    await settle();
    expect(screen.getByText("automation paused")).toBeInTheDocument();
  });
});

describe("staying current", () => {
  it("asks again every 30 seconds, not sooner, and stops when it is closed", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    let asked = 0;
    let state = running;
    server.use(
      http.get("/api/automation", () => {
        asked += 1;
        return HttpResponse.json(state);
      }),
    );
    const { unmount } = renderAutomation();
    await settle();
    expect(asked).toBe(1);
    expect(screen.getByText("automation running")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(29_999));
    await settle();
    expect(asked).toBe(1);

    state = paused();
    act(() => vi.advanceTimersByTime(1));
    await settle();
    expect(asked).toBe(2);
    expect(screen.getByText("automation paused")).toBeInTheDocument();

    unmount();
    act(() => vi.advanceTimersByTime(120_000));
    await settle();
    expect(asked).toBe(2);
  });

  it.each(["automation.changed", "approval.pending", "approval.decided", "action.result"])("asks again when the stream says %s", async (type) => {
    let state = running;
    let asked = 0;
    server.use(
      http.get("/api/automation", () => {
        asked += 1;
        return HttpResponse.json(state);
      }),
    );
    renderAutomation();
    await screen.findByText("automation running");
    const before = asked;
    state = paused();
    act(() => FakeEventSource.current().emit(type, {}));
    expect(await screen.findByText("automation paused")).toBeInTheDocument();
    expect(asked).toBeGreaterThan(before);
  });

  it.each(["job.finished", "chat.message", "llm.call"])("does not ask again for %s", async (type) => {
    let asked = 0;
    server.use(
      http.get("/api/automation", () => {
        asked += 1;
        return HttpResponse.json(running);
      }),
    );
    renderAutomation();
    await screen.findByText("automation running");
    const before = asked;
    act(() => FakeEventSource.current().emit(type, {}));
    await settle();
    expect(asked).toBe(before);
  });
});

describe("sinceText", () => {
  it("writes only the time for a moment of today", () => {
    const ts = Date.now() / 1000;
    expect(sinceText(ts)).toBe(new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }));
  });

  it("adds the day for another day", () => {
    const ts = Date.now() / 1000 - 3 * 86_400;
    const d = new Date(ts * 1000);
    const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    const text = sinceText(ts);
    expect(text).toBe(`${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${time}`);
    expect(text).toContain(",");
    expect(sinceText(Date.now() / 1000)).not.toContain(",");
  });
});
