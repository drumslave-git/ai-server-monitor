import { http, HttpResponse, type RequestHandler } from "msw";
import { allBatches, auditLog, pendingBatches } from "../fixtures/approvals";

/** Happy-path answers of the approvals page and the approval cards of a chat answer:
 *  - GET /api/approvals: the waiting approvals; with `of=<item id>` (repeatable) the approvals of those items
 *  - GET /api/approvals/{id}: one approval, decided or not (404 when unknown)
 *  - POST /api/approvals/{id}: the approval as it is (the decision changes nothing here)
 *  - GET /api/actions: the audit log, filtered by status, action and batch, cut at `limit` */
export const approvalsHandlers: RequestHandler[] = [
  http.get("/api/approvals", ({ request }) => {
    const of = new URL(request.url).searchParams.getAll("of").map(Number);
    if (of.length) return HttpResponse.json({ batches: allBatches.filter((b) => b.items.some((i) => of.includes(i.id))), items: [] });
    return HttpResponse.json({ batches: pendingBatches, items: pendingBatches.flatMap((b) => b.items) });
  }),
  http.get("/api/approvals/:id", ({ params }) => {
    const found = allBatches.find((b) => b.id === Number(params.id));
    return found ? HttpResponse.json(found) : HttpResponse.json({ detail: "No such approval" }, { status: 404 });
  }),
  http.post("/api/approvals/:id", ({ params }) => {
    const found = allBatches.find((b) => b.id === Number(params.id));
    return found ? HttpResponse.json(found) : HttpResponse.json({ detail: "No such approval" }, { status: 404 });
  }),
  http.get("/api/actions", ({ request }) => {
    const q = new URL(request.url).searchParams;
    const limit = Number(q.get("limit") ?? 25);
    const rows = auditLog.filter(
      (a) => (!q.get("status") || a.status === q.get("status")) && (!q.get("action") || a.action === q.get("action")) && (!q.get("batch") || a.batch_id === Number(q.get("batch"))),
    );
    return HttpResponse.json({ items: rows.slice(0, limit), total: rows.length });
  }),
];
