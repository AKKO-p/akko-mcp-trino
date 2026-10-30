"""Run the server: ``akko-mcp-trino`` or ``python -m akko_mcp_trino``.

``--version`` prints the version; ``--check`` reads the configuration, builds
the guards and reports the effective settings without secrets, then exits
(2 when something would refuse to start). Both never open a port.

Reads the configuration from the environment, builds the Trino client and the
tools, mounts the identity guard on the served transport, and serves the
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

import uvicorn

from . import __version__
from .app import build_asgi_app, run_stdio, transports_supported
from .audit import LoggingAudit
from .auth import accepted_audiences, build_auth
from .config import Config
from .extensions import Extension, extension_names, load_extensions
from .health import build_health_app
from .metrics import Metrics
from .plugins import tool_registrars
from .server import build_server
from .trino_client import resolve_identity


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line; ``--version`` prints and exits."""
    parser = argparse.ArgumentParser(
        prog="akko-mcp-trino",
        description="Read-only MCP server for Trino: each query runs as the verified caller.",
    )
    parser.add_argument("--version", action="version", version=f"akko-mcp-trino {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the configuration, print the effective settings without secrets, exit",
    )
    return parser.parse_args(argv)


def _audiences(extensions: list[Extension]) -> list[str]:
    return [a for ext in extensions for a in ext.audiences()]


def check_config(config: Config) -> int:
    """Print the effective configuration without secrets; 0 if it would start, 2 otherwise."""
    problems: list[str] = list(config.problems())
    extensions: list[Extension] = []
    loaded = True
    try:
        extensions = load_extensions(config, os.environ)
    except ValueError as exc:
        problems.append(str(exc))
        loaded = False
    identity: list[str] = []
    try:
        identity = resolve_identity(config, extensions).describe()
    except ValueError as exc:
        if loaded:  # an extension that could not load says why; its modes are unknown
            problems.append(str(exc))
    lines = [
        f"transport: {config.transport} (port {config.mcp_port}, health {config.health_port})",
        f"trino: {config.trino_host}:{config.trino_port} as {config.trino_user}, "
        f"catalog {config.trino_catalog}, read_only={config.read_only}, max_rows={config.max_rows}",
        f"auth: enabled={config.auth_enabled} required={config.auth_required} "
        f"jwks={config.jwks_url or '-'} issuer={config.oidc_issuer or '-'} "
        f"audience={config.oidc_audience or '-'} resource={config.resource_url or '-'}",
        "audiences accepted: "
        + (", ".join(accepted_audiences(config, _audiences(extensions))) or "-"),
        f"user token (stdio): {'set' if config.user_token else '-'}",
        f"trino identity mode: {config.trino_identity_mode} over {config.trino_http_scheme}",
        *identity,
        f"trino queries: source {config.trino_source or config.server_name}, time budget: "
        + (
            f"{config.query_timeout_seconds:g} s" if config.query_timeout_seconds > 0 else "Trino's"
        ),
        "tool call time budget: "
        + (
            f"{config.call_timeout_seconds:g} s, every query of the call included"
            if config.call_timeout_seconds > 0
            else "none"
        ),
        f"listings: {config.max_rows} names a page; list_catalogs reads up to "
        f"{config.max_catalogs} catalogs",
        f"tool plugins: {config.tool_plugins or 'none'}",
        f"extensions: {', '.join(extension_names(config.extensions)) or 'none'}",
        *[line for ext in extensions for line in ext.check_lines()],
    ]
    if config.transport not in transports_supported():
        problems.append(
            f"MCP_TRANSPORT {config.transport!r} is not one of {', '.join(transports_supported())}"
        )
    if config.transport == "stdio" and config.auth_required and not config.user_token:
        problems.append("MCP_TRANSPORT=stdio with MCP_AUTH_REQUIRED=true needs MCP_USER_TOKEN")
    try:
        build_auth(config, _audiences(extensions))
    except ValueError as exc:
        problems.append(str(exc))
    try:
        tool_registrars({"MCP_TOOL_PLUGINS": config.tool_plugins})
    except ValueError as exc:
        problems.append(str(exc))
    print("\n".join(lines))
    for warning in config.warnings():
        print(f"WARNING: {warning}")
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
    extensions = load_extensions(config, os.environ)
    metrics = Metrics()
    mcp, client = build_server(
        config,
        extra_tool_registrars=tool_registrars({"MCP_TOOL_PLUGINS": config.tool_plugins}),
        metrics=metrics,
        audit=LoggingAudit(),
        extensions=extensions,
    )
    auth_provider = build_auth(config, _audiences(extensions))
    for warning in config.warnings():
        log.warning(warning)

    if config.transport == "stdio":
        log.info(
            "serving transport=stdio auth=%s strict=%s identity=%s",
            auth_provider is not None,
            config.auth_required,
            config.trino_identity_mode,
        )
        run_stdio(config, mcp, auth_provider=auth_provider, extensions=extensions, client=client)
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

    app = build_asgi_app(config, mcp, auth_provider=auth_provider, extensions=extensions)
    log.info(
        "serving transport=%s port=%d auth=%s strict=%s identity=%s extensions=%s",
        config.transport,
        config.mcp_port,
        auth_provider is not None,
        config.auth_required,
        config.trino_identity_mode,
        ",".join(extension_names(config.extensions)) or "none",
    )
    uvicorn.run(app, host="0.0.0.0", port=config.mcp_port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
