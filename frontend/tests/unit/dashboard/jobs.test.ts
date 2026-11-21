// The words and helpers of src/jobs.ts.
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { ApiError } from "../../../src/api";
import {
  cronText,
  errorText,
  findingCounts,
  firstLine,
  intervalText,
  jobGroups,
  logsHref,
  NO_PREFIX_GROUP,
  originWord,
  runJob,
  scheduleWord,
  whereWord,
} from "../../../src/jobs";
import { server } from "../../mocks/server";

describe("intervalText", () => {
  it.each([
    [60, "every minute"],
    [1800, "every 30 minutes"],
    [3600, "every hour"],
    [7200, "every 2 hours"],
    [86400, "every day"],
    [172800, "every 2 days"],
    [5400, "every 90 minutes"],
    [45, "every 45 seconds"],
  ])("%s s → %s", (seconds, text) => expect(intervalText(seconds)).toBe(text));
});

describe("cronText and scheduleWord", () => {
  it("reads a cron expression in words", () => {
    expect(cronText("0 6 * * *")).toBe("At 06:00");
  });

  it("returns null for an expression it cannot read", () => {
    expect(cronText("not a cron")).toBeNull();
  });

  it("prefers the interval, writes cron words in lowercase, and shows an unreadable cron as written", () => {
    expect(scheduleWord({ interval_s: 1800, cron: "0 6 * * *" })).toBe("every 30 minutes");
    expect(scheduleWord({ interval_s: null, cron: "0 6 * * *" })).toBe("at 06:00");
    expect(scheduleWord({ interval_s: null, cron: "nonsense" })).toBe("nonsense");
    expect(scheduleWord({ interval_s: null, cron: null })).toBe("no schedule");
  });
});

describe("whereWord", () => {
  const names = { daily: "Example daily" };
  it("names the summaries of a topic, by name when known and by slug otherwise", () => {
    expect(whereWord({ kind: "topic", summaries: ["daily", "other"], interval_s: null, cron: null }, names)).toBe("in Example daily, other");
  });
  it("shows its own schedule when it is in no summary, or says it is manual", () => {
    expect(whereWord({ kind: "topic", summaries: [], interval_s: 3600, cron: null }, names)).toBe("every hour");
    expect(whereWord({ kind: "topic", summaries: [], interval_s: null, cron: null }, names)).toBe("no schedule (manual)");
    expect(whereWord({ kind: "summary", summaries: [], interval_s: null, cron: "0 6 * * *" }, names)).toBe("at 06:00");
  });
});

describe("originWord", () => {
  it.each([
    [{ builtin: true, edited: false, created_by: "builtin" }, "builtin"],
    [{ builtin: true, edited: true, created_by: "builtin" }, "builtin, edited"],
    [{ builtin: false, edited: false, created_by: "llm" }, "custom, made by the LLM"],
    [{ builtin: false, edited: false, created_by: "user" }, "custom, made by a user"],
  ] as const)("%j → %s", (job, text) => expect(originWord(job)).toBe(text));
});

describe("jobGroups", () => {
  it("groups by the part of the slug before the colon, sorted, with the jobs without a prefix last", () => {
    const groups = jobGroups([{ slug: "zeta" }, { slug: "docker:status" }, { slug: "health:disk" }, { slug: "docker:resources" }, { slug: "alpha" }]);
    expect(groups.map((g) => g.label)).toEqual(["docker", "health", NO_PREFIX_GROUP]);
    expect(groups[0].jobs.map((j) => j.slug)).toEqual(["docker:status", "docker:resources"]);
    expect(groups[2].jobs.map((j) => j.slug)).toEqual(["zeta", "alpha"]);
  });

  it("treats a slug that starts with a colon as having no prefix", () => {
    expect(jobGroups([{ slug: ":odd" }])[0].label).toBe(NO_PREFIX_GROUP);
  });
});

describe("firstLine", () => {
  it.each([
    ["## Heading\n\nBody", "Heading"],
    ["\n\n  - **Bold** item\nsecond", "Bold item"],
    ["---\nAfter the rule", "After the rule"],
    ["1. First `code`\n2. Second", "First code"],
    ["", ""],
    [null, ""],
    [undefined, ""],
  ])("%j → %j", (md, text) => expect(firstLine(md)).toBe(text));
});

describe("findingCounts", () => {
  it("counts the findings of each severity", () => {
    const f = (severity: "info" | "warn" | "crit") => ({ severity, title: "t", detail: "", evidence: [] });
    expect(findingCounts([f("crit"), f("warn"), f("warn"), f("info")])).toEqual({ crit: 1, warn: 2, info: 1 });
    expect(findingCounts(undefined)).toEqual({ crit: 0, warn: 0, info: 0 });
  });
});

describe("errorText", () => {
  it("returns the detail of an API error when it is a string", () => {
    expect(errorText(new ApiError(409, "Already running"))).toBe("Already running");
  });

  it("writes a validation error list as sentences: the field in words, the message without its prefix", () => {
    const detail = [
      { loc: ["body", "interval_s"], msg: "Value error, must be at least 60" },
      { loc: ["body", "max_steps"], msg: "must be positive" },
      { loc: ["body"], msg: "Value error, something general" },
    ];
    expect(errorText(new ApiError(422, detail))).toBe("Interval: must be at least 60. Max steps: must be positive. something general");
  });

  it("falls back to the status, the message of any error, or the text of anything else", () => {
    expect(errorText(new ApiError(500, null))).toBe("HTTP 500");
    expect(errorText(new Error("boom"))).toBe("boom");
    expect(errorText("plain")).toBe("plain");
  });
});

describe("logsHref", () => {
  it("links to the LLM calls of a job, and of one turn of it when the run has one", () => {
    expect(logsHref({ job_slug: "test:disk", trace_turn: null })).toBe("#/logs/llm/for/job/test%3Adisk");
    expect(logsHref({ job_slug: "test:disk", trace_turn: "turn 1" })).toBe("#/logs/llm/for/job/test%3Adisk/turn%201");
  });
});

describe("runJob", () => {
  it("posts to the run endpoint of the job with its slug encoded, without a body", async () => {
    const seen: { path: string; body: string; type: string | null }[] = [];
    server.use(
      http.post("/api/jobs/:slug/run", async ({ request }) => {
        seen.push({ path: new URL(request.url).pathname, body: await request.text(), type: request.headers.get("content-type") });
        return HttpResponse.json({ run_id: 12 });
      }),
    );
    await expect(runJob("test:disk")).resolves.toEqual({ run_id: 12 });
    expect(seen).toEqual([{ path: "/api/jobs/test%3Adisk/run", body: "", type: null }]);
  });

  it("rejects with the server's error", async () => {
    server.use(http.post("/api/jobs/:slug/run", () => HttpResponse.json({ detail: "No such job" }, { status: 404 })));
    await expect(runJob("nope")).rejects.toMatchObject({ status: 404, message: "No such job" });
  });
});
