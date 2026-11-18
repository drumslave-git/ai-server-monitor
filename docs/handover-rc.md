# Release candidate branch `rc/v0.4`

`m6-web-ui` + `m11-chat` (M11 chat engine, M7 Telegram) merged without conflicts (`24b6a3e`), then the chat page.
Migrations 0001-0004 contiguous. Linux gate (`asm-gate-rc`) was green on the merge: 299 passed.

**Done**: merge; `frontend/src/pages/Chat.tsx` (conversations list/new/delete, streamed answer, collapsed "Used N tools"
line, inline approval cards for write tools, stop button, errors, phone layout); `api-types.ts` regenerated.

**Not re-run after the chat page**: the Linux gate (backend unchanged since); `npm run build` passes.

**Run the demo**: `npm run build` in `frontend/`, then
`cd backend && python ../scripts/dev/demo_server.py --llm-url https://ollama.tcloud.monster/v1 --llm-model gemma4:e4b --tool-mode auto`
(port 8099; fixture collectors). Screenshots:
`node scripts/dev/screenshots.mjs docs/samples/ui http://127.0.0.1:8099 [name,name]`
(headless Chrome over DevTools; `CHAT_WAIT`/`SHOT_WAIT` env in ms; chat shots send a real question to the LLM).

**Traps**
- The Chat page follows the URL hash itself (starting a chat uses `history.replaceState`, which fires no `hashchange`);
  the first version used App's route and showed an empty thread after the answer.
- Tool results of write tools are recognised by `"action_id"`/`"status"` in the result text (preview is 400 chars).
- The collector manager now passes `transport=` to collector constructors; the demo's `DemoArr` takes `**kw`.
- Session titles are cut at ~28 characters by the backend ("... waiting for my ap").
- Chat screenshots used 4 real LLM conversations (two runs, desktop and 375 each); the first run exposed the hash bug.
