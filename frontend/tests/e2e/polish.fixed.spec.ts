// Layout checks that set their own window width (one Playwright project per scheme, "fixed-width-*", runs them: the
// six width projects ignore *.fixed.spec.ts, so none of these is skipped where it does not apply).
import { http, HttpResponse } from "msw";
import { action } from "../mocks/fixtures/approvals";
import { expect, test } from "./fixtures";
import { expectNoSidewaysScroll, LONG_DETAIL, withPairs } from "./polish-support";

for (const width of [1100, 1280]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });

    test(`Approvals audit log: the detail stays on one line, is cut and has the whole text in a tooltip (${width})`, async ({ page, network }) => {
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

    test(`Logs: the time stamp stands on two lines (date above time), whole, and the prompt column gets the room (${width})`, async ({ page }) => {
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

test.describe("Settings, panes at set widths", () => {
  for (const width of [761, 768, 900, 1023, 1100]) {
    test(`the form has the room for its words at ${width} px (a tablet's select above it, a wide window's list beside it)`, async ({ page }) => {
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

  test("options that do not fit one row wrap to the next, each whole", async ({ page }) => {
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
    test(`a link to one MCP server brings its card into view below the head (${width} px)`, async ({ page }) => {
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

test.describe("Settings, pair names on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test("a pair row keeps its name, value and remove button on one line, the button inside the room", async ({ page, network }) => {
    // the Settings form pane of a tablet is 184 px wide: two inputs cannot share a line there; a phone's is wider
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

// The rail takes 212 px of a tablet: beside a list (300 px, or 440 for the runs) the other pane was left 184 px (MCP tools) or 44 px (a job's run)
const STACKED = [
  { page: "MCP tools", hash: "#/mcp", list: "Domains", lead: "Tools" },
  { page: "a job's runs", hash: "#/jobs/test:disk", list: "Runs", lead: "Run" },
];
for (const p of STACKED) {
  test.describe(`${p.page}, the list and the lead pane`, () => {
    // 761 px is the narrowest window with the rail beside the page; from 1024 px the panes stand side by side
    for (const width of [761, 768, 900, 1023]) {
      test(`stand under each other, each with the whole width and a height of its own at ${width} px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await page.goto(`/${p.hash}`);
        const list = page.locator(".fd-pane-side");
        const lead = page.locator(".fd-pane:not(.fd-pane-side)").first();
        await expect(list).toBeVisible();
        await expect(lead).toBeVisible();
        const [l, c, all] = await Promise.all([list.boundingBox(), lead.boundingBox(), page.locator(".fd-panes").boundingBox()]);
        expect(c!.width, `the ${p.lead} pane has room`).toBeGreaterThanOrEqual(360);
        expect(l!.width, `the ${p.list} list has the whole row`).toBeGreaterThanOrEqual(all!.width - 1);
        expect(c!.width, `the ${p.lead} pane has the whole row`).toBeGreaterThanOrEqual(all!.width - 1);
        const [above, below] = l!.y < c!.y ? [l!, c!] : [c!, l!];
        expect(above.y + above.height, "one pane is above the other").toBeLessThanOrEqual(below.y + 1);
        expect(l!.height, `the ${p.list} list is no taller than a picker's list`).toBeLessThanOrEqual(420 + 1);
        expect(c!.height, `the ${p.lead} pane keeps height to be read in`).toBeGreaterThanOrEqual(200);
        expect(below.y + below.height, "the panes end inside the window").toBeLessThanOrEqual(900);
        await expectNoSidewaysScroll(page);
      });
    }

    test("stand side by side from 1024 px, as before", async ({ page }) => {
      await page.setViewportSize({ width: 1024, height: 900 });
      await page.goto(`/${p.hash}`);
      const list = page.locator(".fd-pane-side");
      const lead = page.locator(".fd-pane:not(.fd-pane-side)").first();
      await expect(list).toBeVisible();
      const [l, c] = await Promise.all([list.boundingBox(), lead.boundingBox()]);
      expect(Math.abs(l!.y - c!.y), "they start at one height").toBeLessThanOrEqual(1);
      const [left, right] = l!.x < c!.x ? [l!, c!] : [c!, l!];
      expect(left.x + left.width, "side by side, not over each other").toBeLessThanOrEqual(right.x + 1);
    });
  });
}
