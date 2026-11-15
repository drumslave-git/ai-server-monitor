# MCP live proof (2026-11-15)

Real chat engine, real LLM (`https://ollama.tcloud.monster/v1`, `gemma4:e4b`, native tool calls),
3 conversations, against a **local demo MCP server** (`scripts/dev/mcp_demo.py`: "notes" with a
read-only `list_notes`, a write `add_note` and a write `delete_note` that is not chosen). Nothing
else is touched; the approval at the end is the UI button done by the script.

Setup: server `notes`, Streamable HTTP (`transport: auto`), policy `approve` (default),
`tool_filter = [list_notes, add_note]`. Run with:

```
cd backend && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe ../scripts/dev/mcp_demo.py   --llm-url https://ollama.tcloud.monster/v1 --model gemma4:e4b   --ask "What notes do I have about the backup?"   --ask "Please add a note: renew the SSL certificate on Friday"   --ask "List the notes that mention the domain" --generic-pair-for 1
```

What to see: (1) a read tool answered directly; (2) a write tool ends as a *pending approval*, the
server has not been called; approving it runs it on the server; (3) with
`max_inline_tool_tokens = 0` the 4B model manages with only `mcp_list_tools` + `mcp_call`.
Notes: the event label of `mcp_call` says `write` because the generic tool *may* write (the call
itself was read-only and ran at once); the model's wording "I have added" in (2) is its own, the
tool result and the second half of its answer say it is pending.

---

### Q: What notes do I have about the backup?

MCP tools offered: notes__add_note, notes__list_notes (≈151 tokens as single tools)

- **tool call** `notes__list_notes` {"contains": "backup"} (read)
  - result [ok, 177 chars]: `{"text":"{\n  \"id\": 1,\n  \"text\": \"Server backup runs every night at 03:00 to the NAS\"\n}\n{\n  \"id\": 3,\n  \"text\": \"Replace the failing disk in the backup box\"\n}"}`

**Answer** (2 steps, mode native):

> You have two notes mentioning "backup":

1.  "Server backup runs every night at 03:00 to the NAS"
2.  "Replace the failing disk in the backup box"

### Q: Please add a note: renew the SSL certificate on Friday

MCP tools offered: notes__add_note, notes__list_notes (≈151 tokens as single tools)

- **tool call** `notes__add_note` {"text": "renew the SSL certificate on Friday"} (write)
  - result [ok, 240 chars]: `{"action_id":1,"status":"pending","policy":"approve","target":"notes · add_note","result":null,"note":"Waiting for the user's approval (approval id 1). Tell the user to approve or reject it in the approvals list; it has NOT been done yet."}`

**Answer** (2 steps, mode native):

> I have added the note "renew the SSL certificate on Friday," but it is currently pending your approval. Please check the approvals list to approve or reject this action.

---
*(from here: `max_inline_tool_tokens = 0`, the chat gets only the generic pair)*

### Q: List the notes that mention the domain

MCP tools offered: mcp_call, mcp_list_tools (≈151 tokens as single tools)

- **tool call** `mcp_list_tools` {"server": "notes"} (read)
  - result [ok, 211 chars]: `{"server":"notes","tools":[{"name":"list_notes","kind":"read","description":"List the saved notes, optionally only those containing a word."},{"name":"add_note","kind":"write","description":"Save a new note."}]}`
- **tool call** `mcp_call` {"arguments": {"word": "domain"}, "server": "notes", "tool": "list_notes"} (write)
  - result [ok, 255 chars]: `{"text":"{\n  \"id\": 1,\n  \"text\": \"Server backup runs every night at 03:00 to the NAS\"\n}\n{\n  \"id\": 2,\n  \"text\": \"Renew the tcloud.monster domain in March\"\n}\n{\n  \"id\": 3,\n  \"text\": \"Replace the failing disk in the backup box\"\n}"}`

**Answer** (3 steps, mode native):

> The notes that mention "domain" are:

*   **ID 2:** "Renew the tcloud.monster domain in March"

### Approvals pending after the conversations

- #1 `mcp_call` → notes · add_note · policy approve · source chat:74899ba19f0e4caba26f5a1065aa39ea · args {"title": "", "server": "notes", "tool": "add_note", "arguments": {"text": "renew the SSL certificate on Friday"}}

Notes on the server before approving: 3

Approved #1 → status **executed**, result {"server": "notes", "tool": "add_note", "result": {"text": "saved note 4"}}
Notes on the server after: 4 → {"id": 4, "text": "renew the SSL certificate on Friday"}

Audit log of MCP calls (kind=mcp, newest first): [["add_note", true], ["list_notes", true], ["list_notes", true]]
