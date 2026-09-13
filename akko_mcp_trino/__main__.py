"""Run the server: ``akko-mcp-trino`` or ``python -m akko_mcp_trino``.

``--version`` prints the version; ``--check`` reads the configuration, builds
the guards and reports the effective settings without secrets, then exits
(2 when something would refuse to start). Both never open a port.

Reads the configuration from the environment, builds the Trino client and the
five tools, mounts the identity guard on the served transport, and serves the
health endpoints on a second port so a liveness probe never depends on Trino.

Everything here is glue over pieces that carry their own tests; the one
decision that matters — the guard on the served transport — lives in
``akko_mcp_trino.app`` and is tested there.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from typing import Any

import uvicorn

from . import __version__
from .agents import AgentRegistry
from .app import build_asgi_app, run_stdio
from .audit import LoggingAudit
from .auth import build_auth
from .config import Config
from .context import build_context
from .health import build_health_app
from .identity import current_bearer, current_subject
from .metrics import Metrics
from .ratelimit import RateLimiter
from .revocation import IntrospectionCheck
from .server import build_server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line; ``--version`` prints and exits."""
    parser = argparse.ArgumentParser(
        prog="akko-mcp-trino",
        description="Governed MCP server for Trino: every tool call carries the user's identity.",
    )
    parser.add_argument("--version", action="version", version=f"akko-mcp-trino {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the configuration, print the effective settings without secrets, exit",
    )
    return parser.parse_args(argv)


def _context_env(config: Config) -> dict[str, str]:
    """The environment context providers read: the whole process environment (plugins
    have their own variables) with the MCP_CONTEXT_* settings as the Config resolved them."""
    return {
        **os.environ,
        "MCP_CONTEXT_PROVIDERS": config.context_providers,
        "MCP_CONTEXT_FILE": config.context_file,
        "MCP_CONTEXT_TTL_SECONDS": str(config.context_ttl_seconds),
    }


def check_config(config: Config) -> int:
    """Print the effective configuration without secrets; 0 if it would start, 2 otherwise."""
    lines = [
        f"transport: {config.transport} (port {config.mcp_port}, health {config.health_port})",
        f"trino: {config.trino_host}:{config.trino_port} as {config.trino_user}, "
        f"catalog {config.trino_catalog}, read_only={config.read_only}, max_rows={config.max_rows}",
        f"auth: enabled={config.auth_enabled} required={config.auth_required} "
        f"jwks={config.jwks_url or '-'} issuer={config.oidc_issuer or '-'} "
        f"audience={config.oidc_audience or '-'}",
        f"discovery: {config.resource_url or 'off'}",
        f"quotas: user={config.rate_limit_user} agent={config.rate_limit_agent} "
        f"per {config.rate_limit_window_seconds}s",
        f"introspection: {config.introspection_url or 'off'}",
        f"user token (stdio): {'set' if config.user_token else '-'}",
        f"trino identity mode: {config.trino_identity_mode} over {config.trino_http_scheme}",
        f"context providers: {config.context_providers or 'none'}",
    ]
    problems: list[str] = []
    try:
        build_auth(config)
    except ValueError as exc:
        problems.append(str(exc))
    try:
        registry = AgentRegistry.from_env({"MCP_AGENT_KEYS": config.agent_keys})
        lines.append(f"agent products: {len(registry._by_key)}")
    except ValueError as exc:
        problems.append(str(exc))
    try:
        IntrospectionCheck.from_env(
            {
                "MCP_INTROSPECTION_URL": config.introspection_url,
                "MCP_INTROSPECTION_CLIENT_ID": config.introspection_client_id,
                "MCP_INTROSPECTION_CLIENT_SECRET": config.introspection_client_secret,
            }
        )
    except ValueError as exc:
        problems.append(str(exc))
    RateLimiter.from_env({"MCP_RATE_LIMIT_USER": str(config.rate_limit_user)})
    try:
        build_context(_context_env(config), lambda sql: {"rows": []})
    except (ValueError, FileNotFoundError) as exc:
        problems.append(str(exc))
    print("\n".join(lines))
    for problem in problems:
        print(f"PROBLEM: {problem}")
    return 2 if problems else 0


def main() -> None:  # pragma: no cover - process glue, proven by running it
    """Read the configuration, assemble the server and serve on the configured transport."""
    args = parse_args()
    if args.check:
        sys.exit(check_config(Config.from_env()))
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger(__name__)

    config = Config.from_env()
    metrics = Metrics()
    # The context providers query Trino through the same governed client the
    # tools use; the client exists once build_server has run, hence the holder.
    holder: dict[str, Any] = {}
    context = build_context(
        _context_env(config),
        lambda sql: holder["client"].query(sql, user=current_subject(), bearer=current_bearer()),
    )
    mcp, client = build_server(config, metrics=metrics, audit=LoggingAudit(), context=context)
    holder["client"] = client
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
