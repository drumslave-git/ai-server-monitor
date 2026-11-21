// The Logs list once scrolled sideways in a window between the phone and the wide desktop (about 1100 px, beside
// the rail). The project widths (390/768/1280) did not catch it, so this runs at 1100 px, in the fixed-width projects (see playwright.config.ts).
import { expect, test } from "./fixtures";

test.use({ viewport: { width: 1100, height: 800 } });

const VIEWS = [
  { name: "LLM calls", hash: "#/logs" },
  { name: "app log", hash: "#/logs/app" },
];

for (const view of VIEWS) {
  test(`Logs, ${view.name}: nothing scrolls sideways at 1100 px`, async ({ page }) => {
    await page.goto(`/${view.hash}`);
    await expect(page.getByRole("table").first()).toBeVisible();
    const over = await page.evaluate(() => ({
      window: window.innerWidth,
      root: document.documentElement.scrollWidth,
      inner: [...document.querySelectorAll<HTMLElement>(".fd-main, .fd-pane-scroll")].map((el) => el.scrollWidth - el.clientWidth),
    }));
    expect(over.root, "the page scrolls sideways").toBeLessThanOrEqual(over.window);
    for (const extra of over.inner) expect(extra, "a pane scrolls sideways").toBeLessThanOrEqual(1);
  });
}
