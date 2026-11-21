import { http, HttpResponse, type RequestHandler } from "msw";
import { answerEvents, explainEvents, runEvents, sessionDocs, sessionList, sseResponse } from "../fixtures/chat";

export const chatHandlers: RequestHandler[] = [
  http.get("/api/chat/sessions", () => HttpResponse.json(sessionList)),
  http.get("/api/chat/sessions/:id", ({ params }) => {
    const doc = sessionDocs[String(params.id)];
    return doc ? HttpResponse.json(doc) : HttpResponse.json({ detail: "no such conversation" }, { status: 404 });
  }),
  http.delete("/api/chat/sessions/:id", () => new HttpResponse(null, { status: 204 })),
  // The answers come as event streams.
  http.post("/api/chat", () => sseResponse(answerEvents())),
  http.post("/api/chat/explain", async ({ request }) => {
    const body = (await request.json()) as { run_id: number; finding: number | null };
    const label = body.finding === null ? "Explain this run" : "Explain this finding";
    return sseResponse(explainEvents(label, `Run ${body.run_id} of Example job: one finding.`));
  }),
  http.post("/api/chat/sessions/:id/run", async ({ request }) => {
    const body = (await request.json()) as { command: string };
    return sseResponse(runEvents(body.command));
  }),
];
