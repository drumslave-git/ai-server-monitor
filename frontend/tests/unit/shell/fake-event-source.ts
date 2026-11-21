// A minimal EventSource for the tests that need a stream event (happy-dom has none, so live.ts is inert
// without it). `installFakeEventSource()` stubs the global; live.ts keeps ONE module-level source for the
// whole test file, so use `FakeEventSource.current()` (the newest one) and do not rely on a fresh source per test.
import { vi } from "vitest";

type Listener = (event: MessageEvent<string>) => void;

export class FakeEventSource {
  static instances: FakeEventSource[] = [];
  /** The newest source live.ts opened (undefined before anything subscribed). */
  static current(): FakeEventSource {
    const source = FakeEventSource.instances.at(-1);
    if (!source) throw new Error("live.ts has not opened an EventSource yet");
    return source;
  }

  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private listeners = new Map<string, Listener[]>();

  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close() {
    this.closed = true;
  }

  /** The server sent an event: `data` is JSON-encoded unless it is already a string. */
  emit(type: string, data: unknown = {}, lastEventId = "1") {
    const raw = typeof data === "string" ? data : JSON.stringify(data);
    for (const listener of this.listeners.get(type) ?? []) listener({ data: raw, lastEventId } as MessageEvent<string>);
  }

  /** The connection opened. */
  open() {
    this.onopen?.();
  }

  /** The connection broke. */
  fail() {
    this.onerror?.();
  }
}

export function installFakeEventSource() {
  vi.stubGlobal("EventSource", FakeEventSource);
}
