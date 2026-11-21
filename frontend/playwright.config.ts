// Layout checks in real Chromium against the built app (vite preview). The API is answered by the same
// MSW handlers the Vitest tests use (tests/mocks/handlers), wired in through tests/e2e/fixtures.ts.
// Every spec runs at three widths in both colour schemes: one Playwright project per combination. A spec that sets its
// own window width (*.fixed.spec.ts) would only repeat itself there: it runs in two projects of its own, one per scheme.
import { defineConfig, devices } from "@playwright/test";

const WIDTHS = [
  { name: "phone", width: 390, height: 844 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "desktop", width: 1280, height: 800 },
] as const;
const SCHEMES = ["dark", "light"] as const;

const PORT = 4173;
const FIXED = /\.fixed\.spec\.ts$/;

export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: `http://localhost:${PORT}`, trace: "retain-on-failure" },
  projects: [
    ...WIDTHS.flatMap(({ name, width, height }) =>
      SCHEMES.map((colorScheme) => ({
        name: `${name}-${width}-${colorScheme}`,
        testIgnore: FIXED,
        use: { ...devices["Desktop Chrome"], viewport: { width, height }, colorScheme },
      })),
    ),
    ...SCHEMES.map((colorScheme) => ({
      name: `fixed-width-${colorScheme}`,
      testMatch: FIXED,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 }, colorScheme },
    })),
  ],
  webServer: {
    command: `npx vite build && npx vite preview --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
});
