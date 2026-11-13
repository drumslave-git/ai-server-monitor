"""Entry point: `python -m servermon` (or the `servermon` script)."""

from __future__ import annotations

import logging

import uvicorn

from .config import load_settings
from .main import create_app


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; that is noise for a polling monitor.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    settings = load_settings()
    setup_logging(settings.server.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
