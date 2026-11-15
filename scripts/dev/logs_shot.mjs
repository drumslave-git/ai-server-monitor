// Screenshots of the Logs page via headless Chrome (run scripts/dev/trace_demo.py and the Vite
// harness first: `vite --config dev/vite.preview.config.ts` in frontend/). Development only.
//   node scripts/dev/logs_shot.mjs <out.png> <hash> [width] [height] [dark|light] [--full] [--click "text"] [--open "text"] [--scroll selector]
//   e.g. node scripts/dev/logs_shot.mjs out.png "#/logs/llm/1" 1280 900 light --full
// --full captures the whole page height; --click clicks the first element (button/summary/a)
// with exactly that text before the shot, --open opens every <details> that contains the text.
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const args = process.argv.slice(2);
const flag = (name) => {
  const i = args.indexOf(name);
  if (i < 0) return null;
  const [, value] = args.splice(i, 2);
  return value ?? true;
};
const full = args.includes("--full") && args.splice(args.indexOf("--full"), 1).length > 0;
const clicks = [];
for (let v; (v = flag("--click")); ) clicks.push(v);
const scrollTo = flag("--scroll");
const opens = [];
for (let v; (v = flag("--open")); ) opens.push(v);
const [out, hash, width = "1280", height = "900", scheme = "dark"] = args;
const base = process.env.BASE ?? "http://localhost:5188/dev/logs.html";
const chrome = process.env.CHROME_PATH ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";
mkdirSync(dirname(out), { recursive: true });

const port = 9336;
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

const w = Number(width);
await call("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
await call("Emulation.setDeviceMetricsOverride", { width: w, height: Number(height), deviceScaleFactor: 1, mobile: w < 600 });
await call("Page.navigate", { url: `${base}${hash}` });
await sleep(1800);
for (const text of opens) {
  await evaluate(`[...document.querySelectorAll("details")].filter((d) => d.textContent.includes(${JSON.stringify(text)})).forEach((d) => (d.open = true))`);
}
for (const text of clicks) {
  await evaluate(`[...document.querySelectorAll("button,summary,a")].find((e) => e.textContent.trim() === ${JSON.stringify(text)})?.click()`);
  await sleep(700);
}
if (scrollTo) await evaluate(`document.querySelector(${JSON.stringify(scrollTo)})?.scrollIntoView({ block: "start" })`);
await sleep(400);
let clip;
if (full) {
  const size = await evaluate(`({ w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight })`);
  const { w: sw, h: sh } = size.result.value;
  clip = { x: 0, y: 0, width: sw, height: sh, scale: 1 };
}
const { data } = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: full, ...(clip ? { clip } : {}) });
writeFileSync(out, Buffer.from(data, "base64"));
const overflow = await evaluate(`document.documentElement.scrollWidth > ${w} + 1`);
console.log("saved", out, overflow.result.value ? "WARNING: horizontal overflow" : "no horizontal overflow");
ws.close();
proc.kill();
