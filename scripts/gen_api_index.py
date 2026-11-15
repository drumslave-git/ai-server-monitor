"""Generate the chat's API endpoint index: backend/ai_server_monitor/apiaccess/index_data/endpoints.json

    python scripts/gen_api_index.py --specs <dir>            # read <dir>/{radarr,...}.json
    python scripts/gen_api_index.py --specs <dir> --fetch    # download the specs there first

The *arr part comes from the apps' published OpenAPI specs (Radarr/Sonarr/Prowlarr `develop`,
Whisparr `eros`); qBittorrent, Docker and Prometheus are hand-written in
`api_index_manual.py`. The result is compact on purpose: per endpoint method, path, tag, a
short meaning and the names of the query parameters / body fields, no schemas. Commit the
generated file; the app never reads the specs at run time.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import api_index_manual as manual

OUT = (
    Path(__file__).resolve().parent.parent
    / "backend/ai_server_monitor/apiaccess/index_data/endpoints.json"
)
SPECS = {
    "radarr": ("Radarr", "Radarr/Radarr/develop/src/Radarr.Api.V3", "/api/v3"),
    "sonarr": ("Sonarr", "Sonarr/Sonarr/develop/src/Sonarr.Api.V3", "/api/v3"),
    "whisparr": ("Whisparr (v3)", "Whisparr/Whisparr/eros/src/Whisparr.Api.V3", "/api/v3"),
    "prowlarr": ("Prowlarr", "Prowlarr/Prowlarr/develop/src/Prowlarr.Api.V1", "/api/v1"),
}
METHODS = ("get", "post", "put", "delete", "patch")
# Body fields are listed only for small request bodies; big resources just get their name.
MAX_BODY_FIELDS = 8
# Endpoints that never help a chat (UI plumbing, file/image streams, auth, push hooks).
SKIP_PREFIXES = (
    "/login",
    "/logout",
    "/content",
    "/mediacover",
    "/initialize",
    "/ping",
    "/feed",
    "/{id}/",  # per-indexer newznab/torznab proxies (Prowlarr)
)
SKIP_SUBSTRINGS = ("/schema", "/action/{name}", "/bulk", "/editor")


def fetch(specs_dir: Path) -> None:
    specs_dir.mkdir(parents=True, exist_ok=True)
    for kind, (_, repo, _) in SPECS.items():
        url = f"https://raw.githubusercontent.com/{repo}/openapi.json"
        with urllib.request.urlopen(url, timeout=60) as response:
            (specs_dir / f"{kind}.json").write_bytes(response.read())
        print("fetched", url)


def _resolve(spec: dict[str, Any], node: dict[str, Any] | None) -> dict[str, Any]:
    while node and "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        node = spec["components"]["schemas"].get(name, {})
    return node or {}


def _ref_name(node: dict[str, Any] | None) -> str:
    node = node or {}
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    if node.get("type") == "array":
        return _ref_name(node.get("items"))
    return ""


def _params(spec: dict[str, Any], operation: dict[str, Any], path_item: dict[str, Any]) -> str:
    names: list[str] = []
    for param in [*path_item.get("parameters", []), *operation.get("parameters", [])]:
        if param.get("$ref"):
            param = spec["components"]["parameters"][param["$ref"].rsplit("/", 1)[-1]]
        if param.get("in") != "query":
            continue
        names.append(param["name"] + ("*" if param.get("required") else ""))
    return ", ".join(names)


def _body(spec: dict[str, Any], operation: dict[str, Any]) -> str:
    content = (operation.get("requestBody") or {}).get("content") or {}
    schema = (content.get("application/json") or next(iter(content.values()), {})).get("schema")
    if not schema:
        return ""
    resolved = _resolve(spec, schema if schema.get("type") != "array" else schema.get("items"))
    fields = [k for k in (resolved.get("properties") or {}) if k != "id"]
    label = _ref_name(schema) or "object"
    if schema.get("type") == "array":
        label = f"[{label}]"
    if fields and len(fields) <= MAX_BODY_FIELDS:
        return f"body {label}: {', '.join(fields)}"
    return f"body: {label}"


def _summary(method: str, path: str, tag: str) -> str:
    segments = [s for s in path.split("/") if s]
    words = " ".join(s for s in segments if not s.startswith("{")) or tag.lower()
    by_id = bool(segments) and segments[-1].startswith("{")
    if method == "GET":
        return f"get one {words} by id" if by_id else f"list {words}"
    if method == "POST":
        return f"create {words}" if tag.lower() == words else f"run {words}"
    if method == "PUT":
        return f"update {words}"
    return f"delete {words}"


def arr_entries(kind: str, spec: dict[str, Any], prefix: str) -> list[list[str]]:
    entries: list[list[str]] = []
    for path, item in sorted(spec["paths"].items()):
        if not path.startswith(prefix):
            continue  # outside the versioned API (UI, newznab proxies)
        short = path.removeprefix(prefix) or "/"
        if any(short.startswith(p) for p in SKIP_PREFIXES) or any(
            s in short for s in SKIP_SUBSTRINGS
        ):
            continue
        for method in METHODS:
            operation = item.get(method)
            if operation is None:
                continue
            tag = (operation.get("tags") or [short.strip("/").split("/")[0].title()])[0]
            hint = manual.ARR_HINTS.get((method.upper(), short)) or _summary(
                method.upper(), short, tag
            )
            params = _params(spec, operation, item)
            body = _body(spec, operation) if method in ("post", "put", "patch") else ""
            extra = "; ".join(x for x in (params, body) if x)
            entries.append([method.upper(), prefix + short, tag, hint, extra])
    return entries


def build(specs_dir: Path) -> dict[str, Any]:
    services: dict[str, Any] = {}
    versions: dict[str, str] = {}
    for kind, (title, repo, prefix) in SPECS.items():
        spec = json.loads((specs_dir / f"{kind}.json").read_text("utf-8"))
        versions[kind] = repo.split("/")[2]  # branch the spec came from
        services[kind] = {
            "name": title,
            "prefix": prefix,
            "recipes": manual.arr_recipes(kind),
            "entries": arr_entries(kind, spec, prefix),
        }
    # Whisparr's spec omits these two; its source (MissingController, CutoffController) has them.
    paged = "page, pageSize, sortKey, sortDirection, monitored"
    services["whisparr"]["entries"] += [
        [
            "GET",
            "/api/v3/wanted/missing",
            "Missing",
            manual.ARR_HINTS[("GET", "/wanted/missing")],
            paged,
        ],
        [
            "GET",
            "/api/v3/wanted/cutoff",
            "Cutoff",
            manual.ARR_HINTS[("GET", "/wanted/cutoff")],
            paged,
        ],
    ]
    for kind, data in manual.MANUAL_SERVICES.items():
        services[kind] = {**data, "entries": [list(e) for e in data["entries"]]}
    return {
        "_": "Generated by scripts/gen_api_index.py from the apps' OpenAPI specs; do not edit.",
        "arr_note": manual.ARR_WRITE_NOTE,
        "services": services,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--specs", type=Path, required=True, help="directory of <kind>.json specs")
    parser.add_argument("--fetch", action="store_true", help="download the specs first")
    args = parser.parse_args()
    if args.fetch:
        fetch(args.specs)
    data = build(args.specs)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=None, separators=(",", ":"))
    # One endpoint per line: readable diffs when the specs change.
    text = text.replace('],["', '],\n["').replace('"entries":[', '"entries":[\n')
    OUT.write_text(text + "\n", "utf-8")
    counts = {k: len(v["entries"]) for k, v in data["services"].items()}
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes) endpoints: {counts}")


if __name__ == "__main__":
    main()
