// One shared EventSource for the whole app.
import { useEffect, useRef, useSyncExternalStore } from "react";

export type StreamEvent = { type: string; data: Record<string, any>; id: string };
type Listener = (event: StreamEvent) => void;

const EVENT_TYPES = [
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
];

const listeners = new Set<Listener>();
const stateListeners = new Set<() => void>();
let source: EventSource | null = null;
let connected = false;

function notifyState() {
  stateListeners.forEach((l) => l());
}

function ensureSource() {
  if (source || typeof EventSource === "undefined") return;
  source = new EventSource("/api/stream");
  source.onopen = () => {
    connected = true;
    notifyState();
  };
  source.onerror = () => {
    connected = false;
    notifyState();
  };
  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (raw) => {
      const message = raw as MessageEvent<string>;
      let data: Record<string, any> = {};
      try {
        data = JSON.parse(message.data);
      } catch {
        return;
      }
      const event = { type, data, id: message.lastEventId };
      listeners.forEach((l) => l(event));
    });
  }
}

function subscribeState(cb: () => void) {
  ensureSource();
  stateListeners.add(cb);
  return () => stateListeners.delete(cb);
}

/** Call `handler` for every stream event whose type starts with one of `types`. */
export function useStream(types: string[], handler: (event: StreamEvent) => void) {
  const ref = useRef(handler);
  ref.current = handler;
  const key = types.join(",");
  useEffect(() => {
    ensureSource();
    const wanted = key.split(",");
    const listener: Listener = (event) => {
      if (wanted.some((t) => event.type === t || event.type.startsWith(t + "."))) ref.current(event);
    };
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, [key]);
}

export function useConnected(): boolean {
  return useSyncExternalStore(subscribeState, () => connected);
}
