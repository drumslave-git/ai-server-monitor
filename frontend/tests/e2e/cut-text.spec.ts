// Cases of cut text that need a real browser's layout: a "server · tool" unit (EntityTail) wider than its room.
// The link is cut with an ellipsis and shows a tooltip with the full name; the tool text stays whole.
import { http, HttpResponse } from "msw";
import { action } from "../mocks/fixtures/approvals";
import { expect, test } from "./fixtures";

// longer than any window this suite uses, so the link has to be cut at every width
const LONG_SERVER = `example-infrastructure-monitoring-and-inventory-gateway-${"segment-".repeat(16)}end`;
const TOOL = "list_everything";

test.describe("Approvals, audit log", () => {
  test.beforeEach(({ network }) => {
    network.use(
      http.get("/api/actions", () =>
        HttpResponse.json({
          items: [action({ id: 7, status: "executed", args: { server: LONG_SERVER, tool: TOOL, arguments: {} } })],
          total: 1,
        }),
      ),
    );
  });

  test("a very long server name in the action cell is cut with a tooltip, the tool text stays whole", async ({ page }) => {
    await page.goto("/#/approvals");
    const table = page.getByRole("table");
    const link = table.getByRole("link", { name: LONG_SERVER });
    await expect(link).toBeVisible();

    // the name inside the link is cut: more text than room, ellipsis on
    const text = link.locator(".fd-entity-text");
    const cut = await text.evaluate((el) => ({
      ellipsis: getComputedStyle(el).textOverflow === "ellipsis",
      more: el.scrollWidth > el.clientWidth,
    }));
    expect(cut.ellipsis, "the link text has an ellipsis").toBe(true);
    expect(cut.more, "the name is wider than its room").toBe(true);

    // the tool text next to it is not cut and stays on the same line as the link
    const tail = table.locator(".fd-entity-tail-text");
    expect((await tail.textContent())?.trim()).toBe(`· ${TOOL}`);
    const whole = await tail.evaluate((el) => el.scrollWidth <= el.clientWidth);
    expect(whole, "the tool text is whole").toBe(true);
    const [linkBox, tailBox] = await Promise.all([link.boundingBox(), tail.boundingBox()]);
    expect(Math.abs(linkBox!.y + linkBox!.height / 2 - (tailBox!.y + tailBox!.height / 2)), "same line").toBeLessThan(linkBox!.height);

    // the tooltip carries the full name, inside the window
    await link.scrollIntoViewIfNeeded();
    const tip = page.locator(".fd-trim-tip:popover-open");
    // the tip opens after the pointer rests on the text; a hover that lands while the table is still settling
    // (six workers share the machine) does not count, so the hover is repeated until it does
    await expect(async () => {
      await page.mouse.move(0, 0);
      await text.hover();
      await expect(tip).toBeVisible({ timeout: 1500 });
    }).toPass({ timeout: 10000 });
    await expect(tip).toHaveText(LONG_SERVER);
    const box = (await tip.boundingBox())!;
    const view = page.viewportSize()!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(view.width);

    // and nothing scrolls sideways because of it: not the page, not the main part, not a pane
    const widths = await page.evaluate(() => ({
      window: window.innerWidth,
      root: document.documentElement.scrollWidth,
      inner: [...document.querySelectorAll<HTMLElement>(".fd-main, .fd-pane-scroll")].map((el) => el.scrollWidth - el.clientWidth),
    }));
    expect(widths.root).toBeLessThanOrEqual(widths.window);
    for (const over of widths.inner) expect(over, "something scrolls sideways").toBeLessThanOrEqual(1);
  });
});
