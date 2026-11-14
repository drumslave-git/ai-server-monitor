#!/usr/bin/env python3
"""Bump the release version. CI publishes a new image when it changes on main.

    python scripts/release.py patch      # 0.2.0 -> 0.2.1  (fixes)
    python scripts/release.py minor      # 0.2.1 -> 0.3.0  (features)
    python scripts/release.py major      # 0.3.0 -> 1.0.0  (breaking changes)
    python scripts/release.py 1.2.3      # explicit version

The source of truth is `version` in backend/pyproject.toml; frontend/package.json is
kept in sync. This script does not commit, tag or push: commit the bump and push to
main, and the Release workflow tags vX.Y.Z and publishes the image.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "backend" / "pyproject.toml"
PACKAGE_JSON = ROOT / "frontend" / "package.json"
LOCKFILE = ROOT / "frontend" / "package-lock.json"

# Only the `version = "..."` line inside the [project] table.
_PROJECT_VERSION = re.compile(r'(?ms)^(\[project\]\s*$.*?^version\s*=\s*")([^"]+)(")')
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def read_version() -> str:
    match = _PROJECT_VERSION.search(PYPROJECT.read_text(encoding="utf-8"))
    if not match:
        sys.exit(f"could not find [project] version in {PYPROJECT}")
    return match.group(2)


def next_version(current: str, part: str) -> str:
    if _SEMVER.match(part):
        return part
    match = _SEMVER.match(current)
    if not match:
        sys.exit(f"current version {current!r} is not MAJOR.MINOR.PATCH")
    major, minor, patch = (int(x) for x in match.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    sys.exit(f"unknown bump {part!r}: use patch, minor, major or an explicit X.Y.Z")


def write_json_version(path: Path, version: str, *, lockfile: bool = False) -> None:
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    if lockfile and "" in data.get("packages", {}):
        data["packages"][""]["version"] = version
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        sys.exit(__doc__)
    current = read_version()
    new = next_version(current, argv[1])
    if new == current:
        sys.exit(f"version is already {current}")
    text = PYPROJECT.read_text(encoding="utf-8")
    updated = _PROJECT_VERSION.sub(rf"\g<1>{new}\g<3>", text, count=1)
    PYPROJECT.write_text(updated, encoding="utf-8", newline="\n")
    write_json_version(PACKAGE_JSON, new)
    write_json_version(LOCKFILE, new, lockfile=True)
    print(f"{current} -> {new}")
    print(f'Next: git commit -am "Release v{new}" && git push   (CI tags v{new} and publishes)')


if __name__ == "__main__":
    main(sys.argv)
