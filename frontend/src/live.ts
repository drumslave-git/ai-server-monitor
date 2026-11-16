// One shared EventSource for the whole app, plus a small in-memory history of metric ticks.
import { useEffect, useRef, useSyncExternalStore } from "react";

export type StreamEvent = { type: string; data: Record<string, any>; id: string };
type Listener = (event: StreamEvent) => void;

const EVENT_TYPES = [
  "report.started",
  "report.token",
  "report.done",
  "report.failed",
  "report.cancelled",
  "automation.changed",
  "llm.call",
  "bench.progress",
  "bench.done",
  "metrics.tick",
  "collector.status",
  "action.suggested",
  "action.result",
  "approval.pending",
  "approval.updated",
  "approval.progress",
  "approval.decided",
  "approval.expired",
];

const listeners = new Set<Listener>();
const stateListeners = new Set<() => void>();
let source: EventSource | null = null;
let connected = false;

export type SystemTick = {
  ts: number;
  cpu: number | null;
  mem: number | null;
  swap: number | null;
  load: number | null;
  net_rx_bps: number;
  net_tx_bps: number;
};
export type GpuTick = {
  ts: number;
  gpus: { index: number; util: number | null; vram: number | null; temp: number | null; power_w: number | null }[];
};

const HISTORY = 120;
const history = { system: [] as SystemTick[], gpu: [] as GpuTick[], version: 0 };

function notifyState() {
  history.version += 1;
  stateListeners.forEach((l) => l());
}

function seedHistory() {
  fetch("/api/metrics/recent")
    .then((r) => r.json())
    .then((data: { system?: SystemTick[]; gpu?: GpuTick[] }) => {
      // Ticks that arrived on the stream meanwhile are newer; keep them after the seed.
      history.system.unshift(...(data.system ?? []).filter((t) => !history.system.some((h) => h.ts === t.ts)));
      history.gpu.unshift(...(data.gpu ?? []).filter((t) => !history.gpu.some((h) => h.ts === t.ts)));
      history.system.splice(0, Math.max(0, history.system.length - HISTORY));
      history.gpu.splice(0, Math.max(0, history.gpu.length - HISTORY));
      notifyState();
    })
    .catch(() => undefined);
}

function ensureSource() {
  if (source || typeof EventSource === "undefined") return;
  seedHistory();
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
      if (type === "metrics.tick") {
        const list = (data.source === "gpu" ? history.gpu : history.system) as unknown[];
        list.push(data);
        if (list.length > HISTORY) list.shift();
        notifyState();
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

/** Recent metric ticks (oldest first). Re-renders on every tick. */
export function useMetricHistory() {
  useSyncExternalStore(subscribeState, () => history.version);
  return { system: history.system, gpu: history.gpu };
}
