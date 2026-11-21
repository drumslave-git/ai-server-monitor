// The Memory page in real Chromium at the width of each project and in both colour schemes: the list and the open
// note (side by side from 761 px, one at a time on a phone), the edit form and the delete question stay inside the
// window, long titles and notes wrap, and the page does not scroll sideways. Each state leaves a picture in
// test-results/memory-shots/ for a look by eye.
import { http, HttpResponse } from "msw";
import type { Page, TestInfo } from "@playwright/test";
import { makeNote, memoryPage } from "../mocks/fixtures/memory";
import { expect, test } from "./fixtures";
import { expectNoSidewaysScroll } from "./polish-support";

const LONG_TITLE = "Containers of the host lab-02 are named after the stack that started them and never after the image";
const LONG_BODY = `${"The backup of nas-01 starts at 02:00 and may keep the disks busy until 03:30. ".repeat(12)}\n\n- a slow disk before 03:30 is expected\n- \`smartctl\` may time out meanwhile`;
const LONG_WORD = "/srv/very/long/path/that/has/no/spaces/at/all/and/keeps/going/for/a/while/until/it/is/cut/somewhere/else.log";

const page_ = () =>
  memoryPage({
    items: [
      makeNote({ id: 1, title: LONG_TITLE, body: `${LONG_BODY}\n\n${LONG_WORD}`, domains: ["docker", "monitoring", "storage"] }),
      ...memoryPage().items.slice(1),
    ],
  });

const answer = () => http.get("/api/memory", () => HttpResponse.json(page_()));

const isPhone = (page: Page) => page.viewportSize()!.width <= 760;

async function shot(page: Page, info: TestInfo, state: string) {
  await page.screenshot({ path: `test-results/memory-shots/${info.project.name}-${state}.png` });
}

test.describe("Memory", () => {
  test("the list: every note is a row inside the window, the filters sit in the head", async ({ page, network }, info) => {
    network.use(answer());
    await page.goto("/#/memory");
    const rows = page.getByRole("navigation", { name: "Notes" }).getByRole("link");
    await expect(rows).toHaveCount(4);
    await expect(rows.first()).toContainText(LONG_TITLE.slice(0, 20));
    await expectNoSidewaysScroll(page);
    if (!isPhone(page)) await expect(page.getByText("Pick a note.")).toBeVisible();
    const box = await rows.first().boundingBox();
    expect(box!.x + box!.width, "the row ends inside the window").toBeLessThanOrEqual(page.viewportSize()!.width);
    await shot(page, info, "list");
  });

  test("an open note: body, areas and the writer's link inside the window; the list is beside it only when it fits", async ({ page, network }, info) => {
    network.use(answer());
    await page.goto("/#/memory/1");
    await expect(page.getByRole("heading", { name: LONG_TITLE })).toBeVisible();
    await expect(page.getByRole("link", { name: "chat 3f9c2a7e" })).toBeVisible();
    await expectNoSidewaysScroll(page);
    const list = page.getByRole("navigation", { name: "Notes" });
    if (isPhone(page)) {
      await expect(list).toHaveCount(0);
      await expect(page.getByRole("link", { name: /all notes/ })).toBeVisible();
    } else {
      await expect(list).toBeVisible();
    }
    const body = page.locator(".fd-pane-scroll").last();
    const [b, w] = await Promise.all([body.boundingBox(), page.getByRole("button", { name: "delete" }).boundingBox()]);
    expect(w!.x + w!.width, "the delete button is inside the pane and the window").toBeLessThanOrEqual(Math.min(b!.x + b!.width, page.viewportSize()!.width) + 1);
    await shot(page, info, "note");
  });

  test("the edit form: fields and area boxes inside the window, save and cancel reachable", async ({ page, network }, info) => {
    network.use(answer());
    await page.goto("/#/memory/1/edit");
    const title = page.getByRole("textbox", { name: "title" });
    await expect(title).toHaveValue(LONG_TITLE);
    await shot(page, info, "edit");
    for (const control of [title, page.getByRole("textbox", { name: "note" }), page.getByRole("checkbox", { name: "storage" }), page.getByRole("button", { name: "cancel" })]) {
      await control.scrollIntoViewIfNeeded();
      const box = await control.boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width, "inside the window").toBeLessThanOrEqual(page.viewportSize()!.width);
    }
    await expect(page.getByRole("button", { name: "save" })).toBeDisabled();
    await title.fill(`${LONG_TITLE}!`);
    await expect(page.getByRole("button", { name: "save" })).toBeEnabled();
    await expectNoSidewaysScroll(page);
    if (!isPhone(page)) expect(await page.evaluate(() => document.scrollingElement!.scrollTop), "only the pane scrolls beside the rail").toBe(0);
  });

  test("the delete question opens inside the window and Esc closes it", async ({ page, network }, info) => {
    network.use(answer());
    await page.goto("/#/memory/1");
    await page.getByRole("button", { name: "delete" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();
    const { width, height } = page.viewportSize()!;
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    expect(box!.y + box!.height).toBeLessThanOrEqual(height);
    await shot(page, info, "delete");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });

  test("the page scrolls only inside its panes beside the rail, and the head stays in view", async ({ page, network }) => {
    network.use(
      http.get("/api/memory", () =>
        HttpResponse.json(memoryPage({ items: Array.from({ length: 40 }, (_, i) => makeNote({ id: i + 1, title: `Note number ${i + 1}`, updated: 1 })) })),
      ),
    );
    await page.goto("/#/memory");
    await expect(page.getByRole("navigation", { name: "Notes" }).getByRole("link")).toHaveCount(40);
    if (!isPhone(page)) {
      const scrolls = await page.evaluate(() => document.scrollingElement!.scrollHeight <= window.innerHeight + 1);
      expect(scrolls, "the window itself does not scroll beside the rail").toBe(true);
    }
    await expect(page.getByRole("heading", { name: "Memory" })).toBeInViewport();
  });
});
