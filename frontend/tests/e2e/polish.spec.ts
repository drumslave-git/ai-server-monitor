// UI polish that needs a real browser's layout: the cut detail cell of the audit table, the Logs time column on
// one line, the errors that stand next to what asked for them, and the clash of two pair names in Settings.
import { http, HttpResponse } from "msw";
import { action, singleBatch } from "../mocks/fixtures/approvals";
import { appLog, makeRecord } from "../mocks/fixtures/logs";
import { makeSettingsDoc } from "../mocks/fixtures/settings";
import { expect, test } from "./fixtures";
import type { Page } from "@playwright/test";

const noSideways = (page: Page) =>
  page.evaluate(() => ({
    window: window.innerWidth,
    root: document.documentElement.scrollWidth,
    inner: [...document.querySelectorAll<HTMLElement>(".fd-main, .fd-pane-scroll")].map((el) => el.scrollWidth - el.clientWidth),
  }));

async function expectNoSidewaysScroll(page: Page) {
  const over = await noSideways(page);
  expect(over.root, "the page scrolls sideways").toBeLessThanOrEqual(over.window);
  for (const extra of over.inner) expect(extra, "a pane scrolls sideways").toBeLessThanOrEqual(1);
}

const LONG_DETAIL = `the server answered with a very long explanation that does not fit one line of the table at any width this suite uses ${"and goes on and on, ".repeat(12)}end`;

for (const width of [1100, 1280]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });

    test(`Approvals audit log: the detail stays on one line, is cut and has the whole text in a tooltip (${width})`, async ({ page, network }, testInfo) => {
      test.skip(!testInfo.project.name.startsWith("desktop-1280"), "the width is set here");
      network.use(
        http.get("/api/actions", () =>
          HttpResponse.json({
            items: [
              action({ id: 7, status: "rejected", result: { detail: LONG_DETAIL } }),
              action({ id: 8, status: "rejected", result: { detail: "short" } }),
            ],
            total: 2,
          }),
        ),
      );
      await page.goto("/#/approvals");
      const rows = page.locator("#audit-log table tbody tr");
      await expect(rows).toHaveCount(2);
      const cell = rows.first().locator("td.fd-cut");
      await expect(cell).toBeVisible();
      const cut = await cell.evaluate((el) => ({
        nowrap: getComputedStyle(el).whiteSpace === "nowrap",
        ellipsis: getComputedStyle(el).textOverflow === "ellipsis",
        more: el.scrollWidth > el.clientWidth,
      }));
      expect(cut, "one line, an ellipsis, more text than room").toEqual({ nowrap: true, ellipsis: true, more: true });
      // a long detail does not make its row taller than a short one
      const [tall, short] = await rows.evaluateAll((all) => all.map((r) => Math.round(r.getBoundingClientRect().height)));
      expect(Math.abs(tall - short), "the row with the long detail is as tall as the short one (a pixel of baseline apart at most)").toBeLessThanOrEqual(1);

      const tip = page.locator(".fd-trim-tip:popover-open");
      await expect(async () => {
        await page.mouse.move(0, 0);
        await cell.hover();
        await expect(tip).toBeVisible({ timeout: 1500 });
      }).toPass({ timeout: 10000 });
      await expect(tip).toHaveText(LONG_DETAIL);
      await expectNoSidewaysScroll(page);
    });

    test(`Logs: the time column shows its value whole on one line (${width})`, async ({ page }, testInfo) => {
      test.skip(!testInfo.project.name.startsWith("desktop-1280"), "the width is set here");
      await page.goto("/#/logs");
      const table = page.getByRole("table").first();
      await expect(table).toBeVisible();
      const cells = await table.locator("tbody tr").first().locator("td.fd-nowrap").evaluateAll((all) =>
        all.map((el) => ({ white: getComputedStyle(el).whiteSpace, whole: el.scrollWidth <= el.clientWidth, })),
      );
      expect(cells.length, "the time cell is marked nowrap").toBeGreaterThan(0);
      for (const c of cells) expect(c).toEqual({ white: "nowrap", whole: true });
      await expectNoSidewaysScroll(page);
    });
  });
}

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
      http.get("/api/settings", () =>
        HttpResponse.json(
          makeSettingsDoc((doc) => {
            const llm = (doc.schema.$defs as Record<string, { properties: Record<string, unknown> }>).LLMSettings;
            llm.properties.extra_body = { type: "object", additionalProperties: { type: "string" }, description: "Extra fields." };
            (doc.values.llm as Record<string, unknown>).extra_body = { alpha: "1", beta: "2" };
          }),
        ),
      ),
    );
    await page.goto("/#/settings");
    const group = page.getByRole("group", { name: "extra body" }).first();
    await group.scrollIntoViewIfNeeded();
    const names = group.getByPlaceholder("name");
    await expect(names).toHaveCount(2);
    // the Settings pane of a 768 px window already runs 3 px wider than its room without any clash: the clash must add nothing
    const before = await noSideways(page);
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
    const after = await noSideways(page);
    expect(after.root, "the page scrolls sideways").toBeLessThanOrEqual(after.window);
    expect(after.inner, "the clash made a pane wider").toEqual(before.inner);
  });
});
