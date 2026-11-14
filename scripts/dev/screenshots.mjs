// Screenshots of the running demo UI via headless Chrome and the DevTools protocol (Node 22+).
//   node scripts/dev/screenshots.mjs <out-dir> [base-url]
// Needs Chrome at the usual Windows path or CHROME_PATH. Development only.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const out = process.argv[2] ?? "docs/samples/ui";
const base = process.argv[3] ?? "http://127.0.0.1:8099";
const chrome = process.env.CHROME_PATH ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";
mkdirSync(out, { recursive: true });

const shots = [
  { name: "dashboard-desktop", route: "dashboard", width: 1440, height: 1180 },
  { name: "reports-desktop", route: "reports", width: 1440, height: 1000 },
  { name: "approvals-desktop", route: "approvals", width: 1440, height: 900 },
  { name: "arr-desktop", route: "arr", width: 1440, height: 1200 },
  { name: "settings-desktop", route: "settings/llm", width: 1440, height: 900 },
  { name: "dashboard-light-desktop", route: "dashboard", width: 1440, height: 1180, scheme: "light" },
  { name: "dashboard-375", route: "dashboard", width: 375, height: 1900, mobile: true },
  { name: "reports-375", route: "reports", width: 375, height: 900, mobile: true },
];

const only = process.argv[4]?.split(",");
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
};
const call = (method, params = {}) => new Promise((resolve) => {
  const n = ++id;
  pending.set(n, resolve);
  ws.send(JSON.stringify({ id: n, method, params }));
});

for (const s of shots.filter((x) => !only || only.includes(x.name))) {
  await call("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-color-scheme", value: s.scheme ?? "dark" }],
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
