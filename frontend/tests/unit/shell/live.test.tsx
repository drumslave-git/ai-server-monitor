// src/live.ts: the one shared event stream. EventSource is a fake (see fake-event-source.ts).
// live.ts keeps one connection for the whole file, so the tests that close it (pagehide) come last.
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useConnected, useStream, type StreamEvent } from "../../../src/live";
import { FakeEventSource } from "./fake-event-source";

function Listener({ types, onEvent }: { types: string[]; onEvent: (event: StreamEvent) => void }) {
  useStream(types, onEvent);
  return null;
}

function Connection({ name = "stream" }: { name?: string }) {
  return <p>{`${name}: ${useConnected() ? "connected" : "disconnected"}`}</p>;
}

const emit = (type: string, data?: unknown, id?: string) => act(() => FakeEventSource.current().emit(type, data, id));

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// This one must run first: nothing has opened the connection yet.
describe("without EventSource", () => {
  it("is quiet: nothing connects, nothing breaks", () => {
    vi.stubGlobal("EventSource", undefined);
    const onEvent = vi.fn();
    render(
      <>
        <Listener types={["job"]} onEvent={onEvent} />
        <Connection />
      </>,
    );
    expect(screen.getByText("stream: disconnected")).toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(onEvent).not.toHaveBeenCalled();
  });
});

describe("useStream", () => {
  it("opens /api/stream, once, however many hooks listen", () => {
    render(<Listener types={["job"]} onEvent={vi.fn()} />);
    const count = FakeEventSource.instances.length;
    expect(count).toBe(1);
    expect(FakeEventSource.current().url).toBe("/api/stream");
    render(
      <>
        <Listener types={["approval"]} onEvent={vi.fn()} />
        <Listener types={["chat"]} onEvent={vi.fn()} />
        <Connection />
      </>,
    );
    expect(FakeEventSource.instances).toHaveLength(count);
  });

  it("hands over the type, the decoded data and the event id", () => {
    const onEvent = vi.fn();
    render(<Listener types={["approval"]} onEvent={onEvent} />);
    emit("approval.pending", { id: 5, title: "Restart" }, "42");
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledWith({ type: "approval.pending", data: { id: 5, title: "Restart" }, id: "42" });
  });

  it("matches by the start of the type: 'job' takes every job.* event and only those", () => {
    const onEvent = vi.fn();
    render(<Listener types={["job"]} onEvent={onEvent} />);
    emit("job.started", { slug: "a" });
    emit("job.finished", { slug: "a" });
    emit("chat.message", {});
    emit("automation.changed", {});
    emit("approval.pending", {});
    expect(onEvent.mock.calls.map(([e]) => e.type)).toEqual(["job.started", "job.finished"]);
  });

  it("matches a whole type, and the longer types that continue it after a dot", () => {
    const onEvent = vi.fn();
    render(<Listener types={["chat.message"]} onEvent={onEvent} />);
    emit("chat.message", {});
    emit("chat.message.updated", {});
    expect(onEvent.mock.calls.map(([e]) => e.type)).toEqual(["chat.message", "chat.message.updated"]);
  });

  it("does not match half of a word: 'approval.pen' is not 'approval.pending'", () => {
    const onEvent = vi.fn();
    render(<Listener types={["approval.pen", "approv"]} onEvent={onEvent} />);
    emit("approval.pending", {});
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("takes several types at once", () => {
    const onEvent = vi.fn();
    render(<Listener types={["approval", "action"]} onEvent={onEvent} />);
    emit("approval.decided", {});
    emit("action.result", {});
    emit("action.suggested", {});
    emit("llm.call", {});
    expect(onEvent.mock.calls.map(([e]) => e.type)).toEqual(["approval.decided", "action.result", "action.suggested"]);
  });

  it.each([
    "automation.changed",
    "llm.call",
    "action.suggested",
    "action.result",
    "approval.pending",
    "approval.updated",
    "approval.progress",
    "approval.decided",
    "approval.expired",
    "job.started",
    "job.finished",
    "chat.message",
    "chat.message.updated",
  ])("delivers the server's %s event", (type) => {
    const onEvent = vi.fn();
    render(<Listener types={[type]} onEvent={onEvent} />);
    emit(type, { ok: true });
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ type, data: { ok: true } }));
  });

  it("gives every listener its own copy of the call", () => {
    const a = vi.fn();
    const b = vi.fn();
    render(
      <>
        <Listener types={["job"]} onEvent={a} />
        <Listener types={["job", "chat"]} onEvent={b} />
      </>,
    );
    emit("job.started", {});
    emit("chat.message", {});
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(2);
  });

  it("ignores an event whose data is not JSON, and keeps working", () => {
    const onEvent = vi.fn();
    render(<Listener types={["job"]} onEvent={onEvent} />);
    emit("job.started", "this is { not json");
    expect(onEvent).not.toHaveBeenCalled();
    emit("job.finished", { slug: "a" });
    expect(onEvent).toHaveBeenCalledTimes(1);
  });

  it("calls the newest handler after a re-render, without listening twice", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<Listener types={["job"]} onEvent={first} />);
    rerender(<Listener types={["job"]} onEvent={second} />);
    emit("job.started", {});
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("follows a change of the wanted types", () => {
    const onEvent = vi.fn();
    const { rerender } = render(<Listener types={["job"]} onEvent={onEvent} />);
    rerender(<Listener types={["chat"]} onEvent={onEvent} />);
    emit("job.started", {});
    emit("chat.message", {});
    expect(onEvent.mock.calls.map(([e]) => e.type)).toEqual(["chat.message"]);
  });

  it("stops calling when its component is gone", () => {
    const onEvent = vi.fn();
    const { unmount } = render(<Listener types={["job"]} onEvent={onEvent} />);
    unmount();
    emit("job.started", {});
    expect(onEvent).not.toHaveBeenCalled();
  });
});

describe("useConnected", () => {
  it("follows the connection: open, broken, open again, in every component that asks", () => {
    render(
      <>
        <Connection name="one" />
        <Connection name="two" />
      </>,
    );
    act(() => FakeEventSource.current().fail());
    expect(screen.getByText("one: disconnected")).toBeInTheDocument();
    expect(screen.getByText("two: disconnected")).toBeInTheDocument();

    act(() => FakeEventSource.current().open());
    expect(screen.getByText("one: connected")).toBeInTheDocument();
    expect(screen.getByText("two: connected")).toBeInTheDocument();

    act(() => FakeEventSource.current().fail());
    expect(screen.getByText("one: disconnected")).toBeInTheDocument();
    expect(screen.getByText("two: disconnected")).toBeInTheDocument();

    act(() => FakeEventSource.current().open());
    expect(screen.getByText("one: connected")).toBeInTheDocument();
  });

  it("starts with what the connection already is for a component that arrives late", () => {
    render(<Connection name="early" />);
    act(() => FakeEventSource.current().open());
    render(<Connection name="late" />);
    expect(screen.getByText("late: connected")).toBeInTheDocument();
    act(() => FakeEventSource.current().fail());
  });

  it("does not need a component after it is gone", () => {
    const { unmount } = render(<Connection />);
    unmount();
    expect(() => act(() => FakeEventSource.current().open())).not.toThrow();
    act(() => FakeEventSource.current().fail());
  });
});

// The page cache: these close and reopen the connection, so they come last.
describe("leaving and coming back to the page", () => {
  const pageshow = (persisted: boolean) => act(() => void window.dispatchEvent(Object.assign(new Event("pageshow"), { persisted })));

  it("closes the connection when the page is left", () => {
    render(<Connection />);
    act(() => FakeEventSource.current().open());
    expect(screen.getByText("stream: connected")).toBeInTheDocument();
    const source = FakeEventSource.current();
    expect(source.closed).toBe(false);

    act(() => void window.dispatchEvent(new Event("pagehide")));
    expect(source.closed).toBe(true);
  });

  it("puts the lamp out when the page is left (a component that shows the connection is told)", () => {
    render(<Connection />);
    act(() => FakeEventSource.current().open());
    expect(screen.getByText("stream: connected")).toBeInTheDocument();
    act(() => void window.dispatchEvent(new Event("pagehide")));
    expect(screen.getByText("stream: disconnected")).toBeInTheDocument();
  });

  it("does not open it again when the page is shown fresh (not from the cache)", () => {
    render(<Connection />);
    const count = FakeEventSource.instances.length;
    pageshow(false);
    expect(FakeEventSource.instances).toHaveLength(count);
  });

  it("opens a new connection when the page comes back from the cache, and the old listeners get its events", () => {
    const onEvent = vi.fn();
    render(
      <>
        <Listener types={["job"]} onEvent={onEvent} />
        <Connection />
      </>,
    );
    const count = FakeEventSource.instances.length;
    const old = FakeEventSource.current();
    act(() => void window.dispatchEvent(new Event("pagehide")));
    expect(old.closed).toBe(true);

    pageshow(true);
    expect(screen.getByText("stream: disconnected")).toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(count + 1);
    const fresh = FakeEventSource.current();
    expect(fresh).not.toBe(old);
    expect(fresh.closed).toBe(false);
    expect(fresh.url).toBe("/api/stream");

    act(() => fresh.emit("job.started", { slug: "a" }));
    expect(onEvent).toHaveBeenCalledTimes(1);
    act(() => fresh.open());
    expect(screen.getByText("stream: connected")).toBeInTheDocument();
  });

  it("opens a connection for a component that arrives after the page was left", () => {
    render(<Connection />);
    act(() => void window.dispatchEvent(new Event("pagehide")));
    const count = FakeEventSource.instances.length;
    render(<Listener types={["job"]} onEvent={vi.fn()} />);
    expect(FakeEventSource.instances).toHaveLength(count + 1);
    expect(FakeEventSource.current().closed).toBe(false);
  });
});
