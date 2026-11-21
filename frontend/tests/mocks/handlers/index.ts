// One set of handlers for the whole API, shared by the Vitest tests and the Playwright layout checks.
// Each area of the app has its own file; a test that needs another answer uses `server.use(...)`
// (Vitest) or `network.use(...)` (Playwright).
import { approvalsHandlers } from "./approvals";
import { chatHandlers } from "./chat";
import { dashboardHandlers } from "./dashboard";
import { jobsHandlers } from "./jobs";
import { logsHandlers } from "./logs";
import { mcpHandlers } from "./mcp";
import { settingsHandlers } from "./settings";
import { shellHandlers } from "./shell";

export const handlers = [
  ...shellHandlers,
  ...approvalsHandlers,
  ...chatHandlers,
  ...dashboardHandlers,
  ...jobsHandlers,
  ...logsHandlers,
  ...mcpHandlers,
  ...settingsHandlers,
];
