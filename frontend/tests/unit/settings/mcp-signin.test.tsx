// src/components/McpSignIn.tsx inside the Settings → MCP servers cards: a server with Auth = oauth shows whether a
// login is saved, the last error, and the Connect / Disconnect buttons.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JsonObject, SettingsDoc } from "../../../src/api";
import { leave } from "../../../src/components/McpSignIn";
import { SettingsPage } from "../../../src/pages/Settings";
import { makeSettingsDoc } from "../../mocks/fixtures/settings";
import { server } from "../../mocks/server";

type State = { name: string; connected: boolean; error: string | null };

/** Settings whose second server signs in with OAuth; `states` is what GET /api/mcp/oauth answers (it can change). */
function mockOauth(states: State[], change?: (servers: JsonObject[]) => void) {
  const calls: string[] = [];
  const doc: SettingsDoc = makeSettingsDoc((d) => {
    const servers = (d.values.mcp as JsonObject).servers as JsonObject[];
    servers[1].auth = "oauth";
    change?.(servers);
  });
  const answer = { states, connect: undefined as undefined | (() => Response) };
  server.use(
    http.get("/api/settings", () => HttpResponse.json(doc)),
    http.get("/api/mcp/oauth", () => HttpResponse.json(answer.states)),
    http.post("/api/mcp/oauth/:server/connect", ({ params }) => {
      calls.push(`connect ${String(params.server)}`);
      return answer.connect ? answer.connect() : HttpResponse.json({ authorize_url: "http://login.test/authorize?state=s1" });
    }),
    http.delete("/api/mcp/oauth/:server", ({ params }) => {
      calls.push(`disconnect ${String(params.server)}`);
      answer.states = answer.states.map((s) => ({ ...s, connected: false, error: null }));
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return { calls, answer };
}

async function open(hash = "#/settings/mcp/second-server") {
  history.replaceState(null, "", hash);
  const user = userEvent.setup();
  render(<SettingsPage />);
  await screen.findByRole("heading", { name: "MCP servers" });
  return user;
}

const where = vi.fn();
/** The bearer-token input of the open card, the one the link focused (a server that signs in with OAuth has none). */
const card = () => document.querySelector<HTMLElement>(".fd-card-focus")!;
const tokenField = () => within(card()).queryByLabelText(/^token/, { selector: "input" });

beforeEach(() => {
  vi.stubGlobal("scrollTo", vi.fn());
  Object.defineProperty(Element.prototype, "scrollIntoView", { value: vi.fn(), configurable: true, writable: true });
  where.mockClear();
  vi.spyOn(leave, "to").mockImplementation(where);
});
afterEach(() => {
  history.replaceState(null, "", "#");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

describe("the sign-in line of an OAuth server", () => {
  it("says not connected with a Connect button, and sends the browser to the server's login", async () => {
    const { calls } = mockOauth([{ name: "second-server", connected: false, error: null }]);
    const user = await open();
    expect(await screen.findByText("not connected")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "disconnect" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "connect" }));
    await waitFor(() => expect(where).toHaveBeenCalledWith("http://login.test/authorize?state=s1"));
    expect(calls).toEqual(["connect second-server"]);
  });

  it("says connected with a Disconnect button that forgets the login", async () => {
    const { calls } = mockOauth([{ name: "second-server", connected: true, error: null }]);
    const user = await open();
    expect(await screen.findByText("connected")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "connect" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "disconnect" }));
    expect(await screen.findByText("not connected")).toBeInTheDocument();
    expect(calls).toEqual(["disconnect second-server"]);
    expect(screen.getByRole("button", { name: "connect" })).toBeInTheDocument();
  });

  it("shows a refused login as one state, sign-in expired, with the reason and Connect again (no connected, no disconnect)", async () => {
    mockOauth([{ name: "second-server", connected: true, error: "The login at second-server has expired or was withdrawn: open Settings → MCP and press Connect again." }]);
    await open();
    expect(await screen.findByText("sign-in expired")).toBeInTheDocument();
    expect(screen.getByText(/has expired or was withdrawn/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "connect again" })).toBeInTheDocument();
    expect(screen.queryByText("connected")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "disconnect" })).not.toBeInTheDocument();
  });

  it("shows why Connect failed, in the server's words, and does not leave the page", async () => {
    const { answer } = mockOauth([{ name: "second-server", connected: false, error: null }]);
    answer.connect = () => HttpResponse.json({ detail: "second-server does not publish OAuth sign-in information. Set Auth to token." }, { status: 400 });
    const user = await open();
    await user.click(await screen.findByRole("button", { name: "connect" }));
    expect(await screen.findByText(/does not publish OAuth sign-in information/)).toBeInTheDocument();
    expect(where).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "connect" })).not.toBeDisabled();
  });

  it("shows the error the last attempt left, after the browser came back (denied consent)", async () => {
    mockOauth([{ name: "second-server", connected: false, error: "The login at the server was denied." }]);
    await open();
    expect(await screen.findByText("The login at the server was denied.")).toBeInTheDocument();
    expect(screen.getByText("not connected")).toBeInTheDocument();
  });

  it("is not shown for a server that uses a token, and shows nothing wrong when the state cannot be read", async () => {
    mockOauth([], (servers) => {
      servers[1].auth = "token";
    });
    await open();
    expect(screen.queryByText("not connected")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "connect" })).not.toBeInTheDocument();
  });

  it("says when the state could not be read", async () => {
    mockOauth([]);
    server.use(http.get("/api/mcp/oauth", () => HttpResponse.json({ detail: "Database is locked" }, { status: 500 })));
    await open();
    expect(await screen.findByText("Could not read the sign-in state: Database is locked")).toBeInTheDocument();
  });

  it("asks to save first when the server is not saved yet (the server's answer is shown)", async () => {
    const { answer } = mockOauth([]);
    answer.connect = () => HttpResponse.json({ detail: "No MCP server named second-server is saved yet: save it first." }, { status: 404 });
    const user = await open();
    await user.click(await screen.findByRole("button", { name: "connect" }));
    expect(await screen.findByText(/save it first/)).toBeInTheDocument();
  });

  it("follows the Auth choice of the form: switching to oauth shows the line, switching back hides it", async () => {
    mockOauth([{ name: "second-server", connected: false, error: null }], (servers) => {
      servers[1].auth = "token";
    });
    const user = await open();
    expect(screen.queryByText("not connected")).not.toBeInTheDocument();
    await user.click(within(screen.getByRole("group", { name: /auth/i })).getByRole("radio", { name: "oauth" }));
    expect(await screen.findByText("not connected")).toBeInTheDocument();
    await user.click(within(screen.getByRole("group", { name: /auth/i })).getByRole("radio", { name: "token" }));
    expect(screen.queryByText("not connected")).not.toBeInTheDocument();
  });

  it("hides the token field of a server that signs in with OAuth, and keeps its headers", async () => {
    mockOauth([{ name: "second-server", connected: false, error: null }], (servers) => {
      servers[1].headers = [{ name: "X-Tenant", value: "acme" }];
    });
    await open();
    expect(await screen.findByText("not connected")).toBeInTheDocument();
    expect(tokenField()).not.toBeInTheDocument();
    expect(within(card()).getByDisplayValue("X-Tenant")).toBeInTheDocument();
  });

  it("shows the token field of a server that uses a token, and follows the Auth choice both ways", async () => {
    mockOauth([{ name: "second-server", connected: false, error: null }], (servers) => {
      servers[1].auth = "token";
      servers[1].headers = [{ name: "X-Tenant", value: "acme" }];
    });
    const user = await open();
    const auth = () => within(screen.getByRole("group", { name: /auth/i }));
    expect(tokenField()).toBeInTheDocument();
    await user.click(auth().getByRole("radio", { name: "oauth" }));
    expect(tokenField()).not.toBeInTheDocument();
    expect(within(card()).getByDisplayValue("X-Tenant")).toBeInTheDocument();
    await user.click(auth().getByRole("radio", { name: "token" }));
    expect(tokenField()).toBeInTheDocument();
    expect(within(card()).getByDisplayValue("X-Tenant")).toBeInTheDocument();
  });
});
