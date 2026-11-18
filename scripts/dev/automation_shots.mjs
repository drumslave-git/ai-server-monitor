// Screenshots of the global automation switch: the toggle and the paused banner (Dashboard), at 1280 and 375 px.
//   node scripts/dev/automation_shots.mjs <out-dir> <base-url>
// Drives a local server through its own API (pauses it for the paused shots, resumes at the end).
// Needs Chrome at the usual Windows path or CHROME_PATH. Development only.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const out = process.argv[2] ?? "docs/samples/ui";
const base = process.argv[3] ?? "http://127.0.0.1:8080";
const chrome = process.env.CHROME_PATH ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";
mkdirSync(out, { recursive: true });

const all = [
  { name: "automation-paused-dashboard-1280", route: "dashboard", width: 1280, height: 900, paused: true },
  { name: "automation-paused-dashboard-375", route: "dashboard", width: 375, height: 1300, mobile: true, paused: true },
];
const shots = process.env.ONLY ? all.filter((s) => process.env.ONLY.split(",").includes(s.name)) : all;

const port = 9334;
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
const post = (path) => fetch(`${base}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });

try {
  await post("/api/automation/resume");
  let paused = false;
  for (const s of shots) {
    if (s.paused !== paused) {
      await post(`/api/automation/${s.paused ? "pause" : "resume"}`);
      paused = s.paused;
    }
    await call("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
    await call("Emulation.setDeviceMetricsOverride", {
      width: s.width, height: s.height, deviceScaleFactor: s.mobile ? 2 : 1, mobile: !!s.mobile,
    });
    await call("Page.navigate", { url: `${base}/?shot=${s.name}#/${s.route}` });
    await sleep(Number(process.env.SHOT_WAIT ?? 3500));
    const { data } = await call("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(out, `${s.name}.png`), Buffer.from(data, "base64"));
    console.log("saved", s.name);
  }
} finally {
  await post("/api/automation/resume");
  ws.close();
  proc.kill();
}
