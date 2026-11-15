// Screenshot of Settings with the Test buttons pressed (run scripts/dev/conn_demo.py first).
//   node scripts/dev/settings_tests_shot.mjs <out.png> [base-url] [section] [width] [height]
// Presses every "Test" button of the section, waits for the results, saves a PNG. Development only.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const out = process.argv[2] ?? "docs/samples/ui/settings-tests.png";
const base = process.argv[3] ?? "http://127.0.0.1:8097";
const section = process.argv[4] ?? "arr";
const width = Number(process.argv[5] ?? 1440);
const height = Number(process.argv[6] ?? 1500);
const extra = process.argv[7]; // optional: text of one more button to press, e.g. "Send test message"
const chrome = process.env.CHROME_PATH ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";
mkdirSync(dirname(out), { recursive: true });

const port = 9334;
const proc = spawn(chrome, [
  "--headless=new", "--disable-gpu", "--hide-scrollbars", `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), "asm-shot-"))}`, "about:blank",
], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let target;
for (let i = 0; i < 50 && !target; i++) {
  try {
    target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((t) => t.type === "page");
  } catch { /* chrome is still starting */ }
  if (!target) await sleep(200);
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let id = 0;
const pending = new Map();
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) pending.get(msg.id)(msg.result ?? msg.error);
};
const call = (method, params = {}) => new Promise((resolve) => {
  const n = ++id;
  pending.set(n, resolve);
  ws.send(JSON.stringify({ id: n, method, params }));
});
const evaluate = (expression) => call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });

await call("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
await call("Page.navigate", { url: `${base}/#/settings/${section}` });
await sleep(2500);
const labels = ["Test", ...(extra ? [extra] : [])];
for (const label of labels) {
  await evaluate(`(async () => {
    const wanted = ${JSON.stringify(label)};
    for (const b of [...document.querySelectorAll("button.btn-test")].filter((b) => b.textContent.trim() === wanted)) {
      b.click();
      await new Promise((r) => setTimeout(r, 50));
    }
  })()`);
  await sleep(2500);
}
const results = await evaluate(`[...document.querySelectorAll(".test-result p")].map((p) => p.textContent)`);
console.log(JSON.stringify(results.result?.value, null, 1));
const { data } = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
writeFileSync(out, Buffer.from(data, "base64"));
console.log("saved", out);
ws.close();
proc.kill();
