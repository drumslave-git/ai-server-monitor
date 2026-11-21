// The README's pictures of the chat with an approval, a prompt card and the Memory page (docs/samples/ui): the real UI in
// dark Chromium, the API answered with invented data (the hardware meters too). It takes pictures only when SHOT_DIR is
// set, so a normal test run leaves it out:
//   SHOT_DIR=../docs/samples/ui npx playwright test screenshots --project=fixed-width-dark
import { http, HttpResponse } from "msw";
import type { Page } from "@playwright/test";
import { batch, action } from "../mocks/fixtures/approvals";
import { memoryPage } from "../mocks/fixtures/memory";
import { expect, test } from "./fixtures";

const DIR = process.env.SHOT_DIR;
test.skip(!DIR, "set SHOT_DIR to take the README pictures");
test.beforeEach(({ browserName }, info) => {
  test.skip(browserName !== "chromium" || info.project.name !== "fixed-width-dark", "dark Chromium only");
});

const GIB = 1024 ** 3;
const NOW = Math.floor(Date.now() / 1000);

/** What the rail of every page shows: the version, the meters of a machine that does not exist, one approval waiting. */
const shell = (pending: number) => [
  http.get("/api/info", () => HttpResponse.json({ version: "0.11.0", commit: "" })),
  http.get("/api/hardware", () =>
    HttpResponse.json({
      cpu_percent: 23.4,
      ram_used: Math.round(11.6 * GIB),
      ram_total: 64 * GIB,
      gpus: [{ name: "Demo Accelerator 9000", util_percent: 37, vram_used: 6 * GIB, vram_total: 16 * GIB }],
    }),
  ),
  http.get("/api/status", () =>
    HttpResponse.json({
      automation: { paused: false },
      jobs: [],
      llm_configured: true,
      main_model: "demo-main",
      worker_model: "demo-worker",
      now: NOW,
      pending_approvals: pending,
      uptime_s: 7200,
      version: "0.11.0",
    }),
  ),
  http.get("/api/chat/sessions", () =>
    HttpResponse.json({
      items: [
        { id: "s-journal", title: "Free disk space on nas-01", channel: "web", created: NOW - 600, updated: NOW - 60, messages: 4 },
        { id: "s-backup", title: "Did the nightly backup run?", channel: "telegram", created: NOW - 90_000, updated: NOW - 7_200, messages: 2 },
        { id: "s-fan", title: "Which container uses the most memory", channel: "web", created: NOW - 200_000, updated: NOW - 190_000, messages: 6 },
      ],
      limit: 50,
      offset: 0,
      total: 3,
    }),
  ),
];

const session = (id: string, title: string, messages: Record<string, unknown>[], cards: unknown[] = []) =>
  http.get(`/api/chat/sessions/${id}`, () =>
    HttpResponse.json({ id, title, channel: "web", created: NOW - 600, updated: NOW - 60, cards, messages: messages.map((m, i) => ({ id: i + 1, ts: NOW - 300 + i, ...m })) }),
  );

/** The event stream of a server that is up: the rail says "live" (the page opens one EventSource, which is all it needs). */
const liveStream = (page: Page) =>
  page.addInitScript(() => {
    class OpenStream {
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor() {
        setTimeout(() => this.onopen?.(), 0);
      }
      addEventListener() {}
      close() {}
    }
    (window as unknown as { EventSource: unknown }).EventSource = OpenStream;
  });

const shot = async (page: Page, name: string) => {
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${DIR}/${name}.png` });
};

test.describe("README pictures", () => {
  test("chat-approval: a changing command waits for the owner", async ({ page, network }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const pending = batch({
      id: 71,
      title: "Free disk space",
      action: "host_command",
      summary: "host command",
      source: "chat:s-journal",
      expires_at: NOW + 2 * 3600,
      items: [
        action({
          id: 501,
          action: "host_command",
          target: "host",
          source: "chat:s-journal",
          ts: NOW - 120,
          expires_at: NOW + 2 * 3600,
          args: { command: "journalctl --vacuum-size=200M", reason: "The journal of nas-01 takes 3.1 GB; this keeps the newest 200 MB." },
        }),
      ],
    });
    network.use(
      ...shell(1),
      http.get("/api/approvals", () => HttpResponse.json({ batches: [pending], items: pending.items })),
      session("s-journal", "Free disk space on nas-01", [
        { role: "user", content: "The disk of nas-01 is almost full. Can you free some space?" },
        { role: "assistant", content: null, tool_calls: [{ id: "c1", type: "function", function: { name: "bash", arguments: { command: "du -xh /var --max-depth=2 | sort -h | tail -4" } } }] },
        { role: "tool", name: "bash", tool_call_id: "c1", content: "3.1G\t/var/log/journal\n3.4G\t/var/log\n9.8G\t/var/lib/docker\n14G\t/var", meta: { ok: true } },
        {
          role: "assistant",
          content: "The systemd journal takes 3.1 GB of `/var/log`. I can shrink it to 200 MB; that is a change, so it waits for your approval.",
          meta: { waiting_for: [501] },
        },
      ]),
    );
    await liveStream(page);
    await page.goto("/#/chat/s-journal");
    await expect(page.getByText("waiting for your approval").first()).toBeVisible();
    await expect(page.getByText(/GPU/).first()).toBeVisible();
    await shot(page, "chat-approval");
  });

  test("chat-prompt-card: the model asks the owner and waits", async ({ page, network }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    const questions = [
      { text: "Which disk of nas-01 should the SMART test run on first?", kind: "single", options: ["sda (system, 2 years old)", "sdb (data, 6 years old)"] },
      { text: "Which checks do you want?", kind: "multi", options: ["short SMART test", "long SMART test"] },
      { text: "Anything the test must not interrupt?", kind: "text", options: [] },
    ];
    network.use(
      ...shell(0),
      session(
        "s-journal",
        "Check the disks of nas-01",
        [
          { role: "user", content: "Check the disks of nas-01." },
          { role: "assistant", content: null, tool_calls: [{ id: "ask", type: "function", function: { name: "ask_owner", arguments: { questions } } }] },
          { role: "assistant", content: "Waiting for your answer.", meta: { cards: [9] } },
        ],
        [{ id: 9, session_id: "s-journal", call_id: "ask", status: "open", questions, answers: null, created: NOW - 60, answered: null, answered_via: null }],
      ),
    );
    await liveStream(page);
    await page.goto("/#/chat/s-journal");
    await expect(page.getByRole("button", { name: "send answers" })).toBeVisible();
    await page.getByRole("radio", { name: "sdb (data, 6 years old)" }).check();
    await page.getByRole("checkbox", { name: "long SMART test" }).check();
    await page.getByRole("textbox", { name: "3. Anything the test must not interrupt?" }).fill("The backup of 02:00.");
    await expect(page.getByRole("button", { name: "send answers" })).toBeEnabled();
    await page.locator(".fd-pane-scroll").last().evaluate((el) => el.scrollTo(0, 0));
    await shot(page, "chat-prompt-card");
  });

  test("memory: the notes the model keeps", async ({ page, network }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    network.use(...shell(0), http.get("/api/memory", () => HttpResponse.json(memoryPage())));
    await liveStream(page);
    await page.goto("/#/memory/1");
    await expect(page.getByRole("heading", { name: "Backup window" })).toBeVisible();
    await shot(page, "memory");
  });
});
