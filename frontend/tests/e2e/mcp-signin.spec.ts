// The sign-in line of an MCP server that uses OAuth (Settings → MCP servers → a card with Auth = oauth), in real Chromium at
// the width of each project: whether it fits the card, in each of its states (not connected, connected, an error).
import { http, HttpResponse } from "msw";
import { makeSettingsDoc } from "../mocks/fixtures/settings";
import { expect, test } from "./fixtures";
import { expectNoSidewaysScroll } from "./polish-support";
import type { Page } from "@playwright/test";

const ERROR = "The login at second-server has expired or was withdrawn: open Settings → MCP and press Connect again.";

const oauthSettings = () =>
  http.get("/api/settings", () =>
    HttpResponse.json(
      makeSettingsDoc((doc) => {
        ((doc.values.mcp as Record<string, unknown>).servers as Record<string, unknown>[])[1].auth = "oauth";
      }),
    ),
  );
const states = (connected: boolean, error: string | null) =>
  http.get("/api/mcp/oauth", () => HttpResponse.json([{ name: "second-server", connected, error }]));

/** The card of the second server, open; the Auth choice is the one the page shows. */
async function openCard(page: Page) {
  await page.goto("/#/settings/mcp/second-server");
  const card = page.locator(".fd-card-focus");
  await expect(card.getByRole("button", { name: "second-server", exact: true })).toHaveAttribute("aria-expanded", "true");
  return card;
}

/** Whole inside the card and inside the window: nothing is cut off or pushed out sideways. */
async function expectInside(page: Page, card: ReturnType<Page["locator"]>, what: ReturnType<Page["locator"]>) {
  await what.scrollIntoViewIfNeeded();
  const [c, w] = await Promise.all([card.boundingBox(), what.boundingBox()]);
  const width = page.viewportSize()!.width;
  expect(w!.x, "starts inside the card").toBeGreaterThanOrEqual(c!.x - 1);
  expect(w!.x + w!.width, "ends inside the card").toBeLessThanOrEqual(c!.x + c!.width + 1);
  expect(w!.x + w!.width, "ends inside the window").toBeLessThanOrEqual(width);
}

test.describe("Settings, MCP sign-in line", () => {
  test("not connected: the status and a connect button, in the card; connect goes to the server's login", async ({ page, network }) => {
    network.use(oauthSettings(), states(false, null));
    await page.route("http://login.test/**", (route) => route.fulfill({ contentType: "text/html", body: "<h1>login</h1>" }));
    const card = await openCard(page);
    await expect(card.getByText("not connected")).toBeVisible();
    const connect = card.getByRole("button", { name: "connect", exact: true });
    await expect(connect).toBeVisible();
    await expect(card.getByRole("button", { name: "disconnect" })).toHaveCount(0);
    await expectInside(page, card, card.getByText("not connected"));
    await expectInside(page, card, connect);
    await expectNoSidewaysScroll(page);
    await connect.click();
    await expect(page).toHaveURL(/^http:\/\/login\.test\/authorize\?state=s1$/);
  });

  test("connected: the status and a disconnect button, in the card; disconnect forgets the login", async ({ page, network }) => {
    let connected = true;
    network.use(
      oauthSettings(),
      http.get("/api/mcp/oauth", () => HttpResponse.json([{ name: "second-server", connected, error: null }])),
      http.delete("/api/mcp/oauth/:server", () => {
        connected = false;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const card = await openCard(page);
    await expect(card.getByText("connected", { exact: true })).toBeVisible();
    const disconnect = card.getByRole("button", { name: "disconnect" });
    await expect(disconnect).toBeVisible();
    await expect(card.getByRole("button", { name: "connect", exact: true })).toHaveCount(0);
    await expectInside(page, card, card.getByText("connected", { exact: true }));
    await expectInside(page, card, disconnect);
    await expectNoSidewaysScroll(page);
    await disconnect.click();
    await expect(card.getByText("not connected")).toBeVisible();
    await expect(card.getByRole("button", { name: "connect", exact: true })).toBeVisible();
  });

  test("an error: the whole text under the buttons, inside the card, with connect again beside disconnect", async ({ page, network }) => {
    network.use(oauthSettings(), states(true, ERROR));
    const card = await openCard(page);
    const line = card.getByText(ERROR);
    await expect(line).toBeVisible();
    const again = card.getByRole("button", { name: "connect again" });
    const disconnect = card.getByRole("button", { name: "disconnect" });
    await expect(again).toBeVisible();
    await expectInside(page, card, again);
    await expectInside(page, card, disconnect);
    await expectInside(page, card, line);
    const [lineBox, againBox] = await Promise.all([line.boundingBox(), again.boundingBox()]);
    expect(lineBox!.y, "the error is under the buttons").toBeGreaterThanOrEqual(againBox!.y + againBox!.height - 1);
    await expectNoSidewaysScroll(page);
  });
});
