// The Memory page: the list of notes with its filters, an open note with the link to its writer, the edit form, the
// delete question, the phone's one pane, and the reload when a note changes.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryPage } from "../../../src/pages/Memory";
import { makeNote, memoryPage, type MemoryPage as Page } from "../../mocks/fixtures/memory";
import { server } from "../../mocks/server";
import { installDialogBehaviour } from "../kit/part1-support";
import { FakeEventSource, installFakeEventSource } from "../shell/fake-event-source";

installFakeEventSource();

let dialogs: ReturnType<typeof installDialogBehaviour>;
beforeEach(() => {
  dialogs = installDialogBehaviour();
  location.hash = "#/memory";
});
afterEach(() => {
  dialogs.remove();
  vi.unstubAllGlobals();
  installFakeEventSource();
});

type Seen = { method: string; path: string; body: unknown };

/** The list is answered from `state.page`; the writes are recorded and answer with success. */
function mockApi(page: Page = memoryPage()) {
  const state = { page, seen: [] as Seen[], loads: 0, fail: null as string | null };
  server.use(
    http.get("/api/memory", () => {
      state.loads += 1;
      return HttpResponse.json(state.page);
    }),
    http.put("/api/memory/:id", async ({ request, params }) => {
      state.seen.push({ method: "PUT", path: new URL(request.url).pathname, body: await request.json() });
      if (state.fail) return HttpResponse.json({ detail: state.fail }, { status: 409 });
      return HttpResponse.json({ ...state.page.items[0], id: Number(params.id) });
    }),
    http.delete("/api/memory/:id", ({ request }) => {
      state.seen.push({ method: "DELETE", path: new URL(request.url).pathname, body: undefined });
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return state;
}

async function renderPage(params: string[] = []) {
  const view = render(<MemoryPage params={params} />);
  await screen.findByRole("heading", { name: "Memory" });
  await waitFor(() => expect(screen.queryByText("Loading…")).not.toBeInTheDocument());
  return view;
}

const list = () => within(screen.getByRole("navigation", { name: "Notes" }));

describe("the list", () => {
  it("shows every note with its areas and how long ago it changed, and marks the owner's own", async () => {
    mockApi();
    await renderPage();
    const items = list().getAllByRole("link");
    expect(items.map((a) => a.getAttribute("href"))).toEqual(["#/memory/1", "#/memory/2", "#/memory/3", "#/memory/4"]);
    expect(within(items[0]).getByText("Backup window")).toBeInTheDocument();
    expect(within(items[0]).getByText(/^monitoring · /)).toBeInTheDocument();
    expect(within(items[1]).getByText(/^docker, monitoring · /)).toBeInTheDocument();
    expect(within(items[2]).getByText(/^no area · /)).toBeInTheDocument();
    expect(within(items[2]).getByText("yours")).toBeInTheDocument();
    expect(within(items[0]).queryByText("yours")).not.toBeInTheDocument();
    expect(screen.getByText("Pick a note.")).toBeInTheDocument();
  });

  it("filters by what the title or the note says", async () => {
    mockApi();
    await renderPage();
    await userEvent.setup().type(screen.getByRole("searchbox", { name: "Search titles and notes" }), "svc-");
    expect(list().getAllByRole("link")).toHaveLength(1);
    expect(list().getByText("Docker host naming")).toBeInTheDocument();
    expect(screen.getByText("1 of 4")).toBeInTheDocument();
  });

  it("filters by area, including the notes without one", async () => {
    mockApi();
    await renderPage();
    const user = userEvent.setup();
    const area = screen.getByRole("combobox", { name: "Area" });
    expect(within(area).getAllByRole("option").map((o) => o.textContent)).toEqual(["all areas", "no area", "docker", "monitoring", "storage"]);
    await user.selectOptions(area, "docker");
    expect(list().getAllByRole("link").map((a) => a.textContent)).toEqual([expect.stringContaining("Docker host naming"), expect.stringContaining("Old finding")]);
    await user.selectOptions(area, "none");
    expect(list().getAllByRole("link")).toHaveLength(1);
    expect(list().getByText("Owner's rule")).toBeInTheDocument();
  });

  it("says when no note matches", async () => {
    mockApi();
    await renderPage();
    await userEvent.setup().type(screen.getByRole("searchbox", { name: "Search titles and notes" }), "zzz");
    expect(screen.getByText("No note matches.")).toBeInTheDocument();
  });

  it("explains an empty store, without filters", async () => {
    mockApi(memoryPage({ items: [] }));
    await renderPage();
    expect(screen.getByText(/No notes yet\. The chat model writes one/)).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
  });

  it("shows the error while the first load fails", async () => {
    server.use(http.get("/api/memory", () => HttpResponse.json({ detail: "database is locked" }, { status: 500 })));
    render(<MemoryPage params={[]} />);
    expect(await screen.findByText(/database is locked/)).toBeInTheDocument();
  });
});

describe("an open note", () => {
  it("shows its body, areas and the chat it was written in as a link", async () => {
    mockApi();
    await renderPage(["1"]);
    expect(screen.getByRole("heading", { name: "Backup window" })).toBeInTheDocument();
    expect(screen.getByText("nas-01").tagName).toBe("STRONG");
    const writer = screen.getByRole("link", { name: "chat 3f9c2a7e" });
    expect(writer).toHaveAttribute("href", "#/chat/3f9c2a7e-1111-4222-8333-444455556666");
    expect(list().getByRole("link", { name: /Backup window/ })).toHaveAttribute("aria-current", "true");
  });

  it("links the run that wrote it", async () => {
    mockApi();
    await renderPage(["2"]);
    expect(screen.getByRole("link", { name: "run #41 of test:disk" })).toHaveAttribute("href", expect.stringMatching(/^#\/jobs\/test(:|%3A)disk\/runs\/41$/));
  });

  it("names a writer that is gone without a link", async () => {
    mockApi(
      memoryPage({
        items: [
          makeNote({ id: 1, source: { kind: "chat", id: "x".repeat(36), label: null, exists: false } }),
          makeNote({ id: 2, title: "Second", source: { kind: "run", id: "9", label: "test:disk", exists: false } }),
          makeNote({ id: 3, title: "Third", source: { kind: "run", id: "10", label: null, exists: true } }),
        ],
      }),
    );
    const first = await renderPage(["1"]);
    expect(screen.getByText("a chat that is gone")).toBeInTheDocument();
    first.unmount();
    const second = await renderPage(["2"]);
    expect(screen.getByText("run #9 of test:disk (gone)")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /run #9/ })).not.toBeInTheDocument();
    second.unmount();
    await renderPage(["3"]);
    expect(screen.getByText("run #10")).toBeInTheDocument();
  });

  it("says that the owner's note is theirs and the model cannot change it; a note with no area says none", async () => {
    mockApi();
    await renderPage(["3"]);
    expect(screen.getByText(/the model can read this note but not change it/)).toBeInTheDocument();
    expect(screen.getByText("none")).toBeInTheDocument();
    expect(screen.getByText("unknown")).toBeInTheDocument();
  });

  it("says when the note does not exist", async () => {
    mockApi();
    await renderPage(["99"]);
    expect(screen.getByText("No such note.")).toBeInTheDocument();
  });

  it("treats an address that is not a number as no note", async () => {
    mockApi();
    await renderPage(["abc"]);
    expect(screen.getByText("Pick a note.")).toBeInTheDocument();
  });
});

describe("editing", () => {
  it("the edit button goes to the form of the note", async () => {
    mockApi();
    await renderPage(["1"]);
    await userEvent.setup().click(screen.getByRole("button", { name: "edit" }));
    expect(location.hash).toBe("#/memory/1/edit");
  });

  it("saves the trimmed title, body and chosen areas with a PUT, then reloads and shows the note", async () => {
    const api = mockApi();
    await renderPage(["1", "edit"]);
    const user = userEvent.setup();
    const save = screen.getByRole("button", { name: "save" });
    expect(save).toBeDisabled();
    const title = screen.getByRole("textbox", { name: "title" });
    expect(title).toHaveValue("Backup window");
    await user.clear(title);
    await user.type(title, "  Backups ");
    const body = screen.getByRole("textbox", { name: "note" });
    await user.clear(body);
    await user.type(body, "At 03:00.");
    await user.click(screen.getByRole("checkbox", { name: "docker" }));
    await user.click(screen.getByRole("checkbox", { name: "monitoring" }));
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(location.hash).toBe("#/memory/1"));
    expect(api.seen).toEqual([{ method: "PUT", path: "/api/memory/1", body: { title: "Backups", body: "At 03:00.", domains: ["docker"] } }]);
    await waitFor(() => expect(api.loads).toBe(2));
  });

  it("keeps save off while the title or the note is empty", async () => {
    mockApi();
    await renderPage(["1", "edit"]);
    const user = userEvent.setup();
    await user.clear(screen.getByRole("textbox", { name: "title" }));
    expect(screen.getByRole("button", { name: "save" })).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "title" }), "x");
    expect(screen.getByRole("button", { name: "save" })).toBeEnabled();
    await user.clear(screen.getByRole("textbox", { name: "note" }));
    expect(screen.getByRole("button", { name: "save" })).toBeDisabled();
  });

  it("shows the error of a refused save and stays on the form", async () => {
    const api = mockApi();
    api.fail = "A note with this title already exists";
    await renderPage(["1", "edit"]);
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "title" }), "!");
    await user.click(screen.getByRole("button", { name: "save" }));
    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
    expect(location.hash).toBe("#/memory");
    expect(screen.getByRole("button", { name: "save" })).toBeEnabled();
  });

  it("cancel goes back to the note", async () => {
    mockApi();
    await renderPage(["1", "edit"]);
    await userEvent.setup().click(screen.getByRole("button", { name: "cancel" }));
    expect(location.hash).toBe("#/memory/1");
  });

  it("says there are no areas to pick when no MCP domain exists", async () => {
    mockApi(memoryPage({ domains: [] }));
    await renderPage(["1", "edit"]);
    expect(screen.getByText(/There are no MCP domains yet/)).toBeInTheDocument();
  });
});

describe("deleting", () => {
  it("asks first, and cancel deletes nothing", async () => {
    const api = mockApi();
    await renderPage(["1"]);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete “Backup window”?" });
    await user.click(within(dialog).getByRole("button", { name: "cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.seen).toEqual([]);
  });

  it("deletes the note on confirm, goes back to the list and loads it again", async () => {
    const api = mockApi();
    await renderPage(["1"]);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "delete" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "delete" }));
    await waitFor(() => expect(api.seen).toEqual([{ method: "DELETE", path: "/api/memory/1", body: undefined }]));
    expect(location.hash).toBe("#/memory");
    await waitFor(() => expect(api.loads).toBe(2));
  });

  it("shows the error when the delete fails", async () => {
    mockApi();
    server.use(http.delete("/api/memory/:id", () => HttpResponse.json({ detail: "locked" }, { status: 500 })));
    await renderPage(["1"]);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "delete" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "delete" }));
    expect(await screen.findByText(/locked/)).toBeInTheDocument();
  });
});

describe("a phone", () => {
  const phone = () =>
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query, addEventListener: () => {}, removeEventListener: () => {} }));

  it("shows the list alone, then the note alone under a way back", async () => {
    phone();
    mockApi();
    const view = await renderPage();
    expect(screen.getByRole("navigation", { name: "Notes" })).toBeInTheDocument();
    expect(screen.queryByText("Pick a note.")).not.toBeInTheDocument();
    view.unmount();
    await renderPage(["1"]);
    expect(screen.queryByRole("navigation", { name: "Notes" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Backup window" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /all notes/ })).toHaveAttribute("href", "#/memory");
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
  });
});

describe("live", () => {
  it("loads the list again when a note changes", async () => {
    const api = mockApi();
    await renderPage();
    expect(api.loads).toBe(1);
    api.page = memoryPage({ items: [makeNote({ id: 7, title: "Fresh note" })] });
    act(() => FakeEventSource.current().emit("memory.changed", { id: 7 }));
    expect(await screen.findByText("Fresh note")).toBeInTheDocument();
    expect(api.loads).toBe(2);
  });
});
