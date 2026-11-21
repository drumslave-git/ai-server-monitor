// The Logs page picks its view from the address after #/logs: the list of LLM calls (the default), one call, the
// calls of a chat or job, or the app log.
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { LogsPage } from "../../../src/pages/Logs";
import { CHAT_ID, callList, defaultCalls, makeCall } from "../../mocks/fixtures/logs";
import { server } from "../../mocks/server";
import { FakeEventSource, installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();
beforeEach(() => {
  FakeEventSource.instances.length = 0;
  location.hash = "#/logs";
});
afterEach(() => {
  location.hash = "";
});

/** Record the query of each list request. */
function recordList() {
  const seen: URLSearchParams[] = [];
  server.use(
    http.get("/api/logs/llm", ({ request }) => {
      seen.push(new URL(request.url).searchParams);
      return HttpResponse.json(callList(defaultCalls()));
    }),
  );
  return seen;
}

describe("LogsPage", () => {
  it("shows the list of LLM calls with no address after #/logs", async () => {
    render(<LogsPage params={[]} />);
    expect(await screen.findByRole("link", { name: "The disk on host-a is 61 percent full." })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Logs" })).toBeInTheDocument();
    expect(screen.getByLabelText("Search in messages")).toBeInTheDocument();
  });

  it("shows the same list for llm", async () => {
    render(<LogsPage params={["llm"]} />);
    expect(await screen.findByRole("link", { name: "The disk on host-a is 61 percent full." })).toBeInTheDocument();
  });

  it("shows the list for an address it does not know", async () => {
    render(<LogsPage params={["whatever"]} />);
    expect(await screen.findByRole("link", { name: "The disk on host-a is 61 percent full." })).toBeInTheDocument();
  });

  it("reads any first word but app as llm: whatever/12 is call 12", async () => {
    render(<LogsPage params={["whatever", "12"]} />);
    expect(await screen.findByRole("heading", { level: 1, name: "Chat" })).toBeInTheDocument();
    expect(screen.getByText("call #12")).toBeInTheDocument();
  });

  it("shows the list, not the call, for llm/12abc", async () => {
    render(<LogsPage params={["llm", "12abc"]} />);
    expect(await screen.findByLabelText("Search in messages")).toBeInTheDocument();
    expect(screen.queryByText("call #12abc")).not.toBeInTheDocument();
  });

  it("shows one call for llm/<number>", async () => {
    render(<LogsPage params={["llm", "12"]} />);
    expect(await screen.findByRole("heading", { level: 1, name: "Chat" })).toBeInTheDocument();
    expect(screen.getByText("call #12")).toBeInTheDocument();
    expect(screen.queryByLabelText("Search in messages")).not.toBeInTheDocument();
  });

  it("asks for the call whose number is in the address", async () => {
    const ids: string[] = [];
    server.use(
      http.get("/api/logs/llm/:id", ({ params }) => {
        ids.push(String(params.id));
        return HttpResponse.json({ detail: "gone" }, { status: 404 });
      }),
    );
    render(<LogsPage params={["llm", "345"]} />);
    expect(await screen.findByText("gone")).toBeInTheDocument();
    expect(ids).toEqual(["345"]);
    expect(screen.getByText("call #345")).toBeInTheDocument();
  });

  it("shows the app log for app", async () => {
    render(<LogsPage params={["app"]} />);
    expect(await screen.findByRole("heading", { level: 1, name: "Logs" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Search in messages")).not.toBeInTheDocument();
    expect(await screen.findByText("Model call failed")).toBeInTheDocument();
  });

  it("ignores what follows app", async () => {
    render(<LogsPage params={["app", "12"]} />);
    expect(await screen.findByText("Model call failed")).toBeInTheDocument();
    expect(screen.queryByText("call #12")).not.toBeInTheDocument();
  });

  it("filters the list to the calls of a chat for llm/for/<kind>/<id>", async () => {
    const seen = recordList();
    render(<LogsPage params={["llm", "for", "chat", CHAT_ID]} />);
    await screen.findByRole("link", { name: /^The disk on host-a/ });
    expect(seen.length).toBeGreaterThan(0);
    expect(seen[0].get("link_kind")).toBe("chat");
    expect(seen[0].get("link_id")).toBe(CHAT_ID);
    expect(seen[0].has("turn")).toBe(false);
  });

  it("adds the turn when the address has one", async () => {
    const seen = recordList();
    render(<LogsPage params={["llm", "for", "chat", CHAT_ID, "turn-7"]} />);
    await screen.findByRole("link", { name: /^The disk on host-a/ });
    expect(seen[0].get("turn")).toBe("turn-7");
  });

  it("jumps to the call when the chat has only one", async () => {
    server.use(http.get("/api/logs/llm", () => HttpResponse.json(callList([makeCall({ id: 31 })]))));
    render(<LogsPage params={["llm", "for", "chat", CHAT_ID]} />);
    await screen.findByRole("link", { name: /^The disk on host-a/ });
    expect(location.hash).toBe("#/logs/llm/31");
  });

  it("does not filter when the address after for is incomplete", async () => {
    const seen = recordList();
    render(<LogsPage params={["llm", "for", "chat"]} />);
    await screen.findByRole("link", { name: /^The disk on host-a/ });
    expect(seen[0].has("link_kind")).toBe(false);
    expect(seen[0].has("link_id")).toBe(false);
  });
});
