// Helpers the polish specs share.
import { expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import { makeSettingsDoc } from "../mocks/fixtures/settings";
const noSideways = (page: Page) =>
  page.evaluate(() => ({
    window: window.innerWidth,
    root: document.documentElement.scrollWidth,
    inner: [...document.querySelectorAll<HTMLElement>(".fd-main, .fd-pane-scroll")].map((el) => el.scrollWidth - el.clientWidth),
  }));

export async function expectNoSidewaysScroll(page: Page) {
  const over = await noSideways(page);
  expect(over.root, "the page scrolls sideways").toBeLessThanOrEqual(over.window);
  for (const extra of over.inner) expect(extra, "a pane scrolls sideways").toBeLessThanOrEqual(1);
}

/** The settings document with `llm.extra_body` as a pairs field holding alpha and beta. */
export const withPairs = () =>
  makeSettingsDoc((doc) => {
    const llm = (doc.schema.$defs as Record<string, { properties: Record<string, unknown> }>).LLMSettings;
    llm.properties.extra_body = { type: "object", additionalProperties: { type: "string" }, description: "Extra fields." };
    (doc.values.llm as Record<string, unknown>).extra_body = { alpha: "1", beta: "2" };
  });

export const LONG_DETAIL = `the server answered with a very long explanation that does not fit one line of the table at any width this suite uses ${"and goes on and on, ".repeat(12)}end`;
