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

  // The links of EntityLink / JobRef / runHref carry "test%3Adisk": App.parseHash decodes the segments once, and the
  // page encodes the slug once where it makes the request (/api/jobs/test%3Adisk, not test%253Adisk).
  it("a slug written the way the entity links write it (test%3Adisk) opens the detail", async () => {
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
    expect(asked.every((p) => p === "/api/jobs/test%3Adisk")).toBe(true);
  });

  it("a malformed escape (%zz) does not break the app: the page says it does not know the job", async () => {
    server.use(http.get("/api/jobs/:slug", () => HttpResponse.json({ detail: "Job not found" }, { status: 404 })));
    location.hash = "#/jobs/%zz";
    render(<App />);
    expect(await screen.findByText(/Job not found/)).toBeInTheDocument();
    expect(screen.getAllByRole("navigation", { name: "Main" }).length).toBeGreaterThan(0);
  });

  it("a malformed escape later in the address also leaves the app standing", async () => {
    location.hash = "#/jobs/loose/runs/%E0%A4%A";
    render(<App />);
    expect(screen.getAllByRole("navigation", { name: "Main" }).length).toBeGreaterThan(0);
    await settle();
  });

  it("the link of a job in the list (JobRef) opens its detail with one encoding in the request", async () => {
    const asked: string[] = [];
    server.use(
      http.get("/api/jobs/:slug", ({ request }) => {
        asked.push(new URL(request.url).pathname);
        const slug = decodeURIComponent(new URL(request.url).pathname.slice("/api/jobs/".length));
        const job = jobsList().items.find((j) => j.slug === slug);
        return job ? HttpResponse.json(job) : HttpResponse.json({ detail: "Job not found" }, { status: 404 });
      }),
    );
    location.hash = "#/jobs";
    render(<App />);
    const link = (await screen.findAllByRole("link", { name: /Disk usage/ })).find((a) => a.getAttribute("href")?.includes("%3A"));
    expect(link, "a link to test:disk written with %3A").toBeDefined();
    expect(link).toHaveAttribute("href", "#/jobs/test%3Adisk");
    act(() => {
      location.hash = link!.getAttribute("href")!;
    });
    expect(await screen.findByRole("heading", { level: 1, name: "Disk usage" })).toBeInTheDocument();
    expect(asked.length).toBeGreaterThan(0);
    expect(asked.every((p) => p === "/api/jobs/test%3Adisk")).toBe(true);
  });
});
