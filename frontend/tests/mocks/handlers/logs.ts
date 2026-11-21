// The calls of the Logs pages. The LLM list honours the filters it is asked for (purpose, status, link) so a
// layout check that picks a filter sees a plausible list; the live tail of the app log (/api/logs/app/stream)
// is an EventSource and has no handler here.
import { http, HttpResponse, type RequestHandler } from "msw";
import { appLog, callList, defaultCalls, defaultLoggers, defaultRecords, makeFullCall, makeStats, makeTimeline, makeToolCall } from "../fixtures/logs";

export const logsHandlers: RequestHandler[] = [
  http.get("/api/logs/llm", ({ request }) => {
    const q = new URL(request.url).searchParams;
    let items = defaultCalls();
    if (q.get("purpose")) items = items.filter((c) => c.purpose === q.get("purpose"));
    if (q.get("status")) items = items.filter((c) => c.status === q.get("status"));
    if (q.get("link_kind")) items = items.filter((c) => c.link_kind === q.get("link_kind") && c.link_id === q.get("link_id"));
    return HttpResponse.json(callList(items));
  }),
  http.get("/api/logs/llm/stats", () => HttpResponse.json(makeStats())),
  http.get("/api/logs/llm/:id/timeline", ({ params }) => HttpResponse.json(makeTimeline({ selected: Number(params.id) }))),
  http.get("/api/logs/llm/:id", ({ params }) => HttpResponse.json(makeFullCall({ id: Number(params.id) }))),
  http.get("/api/logs/tools/:id", ({ params }) => HttpResponse.json(makeToolCall({ id: Number(params.id) }))),
  http.get("/api/logs/app/loggers", () => HttpResponse.json(defaultLoggers)),
  http.get("/api/logs/app", ({ request }) => {
    const q = new URL(request.url).searchParams;
    const source = q.get("source") === "stored" ? "stored" : "memory";
    const floor = { DEBUG: 0, INFO: 20, WARNING: 30, ERROR: 40 }[q.get("level") ?? "DEBUG"] ?? 0;
    let items = defaultRecords().filter((r) => r.levelno >= floor);
    if (q.get("logger")) items = items.filter((r) => r.logger === q.get("logger"));
    if (source === "stored") items = items.filter((r) => r.levelno >= 30).map((r) => ({ ...r, id: r.seq, seq: null }));
    return HttpResponse.json(appLog(items, source));
  }),
];
