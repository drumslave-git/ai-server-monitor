// The Settings page's calls. (The shell's own GET /api/settings in handlers/shell.ts comes first in
// handlers/index.ts and answers with the small shell document; a test of the Settings page overrides it with
// `server.use(http.get("/api/settings", () => HttpResponse.json(makeSettingsDoc())))`.)
import { http, HttpResponse, type RequestHandler } from "msw";
import { connectionTestOk, llmTestOk, makeSettingsDoc, modelInfoUnavailable } from "../fixtures/settings";

const SECRET_KEYS = new Set(["api_key", "token", "docker_hub_token"]);

/** What the backend sends back for a saved document: every secret that is set is the mask again. */
function masked(value: unknown, key = ""): unknown {
  if (Array.isArray(value)) return value.map((v) => masked(v, key));
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    // an MCP header is {name, value}: its value is the secret
    return Object.fromEntries(Object.entries(object).map(([k, v]) => [k, k === "value" && "name" in object ? maskOne(v) : masked(v, k)]));
  }
  return SECRET_KEYS.has(key) ? maskOne(value) : value;
}
const maskOne = (v: unknown) => (typeof v === "string" && v !== "" ? "********" : v);

export const settingsHandlers: RequestHandler[] = [
  http.get("/api/settings", () => HttpResponse.json(makeSettingsDoc())),
  http.put("/api/settings", async ({ request }) => HttpResponse.json({ values: masked(await request.json()) })),
  http.post("/api/settings/llm-model-info", () => HttpResponse.json(modelInfoUnavailable)),
  http.post("/api/settings/test-llm", () => HttpResponse.json(llmTestOk)),
  http.post("/api/settings/test/:kind", () => HttpResponse.json(connectionTestOk)),
];
