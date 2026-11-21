import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "happy-dom",
    include: ["tests/unit/**/*.test.{ts,tsx}"],
    setupFiles: ["tests/setup.ts"],
    coverage: {
      provider: "v8",
      // Only the app source counts. Playwright runs do not add to this number.
      include: ["src/**/*.{ts,tsx}"],
      // api-types.ts is generated from the backend's OpenAPI document (npm run gen:api), not written code.
      exclude: ["src/api-types.ts"],
      reporter: ["text-summary", "text"],
      // the numbers are worth seeing when a test fails too
      reportOnFailure: true,
    },
  },
});
