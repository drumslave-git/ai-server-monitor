// The page asks `GET /api/mcp/tools` and shows nothing of the answer until its own `then` has run. A test that clicks
// before that either finds the field empty or has its click overwritten by the late answer. These helpers wait for the
// page's own request (not a timer, no retry): the spy calls through, and the answer's promise is awaited inside act().
import { act, waitFor } from "@testing-library/react";
import { expect, vi, type MockInstance } from "vitest";
import { api } from "../../src/api";

export type GetSpy = MockInstance<typeof api.get>;

/** Watches `api.get` (calls through). Install it in beforeEach, and restore it in afterEach. */
export const watchGets = (): GetSpy => vi.spyOn(api, "get");

/** Waits until the page has sent its `n`-th request for the tools and the answer has been handled. */
export async function toolsAnswered(spy: GetSpy, n = 1) {
  const requests = () => spy.mock.results.filter((_, i) => String(spy.mock.calls[i][0]) === "/api/mcp/tools");
  await waitFor(() => expect(requests().length).toBeGreaterThanOrEqual(n));
  await act(async () => {
    await Promise.allSettled([requests()[n - 1].value]);
  });
}
