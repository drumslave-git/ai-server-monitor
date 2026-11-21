// What every page of the app shows around itself (rail: version, automation switch, hardware meters;
// the pending-approvals badge). All values are invented.
import type { ApiResponse } from "../types";

export const info: ApiResponse<"/api/info"> = { version: "9.9.9", commit: "abc1234" };

export const automationRunning = { paused: false, since: null, by: null, queued_actions: 0 };

export const hardware: ApiResponse<"/api/hardware"> = {
  cpu_percent: 12.4,
  ram_total: 32 * 1024 ** 3,
  ram_used: 8 * 1024 ** 3,
  gpus: [{ name: "Test GPU 1000", util_percent: 40, vram_total: 12 * 1024 ** 3, vram_used: 3 * 1024 ** 3 }],
};

export const status: ApiResponse<"/api/status"> = {
  automation: { paused: false },
  jobs: [],
  llm_configured: true,
  main_model: "test-main",
  worker_model: "test-worker",
  now: 1_800_000_000,
  pending_approvals: 0,
  uptime_s: 7200,
  version: "9.9.9",
};

/** The settings document as far as the shell reads it (the text size). Settings tests bring their own. */
export const settingsShell = { values: { general: { root_font_size: 16 } }, schema: {}, sections: {} };
