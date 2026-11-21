import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LONG_TITLE, sessionList } from "../../mocks/fixtures/chat";
import { server } from "../../mocks/server";
import { renderChat } from "./render";

const list = () => screen.getByRole("navigation", { name: "Conversations" });

beforeEach(() => {
  vi.stubGlobal("confirm", vi.fn(() => true));
});
afterEach(() => {
  vi.unstubAllGlobals();
  history.replaceState(null, "", "#/");
});

describe("conversation list", () => {
  it("lists the conversations with when and how many messages, Telegram ones marked", async () => {
    renderChat();
    const items = await within(list()).findAllByRole("link", { name: /messages/ });
    expect(items).toHaveLength(4);
    expect(within(list()).getByRole("link", { name: /Example disk usage/ })).toHaveTextContent("5m ago · 4 messages");
    expect(within(list()).getByRole("link", { name: /Example backup question/ })).toHaveTextContent("Telegram · 2h ago · 2 messages");
    expect(within(list()).getByRole("link", { name: /Example disk usage/ })).toHaveAttribute("href", "#/chat/s-disk");
  });

  it("asks for the 50 most recent conversations", async () => {
    const urls: string[] = [];
    server.use(
      http.get("/api/chat/sessions", ({ request }) => {
        urls.push(new URL(request.url).search);
        return HttpResponse.json(sessionList);
      }),
    );
    renderChat();
    await screen.findByText("Example disk usage");
    expect(urls[0]).toBe("?limit=50");
  });

  it("an untitled conversation is shown as Untitled", async () => {
    renderChat();
    expect(await within(list()).findByRole("link", { name: /Untitled/ })).toHaveAttribute("href", "#/chat/s-untitled");
  });

  it("a long title is shown in full in the text (the layout cuts it, the text stays complete)", async () => {
    renderChat();
    const link = await within(list()).findByRole("link", { name: new RegExp(LONG_TITLE.slice(0, 30)) });
    expect(link).toHaveTextContent(LONG_TITLE);
  });

  it("shows an empty state when there are no conversations", async () => {
    server.use(http.get("/api/chat/sessions", () => HttpResponse.json({ items: [], limit: 50, offset: 0, total: 0 })));
    renderChat();
    expect(await screen.findByText("No conversations yet.")).toBeInTheDocument();
  });

  it("does not show the empty state while the list is still loading", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.get("/api/chat/sessions", async () => {
        await gate;
        return HttpResponse.json(sessionList);
      }),
    );
    renderChat();
    await screen.findByRole("heading", { name: "Chat" });
    expect(screen.queryByText("No conversations yet.")).not.toBeInTheDocument();
    release();
    expect(await screen.findByText("Example disk usage")).toBeInTheDocument();
  });

  it("has a link that starts a new conversation", async () => {
    renderChat();
    const link = await screen.findByRole("link", { name: "new conversation" });
    expect(link).toHaveAttribute("href", "#/chat/new");
  });
});

describe("opening a conversation", () => {
  it("shows its messages: the question, the tools used, the answer as Markdown", async () => {
    renderChat("#/chat/s-disk");
    expect(await screen.findByText("What is using the disk on host-a?")).toBeInTheDocument();
    expect(screen.getByText("you")).toBeInTheDocument();
    expect(screen.getByText("assistant")).toBeInTheDocument();
    // the answer's inline code became <code>
    expect(await screen.findByText("/var/lib/docker", { selector: "code" })).toBeInTheDocument();
    expect(screen.getByText("used a tool: shell")).toBeInTheDocument();
  });

  it("marks the open conversation in the list", async () => {
    renderChat("#/chat/s-disk");
    const link = await within(list()).findByRole("link", { name: /Example disk usage/ });
    expect(link).toHaveAttribute("aria-current", "true");
    expect(within(list()).getByRole("link", { name: /Example backup question/ })).not.toHaveAttribute("aria-current");
  });

  it("clicking a conversation in the list opens it", async () => {
    const user = userEvent.setup();
    renderChat();
    await user.click(await within(list()).findByRole("link", { name: /Example backup question/ }));
    expect(await screen.findByText("Did the backup run last night?")).toBeInTheDocument();
    expect(screen.getByText("Yes, it finished at 03:12.")).toBeInTheDocument();
  });

  it("shows the server's message when the conversation cannot be loaded", async () => {
    renderChat("#/chat/s-gone");
    expect(await screen.findByText("no such conversation")).toBeInTheDocument();
  });

  it("a new conversation starts empty with a hint, and an empty composer", async () => {
    renderChat("#/chat/new");
    expect(await screen.findByText(/Ask about the server: what is using the disk/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "send" })).toHaveAttribute("aria-disabled", "true");
  });

  it("clicking new conversation after another one was open empties the thread", async () => {
    const user = userEvent.setup();
    renderChat("#/chat/s-disk");
    await screen.findByText("What is using the disk on host-a?");
    await user.click(screen.getByRole("link", { name: "new conversation" }));
    await waitFor(() => expect(screen.queryByText("What is using the disk on host-a?")).not.toBeInTheDocument());
    expect(screen.getByText(/Ask about the server: what is using the disk/)).toBeInTheDocument();
  });
});

describe("deleting a conversation", () => {
  function track() {
    const deleted: string[] = [];
    let items = sessionList.items;
    server.use(
      http.get("/api/chat/sessions", () => HttpResponse.json({ ...sessionList, items })),
      http.delete("/api/chat/sessions/:id", ({ params }) => {
        deleted.push(String(params.id));
        items = items.filter((s) => s.id !== params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    return deleted;
  }

  it("asks first, and cancelling keeps the conversation and sends nothing", async () => {
    const user = userEvent.setup();
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    const deleted = track();
    renderChat();
    await user.click(await screen.findByRole("button", { name: "Delete conversation: Example disk usage" }));
    expect(confirm).toHaveBeenCalledWith("Delete this conversation?");
    expect(deleted).toEqual([]);
    expect(within(list()).getByRole("link", { name: /Example disk usage/ })).toBeInTheDocument();
  });

  it("confirming calls DELETE for that conversation and it leaves the list", async () => {
    const user = userEvent.setup();
    const deleted = track();
    renderChat();
    await user.click(await screen.findByRole("button", { name: "Delete conversation: Example backup question" }));
    expect(deleted).toEqual(["s-telegram"]);
    await waitFor(() => expect(within(list()).queryByRole("link", { name: /Example backup question/ })).not.toBeInTheDocument());
    expect(within(list()).getByRole("link", { name: /Example disk usage/ })).toBeInTheDocument();
  });

  it("deleting the open conversation goes to a new one", async () => {
    const user = userEvent.setup();
    const deleted = track();
    renderChat("#/chat/s-disk");
    await screen.findByText("What is using the disk on host-a?");
    await user.click(await screen.findByRole("button", { name: "Delete conversation: Example disk usage" }));
    expect(deleted).toEqual(["s-disk"]);
    await waitFor(() => expect(location.hash).toBe("#/chat/new"));
    await waitFor(() => expect(screen.queryByText("What is using the disk on host-a?")).not.toBeInTheDocument());
  });

  it("deleting another conversation leaves the open one where it is", async () => {
    const user = userEvent.setup();
    track();
    renderChat("#/chat/s-disk");
    await screen.findByText("What is using the disk on host-a?");
    await user.click(await screen.findByRole("button", { name: "Delete conversation: Example backup question" }));
    await waitFor(() => expect(within(list()).queryByRole("link", { name: /Example backup question/ })).not.toBeInTheDocument());
    expect(location.hash).toBe("#/chat/s-disk");
    expect(screen.getByText("What is using the disk on host-a?")).toBeInTheDocument();
  });
});
