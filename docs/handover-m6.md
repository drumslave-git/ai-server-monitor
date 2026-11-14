# M6 web UI (branch `m6-web-ui`, from `integration/m3-m10`)

**Done**: shell (sidebar, bottom tab bar on phones), Dashboard, Reports (live tokens, history, detail),
Approvals + audit log, *arr, Chat placeholder, Settings unchanged. Screenshots in `docs/samples/ui/`.

**Backend touched**: one small read endpoint, `GET /api/metrics/recent` (last 120 `metrics.tick` per source,
kept by `LiveFeed`), so a freshly opened dashboard draws its sparklines immediately.

**Demo/dev**: `cd backend && python ../scripts/dev/demo_server.py [--llm-url URL --llm-model NAME]` replays
`docs/samples/collectors-live.json` and runs the *arr collector and action layer against the in-memory fake *arr
world (one pending approval seeded). Needs `npm run build` first (serves `frontend/dist`). Never talks to a real
*arr/qBit or the production instance.

**Files**: `frontend/src/{App.tsx,live.ts,fmt.ts,ui.tsx,styles.css}`, `pages/{Dashboard,Reports,Approvals,Arr}.tsx`.
`live.ts` holds the single shared EventSource and the tick history; pages subscribe with `useStream`.

**Traps**
- `src/api-types.ts` is generated (`npm run gen:api`); the collector `data` payloads are not typed, pages read them
  defensively through `Record<string, any>`.
- The Settings page rewrites `location.hash` itself (`#/settings/<section>`); the router only reacts to `hashchange`.
- Chat is a placeholder route (`#/chat`); replace `ChatPlaceholder` in `App.tsx` after M11 lands.
- Sparklines show only what arrived since the backend started (ticks every 15 s, 120 kept).

**Left**: nothing known broken; no light-theme screenshots were taken (the stylesheet has both themes).
