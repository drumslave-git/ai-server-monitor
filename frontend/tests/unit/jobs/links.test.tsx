// The links of other pages to a job (`jobHref`, `runHref`: the slug is percent-encoded, "test:disk" is "test%3Adisk")
// followed through the real shell: the address goes through App's hash routing into the Jobs page.
import { act, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../../../src/App";
import { jobsList } from "../../mocks/fixtures/jobs";
import { server } from "../../mocks/server";
import { installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();

const settle = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 60)));
afterEach(async () => {
  await settle();
  location.hash = "";
});

describe("an address with an encoded slug", () => {
  it("a slug without special characters opens the detail", async () => {
    location.hash = "#/jobs/loose";
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "Loose check" })).toBeInTheDocument();
  });

  it("a slug written with a colon opens the detail", async () => {
    location.hash = "#/jobs/test:disk";
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "Disk usage" })).toBeInTheDocument();
  });

  // FINDING: the links of EntityLink / JobRef / runHref carry "test%3Adisk", but App.parseHash does not decode the
  // segments, and JobDetail encodes the slug again: the request goes to /api/jobs/test%253Adisk, which is not found.
  it.fails("a slug written the way the entity links write it (test%3Adisk) opens the detail", async () => {
    const asked: string[] = [];
    server.use(
      http.get("/api/jobs", () => HttpResponse.json(jobsList())),
      http.get("/api/jobs/:slug", ({ request }) => {
        const path = new URL(request.url).pathname;
        asked.push(path);
        // what the server does: the path segment is decoded once
        const job = jobsList().items.find((j) => j.slug === decodeURIComponent(path.slice("/api/jobs/".length)));
        return job ? HttpResponse.json(job) : HttpResponse.json({ detail: "Job not found" }, { status: 404 });
      }),
    );
    location.hash = "#/jobs/test%3Adisk";
    render(<App />);
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    expect(await screen.findByRole("heading", { level: 1, name: "Disk usage" }, { timeout: 1000 })).toBeInTheDocument();
  });
});
