"""Run the server: ``python -m akko_mcp_trino``.

Reads the configuration from the environment, builds the Trino client and the
five tools, mounts the identity guard on the served transport, and serves the
health endpoints on a second port so a liveness probe never depends on Trino.

Everything here is glue over pieces that carry their own tests; the one
decision that matters — the guard on the served transport — lives in
``akko_mcp_trino.app`` and is tested there.
"""

from __future__ import annotations

import logging
import threading

import uvicorn

from .app import build_asgi_app, run_stdio
from .audit import LoggingAudit
from .auth import build_auth
from .config import Config
from .health import build_health_app
from .metrics import Metrics
from .server import build_server


def main() -> None:  # pragma: no cover - process glue, proven by running it
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger(__name__)

    config = Config.from_env()
    metrics = Metrics()
    mcp, client = build_server(config, metrics=metrics, audit=LoggingAudit())
    auth_provider = build_auth(config)

    if config.transport == "stdio":
        log.info(
            "serving transport=stdio auth=%s strict=%s",
            auth_provider is not None,
            config.auth_required,
        )
        run_stdio(config, mcp, auth_provider=auth_provider)
        return

    threading.Thread(
        target=lambda: uvicorn.run(
            build_health_app(client, metrics=metrics),
            host="0.0.0.0",
            port=config.health_port,
            log_level="warning",
        ),
        daemon=True,
    ).start()

    app = build_asgi_app(config, mcp, auth_provider=auth_provider)
    log.info(
        "serving transport=%s port=%d auth=%s strict=%s",
        config.transport,
        config.mcp_port,
        auth_provider is not None,
        config.auth_required,
    )
    uvicorn.run(app, host="0.0.0.0", port=config.mcp_port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
