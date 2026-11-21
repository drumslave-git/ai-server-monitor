// The calls the Jobs page makes besides the three the dashboard handlers answer (GET /api/jobs, GET /api/jobs/:slug/runs,
// POST /api/jobs/:slug/run) and GET /api/mcp/tools (handlers/mcp.ts). Saves answer with the job as the API would store it.
import { http, HttpResponse, type RequestHandler } from "msw";
import { makeJob, nowSeconds } from "../fixtures/dashboard";
import { jobsList, runWithQueue, type JobCreateBody, type JobUpdateBody } from "../fixtures/jobs";

/** The slug of `/api/jobs/<slug>[/...]` as a server reads it: the path segment decoded once (msw's `params` are decoded
 *  already, and decoding them again would hide a link that encodes the slug twice). */
const slugOf = (request: Request) => decodeURIComponent(new URL(request.url).pathname.split("/")[3]);

export const jobsHandlers: RequestHandler[] = [
  http.get("/api/jobs/runs/:id", ({ params }) => {
    const run = runWithQueue(Number(params.id));
    return run ? HttpResponse.json(run) : HttpResponse.json({ detail: "Run not found" }, { status: 404 });
  }),
  http.get("/api/jobs/:slug", ({ request }) => {
    const job = jobsList().items.find((j) => j.slug === slugOf(request));
    return job ? HttpResponse.json(job) : HttpResponse.json({ detail: "Job not found" }, { status: 404 });
  }),
  http.post("/api/jobs/classify-domains", () => HttpResponse.json({ domains: ["docker"] })),
  http.post("/api/jobs", async ({ request }) => {
    const body = (await request.json()) as JobCreateBody;
    return HttpResponse.json(makeJob({ ...body, notify: body.notify ?? "changed", model: body.model ?? "worker", domains: body.domains ?? [], members: body.members ?? [], summaries: body.summaries ?? [] }), { status: 201 });
  }),
  http.put("/api/jobs/:slug", async ({ request }) => {
    const job = jobsList().items.find((j) => j.slug === slugOf(request));
    if (!job) return HttpResponse.json({ detail: "Job not found" }, { status: 404 });
    const body = (await request.json()) as JobUpdateBody;
    const changed = Object.fromEntries(Object.entries(body).filter(([, v]) => v !== undefined && v !== null));
    return HttpResponse.json({ ...job, ...changed, updated: nowSeconds() });
  }),
  http.delete("/api/jobs/:slug", () => new HttpResponse(null, { status: 204 })),
  http.post("/api/jobs/:slug/reset", ({ request }) => {
    const job = jobsList().items.find((j) => j.slug === slugOf(request));
    return job ? HttpResponse.json({ ...job, edited: false, updated: nowSeconds(), notice: "Back to the version of this release." }) : HttpResponse.json({ detail: "Job not found" }, { status: 404 });
  }),
];
