// The owner's layout rules (memory "ui-layout-and-icons"), checked in real Chromium on every page at 390, 768 and
// 1280 px in dark and light (one Playwright project per width and scheme, see playwright.config.ts).
//
//  - no horizontal overflow of the page
//  - beside the rail (wider than 760 px) the page itself does not scroll; below that it does, and the head stays
//  - a head with controls stays visible while the content scrolls
//  - a text cut by its width shows a tooltip with the full text, inside the window
//  - the phone keeps the bottom bar with all pages (and the wide layout does not show it)
//  - hints of icon buttons fit in the window
import type { Locator, Page } from "@playwright/test";
import { http, HttpResponse, type AnyHandler } from "msw";
import { dashboardStatus } from "../mocks/fixtures/dashboard";
import { expect, test } from "./fixtures";

/** The pages of the app and the address each one opens at. head: the page opens with a PageHeader (the dashboard
 *  opens with a summary card, not a head, so there is no sticky head to check there). */
const PAGES: { name: string; hash: string; head: boolean; use?: () => AnyHandler[] }[] = [
  // the dashboard shows the state of the summaries: give it the richer status
  { name: "Dashboard", hash: "#/dashboard", head: false, use: () => [http.get("/api/status", () => HttpResponse.json(dashboardStatus()))] },
  { name: "Approvals", hash: "#/approvals", head: true },
  { name: "Chat", hash: "#/chat", head: true },
  { name: "Jobs", hash: "#/jobs", head: true },
  { name: "Job detail", hash: "#/jobs/test:disk", head: true },
  { name: "MCP tools", hash: "#/mcp", head: true },
  { name: "Memory", hash: "#/memory", head: true },
  { name: "Memory, one note", hash: "#/memory/1", head: true },
  { name: "Settings", hash: "#/settings", head: true },
  { name: "Settings, MCP server card", hash: "#/settings/mcp/test-main", head: true },
  { name: "Logs, LLM calls", hash: "#/logs", head: true },
  { name: "Logs, one call", hash: "#/logs/llm/1", head: true },
  { name: "Logs, app log", hash: "#/logs/app", head: true },
];
const PHONE_MAX = 760;
const NAV = ["dashboard", "jobs", "approvals", "chat", "mcp tools", "memory", "logs", "settings"];

async function open(page: Page, hash: string) {
  await page.goto(`/${hash}`);
  await expect(page.locator("main").getByRole("heading").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready);
}

/** A control the head shows right now. The compact head (after a scroll) folds its meta line away by design: opacity 0 and
 *  no height inside an overflow-hidden box. Links in it are not controls on the screen, and a user cannot press them. */
const shownInHead = (control: Locator) =>
  control.evaluate((el) => {
    if (!el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return false;
    for (let e: Element | null = el; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.overflow !== "visible" && e.getBoundingClientRect().height === 0) return false;
    }
    return true;
  });

const isPhone =(page: Page) => (page.viewportSize()?.width ?? 0) <= PHONE_MAX;

/** Scroll everything that scrolls on this page to its end: the document on a phone, the main part and the panes beside the rail. */
async function scrollToEnd(page: Page) {
  await page.evaluate(() => {
    window.scrollTo(0, document.documentElement.scrollHeight);
    for (const el of document.querySelectorAll<HTMLElement>(".fd-main, .fd-pane-scroll")) {
      if (el.scrollHeight > el.clientHeight) el.scrollTo(0, el.scrollHeight);
    }
  });
}

/** The box of an element once it has stopped moving (a hint is placed by the browser after it opens, and the page may still be scrolling under it). */
async function settledBox(el: Locator) {
  let last = "";
  let box: { x: number; y: number; width: number; height: number } | null = null;
  await expect
    .poll(async () => {
      box = await el.boundingBox();
      const now = JSON.stringify(box);
      const same = box !== null && now === last;
      last = now;
      return same;
    }, { intervals: [100, 100, 100, 200, 200, 400], timeout: 3000 })
    .toBe(true);
  return box!;
}

/** The elements of the page whose text is cut by its width right now (text-overflow: ellipsis and more text than room). */
async function cutTexts(page: Page): Promise<Locator[]> {
  const count = await page.evaluate(() => {
    let n = 0;
    for (const el of document.querySelectorAll<HTMLElement>("main *, aside *")) {
      const hidden = !el.offsetParent && getComputedStyle(el).position !== "fixed";
      const field = el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement;
      if (!hidden && !field && getComputedStyle(el).textOverflow === "ellipsis" && el.scrollWidth > el.clientWidth) {
        el.setAttribute("data-cut-probe", String(n++));
      }
    }
    return n;
  });
  return Array.from({ length: count }, (_, i) => page.locator(`[data-cut-probe="${i}"]`));
}

for (const { name, hash, head: hasHead, use } of PAGES) {
  test.describe(name, () => {
    test.beforeEach(({ network }) => {
      if (use) network.use(...use());
    });

    test("the page has no horizontal overflow", async ({ page }) => {
      await open(page, hash);
      const widths = await page.evaluate(() => {
        const main = document.querySelector<HTMLElement>(".fd-main")!;
        return {
          window: window.innerWidth,
          root: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
          main: main.scrollWidth - main.clientWidth,
        };
      });
      expect(widths.root, "document wider than the window").toBeLessThanOrEqual(widths.window);
      expect(widths.body, "body wider than the window").toBeLessThanOrEqual(widths.window);
      expect(widths.main, "main part scrolls sideways").toBeLessThanOrEqual(0);
    });

    test("the page itself scrolls only on a phone; beside the rail the main part and panes scroll", async ({ page }) => {
      await open(page, hash);
      // a window this short makes every page's content taller than the window
      await page.setViewportSize({ width: page.viewportSize()!.width, height: 360 });
      const doc = await page.evaluate(() => ({ scroll: document.documentElement.scrollHeight, window: window.innerHeight }));
      if (isPhone(page)) {
        expect(doc.scroll, "a phone page scrolls as a whole").toBeGreaterThan(doc.window);
      } else {
        expect(doc.scroll, "the page must not scroll beside the rail").toBeLessThanOrEqual(doc.window);
        const scrolls = await page.evaluate(() =>
          [...document.querySelectorAll<HTMLElement>(".fd-main, .fd-pane-scroll")].some(
            (el) => el.scrollHeight > el.clientHeight && getComputedStyle(el).overflowY !== "visible",
          ),
        );
        expect(scrolls, "something inside the page scrolls on its own").toBe(true);
      }
    });

    test("the head stays in view while the content scrolls", async ({ page }) => {
      test.skip(!hasHead, "this page opens with a summary card, not a PageHeader");
      await open(page, hash);
      await page.setViewportSize({ width: page.viewportSize()!.width, height: 360 });
      const head = page.locator("header.fd-head").first();
      await scrollToEnd(page);
      await page.waitForTimeout(200);
      const box = await head.boundingBox();
      expect(box, "the head is rendered").not.toBeNull();
      expect(box!.y, "head top edge at the top of the window").toBeGreaterThanOrEqual(-2);
      expect(box!.y, "head top edge at the top of the window").toBeLessThanOrEqual(2);
      await expect(head.locator("h1")).toBeInViewport();
      // every control in the head is on the screen with it
      const controls = head.locator("button, a, input, select");
      for (let i = 0; i < (await controls.count()); i++) {
        const control = controls.nth(i);
        if (await shownInHead(control)) await expect(control).toBeInViewport();
      }
    });

    test("the phone has the bottom bar with every page; wider windows have none", async ({ page }) => {
      await open(page, hash);
      const bar = page.locator("nav.fd-bottom");
      if (isPhone(page)) {
        await expect(bar).toBeVisible();
        const labels = await bar.locator("a").allInnerTexts();
        expect(labels.map((l) => l.replace(/\d+/g, "").trim().toLowerCase())).toEqual(NAV);
        const box = await bar.boundingBox();
        expect(box!.y + box!.height, "bar at the foot of the window").toBeCloseTo(page.viewportSize()!.height, 0);
        // it stays when the page is scrolled
        await scrollToEnd(page);
        await expect(bar).toBeInViewport({ ratio: 1 });
      } else {
        await expect(bar).toBeHidden();
        await expect(page.locator("aside.fd-rail nav.fd-nav a")).toHaveCount(NAV.length);
      }
    });

    test("a cut text shows its full text in a tooltip inside the window", async ({ page }) => {
      await open(page, hash);
      const cut = await cutTexts(page);
      test.info().annotations.push({ type: "cut texts", description: String(cut.length) });
      for (const el of cut.slice(0, 4)) {
        await el.scrollIntoViewIfNeeded();
        const full = ((await el.textContent()) ?? "").trim();
        await el.hover();
        const tip = page.locator(".fd-trim-tip:popover-open");
        await expect(tip, `tooltip for "${full.slice(0, 40)}"`).toBeVisible({ timeout: 3000 });
        const shown = ((await tip.textContent()) ?? "").trim();
        expect(shown.length).toBeGreaterThan(0);
        expect(full, "the tooltip says what the cut text says").toContain(shown);
        const at = await settledBox(tip);
        const view = page.viewportSize()!;
        expect(at.x).toBeGreaterThanOrEqual(0);
        expect(at.y).toBeGreaterThanOrEqual(0);
        expect(at.x + at.width).toBeLessThanOrEqual(view.width);
        expect(at.y + at.height).toBeLessThanOrEqual(view.height);
        await page.mouse.move(0, 0);
        await expect(tip).toBeHidden();
      }
    });

    test("the hint of an icon button fits in the window", async ({ page }) => {
      await open(page, hash);
      const buttons = page.locator("button.fd-btn-icon:visible");
      const count = Math.min(await buttons.count(), 4);
      for (let i = 0; i < count; i++) {
        const button = buttons.nth(i);
        await button.scrollIntoViewIfNeeded();
        const hint = page.locator(".fd-hint:popover-open");
        // a page that is still settling (the Settings card scrolls itself into place once the layout is quiet) can
        // move the button from under the pointer, so the hover is repeated until the hint opens
        await expect(async () => {
          await page.mouse.move(0, 0);
          await button.scrollIntoViewIfNeeded();
          await button.hover();
          await expect(hint).toBeVisible({ timeout: 1500 });
        }).toPass({ timeout: 10000 });
        const at = await settledBox(hint);
        const view = page.viewportSize()!;
        expect(at.x).toBeGreaterThanOrEqual(0);
        expect(at.y).toBeGreaterThanOrEqual(0);
        expect(at.x + at.width).toBeLessThanOrEqual(view.width);
        expect(at.y + at.height).toBeLessThanOrEqual(view.height);
        await page.mouse.move(0, 0);
      }
    });
  });
}
