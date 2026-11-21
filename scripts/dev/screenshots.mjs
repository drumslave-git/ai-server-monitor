// The README's screenshots (docs/samples/ui) of a running demo UI, via headless Chrome and the
// DevTools protocol (Node 22+).
//   node scripts/dev/screenshots.mjs <out-dir> [base-url] [name,name]
// Needs Chrome at the usual Windows path or CHROME_PATH, and an instance at base-url that holds
// demo data: jobs with runs. It only reads from it. The pictures of the chat (an approval, a prompt
// card) and of the Memory page come from frontend/tests/e2e/screenshots.fixed.spec.ts instead: the
// real UI against an API answered with invented data.
// SHOT_SCHEME=light takes them in the light theme. Development only.
// The hardware meters never show the machine the script runs on: /api/hardware is answered with
// the invented reading HARDWARE below.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const out = process.argv[2] ?? "docs/samples/ui";
const base = process.argv[3] ?? "http://127.0.0.1:8099";
const chrome = process.env.CHROME_PATH ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";
mkdirSync(out, { recursive: true });

const only = process.argv[4]?.split(",");

const shots = [
  { name: "dashboard", route: "dashboard", width: 1440, height: 900 },
  { name: "jobs", route: "jobs", width: 1440, height: 900 },
  { name: "job-run", route: "jobs/summary:health", width: 1440, height: 900 },
];

const GIB = 1024 ** 3;
const HARDWARE = {
  cpu_percent: 23.4,
  ram_used: Math.round(11.6 * GIB),
  ram_total: 64 * GIB,
  gpus: [{ name: "Demo Accelerator 9000", util_percent: 37, vram_used: 6 * GIB, vram_total: 16 * GIB }],
};

const port = 9333;
const proc = spawn(chrome, [
  "--headless=new", "--disable-gpu", "--hide-scrollbars", `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), "asm-shot-"))}`, "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let target;
for (let i = 0; i < 50 && !target; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    target = list.find((t) => t.type === "page");
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
  if (msg.method === "Fetch.requestPaused") {
    ws.send(JSON.stringify({
      id: ++id,
      method: "Fetch.fulfillRequest",
      params: {
        requestId: msg.params.requestId,
        responseCode: 200,
        responseHeaders: [{ name: "content-type", value: "application/json" }],
        body: Buffer.from(JSON.stringify(HARDWARE)).toString("base64"),
      },
    }));
  }
};
const call = (method, params = {}) => new Promise((resolve) => {
  const n = ++id;
  pending.set(n, resolve);
  ws.send(JSON.stringify({ id: n, method, params }));
});

await call("Fetch.enable", { patterns: [{ urlPattern: "*/api/hardware*" }] });

for (const s of shots.filter((x) => !only || only.includes(x.name))) {
  await call("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-color-scheme", value: process.env.SHOT_SCHEME ?? "dark" }],
  });
  await call("Emulation.setDeviceMetricsOverride", {
    width: s.width, height: s.height, deviceScaleFactor: s.mobile ? 2 : 1, mobile: !!s.mobile,
  });
  await call("Page.navigate", { url: `${base}/?shot=${s.name}#/${s.route}` });
  await sleep(Number(process.env.SHOT_WAIT ?? 6000));
  const { data } = await call("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(out, `${s.name}.png`), Buffer.from(data, "base64"));
  console.log("saved", s.name);
}
ws.close();
proc.kill();
