# *arr / qBittorrent API: what the clients rely on

Checked on 2026-11-14 against the apps' published `openapi.json` (Radarr `develop`, Sonarr
`develop`, Prowlarr `develop`, Whisparr `eros` = v3), the apps' source where the spec is silent,
and the qBittorrent WebUI API wiki (4.1 and 5.0 pages). The test fakes (`tests/arrfakes.py`)
reject query parameters these apps do not know, so a client drifting from this list fails tests.

## Radarr / Sonarr / Whisparr (`/api/v3`, header `X-Api-Key`)

| Use | Endpoint | Notes |
|---|---|---|
| status, health | `GET /system/status` (`version`), `GET /health` (`source`, `type`, `message`) | |
| queue | `GET /queue?page&pageSize&sortKey&sortDirection` + `includeUnknownMovieItems` (Radarr, Whisparr) / `includeUnknownSeriesItems` (Sonarr) | paged: `totalRecords`, `records`. Item: `id`, `downloadId` (torrent hash), `status`, `trackedDownloadStatus` (ok/warning/error), `trackedDownloadState` (downloading, importBlocked, importPending, importing, imported, failedPending, failed, ignored), `statusMessages[{title, messages[]}]`, `movieId` / `episodeId`+`seriesId` |
| remove from queue | `DELETE /queue/{id}?removeFromClient&blocklist&skipRedownload` | also `changeCategory` (unused). Defaults: removeFromClient true, blocklist false, skipRedownload false |
| wanted | `GET /wanted/missing`, `GET /wanted/cutoff` with `page`, `pageSize`, `monitored`, `sortKey`, `sortDirection` (+ `includeSeries` on Sonarr) | Radarr/Whisparr return movies, Sonarr episodes. Sort keys for "least recently searched first": `movies.lastSearchTime` / `episodes.lastSearchTime` |
| search | `POST /command` `{"name":"MoviesSearch","movieIds":[…]}` (Radarr **and Whisparr**) / `{"name":"EpisodeSearch","episodeIds":[…]}` (Sonarr) | not in openapi (`CommandResource` is generic); confirmed from `MoviesSearchCommand.cs` / `EpisodeSearchCommand.cs` |
| lookup / add | `GET /movie/lookup?term=tmdb:ID` (Radarr, Whisparr), `GET /series/lookup?term=tvdb:ID` (Sonarr); `GET /qualityprofile`, `GET /rootfolder`; `POST /movie` / `POST /series` with the lookup result plus `qualityProfileId`, `rootFolderPath`, `monitored`, `addOptions` | add options: movies `searchForMovie`, series `searchForMissingEpisodes` |

Corrections made after this check (they were written from memory first):
- Whisparr v3 is Radarr-based (movies, `movieId`); the search command is `MoviesSearch`, not `EpisodeSearch`.
- The queue "include unknown items" parameter is `includeUnknownSeriesItems` on Sonarr.
- `importFailed` is not a `trackedDownloadState`; `downloadClientUnavailable` is a queue `status` worth flagging.
- Whisparr's `openapi.json` omits `/wanted/*`, but its source has them (`MissingController`, `CutoffController`, `MovieResource`). A 404 is still tolerated and reported as "unknown total".

## Prowlarr (`/api/v1`, header `X-Api-Key`)

`GET /system/status`, `GET /health`, `GET /indexer` (`id`, `name`, `enable`, `protocol`),
`GET /indexerstatus` (`indexerId`, `disabledTill`, `initialFailure`, `mostRecentFailure`),
`GET /indexer/{id}` then `POST /indexer/test` with that resource as body (there is no
`/indexer/{id}/test`).

## qBittorrent WebUI API v2

- `POST /auth/login` (form `username`, `password`): body `Ok.` on success, `Fails.` otherwise (both HTTP 200); 403 = IP banned. Cookie `SID`. A `Referer`/`Origin` equal to the WebUI origin is required.
- `GET /torrents/info` (`hashes` separated by `|`, `filter`, `category`, …): `hash`, `name`, `state`, `progress`, `num_seeds`, `num_complete`, `num_leechs`, `last_activity`, `added_on`, `availability`, `dlspeed`, `size`, `category`.
- `POST /torrents/delete` (form `hashes`, `deleteFiles`), `GET /transfer/info` (`dl_info_speed`, `up_info_speed`, `connection_status`), `GET /app/version`.
- Known states used: `error`, `missingFiles`, `metaDL`, `stalledDL`, `downloading`, `forcedDL`, `forcedMetaDL`. 5.x renames `pausedDL`/`pausedUP` to `stoppedDL`/`stoppedUP`; neither is treated as an active download, so nothing changes.
