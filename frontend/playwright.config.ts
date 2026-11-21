// Layout checks in real Chromium against the built app (vite preview). The API is answered by the same
// MSW handlers the Vitest tests use (tests/mocks/handlers), wired in through tests/e2e/fixtures.ts.
// Every spec runs at three widths in both colour schemes: one Playwright project per combination.
import { defineConfig, devices } from "@playwright/test";

const WIDTHS = [
  { name: "phone", width: 390, height: 844 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "desktop", width: 1280, height: 800 },
] as const;
const SCHEMES = ["dark", "light"] as const;

const PORT = 4173;

export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: `http://localhost:${PORT}`, trace: "retain-on-failure" },
  projects: WIDTHS.flatMap(({ name, width, height }) =>
    SCHEMES.map((colorScheme) => ({
      name: `${name}-${width}-${colorScheme}`,
      use: { ...devices["Desktop Chrome"], viewport: { width, height }, colorScheme },
    })),
  ),
  webServer: {
    command: `npx vite build && npx vite preview --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
});
