// Prompt cards (questions the chat model asks the owner) in real Chromium at the width of each project and in both
// colour schemes: the card and its controls stay inside the thread's pane, long questions and options wrap, the
// send button is reachable, and the answered card is as tidy.
import { http, HttpResponse } from "msw";
import type { Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { expectNoSidewaysScroll } from "./polish-support";

const LONG_OPTION = "Restart the container test-main on host-a after the nightly backup job has finished and the disk is below eighty percent";
const QUESTIONS = [
  { text: `Which of the disks of host-a should the check run on first, ${"and in which order, ".repeat(3)}please?`, kind: "single", options: ["sda", LONG_OPTION, "sdb"] },
  { text: "Which checks?", kind: "multi", options: ["smart", "fsck", "scrub"] },
  { text: "Anything else?", kind: "text", options: [] },
];

const card = (over: Record<string, unknown> = {}) => ({
  id: 7,
  session_id: "s-cards",
  call_id: "call-ask",
  status: "open",
  questions: QUESTIONS,
  answers: null,
  created: 1,
  answered: null,
  answered_via: null,
  ...over,
});

function session(over: Record<string, unknown> = {}, answered = false) {
  const messages = [
    { id: 1, ts: 1, role: "user", content: "check my disks" },
    { id: 2, ts: 1, role: "assistant", content: null, tool_calls: [{ id: "call-ask", type: "function", function: { name: "ask_owner", arguments: { questions: QUESTIONS } } }] },
    { id: 3, ts: 1, role: "assistant", content: answered ? "Answered." : "Waiting for your answer.", meta: { cards: [7] } },
    ...(answered ? [{ id: 4, ts: 1, role: "assistant", content: "Checking the second disk first." }] : []),
  ];
  return http.get("/api/chat/sessions/s-cards", () => HttpResponse.json({ id: "s-cards", title: "Example cards", channel: "web", created: 1, updated: 1, cards: [card(over)], messages }));
}

/** Whole inside the pane that scrolls the thread and inside the window. */
async function expectInside(page: Page, what: Locator) {
  await what.scrollIntoViewIfNeeded();
  const pane = page.locator(".fd-pane-scroll").last();
  const [p, w] = await Promise.all([pane.boundingBox(), what.boundingBox()]);
  expect(w!.x, "starts inside the pane").toBeGreaterThanOrEqual(p!.x - 1);
  expect(w!.x + w!.width, "ends inside the pane").toBeLessThanOrEqual(p!.x + p!.width + 1);
  expect(w!.x + w!.width, "ends inside the window").toBeLessThanOrEqual(page.viewportSize()!.width);
}

test.describe("Chat, prompt cards", () => {
  test("an open card: every control inside the pane, long texts wrap, the page does not scroll sideways", async ({ page, network }) => {
    network.use(session());
    await page.goto("/#/chat/s-cards");
    const panel = page.locator("section.fd-panel", { hasText: "Which checks?" });
    await expect(panel).toBeVisible();
    await expectInside(page, panel);
    for (const control of [
      page.getByRole("radio", { name: LONG_OPTION }),
      page.getByRole("checkbox", { name: "scrub" }),
      page.getByRole("textbox", { name: "3. Anything else?" }),
      page.getByRole("button", { name: "send answers" }),
    ])
      await expectInside(page, control);
    await page.getByRole("radio", { name: "own answer" }).first().check();
    await expectInside(page, page.getByRole("textbox", { name: /^Own answer to: Which of the disks/ }));
    await expectNoSidewaysScroll(page);
  });

  test("the send button waits for every answer, then sends them", async ({ page, network }) => {
    const bodies: unknown[] = [];
    network.use(
      session(),
      http.post("/api/chat/cards/7/answer", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(card({ status: "answered", answered_via: "web" }));
      }),
    );
    await page.goto("/#/chat/s-cards");
    const send = page.getByRole("button", { name: "send answers" });
    await expect(send).toBeDisabled();
    await page.getByRole("radio", { name: "sda" }).check();
    await page.getByRole("checkbox", { name: "fsck" }).check();
    await expect(send).toBeDisabled();
    await page.getByRole("textbox", { name: "3. Anything else?" }).fill("no");
    await expect(send).toBeEnabled();
    await send.click();
    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0]).toEqual({
      answers: [
        { chosen: ["sda"], text: "" },
        { chosen: ["fsck"], text: "" },
        { chosen: [], text: "no" },
      ],
    });
  });

  test("an answered card: the answers inside the pane, no controls", async ({ page, network }) => {
    network.use(
      session(
        {
          status: "answered",
          answered_via: "telegram",
          answers: [
            { chosen: [LONG_OPTION], text: "" },
            { chosen: ["smart", "scrub"], text: "and the cables of the second drive in the rack" },
            { chosen: [], text: "no" },
          ],
        },
        true,
      ),
    );
    await page.goto("/#/chat/s-cards");
    const panel = page.locator("section.fd-panel", { hasText: "answered in Telegram" });
    await expect(panel).toBeVisible();
    await expectInside(page, panel);
    await expectInside(page, panel.getByText(LONG_OPTION));
    await expect(page.getByRole("radio")).toHaveCount(0);
    await expect(page.getByText("Checking the second disk first.")).toBeVisible();
    await expectNoSidewaysScroll(page);
  });
});
