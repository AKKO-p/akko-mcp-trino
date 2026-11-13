"""Server configuration, read from the environment.

Nothing product-specific is hardcoded: the defaults here are neutral, and a
product built on this core injects its own values through the environment.
An extension reads its own settings from the environment it is given (see
`akko_mcp_trino.extensions`); this class holds the core's only.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from .audit import DEFAULT_AUDIT_MAX_NAMES
from .parse_pool import DEFAULT_PARSE_SECONDS, DEFAULT_PARSE_WORKERS
from .sql_guard import governed_catalog, parse_extra_reads, validate_identifier

# The token signature algorithms the server may accept: public-key ones only, verified
# against the issuer's JWKS.
JWT_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")


def parse_jwt_algorithms(text: str) -> list[str]:
    """The algorithms named in ``MCP_JWT_ALGORITHMS``, comma-separated, upper-cased, in
    order, once each; raises ValueError on none or on one outside ``JWT_ALGORITHMS``."""
    names: list[str] = []
    for raw in text.split(","):
        name = raw.strip().upper()
        if not name or name in names:
            continue
        if name not in JWT_ALGORITHMS:
            raise ValueError(
                f"MCP_JWT_ALGORITHMS: {raw.strip()!r} is not accepted; public-key "
                f"algorithms only: {', '.join(JWT_ALGORITHMS)}"
            )
        names.append(name)
    if not names:
        raise ValueError("MCP_JWT_ALGORITHMS is empty: name one algorithm at least, RS256 for one")
    return names


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
# The most words a search of `search_columns` takes, and the longest word: each word adds
# a few LIKE to each statement, so a text of thousands of words would make a statement too
# long for Trino and slow every schema.
DEFAULT_SEARCH_MAX_WORDS = 8
DEFAULT_SEARCH_MAX_WORD_LENGTH = 64
DEFAULT_SEARCH_RESERVE_SECONDS = 2.0
# The most names a list of the audit record may be set to keep (``MCP_AUDIT_MAX_NAMES``).
MAX_AUDIT_MAX_NAMES = 100000
# The catalogs one call of `search_columns` reads at once, and the most it may be set to:
# each is one metadata statement in flight in Trino, for that one call.
DEFAULT_SEARCH_CONCURRENCY = 8
MAX_SEARCH_CONCURRENCY = 32
# The reads of `search_columns` in flight in its threads in one process, all calls together,
# and the most it may be set to; a call that finds none free reads its catalogs itself.
DEFAULT_SEARCH_MAX_THREADS = 32
MAX_SEARCH_MAX_THREADS = 256
# The most columns of a table `describe_table` reads, page by page, and the most it may be
# set to: a page past the first reads DESCRIBE from the first column to the end of the page.
DEFAULT_DESCRIBE_MAX_COLUMNS = 5000
MAX_DESCRIBE_MAX_COLUMNS = 100000
# The most columns `profile_table` profiles in one call, and the rows its sample reads.
DEFAULT_PROFILE_MAX_COLUMNS = 50
# The most columns a server may let it profile in one call: each adds five references to the
# statement, which Trino and the read-only guard read whole.
MAX_PROFILE_MAX_COLUMNS = 500
DEFAULT_PROFILE_SAMPLE_ROWS = 100000
# The modes of `profile_table` there are, and those a server allows by default.
PROFILE_MODES = ("sample", "bernoulli", "full")
DEFAULT_PROFILE_MODES = ",".join(PROFILE_MODES)
# The characters of a text or binary min or max it serves, the rows of its DESCRIBE it reads,
# and the client tag of its aggregate query, by which Trino may put it in a resource group.
DEFAULT_PROFILE_VALUE_CHARS = 200
# The characters of a column's type it serves, and the most it may be set to: a nested row
# type can be written in thousands of characters.
DEFAULT_PROFILE_TYPE_CHARS = 500
MAX_PROFILE_TYPE_CHARS = 100000
DEFAULT_PROFILE_DESCRIBE_ROWS = 5000
DEFAULT_PROFILE_CLIENT_TAG = "mcp-profile"
# The calls of `profile_table` one process runs at once, and the most it may be set to: a
# call waiting in Trino holds one of the threads every tool shares.
DEFAULT_PROFILE_MAX_CONCURRENT = 4
MAX_PROFILE_MAX_CONCURRENT = 64
# What a client tag may hold: Trino splits the tags on commas.
_CLIENT_TAG_RE = re.compile(r"[A-Za-z0-9_.:=/-]{1,100}")
# The longest statement an agent may send, and the deepest its brackets may nest, both checked
# before the statement is parsed, and the least each may be set to: a cheap early refusal. The
# parse time grows with some nesting and repetition; the parse deadline bounds it
# (MCP_SQL_PARSE_SECONDS, MCP_SQL_PARSE_WORKERS).
DEFAULT_MAX_SQL_CHARS = 100000
MIN_MAX_SQL_CHARS = 1000
DEFAULT_MAX_SQL_NESTING = 32
MIN_MAX_SQL_NESTING = 8
# The least MCP_CONTEXT_RESERVE_SECONDS a server with a context provider accepts. Past its
# deadline, the chain waits up to 0.25 s for a provider to hand back control (its
# ``_GRACE_SECONDS``, which includes 0.1 s for a late read to end on its own, then 0.1 s for
# it to end once cancelled): a shorter reserve lets the context end past the call's own
# deadline, and the call then answers its time_budget error instead of the columns.
MIN_CONTEXT_RESERVE_SECONDS = 0.3


# What a claim the server reads may be called: a top-level claim, never a path.
_CLAIM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_:./#-]*")


def claim_names(value: str) -> tuple[str, ...]:
    """The claim names a comma-separated setting lists, each once, in order."""
    return tuple(dict.fromkeys(n.strip() for n in value.split(",") if n.strip()))


def provider_names(value: str) -> list[str]:
    """The names a ``MCP_CONTEXT_PROVIDERS`` value lists, in order."""
    return [n.strip() for n in value.split(",") if n.strip()]


def profile_mode_names(value: str) -> tuple[str, ...]:
    """The modes a ``MCP_PROFILE_MODES`` value lists, folded to lower case, once each, in
    order; known or not."""
    names = [n.strip().lower() for n in value.split(",") if n.strip()]
    return tuple(dict.fromkeys(names))


def _is_table_name(text: str) -> bool:
    """Whether ``text`` is ``catalog.schema.table``: three valid identifiers, the first
    outside the ``system`` catalog."""
    parts = text.split(".")
    if len(parts) != 3:
        return False
    try:
        governed_catalog(parts[0])
        validate_identifier(parts[1], "schema")
        validate_identifier(parts[2], "table")
    except ValueError:
        return False
    return True


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
    # The signature algorithms a caller's token may use, comma-separated: public-key
    # algorithms only (RS, PS, ES). A shared-secret algorithm (HS*) or `none` refuses to
    # start: the server verifies against the issuer's published public keys.
    jwt_algorithms: str = "RS256"
    # The claim of a caller's token that names the user, as the identity provider and the
    # access rules know them. `sub`, the default, is the OpenID Connect standard: the
    # stable, unique identifier the issuer gives the user. An issuer whose `sub` is an
    # opaque id the access rules do not know (Keycloak's UUID) names another claim
    # (`preferred_username`, `upn`, `uid`...). A token without it is refused.
    jwt_user_claim: str = "sub"
    # The claims that name the application that asked for the token, and the claims that
    # hold its scopes, comma-separated, in order: the first one present wins. `azp` (OpenID
    # Connect) then `client_id` (RFC 9068), and `scope` (RFC 6749 §3.3), by default; Entra ID
    # names `appid` and `scp`, Okta `cid` and `scp`.
    jwt_client_claims: str = "azp,client_id"
    jwt_scope_claims: str = "scope"
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
    # The longest statement execute_query and explain_query read, and the deepest its round
    # and square brackets may nest; past either, the statement is refused before it is parsed.
    max_sql_chars: int = DEFAULT_MAX_SQL_CHARS
    max_sql_nesting: int = DEFAULT_MAX_SQL_NESTING
    # The statements of execute_query and explain_query are parsed in this many worker
    # processes, each within this many seconds; past it, the worker is killed and the call
    # refused, and so is a call that finds no worker free within it.
    sql_parse_seconds: float = DEFAULT_PARSE_SECONDS
    sql_parse_workers: int = DEFAULT_PARSE_WORKERS
    # The names each list of a call's audit record keeps (its tables, the tables whose
    # description it served); the others are counted, once each, never written.
    audit_max_names: int = DEFAULT_AUDIT_MAX_NAMES
    # A page of `describe_schema`: at most this many tables, and whole tables only while
    # their columns add up to this many at most; the first table of a page comes whole,
    # however wide, and the answer says when a page was shortened.
    describe_schema_tables: int = DEFAULT_DESCRIBE_SCHEMA_TABLES
    describe_schema_columns: int = DEFAULT_DESCRIBE_SCHEMA_COLUMNS
    describe_max_columns: int = DEFAULT_DESCRIBE_MAX_COLUMNS
    # `search_columns`: the shortest word it searches (shorter words are ignored), so that
    # a letter or two does not read every column, and the seconds of the call's budget it
    # keeps in reserve, so that it stops early enough to answer what it searched rather
    # than the call's own error.
    search_min_length: int = DEFAULT_SEARCH_MIN_LENGTH
    # `search_columns`: the most words a text may hold, and the longest word; above either,
    # the search is refused before Trino is asked anything.
    search_max_words: int = DEFAULT_SEARCH_MAX_WORDS
    search_max_word_length: int = DEFAULT_SEARCH_MAX_WORD_LENGTH
    search_reserve_seconds: float = DEFAULT_SEARCH_RESERVE_SECONDS
    search_concurrency: int = DEFAULT_SEARCH_CONCURRENCY
    search_max_threads: int = DEFAULT_SEARCH_MAX_THREADS
    # Read commands the allow-list refuses by default, named one by one by the
    # administrator (comma-separated: EXPLAIN, SHOW STATS, SHOW CREATE, SHOW GRANTS,
    # SHOW ROLES, SHOW SESSION). They can show facts beyond a row filter or a column
    # mask. Empty, the default,
    # opens none, and explain_query checks a statement without printing its plan.
    extra_read_commands: str = ""
    # Context from a catalogue (entry point group `akko_mcp_trino.context`): the providers
    # to load, by name, comma-separated; empty, the default, loads none and starts nothing.
    context_providers: str = ""
    # One context read: its time budget, the part of a tool call's budget kept in reserve,
    # the most reads in flight, the delays before a source that is down or that refused the
    # account is called again, the tables of one batch and the lifetime of a cached read.
    context_timeout_seconds: float = 5.0
    context_reserve_seconds: float = 0.5
    context_max_concurrency: int = 32
    context_retry_after_seconds: float = 30.0
    context_refused_retry_seconds: float = 3600.0
    context_batch: int = 50
    context_ttl_seconds: float = 900.0
    # The JSON length the caches of tables and of glossary terms may hold, in all.
    context_cache_bytes: int = 134217728
    context_terms_cache_bytes: int = 16777216
    # What Trino listed for a caller: how long it is kept, the JSON length of the entries it
    # may hold and the most names kept for one caller.
    visibility_ttl_seconds: float = 60.0
    visibility_cache_bytes: int = 33554432
    visibility_max_names: int = 10000
    context_require_select: bool = False
    # One table, `catalog.schema.table`, that no caller of this server may see: context
    # is served to a caller only while Trino hides it from them.
    context_canary: str = ""
    # Bounds on what context adds to a response.
    context_text_chars: int = 500
    context_response_chars: int = 60000
    context_max_columns: int = 500
    profile_max_columns: int = DEFAULT_PROFILE_MAX_COLUMNS
    profile_sample_rows: int = DEFAULT_PROFILE_SAMPLE_ROWS
    profile_modes: str = DEFAULT_PROFILE_MODES
    profile_value_chars: int = DEFAULT_PROFILE_VALUE_CHARS
    profile_type_chars: int = DEFAULT_PROFILE_TYPE_CHARS
    profile_describe_rows: int = DEFAULT_PROFILE_DESCRIBE_ROWS
    profile_client_tag: str = DEFAULT_PROFILE_CLIENT_TAG
    # The time budget of the aggregate query of `profile_table`; 0: the smallest of the others.
    profile_timeout_seconds: float = 0.0
    profile_max_concurrent: int = DEFAULT_PROFILE_MAX_CONCURRENT
    # The raw `MCP_VISIBILITY_ENABLED`, kept only to refuse `false`: the filter by Trino's
    # rights cannot be turned off.
    visibility_switch: str = ""

    def client_claims(self) -> tuple[str, ...]:
        """The claims that name a token's client, in order (``MCP_JWT_CLIENT_CLAIMS``)."""
        return claim_names(self.jwt_client_claims)

    def scope_claims(self) -> tuple[str, ...]:
        """The claims that hold a token's scopes, in order (``MCP_JWT_SCOPE_CLAIMS``)."""
        return claim_names(self.jwt_scope_claims)

    def allowed_profile_modes(self) -> tuple[str, ...]:
        """The modes of ``profile_table`` this server allows (``MCP_PROFILE_MODES``)."""
        return profile_mode_names(self.profile_modes)

    def profile_budget_seconds(self) -> float:
        """The time budget of the aggregate query of ``profile_table``: the smallest of
        ``MCP_PROFILE_TIMEOUT_SECONDS``, ``TRINO_QUERY_TIMEOUT_SECONDS`` and
        ``MCP_CALL_TIMEOUT_SECONDS`` above 0; 0 when none is."""
        budgets = (
            self.profile_timeout_seconds,
            self.query_timeout_seconds,
            self.call_timeout_seconds,
        )
        return min((b for b in budgets if b > 0), default=0.0)

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
        if self.max_sql_chars < MIN_MAX_SQL_CHARS:
            found.append(
                f"MCP_MAX_SQL_CHARS={self.max_sql_chars}: a statement must be allowed "
                f"{MIN_MAX_SQL_CHARS} characters or more"
            )
        if self.max_sql_nesting < MIN_MAX_SQL_NESTING:
            found.append(
                f"MCP_MAX_SQL_NESTING={self.max_sql_nesting}: a statement must be allowed "
                f"{MIN_MAX_SQL_NESTING} nested brackets or more"
            )
        if not self.sql_parse_seconds > 0:
            found.append(
                f"MCP_SQL_PARSE_SECONDS={self.sql_parse_seconds:g}: the parse of a statement "
                "must be given more than 0 seconds"
            )
        if self.sql_parse_workers < 1:
            found.append(
                f"MCP_SQL_PARSE_WORKERS={self.sql_parse_workers}: statements are parsed by 1 "
                "worker process or more"
            )
        if not 1 <= self.audit_max_names <= MAX_AUDIT_MAX_NAMES:
            found.append(
                f"MCP_AUDIT_MAX_NAMES={self.audit_max_names}: an audit line keeps 1 to "
                f"{MAX_AUDIT_MAX_NAMES} names a list"
            )
        if not 1 <= self.describe_max_columns <= MAX_DESCRIBE_MAX_COLUMNS:
            found.append(
                f"MCP_DESCRIBE_MAX_COLUMNS={self.describe_max_columns}: describe_table reads "
                f"1 to {MAX_DESCRIBE_MAX_COLUMNS} columns of a table"
            )
        if not 1 <= self.search_concurrency <= MAX_SEARCH_CONCURRENCY:
            found.append(
                f"MCP_SEARCH_CONCURRENCY={self.search_concurrency}: search_columns reads 1 to "
                f"{MAX_SEARCH_CONCURRENCY} catalogs at once"
            )
        if not 1 <= self.search_max_threads <= MAX_SEARCH_MAX_THREADS:
            found.append(
                f"MCP_SEARCH_MAX_THREADS={self.search_max_threads}: search_columns runs 1 to "
                f"{MAX_SEARCH_MAX_THREADS} reads at once in a process"
            )
        if self.search_min_length < 1:
            found.append(
                f"MCP_SEARCH_MIN_LENGTH={self.search_min_length}: "
                "search_columns must ask for 1 character or more"
            )
        if self.search_max_words < 1:
            found.append(
                f"MCP_SEARCH_MAX_WORDS={self.search_max_words}: "
                "search_columns must take 1 word or more"
            )
        if self.search_max_word_length < self.search_min_length:
            found.append(
                f"MCP_SEARCH_MAX_WORD_LENGTH={self.search_max_word_length} is below "
                f"MCP_SEARCH_MIN_LENGTH={self.search_min_length}: no word could be searched"
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
        try:
            parse_extra_reads(self.extra_read_commands)
        except ValueError as exc:
            found.append(str(exc))
        try:
            parse_jwt_algorithms(self.jwt_algorithms)
        except ValueError as exc:
            found.append(str(exc))
        for setting, claims in (
            ("MCP_JWT_CLIENT_CLAIMS", self.jwt_client_claims),
            ("MCP_JWT_SCOPE_CLAIMS", self.jwt_scope_claims),
        ):
            names = claim_names(claims)
            if not names or not all(_CLAIM_NAME.fullmatch(n) for n in names):
                found.append(
                    f"{setting}={claims!r}: name one claim at least, comma-separated, each a "
                    "top-level claim of letters, digits and _ : . / # -"
                )
        if not _CLAIM_NAME.fullmatch(self.jwt_user_claim):
            found.append(
                f"MCP_JWT_USER_CLAIM={self.jwt_user_claim!r}: a claim name is a top-level claim, "
                "letters, digits and _ : . / # - (sub, upn, preferred_username, "
                "https://example.com/uid)"
            )
        for name, value in (
            ("TRINO_QUERY_TIMEOUT_SECONDS", self.query_timeout_seconds),
            ("MCP_CALL_TIMEOUT_SECONDS", self.call_timeout_seconds),
        ):
            if value < 0:
                found.append(f"{name}={value:g}: a time budget is 0 (none) or more")
        found.extend(self._context_problems())
        return found

    def _context_problems(self) -> list[str]:
        found = []
        if self.visibility_switch.strip().lower() == "false":
            found.append(
                "MCP_VISIBILITY_ENABLED was removed in 0.8: the filter by Trino's rights "
                "cannot be turned off"
            )
        if self.context_timeout_seconds <= 0:
            found.append(
                f"MCP_CONTEXT_TIMEOUT_SECONDS={self.context_timeout_seconds:g}: "
                "a context read needs more than 0 seconds"
            )
        if self.context_reserve_seconds < 0:
            found.append(
                f"MCP_CONTEXT_RESERVE_SECONDS={self.context_reserve_seconds:g}: "
                "a reserve is 0 seconds or more"
            )
        elif (
            provider_names(self.context_providers)
            and self.context_reserve_seconds < MIN_CONTEXT_RESERVE_SECONDS
        ):
            found.append(
                f"MCP_CONTEXT_RESERVE_SECONDS={self.context_reserve_seconds:g}: with a context "
                "provider, part of each call's budget must be kept back so that the context "
                f"ends before the call does; set {MIN_CONTEXT_RESERVE_SECONDS:g} or more"
            )
        for name, value in (
            ("MCP_CONTEXT_RETRY_AFTER_SECONDS", self.context_retry_after_seconds),
            ("MCP_CONTEXT_REFUSED_RETRY_SECONDS", self.context_refused_retry_seconds),
        ):
            if value < 0:
                found.append(f"{name}={value:g}: a delay is 0 seconds or more")
        for name, value in (
            ("MCP_CONTEXT_TTL_SECONDS", self.context_ttl_seconds),
            ("MCP_VISIBILITY_TTL_SECONDS", self.visibility_ttl_seconds),
        ):
            if value < 0:
                found.append(f"{name}={value:g}: a lifetime is 0 seconds or more")
        for name, count in (
            ("MCP_CONTEXT_MAX_CONCURRENCY", self.context_max_concurrency),
            ("MCP_CONTEXT_BATCH", self.context_batch),
            ("MCP_CONTEXT_CACHE_BYTES", self.context_cache_bytes),
            ("MCP_CONTEXT_TERMS_CACHE_BYTES", self.context_terms_cache_bytes),
            ("MCP_VISIBILITY_CACHE_BYTES", self.visibility_cache_bytes),
            ("MCP_VISIBILITY_MAX_NAMES", self.visibility_max_names),
            ("MCP_CONTEXT_TEXT_CHARS", self.context_text_chars),
            ("MCP_CONTEXT_RESPONSE_CHARS", self.context_response_chars),
            ("MCP_CONTEXT_MAX_COLUMNS", self.context_max_columns),
            ("MCP_PROFILE_MAX_COLUMNS", self.profile_max_columns),
            ("MCP_PROFILE_SAMPLE_ROWS", self.profile_sample_rows),
            ("MCP_PROFILE_VALUE_CHARS", self.profile_value_chars),
            ("MCP_PROFILE_DESCRIBE_ROWS", self.profile_describe_rows),
        ):
            if count < 1:
                found.append(f"{name}={count}: a bound is 1 or more")
        if self.profile_max_columns > MAX_PROFILE_MAX_COLUMNS:
            found.append(
                f"MCP_PROFILE_MAX_COLUMNS={self.profile_max_columns}: profile_table profiles "
                f"{MAX_PROFILE_MAX_COLUMNS} columns at most"
            )
        if not 1 <= self.profile_type_chars <= MAX_PROFILE_TYPE_CHARS:
            found.append(
                f"MCP_PROFILE_TYPE_CHARS={self.profile_type_chars}: profile_table serves 1 to "
                f"{MAX_PROFILE_TYPE_CHARS} characters of a type"
            )
        if not 1 <= self.profile_max_concurrent <= MAX_PROFILE_MAX_CONCURRENT:
            found.append(
                f"MCP_PROFILE_MAX_CONCURRENT={self.profile_max_concurrent}: profile_table runs "
                f"1 to {MAX_PROFILE_MAX_CONCURRENT} calls at once"
            )
        modes = self.allowed_profile_modes()
        if not modes or not set(modes) <= set(PROFILE_MODES):
            found.append(
                f"MCP_PROFILE_MODES={self.profile_modes!r}: name one mode at least, "
                "comma-separated, among sample, bernoulli and full"
            )
        if not _CLIENT_TAG_RE.fullmatch(self.profile_client_tag):
            found.append(
                f"MCP_PROFILE_CLIENT_TAG={self.profile_client_tag!r}: a client tag is 1 to "
                "100 letters, digits and _ . : = / -, without a comma"
            )
        budget = self.profile_timeout_seconds
        if not math.isfinite(budget) or budget < 0:
            found.append(
                f"MCP_PROFILE_TIMEOUT_SECONDS={budget:g}: a time budget is 0 (the call's) "
                "or a number of seconds above 0"
            )
        elif 0 < self.call_timeout_seconds < budget:
            found.append(
                f"MCP_PROFILE_TIMEOUT_SECONDS={budget:g} is past "
                f"MCP_CALL_TIMEOUT_SECONDS={self.call_timeout_seconds:g}: the aggregate query "
                f"runs within its call; set it to {self.call_timeout_seconds:g} or less, or 0 "
                "for the call's budget"
            )
        elif 0 < self.query_timeout_seconds < budget:
            found.append(
                f"MCP_PROFILE_TIMEOUT_SECONDS={budget:g} is past "
                f"TRINO_QUERY_TIMEOUT_SECONDS={self.query_timeout_seconds:g}: the aggregate "
                f"query is a Trino query; set it to {self.query_timeout_seconds:g} or less, or "
                "raise TRINO_QUERY_TIMEOUT_SECONDS"
            )
        if self.context_canary and not _is_table_name(self.context_canary):
            found.append(
                f"MCP_CONTEXT_CANARY={self.context_canary!r}: name one table as "
                "catalog.schema.table, three identifiers (letters, digits, _ and -), "
                "outside the system catalog"
            )
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
        try:
            extra = sorted(parse_extra_reads(self.extra_read_commands))
        except ValueError:
            extra = []  # a problem, said by problems()
        if extra:
            found.append(
                f"MCP_EXTRA_READ_COMMANDS={', '.join(extra)}: these reads can show facts "
                "beyond a row filter or a column mask"
            )
        if self.describe_max_columns > self.profile_describe_rows:
            found.append(
                f"MCP_DESCRIBE_MAX_COLUMNS={self.describe_max_columns} is above "
                f"MCP_PROFILE_DESCRIBE_ROWS={self.profile_describe_rows}: describe_table shows "
                "columns profile_table cannot name"
            )
        if "full" in self.allowed_profile_modes() and self.profile_budget_seconds() == 0:
            found.append(
                "MCP_PROFILE_MODES allows full while MCP_PROFILE_TIMEOUT_SECONDS, "
                "TRINO_QUERY_TIMEOUT_SECONDS and MCP_CALL_TIMEOUT_SECONDS are 0: a full profile "
                "reads every row it may read with no time limit"
            )
        if provider_names(self.context_providers):
            if not self.context_canary:
                found.append(
                    "MCP_CONTEXT_CANARY is not set: nothing checks that Trino filters its "
                    "listings, and context follows what Trino lists"
                )
            if self.trino_identity_mode == "service":
                found.append("context visibility is the service account's")
        return found

    @staticmethod
    def from_env(*, server_name: str = "trino-mcp") -> "Config":
        """Read every setting from the environment."""
        tag = os.environ.get("MCP_PROFILE_CLIENT_TAG", DEFAULT_PROFILE_CLIENT_TAG)
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
            extra_read_commands=os.environ.get("MCP_EXTRA_READ_COMMANDS", ""),
            jwt_algorithms=os.environ.get("MCP_JWT_ALGORITHMS", "RS256"),
            jwt_user_claim=os.environ.get("MCP_JWT_USER_CLAIM", "sub").strip() or "sub",
            jwt_client_claims=os.environ.get("MCP_JWT_CLIENT_CLAIMS", "azp,client_id"),
            jwt_scope_claims=os.environ.get("MCP_JWT_SCOPE_CLAIMS", "scope"),
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
            max_sql_chars=int(os.environ.get("MCP_MAX_SQL_CHARS", str(DEFAULT_MAX_SQL_CHARS))),
            max_sql_nesting=int(
                os.environ.get("MCP_MAX_SQL_NESTING", str(DEFAULT_MAX_SQL_NESTING))
            ),
            sql_parse_seconds=float(
                os.environ.get("MCP_SQL_PARSE_SECONDS", str(DEFAULT_PARSE_SECONDS))
            ),
            sql_parse_workers=int(
                os.environ.get("MCP_SQL_PARSE_WORKERS", str(DEFAULT_PARSE_WORKERS))
            ),
            audit_max_names=int(
                os.environ.get("MCP_AUDIT_MAX_NAMES", str(DEFAULT_AUDIT_MAX_NAMES))
            ),
            describe_schema_tables=int(
                os.environ.get("MCP_DESCRIBE_SCHEMA_TABLES", str(DEFAULT_DESCRIBE_SCHEMA_TABLES))
            ),
            describe_schema_columns=int(
                os.environ.get("MCP_DESCRIBE_SCHEMA_COLUMNS", str(DEFAULT_DESCRIBE_SCHEMA_COLUMNS))
            ),
            describe_max_columns=int(
                os.environ.get("MCP_DESCRIBE_MAX_COLUMNS", str(DEFAULT_DESCRIBE_MAX_COLUMNS))
            ),
            search_min_length=int(
                os.environ.get("MCP_SEARCH_MIN_LENGTH", str(DEFAULT_SEARCH_MIN_LENGTH))
            ),
            search_max_words=int(
                os.environ.get("MCP_SEARCH_MAX_WORDS", str(DEFAULT_SEARCH_MAX_WORDS))
            ),
            search_max_word_length=int(
                os.environ.get("MCP_SEARCH_MAX_WORD_LENGTH", str(DEFAULT_SEARCH_MAX_WORD_LENGTH))
            ),
            search_reserve_seconds=float(
                os.environ.get("MCP_SEARCH_RESERVE_SECONDS", str(DEFAULT_SEARCH_RESERVE_SECONDS))
            ),
            search_concurrency=int(
                os.environ.get("MCP_SEARCH_CONCURRENCY", str(DEFAULT_SEARCH_CONCURRENCY))
            ),
            search_max_threads=int(
                os.environ.get("MCP_SEARCH_MAX_THREADS", str(DEFAULT_SEARCH_MAX_THREADS))
            ),
            context_providers=os.environ.get("MCP_CONTEXT_PROVIDERS", ""),
            context_timeout_seconds=float(os.environ.get("MCP_CONTEXT_TIMEOUT_SECONDS", "5")),
            context_reserve_seconds=float(os.environ.get("MCP_CONTEXT_RESERVE_SECONDS", "0.5")),
            context_max_concurrency=int(os.environ.get("MCP_CONTEXT_MAX_CONCURRENCY", "32")),
            context_retry_after_seconds=float(
                os.environ.get("MCP_CONTEXT_RETRY_AFTER_SECONDS", "30")
            ),
            context_refused_retry_seconds=float(
                os.environ.get("MCP_CONTEXT_REFUSED_RETRY_SECONDS", "3600")
            ),
            context_batch=int(os.environ.get("MCP_CONTEXT_BATCH", "50")),
            context_ttl_seconds=float(os.environ.get("MCP_CONTEXT_TTL_SECONDS", "900")),
            context_cache_bytes=int(os.environ.get("MCP_CONTEXT_CACHE_BYTES", "134217728")),
            context_terms_cache_bytes=int(
                os.environ.get("MCP_CONTEXT_TERMS_CACHE_BYTES", "16777216")
            ),
            visibility_ttl_seconds=float(os.environ.get("MCP_VISIBILITY_TTL_SECONDS", "60")),
            visibility_cache_bytes=int(os.environ.get("MCP_VISIBILITY_CACHE_BYTES", "33554432")),
            visibility_max_names=int(os.environ.get("MCP_VISIBILITY_MAX_NAMES", "10000")),
            context_require_select=os.environ.get("MCP_CONTEXT_REQUIRE_SELECT", "false").lower()
            == "true",
            context_canary=os.environ.get("MCP_CONTEXT_CANARY", "").strip(),
            context_text_chars=int(os.environ.get("MCP_CONTEXT_TEXT_CHARS", "500")),
            context_response_chars=int(os.environ.get("MCP_CONTEXT_RESPONSE_CHARS", "60000")),
            context_max_columns=int(os.environ.get("MCP_CONTEXT_MAX_COLUMNS", "500")),
            profile_max_columns=int(
                os.environ.get("MCP_PROFILE_MAX_COLUMNS", str(DEFAULT_PROFILE_MAX_COLUMNS))
            ),
            profile_sample_rows=int(
                os.environ.get("MCP_PROFILE_SAMPLE_ROWS", str(DEFAULT_PROFILE_SAMPLE_ROWS))
            ),
            profile_modes=os.environ.get("MCP_PROFILE_MODES", DEFAULT_PROFILE_MODES),
            profile_value_chars=int(
                os.environ.get("MCP_PROFILE_VALUE_CHARS", str(DEFAULT_PROFILE_VALUE_CHARS))
            ),
            profile_type_chars=int(
                os.environ.get("MCP_PROFILE_TYPE_CHARS", str(DEFAULT_PROFILE_TYPE_CHARS))
            ),
            profile_describe_rows=int(
                os.environ.get("MCP_PROFILE_DESCRIBE_ROWS", str(DEFAULT_PROFILE_DESCRIBE_ROWS))
            ),
            profile_client_tag=tag.strip(),
            profile_timeout_seconds=float(os.environ.get("MCP_PROFILE_TIMEOUT_SECONDS", "0")),
            profile_max_concurrent=int(
                os.environ.get("MCP_PROFILE_MAX_CONCURRENT", str(DEFAULT_PROFILE_MAX_CONCURRENT))
            ),
            visibility_switch=os.environ.get("MCP_VISIBILITY_ENABLED", ""),
        )
