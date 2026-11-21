// The calls the dashboard makes besides /api/status (that one is in shell.ts: the dashboard tests and the
// layout checks that need the summaries' state use `dashboardStatus()` from the dashboard fixtures).
import { http, HttpResponse, type RequestHandler } from "msw";
import { dashboardJobs, dashboardRuns } from "../fixtures/dashboard";

export const dashboardHandlers: RequestHandler[] = [
  http.get("/api/jobs", () => HttpResponse.json(dashboardJobs())),
  http.get("/api/jobs/:slug/runs", ({ params }) => HttpResponse.json(dashboardRuns(String(params.slug)))),
  http.post("/api/jobs/:slug/run", () => HttpResponse.json({ run_id: 99 })),
];
