"""Server configuration, read from the environment.

Nothing product-specific is hardcoded: the defaults here are neutral, and a
product built on this core injects its own values through the environment.
An extension reads its own settings from the environment it is given (see
`akko_mcp_trino.extensions`); this class holds the core's only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Defaults shared with the tools' registration, so a page and a bound never drift apart.
DEFAULT_MAX_ROWS = 100
DEFAULT_MAX_CATALOGS = 10000
DEFAULT_DESCRIBE_SCHEMA_TABLES = 20
DEFAULT_DESCRIBE_SCHEMA_COLUMNS = 1000
# The most tables a page of `describe_schema` may hold: a page reads each of its tables by
# its own UNION ALL branch, twice, so its cost grows with the page, and Trino's analyzer
# fails on a statement of thousands of branches.
MAX_DESCRIBE_SCHEMA_TABLES = 100
DEFAULT_SEARCH_MIN_LENGTH = 3
DEFAULT_SEARCH_RESERVE_SECONDS = 2.0


@dataclass(frozen=True)
class Config:
    """Every setting the server reads, with neutral defaults; built from the environment by
    ``from_env``.
    """

    trino_host: str
    trino_port: int
    trino_user: str
    trino_catalog: str
    max_rows: int
    read_only: bool
    auth_enabled: bool
    server_name: str
    health_port: int
    jwks_url: str
    oidc_issuer: str
    oidc_audience: str
    transport: str
    mcp_port: int
    auth_required: bool
    # Host header filter (the SDK's DNS rebinding protection). Empty, the
    # default, serves any Host: this is a network service bound on 0.0.0.0
    # behind the identity guard, reached as a service name in a cluster and
    # as a public name on the internet. A comma-separated list turns the
    # filter on for exactly those hosts (`host` or `host:port`; `host:*` for
    # any port is the only wildcard). Once on, a request that carries an
    # `Origin` header is refused 403: no origin is allowed.
    allowed_hosts: str = ""
    # stdio only: the user's access token, verified like a bearer header would
    # be. A local host (Claude Desktop, Cursor, Vibe) launches the server for
    # one person; that token names the verified caller of the whole process.
    user_token: str = ""
    # How the server reaches Trino. `jwt` (the default): every query runs as the
    # verified caller, with a token for Trino the server obtains from the identity
    # provider by OAuth 2.0 Token Exchange (RFC 8693); the caller's own token never
    # leaves the server. `impersonation`: the server authenticates as its own
    # Trino account and Trino runs the query as the verified caller, under an
    # impersonation rule limited to that account. `service`, only when named: every
    # query runs as `TRINO_USER`, whoever calls. https only for the first two. An
    # extension (`MCP_EXTENSIONS`) may add other modes; an unknown mode refuses to start.
    trino_http_scheme: str = "http"
    trino_verify: str = "true"  # true, false, or a path to a CA bundle
    trino_request_timeout: float = 30.0
    # Name the server gives Trino as the query source (X-Trino-Source); empty =
    # the server name, so an operator tells this server's queries apart in Trino's log.
    trino_source: str = ""
    # Time budget of one query, in seconds, enforced by this server: past it the
    # query is cancelled in Trino. 0 leaves the limit to Trino's own settings.
    query_timeout_seconds: float = 60.0
    trino_identity_mode: str = "jwt"
    # The configured identity's password (Basic authentication, https only); empty sends none.
    trino_password: str = ""
    # Time budget of one tool call, every Trino query of the call included: past
    # it no query starts, the one running is cancelled, and the agent gets an
    # error, never a partial answer. 0 sets none.
    call_timeout_seconds: float = 120.0
    # Tools another package adds (entry point group `akko_mcp_trino.tools`),
    # by plugin name, comma-separated. Empty serves the generic tools only.
    tool_plugins: str = ""
    # Extensions another package adds (entry point group `akko_mcp_trino.extensions`),
    # by name, comma-separated: identity modes, request guards, audit, routes and
    # tools, all at once (see `akko_mcp_trino.extensions`). Empty loads none.
    extensions: str = ""
    # Tolerance on exp/nbf/iat for clock drift between the issuer and this server.
    jwt_leeway_seconds: int = 30
    # This server's canonical URI (RFC 8707, RFC 9728), such as https://mcp.example.com/mcp.
    # Accepted as a token audience besides `MCP_OIDC_AUDIENCE`, named in the protected
    # resource metadata the server then publishes, and in the challenge of a 401.
    resource_url: str = ""
    # `jwt` mode: the OAuth 2.0 Token Exchange (RFC 8693) that turns the caller's
    # token, issued for this server, into a token for Trino. The endpoint (https),
    # this server's client at the identity provider, the audience Trino accepts,
    # optional scopes, and how the endpoint's certificate is checked.
    token_exchange_url: str = ""
    token_exchange_client_id: str = ""
    token_exchange_client_secret: str = ""
    token_exchange_audience: str = ""
    token_exchange_scope: str = ""
    token_exchange_verify: str = "true"
    # A client certificate and its key (PEM paths) the server authenticates to
    # Trino with (mTLS), in `service` and `impersonation` modes, instead of a password.
    trino_client_cert: str = ""
    trino_client_key: str = ""
    # The most catalogs `list_catalogs` reads, past the row cap, before it pages them:
    # above it the tool answers an error, never a cut list.
    max_catalogs: int = DEFAULT_MAX_CATALOGS
    # A page of `describe_schema`: at most this many tables, and whole tables only while
    # their columns add up to this many at most; the first table of a page comes whole,
    # however wide, and the answer says when a page was shortened.
    describe_schema_tables: int = DEFAULT_DESCRIBE_SCHEMA_TABLES
    describe_schema_columns: int = DEFAULT_DESCRIBE_SCHEMA_COLUMNS
    # `search_columns`: the shortest text it searches, so that a letter or two does not
    # read every column, and the seconds of the call's budget it keeps in reserve, so that
    # it stops early enough to answer what it searched rather than the call's own error.
    search_min_length: int = DEFAULT_SEARCH_MIN_LENGTH
    search_reserve_seconds: float = DEFAULT_SEARCH_RESERVE_SECONDS

    def problems(self) -> list[str]:
        """Settings no server can serve: the server refuses to start, ``--check`` says why."""
        found = []
        if self.auth_required and not self.auth_enabled:
            found.append(
                "MCP_AUTH_REQUIRED=true but MCP_AUTH_ENABLED=false: no token can be verified "
                "and every request would be refused; enable MCP_AUTH_ENABLED, or set "
                "MCP_AUTH_REQUIRED=false to serve without verifying callers"
            )
        if self.max_rows < 1:
            found.append(f"TRINO_MAX_ROWS={self.max_rows}: the row cap must be 1 or more")
        if self.max_catalogs < 1:
            found.append(
                f"MCP_MAX_CATALOGS={self.max_catalogs}: "
                "list_catalogs must be allowed 1 catalog or more"
            )
        if self.describe_schema_tables < 1:
            found.append(
                f"MCP_DESCRIBE_SCHEMA_TABLES={self.describe_schema_tables}: "
                "a page of describe_schema must hold 1 table or more"
            )
        elif self.describe_schema_tables > MAX_DESCRIBE_SCHEMA_TABLES:
            found.append(
                f"MCP_DESCRIBE_SCHEMA_TABLES={self.describe_schema_tables}: "
                f"a page of describe_schema holds {MAX_DESCRIBE_SCHEMA_TABLES} tables at most"
            )
        if self.describe_schema_columns < 1:
            found.append(
                f"MCP_DESCRIBE_SCHEMA_COLUMNS={self.describe_schema_columns}: "
                "describe_schema must be allowed 1 column or more"
            )
        if self.search_min_length < 1:
            found.append(
                f"MCP_SEARCH_MIN_LENGTH={self.search_min_length}: "
                "search_columns must ask for 1 character or more"
            )
        if self.search_reserve_seconds < 0:
            found.append(
                f"MCP_SEARCH_RESERVE_SECONDS={self.search_reserve_seconds:g}: "
                "a reserve is 0 seconds or more"
            )
        elif 0 < self.call_timeout_seconds <= self.search_reserve_seconds:
            found.append(
                f"MCP_SEARCH_RESERVE_SECONDS={self.search_reserve_seconds:g} is not below "
                f"MCP_CALL_TIMEOUT_SECONDS={self.call_timeout_seconds:g}: "
                "search_columns would have no time left to search"
            )
        for name, value in (
            ("TRINO_QUERY_TIMEOUT_SECONDS", self.query_timeout_seconds),
            ("MCP_CALL_TIMEOUT_SECONDS", self.call_timeout_seconds),
        ):
            if value < 0:
                found.append(f"{name}={value:g}: a time budget is 0 (none) or more")
        return found

    def warnings(self) -> list[str]:
        """What this configuration leaves open, said at start, in ``--check`` and to every
        MCP host in the initialization's instructions; empty for the default modes."""
        found = []
        if self.trino_identity_mode == "service":
            found.append(
                f"TRINO_IDENTITY_MODE=service: every query runs as the Trino user "
                f"{self.trino_user!r}, whoever calls; Trino cannot tell callers apart"
            )
        if not self.auth_enabled:
            found.append(
                "MCP_AUTH_ENABLED=false: no caller is verified; anyone who reaches this "
                "server queries Trino"
            )
        elif not self.auth_required:
            found.append("MCP_AUTH_REQUIRED=false: a request without a valid token is served too")
        if self.call_timeout_seconds > 0 and self.search_reserve_seconds == 0:
            found.append(
                "MCP_SEARCH_RESERVE_SECONDS=0: a search of search_columns may run to the end of "
                "the call's budget, and the call then answers its time_budget error with "
                "nothing it found"
            )
        if self.call_timeout_seconds == 0:
            found.append(
                "MCP_CALL_TIMEOUT_SECONDS=0: a tool call has no time budget, so search_columns "
                "refuses to search every catalog and searches a named catalog only"
            )
        return found

    @staticmethod
    def from_env(*, server_name: str = "trino-mcp") -> "Config":
        """Read every setting from the environment."""
        return Config(
            trino_host=os.environ.get("TRINO_HOST", "localhost"),
            trino_port=int(os.environ.get("TRINO_PORT", "8080")),
            trino_user=os.environ.get("TRINO_USER", "trino"),
            trino_catalog=os.environ.get("TRINO_CATALOG", "system"),
            max_rows=int(os.environ.get("TRINO_MAX_ROWS", str(DEFAULT_MAX_ROWS))),
            read_only=os.environ.get("TRINO_READ_ONLY", "true").lower() == "true",
            auth_enabled=os.environ.get("MCP_AUTH_ENABLED", "true").lower() == "true",
            server_name=os.environ.get("MCP_SERVER_NAME", server_name),
            health_port=int(os.environ.get("MCP_HEALTH_PORT", "3001")),
            jwks_url=os.environ.get("MCP_JWKS_URL", ""),
            oidc_issuer=os.environ.get("MCP_OIDC_ISSUER", ""),
            oidc_audience=os.environ.get("MCP_OIDC_AUDIENCE", ""),
            transport=os.environ.get("MCP_TRANSPORT", "streamable-http"),
            mcp_port=int(os.environ.get("MCP_PORT", "3000")),
            auth_required=os.environ.get("MCP_AUTH_REQUIRED", "true").lower() == "true",
            allowed_hosts=os.environ.get("MCP_ALLOWED_HOSTS", ""),
            user_token=os.environ.get("MCP_USER_TOKEN", ""),
            trino_http_scheme=os.environ.get("TRINO_HTTP_SCHEME", "http").lower(),
            trino_verify=os.environ.get("TRINO_VERIFY", "true"),
            trino_request_timeout=float(os.environ.get("TRINO_REQUEST_TIMEOUT_SECONDS", "30")),
            trino_source=os.environ.get("TRINO_SOURCE", ""),
            query_timeout_seconds=float(os.environ.get("TRINO_QUERY_TIMEOUT_SECONDS", "60")),
            trino_identity_mode=os.environ.get("TRINO_IDENTITY_MODE", "jwt").lower(),
            trino_password=os.environ.get("TRINO_PASSWORD", ""),
            call_timeout_seconds=float(os.environ.get("MCP_CALL_TIMEOUT_SECONDS", "120")),
            tool_plugins=os.environ.get("MCP_TOOL_PLUGINS", ""),
            extensions=os.environ.get("MCP_EXTENSIONS", ""),
            jwt_leeway_seconds=int(os.environ.get("MCP_JWT_LEEWAY_SECONDS", "30")),
            resource_url=os.environ.get("MCP_RESOURCE_URL", ""),
            token_exchange_url=os.environ.get("TRINO_TOKEN_EXCHANGE_URL", ""),
            token_exchange_client_id=os.environ.get("TRINO_TOKEN_EXCHANGE_CLIENT_ID", ""),
            token_exchange_client_secret=os.environ.get("TRINO_TOKEN_EXCHANGE_CLIENT_SECRET", ""),
            token_exchange_audience=os.environ.get("TRINO_TOKEN_EXCHANGE_AUDIENCE", ""),
            token_exchange_scope=os.environ.get("TRINO_TOKEN_EXCHANGE_SCOPE", ""),
            token_exchange_verify=os.environ.get("TRINO_TOKEN_EXCHANGE_VERIFY", "true"),
            trino_client_cert=os.environ.get("TRINO_CLIENT_CERT", ""),
            trino_client_key=os.environ.get("TRINO_CLIENT_KEY", ""),
            max_catalogs=int(os.environ.get("MCP_MAX_CATALOGS", str(DEFAULT_MAX_CATALOGS))),
            describe_schema_tables=int(
                os.environ.get("MCP_DESCRIBE_SCHEMA_TABLES", str(DEFAULT_DESCRIBE_SCHEMA_TABLES))
            ),
            describe_schema_columns=int(
                os.environ.get("MCP_DESCRIBE_SCHEMA_COLUMNS", str(DEFAULT_DESCRIBE_SCHEMA_COLUMNS))
            ),
            search_min_length=int(
                os.environ.get("MCP_SEARCH_MIN_LENGTH", str(DEFAULT_SEARCH_MIN_LENGTH))
            ),
            search_reserve_seconds=float(
                os.environ.get("MCP_SEARCH_RESERVE_SECONDS", str(DEFAULT_SEARCH_RESERVE_SECONDS))
            ),
        )
