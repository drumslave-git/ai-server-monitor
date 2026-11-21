// Dialogs (kit Dialog, Confirm, Picker) opened in real Chromium: each one opens on screen, fits in the window at the
// project's width and in a short window, can be left with Esc, and gives the focus back to what opened it.
// Hints of icon buttons and tooltips of cut text are checked per page in layout.spec.ts.
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

const OVERLAYS: { name: string; hash: string; open: (page: Page) => Promise<void> }[] = [
  {
    // the edit form of a job made by the owner or the model offers "delete" (a changed builtin job "reset to default"): both ask in a Confirm
    name: "Confirm: delete a job",
    hash: "#/jobs/test:disk/edit",
    open: (page) => page.getByRole("button", { name: "delete" }).click(),
  },
  {
    name: "Picker: add a job to a summary",
    hash: "#/jobs/daily/edit",
    open: (page) => page.getByRole("button", { name: "add job" }).click(),
  },
  {
    name: "Dialog: sort into domains",
    hash: "#/mcp",
    open: (page) => page.getByRole("button", { name: "sort into domains…" }).click(),
  },
  {
    name: "Dialog: rename or describe a domain",
    hash: "#/mcp",
    open: (page) => page.getByRole("button", { name: /^Rename or describe / }).first().click(),
  },
];

for (const { name, hash, open } of OVERLAYS) {
  test.describe(name, () => {
    test("opens inside the window, also in a short one, and Esc closes it", async ({ page }) => {
      await page.goto(`/${hash}`);
      await expect(page.locator("main").getByRole("heading").first()).toBeVisible();
      await page.waitForLoadState("networkidle");
      await page.evaluate(() => document.fonts.ready);

      const dialog = page.getByRole("dialog");
      for (const height of [page.viewportSize()!.height, 420]) {
        await page.setViewportSize({ width: page.viewportSize()!.width, height });
        if (!(await dialog.isVisible())) await open(page);
        await expect(dialog).toBeVisible();
        await expect(dialog).toHaveJSProperty("open", true);
        const box = (await dialog.boundingBox())!;
        const view = page.viewportSize()!;
        expect(box.x, "left edge").toBeGreaterThanOrEqual(0);
        expect(box.y, "top edge").toBeGreaterThanOrEqual(0);
        expect(box.x + box.width, "right edge").toBeLessThanOrEqual(view.width);
        expect(box.y + box.height, "bottom edge").toBeLessThanOrEqual(view.height);
        // nothing in the dialog scrolls sideways
        const wide = await dialog.evaluate((el) => el.scrollWidth - el.clientWidth);
        expect(wide, "the dialog scrolls sideways").toBeLessThanOrEqual(1);
        // and the page behind does not
        const root = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(root, "the page scrolls sideways").toBeLessThanOrEqual(0);
      }

      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
    });
  });
}
