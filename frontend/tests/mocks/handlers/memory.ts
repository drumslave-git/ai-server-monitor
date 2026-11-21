// The calls of the Memory page: the list of notes, an edit and a delete (the last two answer as the API does).
import { http, HttpResponse, type RequestHandler } from "msw";
import { memoryPage, type MemoryNoteEdit } from "../fixtures/memory";

export const memoryHandlers: RequestHandler[] = [
  http.get("/api/memory", () => HttpResponse.json(memoryPage())),
  http.put("/api/memory/:id", async ({ params, request }) => {
    const note = memoryPage().items.find((n) => n.id === Number(params.id));
    if (!note) return HttpResponse.json({ detail: "Note not found" }, { status: 404 });
    const body = (await request.json()) as MemoryNoteEdit;
    return HttpResponse.json({ ...note, ...body, author: "owner", updated: Math.floor(Date.now() / 1000) });
  }),
  http.delete("/api/memory/:id", () => new HttpResponse(null, { status: 204 })),
];
