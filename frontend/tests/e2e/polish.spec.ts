// UI polish that needs a real browser's layout: the cut detail cell of the audit table, the Logs time stamp on two
// lines, the errors that stand next to what asked for them, the pair names in Settings (a clash, the remove button on a
// phone), the Approvals link that lines up with its buttons, and the Settings panes that never scroll sideways.
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

/** The settings document with `llm.extra_body` as a pairs field holding alpha and beta. */
const withPairs = () =>
  makeSettingsDoc((doc) => {
    const llm = (doc.schema.$defs as Record<string, { properties: Record<string, unknown> }>).LLMSettings;
    llm.properties.extra_body = { type: "object", additionalProperties: { type: "string" }, description: "Extra fields." };
    (doc.values.llm as Record<string, unknown>).extra_body = { alpha: "1", beta: "2" };
  });

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

    test(`Logs: the time stamp stands on two lines (date above time), whole, and the prompt column gets the room (${width})`, async ({ page }, testInfo) => {
      test.skip(!testInfo.project.name.startsWith("desktop-1280"), "the width is set here");
      await page.goto("/#/logs");
      const table = page.getByRole("table").first();
      await expect(table).toBeVisible();
      const first = table.locator("tbody tr").first().locator("td").first();
      const lines = await first.locator(".fd-meta").evaluateAll((all) => all.map((el) => ({ text: el.textContent, top: el.getBoundingClientRect().top, whole: el.scrollWidth <= el.clientWidth })));
      expect(lines.length, "the date and the time are two lines").toBe(2);
      expect(lines[1].top, "the time is under the date").toBeGreaterThan(lines[0].top);
      for (const l of lines) expect(l.whole, `${l.text} is whole`).toBe(true);
      // the time cells of every row are nowrap and whole; the prompt column is wider than the time stamp column
      const cells = await table.locator("tbody tr").first().locator("td.fd-nowrap").evaluateAll((all) =>
        all.map((el) => ({ white: getComputedStyle(el).whiteSpace, whole: el.scrollWidth <= el.clientWidth })),
      );
      expect(cells.length).toBeGreaterThan(0);
      for (const c of cells) expect(c).toEqual({ white: "nowrap", whole: true });
      const widths = await table.locator("thead th").evaluateAll((all) => all.map((el) => Math.round(el.getBoundingClientRect().width)));
      expect(widths[2], "the prompt column is wider than the time stamp column").toBeGreaterThan(widths[0]);
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

  test("a pair row keeps its name, value and remove button on one line, the button inside the room", async ({ page, network }, testInfo) => {
    // the Settings form pane of a tablet is 184 px wide: two inputs cannot share a line there; a phone's is wider
    test.skip(!testInfo.project.name.startsWith("phone-390"), "the phone row is the one that must hold three controls");
    network.use(http.get("/api/settings", () => HttpResponse.json(withPairs())));
    await page.goto("/#/settings");
    const group = page.getByRole("group", { name: "extra body" }).first();
    await group.scrollIntoViewIfNeeded();
    await expect(group.getByPlaceholder("name")).toHaveCount(2);
    const remove = group.getByRole("button", { name: "remove" });
    await expect(remove).toHaveCount(2);
    for (const i of [0, 1]) {
      const [n, v, r, row] = await Promise.all([
        group.getByPlaceholder("name").nth(i).boundingBox(),
        group.getByPlaceholder("value").nth(i).boundingBox(),
        remove.nth(i).boundingBox(),
        group.locator(".fd-joined").nth(i).boundingBox(),
      ]);
      expect(Math.abs(n!.y - v!.y), `row ${i}: name and value on one line`).toBeLessThanOrEqual(1);
      expect(Math.abs(n!.y - r!.y), `row ${i}: the remove button on that line too`).toBeLessThanOrEqual(1);
      expect(r!.x + r!.width, `row ${i}: the button inside its row`).toBeLessThanOrEqual(row!.x + row!.width + 1);
    }
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

  // 761 px is the narrowest window with the rail beside the page; from 1024 px the list of sections is beside the form
  for (const width of [761, 768, 900, 1023, 1100]) {
    test(`the form has the room for its words at ${width} px (a tablet's select above it, a wide window's list beside it)`, async ({ page }, testInfo) => {
      test.skip(!testInfo.project.name.startsWith("desktop-1280-dark"), "the width is set here");
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/#/settings");
      const form = page.locator(".fd-pane-narrow");
      await expect(form.getByRole("radiogroup").first()).toBeVisible();
      const formWidth = (await form.boundingBox())!.width;
      const picker = page.locator(".fd-picker-select");
      if (width < 1024) {
        // the section select stands above the form and the form has the whole width of the main part
        await expect(picker).toBeVisible();
        await expect(page.getByRole("navigation", { name: "Settings sections" })).toBeHidden();
        const main = (await page.locator(".fd-panes").boundingBox())!;
        expect(formWidth, "the form takes the whole row").toBeGreaterThanOrEqual(main.width - 1);
        const [p, f] = await Promise.all([picker.boundingBox(), form.boundingBox()]);
        expect(p!.y + p!.height, "the select is above the form").toBeLessThanOrEqual(f!.y + 1);
      } else {
        await expect(picker).toBeHidden();
        await expect(page.getByRole("navigation", { name: "Settings sections" })).toBeVisible();
        expect(formWidth).toBeGreaterThanOrEqual(440);
      }
      // every option of every segmented control is whole: nothing is cut or spilled
      const options = await page.locator(".fd-seg-item").evaluateAll((els) =>
        els.map((el) => ({ text: el.textContent, extra: el.scrollWidth - el.clientWidth, ellipsis: getComputedStyle(el).textOverflow })),
      );
      expect(options.length).toBeGreaterThan(0);
      for (const o of options) {
        expect(o.extra, `${o.text} is cut`).toBeLessThanOrEqual(0);
        expect(o.ellipsis).toBe("clip");
      }
      await expectNoSidewaysScroll(page);
    });
  }

  test("options that do not fit one row wrap to the next, each whole", async ({ page }, testInfo) => {
    test.skip(!testInfo.project.name.startsWith("desktop-1280-dark"), "the width is set here");
    await page.setViewportSize({ width: 320, height: 800 });
    await page.goto("/#/settings");
    const group = page.getByRole("radiogroup").first();
    await expect(group).toBeVisible();
    const items = await group.locator(".fd-seg-item").evaluateAll((els) => {
      const box = els[0].parentElement!.getBoundingClientRect();
      return {
        control: { left: box.left, right: box.right },
        items: els.map((el) => {
          const r = el.getBoundingClientRect();
          return { text: el.textContent, top: Math.round(r.top), left: r.left, right: r.right, extra: el.scrollWidth - el.clientWidth };
        }),
      };
    });
    expect(new Set(items.items.map((i) => i.top)).size, "the options stand on more than one row").toBeGreaterThan(1);
    for (const i of items.items) {
      expect(i.extra, `${i.text} is cut`).toBeLessThanOrEqual(0);
      expect(i.left).toBeGreaterThanOrEqual(items.control.left - 1);
      expect(i.right).toBeLessThanOrEqual(items.control.right + 1);
    }
    await expectNoSidewaysScroll(page);
  });

  for (const width of [390, 768, 1100, 1280]) {
    test(`a link to one MCP server brings its card into view below the head (${width} px)`, async ({ page }, testInfo) => {
      test.skip(!testInfo.project.name.startsWith("desktop-1280-dark"), "the width is set here");
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/#/settings/mcp/second-server");
      const card = page.locator(".fd-card-focus");
      await expect(card).toBeVisible();
      await expect(async () => {
        const [c, head] = await Promise.all([card.boundingBox(), page.locator("header.fd-head").first().boundingBox()]);
        expect(c!.y, "the card is below the page head").toBeGreaterThanOrEqual(head!.y + head!.height - 1);
        expect(c!.y, "the card is in the window").toBeLessThan(900);
      }).toPass({ timeout: 5000 });
      await expectNoSidewaysScroll(page);
    });
  }
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
