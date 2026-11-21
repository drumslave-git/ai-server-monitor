// The Playwright `test` of the layout checks: every page starts with the shared MSW handlers answering
// the API (the same ones the Vitest tests use), so no spec talks to a real server.
import { defineNetworkFixture, type NetworkFixture } from "@msw/playwright";
import { test as base } from "@playwright/test";
import type { AnyHandler } from "msw";
import { handlers } from "../mocks/handlers";

export const test = base.extend<{ handlers: AnyHandler[]; network: NetworkFixture }>({
  handlers: [handlers, { option: true }],
  network: [
    async ({ context, handlers }, use) => {
      const network = defineNetworkFixture({ context, handlers });
      await network.enable();
      await use(network);
      await network.disable();
    },
    { auto: true },
  ],
});

export { expect } from "@playwright/test";
