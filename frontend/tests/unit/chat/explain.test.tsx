import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { ExplainButton, takeExplain } from "../../../src/components/Explain";
import { explainEvents, liveStream } from "../../mocks/fixtures/chat";
import { server } from "../../mocks/server";
import { renderChat } from "./render";

afterEach(() => {
  takeExplain(); // a request a test left behind must not reach the next one
  history.replaceState(null, "", "#/");
});

describe("ExplainButton", () => {
  it("is an icon button named explain, with a hint saying what it explains", () => {
    const { rerender } = render(<ExplainButton runId={7} />);
    expect(screen.getByRole("button", { name: "explain" })).toBeInTheDocument();
    rerender(<ExplainButton runId={7} finding={2} small />);
    expect(screen.getByRole("button", { name: "explain" })).toBeInTheDocument();
  });

  it("opens a new chat and leaves the request for the Chat page, once", async () => {
    const user = userEvent.setup();
    render(<ExplainButton runId={7} finding={2} />);
    await user.click(screen.getByRole("button", { name: "explain" }));
    expect(location.hash).toBe("#/chat/new");
    expect(takeExplain()).toEqual({ runId: 7, finding: 2 });
    expect(takeExplain()).toBeNull();
  });

  it("a run without a finding is requested with finding null", async () => {
    const user = userEvent.setup();
    render(<ExplainButton runId={9} />);
    await user.click(screen.getByRole("button", { name: "explain" }));
    expect(takeExplain()).toEqual({ runId: 9, finding: null });
  });

  it("clicking does not follow or trigger what it sits inside (a link or a card)", async () => {
    const user = userEvent.setup();
    let outer = 0;
    render(
      <a href="#/jobs/example" onClick={() => outer++}>
        <ExplainButton runId={1} />
      </a>,
    );
    await user.click(screen.getByRole("button", { name: "explain" }));
    expect(outer).toBe(0);
    expect(location.hash).toBe("#/chat/new");
  });
});

describe("the Chat page opened by an explain request", () => {
  async function request(runId: number, finding: number | null) {
    const user = userEvent.setup();
    const view = render(<ExplainButton runId={runId} finding={finding} />);
    await user.click(screen.getByRole("button", { name: "explain" }));
    view.unmount();
  }

  function capture(stream: ReturnType<typeof liveStream>) {
    const bodies: unknown[] = [];
    server.use(
      http.post("/api/chat/explain", async ({ request: r }) => {
        bodies.push(await r.json());
        return stream.response();
      }),
    );
    return bodies;
  }

  it("posts the run to the explain endpoint and shows the label with the source data folded", async () => {
    await request(7, null);
    const stream = liveStream();
    const bodies = capture(stream);
    renderChat("#/chat/new");
    expect(await screen.findByText("Explain this run")).toBeInTheDocument();
    await waitFor(() => expect(bodies).toEqual([{ run_id: 7, finding: null }]));

    for (const [type, data] of explainEvents("Explain this run", "Run 7 of Example job\nfinding: disk almost full").slice(0, 2)) stream.send(type, data);
    expect(await screen.findByText("This run found one problem.")).toBeInTheDocument();
    const fold = screen.getByText("source data").closest("details")!;
    expect(fold).not.toHaveAttribute("open");
    expect(fold).toHaveTextContent("finding: disk almost full");
    // the conversation now has an address
    expect(location.hash).toBe("#/chat/s-explain");
    stream.close();
  });

  it("a finding is labelled as a finding and sent with its index", async () => {
    await request(7, 2);
    const stream = liveStream();
    const bodies = capture(stream);
    renderChat("#/chat/new");
    expect(await screen.findByText("Explain this finding")).toBeInTheDocument();
    await waitFor(() => expect(bodies).toEqual([{ run_id: 7, finding: 2 }]));
    stream.close();
  });

  it("without a request the page sends nothing to the explain endpoint", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post("/api/chat/explain", async ({ request: r }) => {
        bodies.push(await r.json());
        return HttpResponse.json({});
      }),
    );
    renderChat("#/chat/new");
    await screen.findByText(/Ask about the server/);
    expect(bodies).toEqual([]);
    expect(screen.queryByText("Explain this run")).not.toBeInTheDocument();
  });

  it("the request is used once: opening the page again does not repeat it", async () => {
    await request(7, null);
    const stream = liveStream();
    const bodies = capture(stream);
    const first = renderChat("#/chat/new");
    await waitFor(() => expect(bodies).toHaveLength(1));
    stream.close();
    first.unmount();
    renderChat("#/chat/new");
    await screen.findByText(/Ask about the server/);
    expect(bodies).toHaveLength(1);
  });
});
