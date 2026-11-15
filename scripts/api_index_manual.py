"""Hand-written parts of the chat's API endpoint index (input of `gen_api_index.py`).

- RECIPES: ready-made calls for the questions people actually ask. A small local model copies
  an example far more reliably than it composes a call from a list of endpoints.
- HINTS: one-line meaning of the *arr endpoints that matter (the OpenAPI specs have no
  summaries, only tags). The words in a hint are also what `api_endpoints` searches.
- QBIT / DOCKER / PROMETHEUS: indexes written by hand (qBittorrent WebUI API v2, the GET
  subset of the Docker Engine API, the Prometheus HTTP API), as there is no usable spec.

Entries are (method, path, tag, summary, params). `params` are the query/form fields worth
knowing, `*` marks a required one.
"""

from __future__ import annotations

from typing import Any

Entry = tuple[str, str, str, str, str]
Recipe = dict[str, Any]

# ----------------------------------------------------------------------------- *arr hints
# Keyed by (method, path relative to /api/v3 or /api/v1). `{id}` style as in the spec.
ARR_HINTS: dict[tuple[str, str], str] = {
    ("GET", "/wanted/missing"): "missing: monitored items without a file (paged: records, "
    "totalRecords)",
    ("GET", "/wanted/cutoff"): "cutoff unmet: items with a file below the wanted quality (paged)",
    ("GET", "/queue"): "download queue: stuck, failed, warning, importBlocked downloads "
    "(paged: records, totalRecords)",
    ("GET", "/queue/status"): "queue counts: total, errors, warnings",
    ("GET", "/queue/details"): "queue items without paging",
    ("DELETE", "/queue/{id}"): "remove a download from the queue (removeFromClient, blocklist)",
    ("GET", "/history"): "history: grabbed, imported, failed downloads (paged)",
    ("GET", "/history/since"): "history since a date",
    ("GET", "/blocklist"): "blocklisted releases (paged)",
    ("GET", "/health"): "health warnings and errors of the app (source, type, message)",
    ("GET", "/system/status"): "app version, OS, paths, uptime",
    ("GET", "/system/task"): "scheduled tasks: last/next run",
    ("GET", "/diskspace"): "free and total space of the disks used by the app",
    ("GET", "/rootfolder"): "root folders where media is stored (path, freeSpace)",
    ("GET", "/qualityprofile"): "quality profiles (id, name)",
    ("GET", "/downloadclient"): "configured download clients",
    ("GET", "/indexer"): "configured indexers",
    ("GET", "/log"): "application log entries (paged)",
    ("GET", "/calendar"): "upcoming releases between start and end dates",
    ("GET", "/command"): "running and recent commands (searches, refreshes)",
    ("POST", "/command"): "run a command, e.g. a search; body {name, ...ids}",
    ("GET", "/movie"): "the whole movie library (large: use select; tmdbId filters)",
    ("GET", "/movie/{id}"): "one movie by id",
    ("GET", "/movie/lookup"): "search TMDB for a movie to add (term=tmdb:ID or title)",
    ("POST", "/movie"): "add a movie to the library",
    ("PUT", "/movie/{id}"): "change a movie (e.g. monitored)",
    ("DELETE", "/movie/{id}"): "delete a movie (deleteFiles, addImportExclusion)",
    ("GET", "/moviefile"): "movie files on disk (movieId filter)",
    ("GET", "/series"): "the whole series library (large: use select)",
    ("GET", "/series/{id}"): "one series by id",
    ("GET", "/series/lookup"): "search TVDB for a series to add (term=tvdb:ID or title)",
    ("POST", "/series"): "add a series to the library",
    ("PUT", "/series/{id}"): "change a series (e.g. monitored)",
    ("DELETE", "/series/{id}"): "delete a series (deleteFiles)",
    ("GET", "/episode"): "episodes of a series (seriesId required)",
    ("GET", "/episodefile"): "episode files on disk (seriesId required)",
    ("GET", "/release"): "interactive search: asks every indexer for releases (needs approval)",
    ("POST", "/release"): "grab a release found by the interactive search",
    ("GET", "/indexerstatus"): "failing indexers: indexerId, disabledTill, mostRecentFailure",
    ("GET", "/indexerstats"): "per-indexer query/grab/failure counts",
    ("GET", "/search"): "search all indexers for releases (needs approval)",
    ("GET", "/applications"): "apps (Radarr/Sonarr...) that Prowlarr syncs indexers to",
    ("GET", "/downloadclient/{id}"): "one download client",
    ("GET", "/tag"): "tags",
}

ARR_WRITE_NOTE = (
    "Anything but GET (and GET /release, GET /search, which query the indexers) is not run "
    "directly: it becomes a request the owner must approve."
)

_M = "movies"  # label used in the movie-like recipes

# ----------------------------------------------------------------------------- recipes


def _movie_recipes(kind: str) -> list[Recipe]:
    unknown = "includeUnknownMovieItems"
    return [
        {
            "for": f"which {_M} are missing / not downloaded (monitored, no file)",
            "call": "GET /api/v3/wanted/missing?page=1&pageSize=50&monitored=true",
            "select": "records[].{id: id, title: title, year: year}",
            "note": "totalRecords is the full count; next page: page=2. For a count only use "
            "pageSize=1 and select totalRecords.",
        },
        {
            "for": "what is stuck / failing in the download queue",
            "call": f"GET /api/v3/queue?page=1&pageSize=50&{unknown}=true",
            "select": "records[?trackedDownloadStatus!='ok'].{title: title, status: status, "
            "state: trackedDownloadState, why: statusMessages[].messages[]}",
            "note": "empty result = nothing wrong. pageSize=50 covers most queues.",
        },
        {
            "for": "health warnings of the app",
            "call": "GET /api/v3/health",
            "select": "[].{source: source, type: type, message: message}",
        },
        {
            "for": "find a movie in the library by title (case-sensitive match)",
            "call": "GET /api/v3/movie",
            "select": "[?contains(title, 'Matrix')].{id: id, title: title, year: year, "
            "hasFile: hasFile, monitored: monitored}",
        },
        {
            "for": "library movies that have no file yet",
            "call": "GET /api/v3/movie",
            "select": "[?monitored && !hasFile].{id: id, title: title, year: year}",
        },
        {
            "for": "recent downloads, imports and failures",
            "call": "GET /api/v3/history?page=1&pageSize=20&sortKey=date&sortDirection=descending",
            "select": "records[].{title: sourceTitle, event: eventType, date: date}",
        },
        {
            "for": "free disk space",
            "call": "GET /api/v3/diskspace",
            "select": "[].{path: path, free: freeSpace, total: totalSpace}",
        },
        {
            "for": "search for a movie right now (needs owner approval)",
            "call": "POST /api/v3/command",
            "body": {"name": "MoviesSearch", "movieIds": [123]},
        },
    ]


def _sonarr_recipes() -> list[Recipe]:
    return [
        {
            "for": "which episodes are missing / not downloaded (monitored, no file)",
            "call": "GET /api/v3/wanted/missing?page=1&pageSize=50&monitored=true"
            "&includeSeries=true",
            "select": "records[].{series: series.title, season: seasonNumber, "
            "episode: episodeNumber, title: title, aired: airDate}",
            "note": "totalRecords is the full count; next page: page=2.",
        },
        {
            "for": "what is stuck / failing in the download queue",
            "call": "GET /api/v3/queue?page=1&pageSize=50&includeUnknownSeriesItems=true",
            "select": "records[?trackedDownloadStatus!='ok'].{title: title, status: status, "
            "state: trackedDownloadState, why: statusMessages[].messages[]}",
            "note": "empty result = nothing wrong.",
        },
        {
            "for": "health warnings of the app",
            "call": "GET /api/v3/health",
            "select": "[].{source: source, type: type, message: message}",
        },
        {
            "for": "find a series in the library by title (case-sensitive match)",
            "call": "GET /api/v3/series",
            "select": "[?contains(title, 'Office')].{id: id, title: title, "
            "have: statistics.episodeFileCount, total: statistics.episodeCount}",
        },
        {
            "for": "episodes of one series",
            "call": "GET /api/v3/episode?seriesId=1",
            "select": "[].{season: seasonNumber, episode: episodeNumber, title: title, "
            "hasFile: hasFile}",
        },
        {
            "for": "recent downloads, imports and failures",
            "call": "GET /api/v3/history?page=1&pageSize=20&sortKey=date&sortDirection=descending",
            "select": "records[].{title: sourceTitle, event: eventType, date: date}",
        },
        {
            "for": "free disk space",
            "call": "GET /api/v3/diskspace",
            "select": "[].{path: path, free: freeSpace, total: totalSpace}",
        },
        {
            "for": "search for an episode right now (needs owner approval)",
            "call": "POST /api/v3/command",
            "body": {"name": "EpisodeSearch", "episodeIds": [123]},
        },
    ]


PROWLARR_RECIPES: list[Recipe] = [
    {
        "for": "which indexers failed and why",
        "call": "GET /api/v1/indexerstatus",
        "select": "[].{indexerId: indexerId, disabledTill: disabledTill, "
        "lastFailure: mostRecentFailure}",
        "note": "indexerId -> name: GET /api/v1/indexer with select "
        "[].{id: id, name: name, enable: enable}. The reason is in GET /api/v1/health.",
    },
    {
        "for": "health warnings (indexer failures, unavailable apps)",
        "call": "GET /api/v1/health",
        "select": "[].{source: source, type: type, message: message}",
    },
    {
        "for": "list indexers",
        "call": "GET /api/v1/indexer",
        "select": "[].{id: id, name: name, enable: enable, protocol: protocol}",
    },
    {
        "for": "queries, grabs and failures per indexer",
        "call": "GET /api/v1/indexerstats",
        "select": "indexers[].{name: indexerName, queries: numberOfQueries, "
        "failed: numberOfFailedQueries}",
    },
    {
        "for": "search all indexers for a release (queries every indexer: needs owner approval)",
        "call": "GET /api/v1/search?query=ubuntu&type=search&limit=20",
    },
]

QBIT_RECIPES: list[Recipe] = [
    {
        "for": "what is qBittorrent downloading",
        "call": "GET /api/v2/torrents/info?filter=downloading",
        "select": "[].{name: name, progress: progress, speed: dlspeed, eta: eta, state: state}",
        "note": "progress 0..1, speed bytes/s, eta seconds. Other filters: stalled_downloading, "
        "errored, completed, paused, all.",
    },
    {
        "for": "stalled or errored torrents",
        "call": "GET /api/v2/torrents/info?filter=stalled_downloading",
        "select": "[].{name: name, state: state, progress: progress, hash: hash}",
    },
    {
        "for": "overall speed and connection",
        "call": "GET /api/v2/transfer/info",
    },
    {
        "for": "files of one torrent",
        "call": "GET /api/v2/torrents/files?hash=HASH",
        "select": "[].{name: name, size: size, progress: progress}",
    },
    {
        "for": "trackers of one torrent (why it is not working)",
        "call": "GET /api/v2/torrents/trackers?hash=HASH",
        "select": "[].{url: url, status: status, msg: msg, seeds: num_seeds}",
    },
    {
        "for": "pause a torrent (needs owner approval)",
        "call": "POST /api/v2/torrents/pause",
        "body": {"hashes": "HASH"},
        "note": "qBittorrent 5: /torrents/stop. Writes use form fields; lists are joined by |.",
    },
]

DOCKER_RECIPES: list[Recipe] = [
    {
        "for": "list containers and their state",
        "call": "GET /containers/json?all=true",
        "select": "[].{name: Names[0], state: State, status: Status, image: Image}",
    },
    {
        "for": "mounts (volumes, bind mounts) of one container",
        "call": "GET /containers/NAME/json",
        "select": "{name: Name, mounts: Mounts[].{type: Type, source: Source, "
        "target: Destination, rw: RW}}",
    },
    {
        "for": "last log lines of a container",
        "call": "GET /containers/NAME/logs?stdout=true&stderr=true&tail=50",
    },
    {
        "for": "restart count, state and exit code of a container",
        "call": "GET /containers/NAME/json",
        "select": "{state: State.Status, restarts: RestartCount, exit: State.ExitCode, "
        "started: State.StartedAt, health: State.Health.Status}",
    },
    {
        "for": "disk used by images, containers and volumes",
        "call": "GET /system/df",
        "select": "{images: Images[].{tags: RepoTags, size: Size}, "
        "volumes: Volumes[].{name: Name, size: UsageData.Size}}",
    },
    {
        "for": "current CPU/memory use of one container",
        "call": "GET /containers/NAME/stats?stream=false&one-shot=true",
        "select": "{mem: memory_stats.usage, mem_limit: memory_stats.limit}",
    },
]

PROMETHEUS_RECIPES: list[Recipe] = [
    {
        "for": "which scrape targets are down",
        "call": "GET /api/v1/targets?state=active",
        "select": "data.activeTargets[?health!='up'].{job: labels.job, "
        "instance: labels.instance, error: lastError}",
    },
    {
        "for": "an instant value (CPU, memory, anything)",
        "call": "GET /api/v1/query?query=up",
        "select": "data.result[].{labels: metric, value: value[1]}",
        "note": "query holds a PromQL expression, e.g. "
        "100 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100",
    },
    {
        "for": "a value over time",
        "call": "GET /api/v1/query_range?query=up&start=UNIX_TS&end=UNIX_TS&step=300",
        "select": "data.result[].{labels: metric, last: values[-1]}",
    },
    {
        "for": "firing alerts",
        "call": "GET /api/v1/alerts",
        "select": "data.alerts[].{name: labels.alertname, state: state, since: activeAt}",
    },
    {
        "for": "find a metric name",
        "call": "GET /api/v1/label/__name__/values",
        "select": "data[?contains(@, 'memory')]",
    },
]

# ----------------------------------------------------------------------------- hand indexes

QBIT: list[Entry] = [
    ("GET", "/api/v2/app/version", "App", "qBittorrent version", ""),
    ("GET", "/api/v2/app/webapiVersion", "App", "WebUI API version", ""),
    ("GET", "/api/v2/app/buildInfo", "App", "build info (qt, libtorrent versions)", ""),
    ("GET", "/api/v2/app/preferences", "App", "all settings (large: use select)", ""),
    ("GET", "/api/v2/app/defaultSavePath", "App", "default save path", ""),
    ("GET", "/api/v2/transfer/info", "Transfer", "global speeds, connection status, dht nodes", ""),
    ("GET", "/api/v2/transfer/speedLimitsMode", "Transfer", "1 = alternative limits on", ""),
    (
        "GET",
        "/api/v2/torrents/info",
        "Torrents",
        "list torrents: name, state, progress, dlspeed, eta, hash, category, size",
        "filter (all, downloading, seeding, completed, paused, active, inactive, stalled, "
        "stalled_downloading, errored), category, tag, sort, reverse, limit, offset, hashes",
    ),
    ("GET", "/api/v2/torrents/count", "Torrents", "number of torrents (5.x)", ""),
    ("GET", "/api/v2/torrents/properties", "Torrents", "details of one torrent", "hash*"),
    ("GET", "/api/v2/torrents/trackers", "Torrents", "trackers of one torrent with status", "hash*"),
    ("GET", "/api/v2/torrents/webseeds", "Torrents", "web seeds of one torrent", "hash*"),
    ("GET", "/api/v2/torrents/files", "Torrents", "files of one torrent", "hash*, indexes"),
    ("GET", "/api/v2/torrents/pieceStates", "Torrents", "piece states of one torrent", "hash*"),
    ("GET", "/api/v2/torrents/categories", "Torrents", "categories and save paths", ""),
    ("GET", "/api/v2/torrents/tags", "Torrents", "tags", ""),
    ("GET", "/api/v2/sync/maindata", "Sync", "everything at once (huge)", "rid"),
    ("GET", "/api/v2/log/main", "Log", "application log", "normal, info, warning, critical, last_known_id"),
    ("GET", "/api/v2/log/peers", "Log", "banned/connected peer log", "last_known_id"),
    ("POST", "/api/v2/torrents/pause", "Torrents", "pause torrents (4.x)", "hashes* (a|b|all)"),
    ("POST", "/api/v2/torrents/resume", "Torrents", "resume torrents (4.x)", "hashes*"),
    ("POST", "/api/v2/torrents/stop", "Torrents", "pause torrents (5.x)", "hashes*"),
    ("POST", "/api/v2/torrents/start", "Torrents", "resume torrents (5.x)", "hashes*"),
    ("POST", "/api/v2/torrents/delete", "Torrents", "remove torrents", "hashes*, deleteFiles*"),
    ("POST", "/api/v2/torrents/recheck", "Torrents", "recheck data", "hashes*"),
    ("POST", "/api/v2/torrents/reannounce", "Torrents", "ask trackers again", "hashes*"),
    ("POST", "/api/v2/torrents/setCategory", "Torrents", "set category", "hashes*, category*"),
    ("POST", "/api/v2/torrents/setLocation", "Torrents", "move data", "hashes*, location*"),
    ("POST", "/api/v2/torrents/add", "Torrents", "add by url/magnet", "urls*, category, savepath"),
    ("POST", "/api/v2/torrents/addTrackers", "Torrents", "add trackers", "hash*, urls*"),
    ("POST", "/api/v2/torrents/topPrio", "Torrents", "queue to top", "hashes*"),
    ("POST", "/api/v2/transfer/toggleSpeedLimitsMode", "Transfer", "toggle alternative limits", ""),
    ("POST", "/api/v2/app/setPreferences", "App", "change settings", "json* (JSON string)"),
]

DOCKER: list[Entry] = [
    ("GET", "/_ping", "System", "is the daemon up (text OK)", ""),
    ("GET", "/version", "System", "engine and API version", ""),
    ("GET", "/info", "System", "containers running/stopped, images, storage driver, memory", ""),
    ("GET", "/system/df", "System", "disk used by images, containers, volumes, build cache", ""),
    (
        "GET",
        "/containers/json",
        "Containers",
        "list containers: Names, Image, State, Status, Ports, Mounts",
        "all (true = also stopped), limit, filters (JSON, e.g. {\"status\":[\"exited\"]})",
    ),
    (
        "GET",
        "/containers/{id}/json",
        "Containers",
        "inspect one container: State, RestartCount, Mounts, Config.Env, HostConfig, "
        "NetworkSettings",
        "",
    ),
    (
        "GET",
        "/containers/{id}/logs",
        "Containers",
        "log lines of a container",
        "stdout*, stderr*, tail (lines), since (unix ts), timestamps",
    ),
    (
        "GET",
        "/containers/{id}/stats",
        "Containers",
        "CPU, memory, network, block IO of a container",
        "stream* (false), one-shot (true)",
    ),
    ("GET", "/containers/{id}/top", "Containers", "processes in a container", "ps_args"),
    ("GET", "/containers/{id}/changes", "Containers", "files changed in the container", ""),
    ("GET", "/images/json", "Images", "list images: RepoTags, Size, Created", "all, filters"),
    ("GET", "/images/{name}/json", "Images", "inspect one image", ""),
    ("GET", "/images/{name}/history", "Images", "layers of an image", ""),
    ("GET", "/volumes", "Volumes", "list volumes", "filters"),
    ("GET", "/volumes/{name}", "Volumes", "inspect one volume (mountpoint)", ""),
    ("GET", "/networks", "Networks", "list networks", "filters"),
    ("GET", "/networks/{id}", "Networks", "inspect one network and its containers", ""),
    ("GET", "/events", "System", "daemon events in a time window", "since*, until* (unix ts)"),
]

PROMETHEUS: list[Entry] = [
    ("GET", "/api/v1/query", "Query", "instant PromQL query", "query*, time"),
    (
        "GET",
        "/api/v1/query_range",
        "Query",
        "PromQL over a time range",
        "query*, start*, end*, step* (seconds)",
    ),
    ("GET", "/api/v1/series", "Metadata", "series matching selectors", "match[]*, start, end"),
    ("GET", "/api/v1/labels", "Metadata", "all label names", "match[]"),
    ("GET", "/api/v1/label/{name}/values", "Metadata", "values of a label, e.g. __name__ = all "
     "metric names, job, instance", "match[]"),
    ("GET", "/api/v1/targets", "Targets", "scrape targets with health and lastError", "state "
     "(active, dropped, any)"),
    ("GET", "/api/v1/targets/metadata", "Targets", "metric metadata of targets", "match_target, "
     "metric, limit"),
    ("GET", "/api/v1/metadata", "Metadata", "type and help text of metrics", "metric, limit"),
    ("GET", "/api/v1/rules", "Alerts", "alerting and recording rules and their state", "type"),
    ("GET", "/api/v1/alerts", "Alerts", "currently firing alerts", ""),
    ("GET", "/api/v1/alertmanagers", "Alerts", "alertmanagers in use", ""),
    ("GET", "/api/v1/status/buildinfo", "Status", "version", ""),
    ("GET", "/api/v1/status/runtimeinfo", "Status", "uptime, storage retention, reload status", ""),
    ("GET", "/api/v1/status/tsdb", "Status", "series counts, top metrics by cardinality", ""),
    ("GET", "/api/v1/status/flags", "Status", "command-line flags", ""),
]

MANUAL_SERVICES: dict[str, dict[str, Any]] = {
    "qbittorrent": {
        "name": "qBittorrent WebUI API v2",
        "prefix": "/api/v2",
        "entries": QBIT,
        "recipes": QBIT_RECIPES,
    },
    "docker": {
        "name": "Docker Engine API (read-only)",
        "prefix": "",
        "entries": DOCKER,
        "recipes": DOCKER_RECIPES,
    },
    "prometheus": {
        "name": "Prometheus HTTP API (read-only)",
        "prefix": "/api/v1",
        "entries": PROMETHEUS,
        "recipes": PROMETHEUS_RECIPES,
    },
}


def arr_recipes(kind: str) -> list[Recipe]:
    if kind == "sonarr":
        return _sonarr_recipes()
    if kind == "prowlarr":
        return PROWLARR_RECIPES
    return _movie_recipes(kind)
