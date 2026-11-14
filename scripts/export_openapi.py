"""Write the backend's OpenAPI schema to a file (input for `npm run gen:api`).

    python scripts/export_openapi.py frontend/.openapi.json

Builds the app object only (no startup, no database), so it needs no configuration.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    from ai_server_monitor.main import create_app

    if len(sys.argv) != 2:
        raise SystemExit("usage: export_openapi.py <output.json>")
    schema = create_app().openapi()
    Path(sys.argv[1]).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", "utf-8")


if __name__ == "__main__":
    main()
