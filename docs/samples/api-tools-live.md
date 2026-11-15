# Generic API tools (`api_call` + `api_endpoints`), live run

Model `gemma4:e4b` at `https://ollama.tcloud.monster/v1`; only `api_call` and `api_endpoints` are offered. The world is fake (Radarr 54 / Whisparr 3763 missing, a Sonarr queue with 3 problems, Prowlarr with one failing indexer).

Tool schemas: 2184 chars, **619 tokens** (prompt of a trivial request: 21 without tools, 640 with).

3 conversations in total (the budget for this check). The system prompt is the chat's own
without the host-tool bullet, plus one paragraph about the API tools (`API_PROMPT` in
`scripts/dev/api_tools_live.py`, proposed for `chat/prompts.py`). Reproduce with
`scripts/dev/api_tools_live.py`.

## Verdict

- **The model finds the endpoint through `api_endpoints`/the recipes, every time (3 of 3).** It
  never guessed a path: step 1 is `api_endpoints`, step 2 is `api_call` with the recipe's path,
  query and `select` copied verbatim (including the `?query` inside `path`, which the gateway
  accepts). Two tool calls, three model turns, 3.9-5.8k prompt tokens per question.
- **`select` is used sensibly** (copied from the recipe: `records[].{id: id, title: title, year: year}`
  and the queue filter `records[?trackedDownloadStatus!='ok'].{...why: statusMessages[].messages[]}`),
  so the answer holds the exact titles / the exact reasons, not counts.
- Queue answer is complete (3 of 3 problem items, with the reason), the missing-movies answer
  lists the exact 50 titles and offers the 4 left (page 2).
- **Weak spot: with Radarr *and* Whisparr configured the model asks only Radarr** (54 missing),
  even after the prompt said to check every configured movie service (run 2), so the user's
  3763 Whisparr titles were not mentioned. After these runs `api_endpoints` got an `also` field
  ("whisparr holds the same kind of data: query it too") for services of the same family.
  That change is covered by a unit test but **not re-tested live** (the 3 conversations were
  used up).
- Tool schemas: **619 tokens** for both tools together (2184 chars).

# Run 1 (prompt paragraph without the 'check every movie service' sentence)

## Q: what exact movies are we missing?

- **tool call** `api_endpoints` `{"service": "radarr"}`
  - result (ok, 2553 chars): `'{"service":"radarr","prefix":"/api/v3","recipes":[{"for":"which movies are missing / not downloaded (monitored, no file)","call":"GET /api/v3/wanted/missing?page=1&pageSize=50&monitored=true","select":"records[].{id: id, title: title, year: year}","note":"tota'`
- **tool call** `api_call` `{"path": "/api/v3/wanted/missing?page=1&pageSize=50&monitored=true", "select": "records[].{id: id, title: title, year: year}", "service": "radarr"}`
  - result (ok, 2743 chars): `'{"service":"radarr","call":"GET /api/v3/wanted/missing","status":200,"paged":{"page":1,"pageSize":50,"totalRecords":54},"note":"the server has 54 records, this response holds 50 (page 1): add query page=2 for the next ones, or all_pages=true","data":[{"id":1,"'`

**Answer** (tool mode native, 3 steps, 5765 prompt tokens):

We are missing a total of 54 movies.

The first 50 missing movies are:

*   Radarr Missing 01 (1991) through Radarr Missing 50 (2010).

There are 4 more missing movies on the next page. Would you like me to fetch the remaining 4?

## Q: what's wrong in the Sonarr queue?

- **tool call** `api_endpoints` `{"search": "queue", "service": "sonarr"}`
  - result (ok, 1176 chars): `'{"service":"sonarr","prefix":"/api/v3","recipes":[{"for":"what is stuck / failing in the download queue","call":"GET /api/v3/queue?page=1&pageSize=50&includeUnknownSeriesItems=true","select":"records[?trackedDownloadStatus!=\'ok\'].{title: title, status: status,'`
- **tool call** `api_call` `{"path": "/api/v3/queue?page=1&pageSize=50&includeUnknownSeriesItems=true", "select": "records[?trackedDownloadStatus!='ok'].{title: title, status: status, state: trackedDownloadState, why: statusMessages[].messages[]}", "service": "sonarr"}`
  - result (ok, 462 chars): `'{"service":"sonarr","call":"GET /api/v3/queue","status":200,"paged":{"page":1,"pageSize":50,"totalRecords":4},"data":[{"title":"Severance.S02E03.1080p.WEB","status":"completed","state":"importBlocked","why":["No files found are eligible for import"]},{"title":'`

**Answer** (tool mode native, 3 steps, 3940 prompt tokens):

There are a few issues in your Sonarr queue:

1.  **Severance.S02E03.1080p.WEB**: This item is marked as `importBlocked` because "No files found are eligible for import."
2.  **Andor.S01E05.2160p**: This download is in a `warning` state and is currently "stalled."
3.  **Foundation.S02E02.1080p**: This download has `failed` and is in a `failedPending` state because the "Download failed."

# Run 2 (after adding the sentence about Radarr/Whisparr)

## Q: what exact movies are we missing?

- **tool call** `api_endpoints` `{"service": "radarr"}`
  - result (ok, 2553 chars): `'{"service":"radarr","prefix":"/api/v3","recipes":[{"for":"which movies are missing / not downloaded (monitored, no file)","call":"GET /api/v3/wanted/missing?page=1&pageSize=50&monitored=true","select":"records[].{id: id, title: title, year: year}","note":"tota'`
- **tool call** `api_call` `{"path": "/api/v3/wanted/missing?page=1&pageSize=50&monitored=true", "select": "records[].{id: id, title: title, year: year}", "service": "radarr"}`
  - result (ok, 2743 chars): `'{"service":"radarr","call":"GET /api/v3/wanted/missing","status":200,"paged":{"page":1,"pageSize":50,"totalRecords":54},"note":"the server has 54 records, this response holds 50 (page 1): add query page=2 for the next ones, or all_pages=true","data":[{"id":1,"'`

**Answer** (tool mode native, 3 steps, 5825 prompt tokens):

We are missing a total of 54 movies.

Here are the first 50 missing movies found on page 1:

*   Radarr Missing 01 (1991)
*   Radarr Missing 02 (1992)
*   Radarr Missing 03 (1993)
*   … (titles 04–47 elided in this file; the model listed all 50 correctly)
*   Radarr Missing 48 (2008)
*   Radarr Missing 49 (2009)
*   Radarr Missing 50 (2010)

There are 4 more records on the next page. Would you like me to fetch the remaining results?

## What the fake services saw (run 1 + run 2)

- `GET radarr.test/api/v3/wanted/missing` page=1 pageSize=50 monitored=true (both runs)
- `GET sonarr.test/api/v3/queue` page=1 pageSize=50 includeUnknownSeriesItems=true

# Run 3: the wired app (`/api/chat` engine, all 20 tools, real system prompt), Radarr AND Whisparr configured

Model `gemma4:e4b` at `https://ollama.tcloud.monster/v1`; the real engine with all tools and the real system prompt. The world is fake: Radarr 54 and Whisparr 3763 missing movies, a Sonarr queue with 3 problems, Prowlarr with one failing indexer.

Tool schemas: 20 tools = **2689 tokens**; `api_call` + `api_endpoints` alone = 619 tokens.

## Q: what exact movies are we missing?

- **tool call** `api_endpoints` `{"search": "missing", "service": "radarr"}`
  - result (ok, 617 chars): `'{"service":"radarr","prefix":"/api/v3","recipes":[{"for":"which movies are missing / not downloaded (monitored, no file)","call":"GET /api/v3/wanted/missing?page=1&pageSize=50&monitored=true","select":"records[].{id: id, title: title, year: year}","note":"tota'`
- **tool call** `api_call` `{"select": "records[].{id: id, title: title, year: year}", "service": "radarr", "path": "/api/v3/wanted/missing?page=1&pageSize=50&monitored=true"}`
  - result (ok, 2743 chars): `'{"service":"radarr","call":"GET /api/v3/wanted/missing","status":200,"paged":{"page":1,"pageSize":50,"totalRecords":54},"note":"the server has 54 records, this response holds 50 (page 1): add query page=2 for the next ones, or all_pages=true","data":[{"id":1,"'`

**Answer** (tool mode native, 3 steps, 11379 prompt tokens):

We are missing a total of 54 movies in Radarr.

Here are the first 50 missing movies:

*   Radarr Missing 01 (1991)
*   Radarr Missing 02 (1992)
*   Radarr Missing 03 (1993)
*   … (titles 04–47 elided in this file; the model listed all 50)
*   Radarr Missing 48 (2008)
*   Radarr Missing 49 (2009)
*   Radarr Missing 50 (2010)

There are 4 more records on the next page. Would you like me to fetch the remaining missing movies?

## Q: remove movie 12 from Radarr

- **tool call** `api_endpoints` `{"search": "delete", "service": "radarr"}`
  - result (ok, 1047 chars): `'{"service":"radarr","prefix":"/api/v3","recipes":[],"endpoints":["DELETE /api/v3/autotagging/{id} - delete autotagging","DELETE /api/v3/blocklist/{id} - delete blocklist","DELETE /api/v3/command/{id} - delete command","DELETE /api/v3/customfilter/{id} - delete'`
- **tool call** `api_call` `{"method": "DELETE", "path": "/api/v3/movie/12", "service": "radarr"}`
  - result (ok, 256 chars): `'{"action_id":1,"status":"pending","policy":"approve","target":"radarr: DELETE /api/v3/movie/12","result":null,"note":"Waiting for the owner\'s approval (approval id 1). Tell the user to approve or reject it in the approvals list; it has NOT been done yet."}'`

**Answer** (tool mode native, 3 steps, 10559 prompt tokens):

The request to remove movie 12 from Radarr is pending and requires your approval.

The action has **not** been executed yet. Please approve or reject it in the pending approvals list.

## Pending approvals left by the chat

- #1 `api_call` radarr: DELETE /api/v3/movie/12 (source chat:418e786130a54c29860ca063923005c1)


Verdict of run 3 (this is the check after wiring; 2 conversations):

- **The write request works end to end**: `DELETE /api/v3/movie/12` became a pending approval
  (`api_call`, target `radarr: DELETE /api/v3/movie/12`), nothing reached the fake Radarr.
- **The movies question still stopped at Radarr** (54 missing; the 3763 Whisparr titles were not
  asked for), although the real prompt mentions the API tools and `api_endpoints` carried the
  `also` hint. The hint is now also put into every `api_call` result next to the data, where
  the model reads it right before answering; run 4 below tried it.
- With all tools offered the model still goes straight to `api_endpoints` then `api_call`;
  all 20 tool schemas cost 2689 tokens, the two API tools 619 of them.

# Run 4: after putting the `also` hint into every `api_call` result (1 extra conversation)

## Q: what exact movies are we missing?

- **tool call** `api_call` `{"path": "/api/v3/wanted/missing", "select": "records[].{title: title, id: id, series: series}", "service": "radarr"}`
  - result (ok, 845 chars): `'{"service":"radarr","call":"GET /api/v3/wanted/missing","status":200,"paged":{"page":1,"pageSize":10,"totalRecords":54},"note":"the server has 54 records, this response holds 10 (page 1): add query page=2 for the next ones, or all_pages=true","data":[{"title":'`
- **tool call** `api_call` `{"path": "/api/v3/wanted/missing", "select": "records[].{title: title, id: id, series: series}", "service": "whisparr"}`
  - result (ok, 869 chars): `'{"service":"whisparr","call":"GET /api/v3/wanted/missing","status":200,"paged":{"page":1,"pageSize":10,"totalRecords":3763},"note":"the server has 3763 records, this response holds 10 (page 1): add query page=2 for the next ones, or all_pages=true","data":[{"t'`

**Answer** (tool mode native, 3 steps, 10988 prompt tokens):

Based on the initial pages of data, here are the missing items reported by Radarr and Whisparr. Please note that the titles returned by the API are generic placeholders.

**Radarr (54 total missing):**
1. Radarr Missing 01 (ID: 1)
2. Radarr Missing 02 (ID: 2)
3. Radarr Missing 03 (ID: 3)
4. Radarr Missing 04 (ID: 4)
5. Radarr Missing 05 (ID: 5)
6. Radarr Missing 06 (ID: 6)
7. Radarr Missing 07 (ID: 7)
8. Radarr Missing 08 (ID: 8)
9. Radarr Missing 09 (ID: 9)
10. Radarr Missing 10 (ID: 10)

**Whisparr (3763 total missing):**
1. Whisparr Title 0001 (ID: 1)
2. Whisparr Title 0002 (ID: 2)
3. Whisparr Title 0003 (ID: 3)
4. Whisparr Title 0004 (ID: 4)
5. Whisparr Title 0005 (ID: 5)
6. Whisparr Title 0006 (ID: 6)
7. Whisparr Title 0007 (ID: 7)
8. Whisparr Title 0008 (ID: 8)
9. Whisparr Title 0009 (ID: 9)
10. Whisparr Title 0010 (ID: 10)

Both services indicate a large number of missing items, and I can retrieve more pages if you would like me to continue fetching the full lists.

## Pending approvals left by the chat



Verdict of run 4: **the answer now covers both** services (Radarr 54 total, Whisparr 3763 total,
first 10 of each). The model called `api_call` for `radarr` and then, because of the hint in
that result, for `whisparr`. It skipped `api_endpoints` this time and guessed the path without
`pageSize`, so only 10 titles per service came back (the note said how to get the rest).
A deterministic fan-out over service groups was not needed and not built.

## What the fake services saw (run 4)

- `GET radarr.test/api/v3/wanted/missing`
- `GET whisparr.test/api/v3/wanted/missing`
