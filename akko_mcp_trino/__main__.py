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
import signal
import sys
import threading
from collections.abc import Callable
from typing import Any

import uvicorn

from . import __version__
from .app import build_asgi_app, run_stdio, transports_supported
from .audit import LoggingAudit
from .auth import accepted_audiences, build_auth
from .config import MAX_DESCRIBE_SCHEMA_TABLES, Config, parse_jwt_algorithms, provider_names
from .context import ContextProvider, ContextSetup
from .context.loading import close_providers, load_providers
from .extensions import Extension, extension_names, load_extensions
from .health import build_health_app
from .metrics import Metrics
from .parse_pool import close_all as close_parse_pools
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
    parser.add_argument(
        "--strict",
        action="store_true",
        help="with --check: exit 2 also when a context provider says it is unreachable",
    )
    return parser.parse_args(argv)


def _audiences(extensions: list[Extension]) -> list[str]:
    return [a for ext in extensions for a in ext.audiences()]


def _algorithms(config: Config) -> list[str]:
    """The token algorithms the server applies, or the raw setting when it is a problem."""
    try:
        return parse_jwt_algorithms(config.jwt_algorithms)
    except ValueError:
        return [config.jwt_algorithms]


def check_config(config: Config, *, strict: bool = False) -> int:
    """Print the effective configuration without secrets; 0 if it would start, 2 otherwise.

    With ``strict``, a context provider that reports itself unreachable (a check line
    that starts with ``UNREACHABLE:``) also makes it 2."""
    problems: list[str] = list(config.problems())
    extensions: list[Extension] = []
    loaded = True
    try:
        extensions = load_extensions(config, os.environ)
    except ValueError as exc:
        problems.append(str(exc))
        loaded = False
    providers: list[ContextProvider] = []
    try:
        providers = load_providers(config, os.environ)
    except ValueError as exc:
        problems.append(str(exc))
    provider_lines = [line for provider in providers for line in provider.check_lines()]
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
        f"token signatures: {','.join(_algorithms(config))}; user from: {config.jwt_user_claim}; "
        f"client from: {','.join(config.client_claims())}; scopes from: "
        f"{','.join(config.scope_claims())}",
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
        f"agent SQL: up to {config.max_sql_chars} characters, brackets nested "
        f"{config.max_sql_nesting} deep at most, refused past either before it is parsed; "
        f"parsed by {config.sql_parse_workers} worker processes within "
        f"{config.sql_parse_seconds:g} s, one statement at a time per caller",
        f"listings: {config.max_rows} names a page; list_catalogs reads up to "
        f"{config.max_catalogs} catalogs",
        f"describe_schema: up to {config.describe_schema_tables} tables a page "
        f"({MAX_DESCRIBE_SCHEMA_TABLES} at most), "
        f"{config.describe_schema_columns} columns in all, a wider table whole",
        f"search_columns: words of {config.search_min_length} to "
        f"{config.search_max_word_length} characters, {config.search_max_words} words at "
        f"most, {config.search_reserve_seconds:g} s of the call's budget kept in reserve, "
        f"{config.search_concurrency} catalogs at once, {config.search_max_threads} reads at "
        "once in all",
        f"describe_table: pages of {config.max_rows} columns, {config.describe_max_columns} "
        "columns at most",
        f"profile_table: modes {', '.join(config.allowed_profile_modes())}; up to "
        f"{config.profile_max_columns} columns, sample of {config.profile_sample_rows} first "
        f"rows, values cut at {config.profile_value_chars} characters, types at "
        f"{config.profile_type_chars}, DESCRIBE read up to "
        f"{config.profile_describe_rows} rows, client tag {config.profile_client_tag}, "
        f"{config.profile_max_concurrent} at once, time budget "
        + (
            f"{config.profile_budget_seconds():g} s"
            if config.profile_budget_seconds() > 0
            else "none"
        ),
        f"audit: up to {config.audit_max_names} names a list, the rest counted",
        f"tool plugins: {config.tool_plugins or 'none'}",
        f"extensions: {', '.join(extension_names(config.extensions)) or 'none'}",
        *[line for ext in extensions for line in ext.check_lines()],
        f"context: {', '.join(provider_names(config.context_providers)) or 'none'} "
        f"(timeout {config.context_timeout_seconds:g} s, reserve "
        f"{config.context_reserve_seconds:g} s, {config.context_max_concurrency} reads in "
        f"flight, retry after {config.context_retry_after_seconds:g} s, after a refusal "
        f"{config.context_refused_retry_seconds:g} s, batch {config.context_batch}, "
        f"ttl {config.context_ttl_seconds:g} s, caches {config.context_cache_bytes} + "
        f"{config.context_terms_cache_bytes} bytes, text {config.context_text_chars} "
        f"characters, response {config.context_response_chars} characters, "
        f"{config.context_max_columns} columns)",
        f"visibility: ttl {config.visibility_ttl_seconds:g} s, cache "
        f"{config.visibility_cache_bytes} bytes, {config.visibility_max_names} names, "
        f"canary {'set' if config.context_canary else '-'}, "
        f"require select {str(config.context_require_select).lower()}",
        *provider_lines,
    ]
    if strict:
        problems.extend(line for line in provider_lines if line.startswith("UNREACHABLE:"))
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
    close_providers(providers)
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
        sys.exit(check_config(Config.from_env(), strict=args.strict))
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger(__name__)

    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines carry request URLs
    config = Config.from_env()
    extensions = load_extensions(config, os.environ)
    providers = load_providers(config, os.environ)
    metrics = Metrics()

    started: list[ContextSetup] = []
    closing: list[bool] = []

    def on_context(setup: ContextSetup) -> None:
        started.append(setup)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, lambda *_: setup.chain.reset_refused())

    def close_context() -> None:
        # One close only: a stop may come by several of these paths, even during a close.
        if closing:
            return
        closing.append(True)
        close_parse_pools()  # the parse workers first: they end at once
        for setup in started:
            setup.close()

    mcp, client = build_server(
        config,
        extra_tool_registrars=tool_registrars({"MCP_TOOL_PLUGINS": config.tool_plugins}),
        metrics=metrics,
        audit=LoggingAudit(),
        extensions=extensions,
        context_providers=providers,
        context_started=on_context,
    )
    # The providers are closed, then the context loop, once the server stops. Over
    # HTTP, uvicorn shuts the app down on SIGTERM, then lets the signal end the
    # process: the close is in the app's shutdown. Over stdio with a provider, SIGTERM
    # closes and exits 0, or lets a close under way end the process; without one, the
    # signal ends the process as in 0.7.2.
    if config.transport == "stdio" and started:

        def on_term(*_: object) -> None:
            if closing:
                return
            close_context()
            sys.exit(0)

        signal.signal(signal.SIGTERM, on_term)
    try:
        auth_provider = build_auth(config, _audiences(extensions))
        for warning in config.warnings():
            log.warning(warning)
        _serve(config, mcp, client, auth_provider, extensions, metrics, close_context)
    finally:
        close_context()


def _serve(  # pragma: no cover - process glue, proven by running it
    config: Config,
    mcp: Any,
    client: Any,
    auth_provider: Any,
    extensions: list[Extension],
    metrics: Metrics,
    on_shutdown: Callable[[], None],
) -> None:
    """Serve on the configured transport until the server stops."""
    log = logging.getLogger(__name__)
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

    app = build_asgi_app(
        config, mcp, auth_provider=auth_provider, extensions=extensions, on_shutdown=on_shutdown
    )
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
