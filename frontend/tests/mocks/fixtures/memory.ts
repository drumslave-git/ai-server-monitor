// Invented data for the Memory page (GET /api/memory): notes the chat model wrote in a chat and in a run, one the owner
// edited, one without an area and one whose writer is gone, so every state of the page has something to show.
import type { ApiRequest, ApiResponse } from "../types";

export type MemoryPage = ApiResponse<"/api/memory">;
export type MemoryNote = MemoryPage["items"][number];
export type MemoryNoteEdit = ApiRequest<"/api/memory/{note_id}", "put">;

const nowSeconds = () => Math.floor(Date.now() / 1000);

export function makeNote(over: Partial<MemoryNote> = {}): MemoryNote {
  const now = nowSeconds();
  return {
    id: 1,
    title: "Backup window",
    body: "Backups of **nas-01** run at 02:00; a slow disk before then is expected.",
    author: "model",
    created: now - 86_400,
    updated: now - 3_600,
    domains: ["monitoring"],
    source: { kind: "chat", id: "3f9c2a7e-1111-4222-8333-444455556666", label: null, exists: true },
    ...over,
  };
}

export function memoryPage(over: Partial<MemoryPage> = {}): MemoryPage {
  const now = nowSeconds();
  return {
    items: [
      makeNote(),
      makeNote({
        id: 2,
        title: "Docker host naming",
        body: "Containers on `lab-02` are named `svc-<name>`.",
        domains: ["docker", "monitoring"],
        updated: now - 7_200,
        source: { kind: "run", id: "41", label: "test:disk", exists: true },
      }),
      makeNote({ id: 3, title: "Owner's rule", body: "Never restart `db-01` before 06:00.", author: "owner", domains: [], updated: now - 600, source: null }),
      makeNote({
        id: 4,
        title: "Old finding",
        body: "The fan of `lab-03` was loud last spring.",
        domains: ["docker"],
        updated: now - 5 * 86_400,
        source: { kind: "chat", id: "aaaabbbb-0000-4000-8000-000000000000", label: null, exists: false },
      }),
    ],
    domains: ["docker", "monitoring", "storage"],
    ...over,
  };
}
