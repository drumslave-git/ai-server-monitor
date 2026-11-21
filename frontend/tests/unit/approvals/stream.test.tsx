// The live stream: happy-dom has no EventSource, so this file brings a minimal fake one (stubbed before
// src/live.ts is first imported; live.ts keeps one source for the whole file) and sends events through it.
import { act, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fake = vi.hoisted(() => {
  class FakeEventSource {
    static instances: FakeEventSource[] = [];
    onopen: (() => void) | null = null;
    onerror: (() => void) | null = null;
    private listeners = new Map<string, ((e: { data: string; lastEventId: string }) => void)[]>();
    constructor(public url: string) {
      FakeEventSource.instances.push(this);
    }
    addEventListener(type: string, fn: (e: { data: string; lastEventId: string }) => void) {
      this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
    }
    close() {}
    emit(type: string, data: unknown) {
      for (const fn of this.listeners.get(type) ?? []) fn({ data: JSON.stringify(data), lastEventId: "" });
    }
  }
  (globalThis as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
  return FakeEventSource;
});

import { ApprovalCards } from "../../../src/components/ApprovalCards";
import { AutomationProvider } from "../../../src/automation";
import { ApprovalsPage } from "../../../src/pages/Approvals";
import { multiBatch } from "../../mocks/fixtures/approvals";
import { server } from "../../mocks/server";

const emit = (type: string, data: unknown) => act(() => fake.instances[0].emit(type, data));
const settle = () => new Promise((r) => setTimeout(r, 60));

/** Count the GETs of the paths the pages poll. */
function countGets() {
  const n = { approvals: 0, actions: 0 };
  server.use(
    http.get("/api/approvals", () => {
      n.approvals += 1;
      return HttpResponse.json({ batches: [multiBatch], items: [] });
    }),
    http.get("/api/actions", () => {
      n.actions += 1;
      return HttpResponse.json({ items: [], total: 0 });
    }),
  );
  return n;
}

describe("ApprovalsPage and the live stream", () => {
  let n: ReturnType<typeof countGets>;
  beforeEach(async () => {
    n = countGets();
    render(
      <AutomationProvider>
        <ApprovalsPage />
      </AutomationProvider>,
    );
    await screen.findByRole("article", { name: multiBatch.title });
    await waitFor(() => expect(n.actions).toBe(1));
  });

  it("opens the stream once", () => {
    expect(fake.instances.length).toBeGreaterThan(0);
    expect(fake.instances[0].url).toBe("/api/stream");
  });

  it.each(["approval.pending", "approval.decided", "approval.expired", "action.result", "action.suggested"])("loads the approvals and the audit log again on %s", async (type) => {
    const before = { ...n };
    await emit(type, { id: 41 });
    await waitFor(() => expect(n.approvals).toBeGreaterThan(before.approvals));
    await waitFor(() => expect(n.actions).toBeGreaterThan(before.actions));
  });

  it("does not load anything again on an event of something else", async () => {
    const before = { ...n };
    await emit("job.finished", { slug: "example-job" });
    await emit("chat.message", { id: "x" });
    await settle();
    expect(n).toEqual(before);
  });
});

describe("ApprovalCards and the live stream", () => {
  let approvals = 0;
  beforeEach(async () => {
    approvals = 0;
    server.use(
      http.get("/api/approvals", () => {
        approvals += 1;
        return HttpResponse.json({ batches: [multiBatch], items: [] });
      }),
    );
    render(<ApprovalCards items={[101]} />);
    await screen.findByRole("button", { name: "approve all 3" });
    expect(approvals).toBe(1);
  });

  it("loads again when its approval changes", async () => {
    await emit("approval.updated", { id: 41 });
    await waitFor(() => expect(approvals).toBe(2));
  });

  it("loads again when an action of its approval finishes", async () => {
    await emit("action.result", { id: 555, batch_id: 41 });
    await waitFor(() => expect(approvals).toBe(2));
  });

  it("loads again when one of its own items is the subject of an action event", async () => {
    await emit("action.result", { id: 101, batch_id: null });
    await waitFor(() => expect(approvals).toBe(2));
  });

  it("ignores events of other approvals and actions", async () => {
    await emit("approval.updated", { id: 999 });
    await emit("action.result", { id: 555, batch_id: 999 });
    await settle();
    expect(approvals).toBe(1);
  });
});
