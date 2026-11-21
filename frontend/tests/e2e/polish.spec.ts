// UI polish that needs a real browser's layout, at the width of each project: the errors that stand next to what asked
// for them, the pair names in Settings, the Approvals link that lines up with its buttons, a link followed inside the
// app. What sets its own window width is in polish.fixed.spec.ts.
import { http, HttpResponse } from "msw";
import { singleBatch } from "../mocks/fixtures/approvals";
import { appLog, makeRecord } from "../mocks/fixtures/logs";
import { makeSettingsDoc } from "../mocks/fixtures/settings";
import { expect, test } from "./fixtures";
import { expectNoSidewaysScroll, withPairs } from "./polish-support";
import type { Page } from "@playwright/test";

test.describe("errors next to what asked", () => {
  test("a failed decision shows its error on the card that was clicked, and only there", async ({ page, network }) => {
    network.use(http.post("/api/approvals/:id", () => HttpResponse.json({ detail: "The server refused the decision" }, { status: 500 })));
    await page.goto("/#/approvals");
    const card = page.getByRole("article", { name: singleBatch.title });
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: /^approve all/ }).click();
    const alert = card.getByRole("alert");
    await expect(alert).toHaveText("The server refused the decision");
    await expect(page.getByRole("alert")).toHaveCount(1);
    // inside the card, under its buttons, not wider than the card
    const [cardBox, alertBox, buttonBox] = await Promise.all([card.boundingBox(), alert.boundingBox(), card.getByRole("button", { name: /^approve all/ }).boundingBox()]);
    expect(alertBox!.x).toBeGreaterThanOrEqual(cardBox!.x);
    expect(alertBox!.x + alertBox!.width).toBeLessThanOrEqual(cardBox!.x + cardBox!.width + 1);
    expect(alertBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);
    await expectNoSidewaysScroll(page);
  });

  test("a failed 'older records' shows its error beside the button, the list stays", async ({ page, network }) => {
    const rows = Array.from({ length: 200 }, (_, i) => makeRecord({ seq: 1000 - i, message: `Record ${i}` }));
    network.use(
      http.get("/api/logs/app", ({ request }) =>
        new URL(request.url).searchParams.has("before") ? HttpResponse.json({ detail: "No older records today" }, { status: 500 }) : HttpResponse.json(appLog(rows)),
      ),
    );
    await page.goto("/#/logs/app");
    const button = page.getByRole("button", { name: "older records" });
    await button.scrollIntoViewIfNeeded();
    await button.click();
    const alert = page.getByRole("alert");
    await expect(alert).toHaveText("No older records today");
    await expect(page.getByRole("alert")).toHaveCount(1);
    await expect(page.getByText("Record 0", { exact: true })).toBeVisible();
    const [b, a] = await Promise.all([button.boundingBox(), alert.boundingBox()]);
    const sameRow = Math.abs(b!.y + b!.height / 2 - (a!.y + a!.height / 2)) < Math.max(b!.height, a!.height);
    const below = a!.y >= b!.y + b!.height - 1;
    expect(sameRow || below, "the error is by the button").toBe(true);
    const overlap = a!.x < b!.x + b!.width && a!.x + a!.width > b!.x && a!.y < b!.y + b!.height && a!.y + a!.height > b!.y;
    expect(overlap, "the error covers the button").toBe(false);
    await expectNoSidewaysScroll(page);
  });
});

test.describe("Settings, pair names", () => {
  test("two rows with one name: the message stands under the row, the border is invalid, nothing overlaps", async ({ page, network }) => {
    network.use(
      http.get("/api/settings", () => HttpResponse.json(withPairs())),
    );
    await page.goto("/#/settings");
    const group = page.getByRole("group", { name: "extra body" }).first();
    await group.scrollIntoViewIfNeeded();
    const names = group.getByPlaceholder("name");
    await expect(names).toHaveCount(2);
    await names.nth(1).fill("alpha");
    const messages = group.getByText("Name already used");
    await expect(messages).toHaveCount(2);
    await expect(names.nth(0)).toHaveAttribute("aria-invalid", "true");
    await expect(names.nth(1)).toHaveAttribute("aria-invalid", "true");
    // the invalid border has the colour of the field errors, and each message is under its own row
    const border = await names.nth(1).evaluate((el) => getComputedStyle(el).borderTopColor);
    const normal = await group.getByPlaceholder("value").first().evaluate((el) => getComputedStyle(el).borderTopColor);
    expect(border, "the invalid name has its own border colour").not.toBe(normal);
    for (const i of [0, 1]) {
      const [n, m] = await Promise.all([names.nth(i).boundingBox(), messages.nth(i).boundingBox()]);
      expect(m!.y, `message ${i} is under its row`).toBeGreaterThanOrEqual(n!.y + n!.height - 1);
    }
    // the two rows do not overlap each other or the messages
    const boxes = await Promise.all([names.nth(0), messages.nth(0), names.nth(1), messages.nth(1)].map((l) => l.boundingBox()));
    for (let i = 0; i < boxes.length - 1; i++) expect(boxes[i + 1]!.y, `box ${i + 1} starts below box ${i}`).toBeGreaterThanOrEqual(boxes[i]!.y + boxes[i]!.height - 1);
    await expectNoSidewaysScroll(page);
  });
});

test.describe("Settings, panes", () => {
  test("no pane scrolls sideways", async ({ page }) => {
    await page.goto("/#/settings");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByRole("radiogroup").first()).toBeVisible();
    await expectNoSidewaysScroll(page);
  });
});

test.describe("Settings, a link followed inside the app", () => {
  /** The card in view below the head, as after a fresh load. */
  async function expectCardInView(page: Page, name: string) {
    const card = page.locator(".fd-card-focus");
    await expect(card).toBeVisible();
    await expect(card.getByRole("button", { name, exact: true })).toHaveAttribute("aria-expanded", "true");
    await expect(async () => {
      const [c, head] = await Promise.all([card.boundingBox(), page.locator("header.fd-head").first().boundingBox()]);
      expect(c!.y, "the card is below the page head").toBeGreaterThanOrEqual(head!.y + head!.height - 1);
      expect(c!.y, "the card is in the window").toBeLessThan(page.viewportSize()!.height);
    }).toPass({ timeout: 5000 });
  }

  /** What an anchor does: the address changes, the page is not loaded again. */
  const follow = (page: Page, hash: string) => page.evaluate((h) => { location.hash = h; }, hash);

  test("from #/settings: the card of the server opens and comes into view, as after a fresh load", async ({ page }) => {
    await page.goto("/#/settings");
    await expect(page.getByRole("radiogroup").first()).toBeVisible();
    await follow(page, "#/settings/mcp/second-server");
    await expectCardInView(page, "second-server");
    await expectNoSidewaysScroll(page);
  });

  test("from the section itself, with its cards shut: the next link opens its card too", async ({ page }) => {
    await page.goto("/#/settings/mcp");
    await expect(page.getByRole("button", { name: "second-server", exact: true })).toHaveAttribute("aria-expanded", "false");
    await follow(page, "#/settings/mcp/second-server");
    await expectCardInView(page, "second-server");
    await follow(page, "#/settings/mcp/test-server");
    await expectCardInView(page, "test-server");
  });

  test("from another page: the name of a server on the MCP tools page opens its card in Settings", async ({ page, network }) => {
    network.use(
      http.get("/api/settings", () =>
        HttpResponse.json(
          makeSettingsDoc((doc) => {
            ((doc.values.mcp as Record<string, unknown>).servers as { name: string }[])[1].name = "test-extra";
          }),
        ),
      ),
    );
    await page.goto("/#/mcp");
    const link = page.getByRole("link", { name: "test-extra", exact: true }).first();
    await expect(link).toBeVisible();
    await link.click();
    await expect(page).toHaveURL(/#\/settings\/mcp\/test-extra$/);
    await expectCardInView(page, "test-extra");
  });
});

test.describe("Approvals, links", () => {
  test("'show in the audit log' starts where the buttons above it start", async ({ page }, testInfo) => {
    await page.goto("/#/approvals");
    const card = page.getByRole("article", { name: singleBatch.title });
    const link = card.getByRole("button", { name: "show in the audit log" });
    const approve = card.getByRole("button", { name: /^approve all/ });
    await expect(link).toBeVisible();
    const [l, a] = await Promise.all([link.boundingBox(), approve.boundingBox()]);
    const ownRow = l!.y >= a!.y + a!.height - 1;
    // on a phone the buttons stand one under the other, so the link starts a row; on a wide window it may follow them
    if (testInfo.project.name.startsWith("phone-390")) expect(ownRow, "the link stands on a row of its own").toBe(true);
    if (ownRow) expect(Math.abs(l!.x - a!.x), "the link is not inset against the button").toBeLessThanOrEqual(1);
    await expectNoSidewaysScroll(page);
  });
});
