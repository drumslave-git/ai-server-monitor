# M6 web UI (branch `m6-web-ui`, from `integration/m3-m10`)

**Done**: shell (sidebar, bottom tab bar on phones), Approvals + audit log, Chat placeholder, Settings unchanged. Screenshots in `docs/samples/ui/`.

**Files**: `frontend/src/{App.tsx,live.ts,fmt.ts,ui.tsx,styles.css}`, `pages/Approvals.tsx`.
`live.ts` holds the single shared EventSource; pages subscribe with `useStream`.

**Traps**
- `src/api-types.ts` is generated (`npm run gen:api`).
- The Settings page rewrites `location.hash` itself (`#/settings/<section>`); the router only reacts to `hashchange`.
- Chat is a placeholder route (`#/chat`); replace `ChatPlaceholder` in `App.tsx` after M11 lands.

**Left**: nothing known broken; no light-theme screenshots were taken (the stylesheet has both themes).
