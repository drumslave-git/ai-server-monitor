import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { api, ApiError } from "../../src/api";
import { server } from "../mocks/server";

/** The request the app sent: method, path, query, JSON body, content type. */
function record(method: "get" | "put" | "post" | "patch" | "delete", path: string, answer: () => Response = () => HttpResponse.json({ ok: true })) {
  const seen: { body: unknown; contentType: string | null; search: string }[] = [];
  server.use(
    http[method](path, async ({ request }) => {
      const text = await request.text();
      seen.push({ body: text ? JSON.parse(text) : undefined, contentType: request.headers.get("content-type"), search: new URL(request.url).search });
      return answer();
    }),
  );
  return seen;
}

describe("api", () => {
  it("get returns the parsed JSON and sends no body", async () => {
    const seen = record("get", "/api/thing", () => HttpResponse.json({ a: 1 }));
    await expect(api.get("/api/thing?x=1")).resolves.toEqual({ a: 1 });
    expect(seen).toEqual([{ body: undefined, contentType: null, search: "?x=1" }]);
  });

  it.each([
    ["put", api.put],
    ["post", api.post],
    ["patch", api.patch],
  ] as const)("%s sends the body as JSON", async (method, call) => {
    const seen = record(method, "/api/thing");
    await call("/api/thing", { name: "x" });
    expect(seen[0].body).toEqual({ name: "x" });
    expect(seen[0].contentType).toBe("application/json");
  });

  it("del resolves to null for an empty answer", async () => {
    server.use(http.delete("/api/thing", () => new HttpResponse(null, { status: 204 })));
    await expect(api.del("/api/thing")).resolves.toBeNull();
  });

  it("an error answer throws an ApiError with the status and the detail text as its message", async () => {
    server.use(http.get("/api/thing", () => HttpResponse.json({ detail: "no such thing" }, { status: 404 })));
    const error = await api.get("/api/thing").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, detail: "no such thing", message: "no such thing" });
  });

  it("a validation error list becomes 'field: message' entries without body/query", async () => {
    server.use(
      http.post("/api/thing", () =>
        HttpResponse.json(
          {
            detail: [
              { loc: ["body", "cron"], msg: "invalid cron" },
              { loc: ["query", "limit"], msg: "too big" },
              { loc: ["body"], msg: "bad body" },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    const error = await api.post("/api/thing", {}).catch((e: unknown) => e as ApiError);
    expect((error as ApiError).message).toBe("cron: invalid cron; limit: too big; bad body");
    expect((error as ApiError).status).toBe(422);
  });

  it("an error answer without a detail falls back to the status", async () => {
    server.use(http.get("/api/thing", () => HttpResponse.json({}, { status: 500 })));
    await expect(api.get("/api/thing")).rejects.toMatchObject({ status: 500, message: "HTTP 500" });
  });

  it("an error answer that is not JSON is reported as a failure, not as an ApiError", async () => {
    server.use(http.get("/api/thing", () => new HttpResponse("<html>bad gateway</html>", { status: 502 })));
    await expect(api.get("/api/thing")).rejects.toBeInstanceOf(SyntaxError);
  });
});
