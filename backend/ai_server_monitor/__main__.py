"""Entry point: `python -m ai_server_monitor` (or the `ai-server-monitor` script)."""

from __future__ import annotations

import logging

import uvicorn

from .config import Bootstrap
from .main import create_app


def setup_logging() -> None:
    # The level is applied from Settings → General once settings are loaded.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; that is noise for a polling monitor.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    bootstrap = Bootstrap()
    setup_logging()
    uvicorn.run(
        create_app(bootstrap),
        host=bootstrap.host,
        port=bootstrap.port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
        # Long-lived SSE streams must not hold up a restart.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
