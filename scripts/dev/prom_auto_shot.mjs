// Screenshot of Settings → Collectors → Automatic queries (run scripts/dev/prom_auto_demo.py first).
//   node scripts/dev/prom_auto_shot.mjs <out.png> [base-url] [width] [height]
// Waits until a running plan is done, scrolls the panel into view, saves a PNG. Development only.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const out = process.argv[2] ?? "docs/samples/ui/settings-prom-auto.png";
const base = process.argv[3] ?? "http://127.0.0.1:8098";
const width = Number(process.argv[4] ?? 1440);
const height = Number(process.argv[5] ?? 1300);
const chrome = process.env.CHROME_PATH ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";
mkdirSync(dirname(out), { recursive: true });

const port = 9335;
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
await call("Page.navigate", { url: `${base}/#/settings/collectors` });
await sleep(2000);
for (let i = 0; i < 180; i++) {
  const state = await evaluate(`fetch("/api/prometheus/queries").then((r) => r.json()).then((d) => d.plan.running)`);
  if (state.result?.value === false) break;
  await sleep(2000);
}
await sleep(3000);
await evaluate(`document.querySelector("#promauto-title")?.scrollIntoView({ block: "start" })`);
await sleep(500);
const { data } = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
writeFileSync(out, Buffer.from(data, "base64"));
console.log("saved", out);
ws.close();
proc.kill();
