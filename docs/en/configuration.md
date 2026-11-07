# Configuration

**English** · [Français](../fr/configuration.md)

The server reads every setting from the environment; nothing is hardcoded. An extension reads its
own variables from the same environment. `akko-mcp-trino --check` prints the effective
configuration, never a secret. A "startup error" below means the server refuses to start and
says why.

## Trino

| Variable | Meaning | Default |
|---|---|---|
| `TRINO_HOST`, `TRINO_PORT` | the coordinator | `localhost`, `8080` |
| `TRINO_USER` | the server's own Trino account. In `service` mode, every query runs as this account; in `impersonation` mode, it authenticates and impersonates the caller; ignored in `oauth2` mode | `trino` |
| `TRINO_PASSWORD` | its password, sent with Basic authentication; refused over `http`. `--check` says whether one is set, never which | none |
| `TRINO_CLIENT_CERT`, `TRINO_CLIENT_KEY` | a client certificate and its key (PEM paths) the account authenticates with instead (mTLS); https only | none |
| `TRINO_CATALOG` | the session catalog, where a name without a catalog is read. Set it to a catalog that holds your data, such as `iceberg`: with `system`, such names are refused | `system` |
| `TRINO_READ_ONLY` | refuse writes in `execute_query` | `true` |
| `MCP_EXTRA_READ_COMMANDS` | read commands refused by default, enabled one by one, comma-separated: `EXPLAIN` (also enables the plan in `explain_query`), `SHOW STATS`, `SHOW CREATE`, `SHOW GRANTS`, `SHOW ROLES` (with `SHOW CURRENT ROLES` and `SHOW ROLE GRANTS`), `SHOW SESSION`. Read [Security](security.md#the-statements-an-agent-may-send) before enabling one. Announced at every start and to every host; an unknown name is a startup error | none |
| `TRINO_SOURCE` | the source Trino records for every query (`X-Trino-Source`) | the server name |
| `TRINO_HTTP_SCHEME` | `http` or `https`; a password is never sent over `http` | `http` |
| `TRINO_VERIFY` | TLS verification: `true`, `false`, or the path of a CA bundle to check Trino's TLS chain against | `true` |
| `TRINO_REQUEST_TIMEOUT_SECONDS` | timeout of one HTTP request of the Trino client | `30` |
| `TRINO_IDENTITY_MODE` | who the server is for Trino: `jwt`, `impersonation`, `oauth2` or `service` (see [Identity](identity.md)). An extension may add others; an unknown mode is a startup error | `jwt` |
| `TRINO_TOKEN_EXCHANGE_URL` | `jwt`: the identity provider's token endpoint (https only) | none (required in `jwt`) |
| `TRINO_TOKEN_EXCHANGE_CLIENT_ID`, `TRINO_TOKEN_EXCHANGE_CLIENT_SECRET` | `jwt`: this server's confidential client at the identity provider, sent with Basic authentication. `--check` never prints the secret | none (required in `jwt`) |
| `TRINO_TOKEN_EXCHANGE_AUDIENCE` | `jwt`: the audience of the token requested for Trino, the one Trino's `required-audience` names; never an audience this server accepts | none (required in `jwt`) |
| `TRINO_TOKEN_EXCHANGE_SCOPE` | `jwt`: scopes to request with this token, space-separated | none |
| `TRINO_TOKEN_EXCHANGE_VERIFY` | TLS verification of the token endpoint: `true`, `false`, or a CA bundle path | `true` |

## Bounds and time budgets

| Variable | Meaning | Default |
|---|---|---|
| `TRINO_MAX_ROWS` | result cap, at least 1. The server reads one more row to say whether the answer was `truncated`. Also the size of one page of a listing | `100` |
| `TRINO_QUERY_TIMEOUT_SECONDS` | time budget of one query: past it, the server cancels the query in Trino. `0` leaves the limit to Trino | `60` |
| `MCP_CALL_TIMEOUT_SECONDS` | time budget of one tool call, every query of the call included. Past it, no query starts, the running query is cancelled and the agent gets an error. `0` sets no budget | `120` |

## Listings and search

| Variable | Meaning | Default |
|---|---|---|
| `MCP_MAX_CATALOGS` | maximum number of catalogs `list_catalogs` reads before paging, `system` excluded; at least 1. Above it, the tool returns an error, never a truncated list | `10000` |
| `MCP_DESCRIBE_SCHEMA_TABLES` | maximum number of tables in one page of `describe_schema`, from 1 to 100. A page reads each of its tables through its own branch, so its cost grows with the page | `20` |
| `MCP_DESCRIBE_SCHEMA_COLUMNS` | maximum number of columns in one page of `describe_schema`, at least 1. The first table of a page comes whole however wide, and a page shortened for this budget says so | `1000` |
| `MCP_DESCRIBE_MAX_COLUMNS` | maximum number of columns of a table that `describe_table` reads, page by page, from 1 to 100000. Past it, the answer says `columns_cut` | `5000` |
| `MCP_SEARCH_MIN_LENGTH` | shortest word `search_columns` searches, at least 1. Shorter words are ignored, and a text with none left is refused | `3` |
| `MCP_SEARCH_MAX_WORDS` | maximum number of words in a `search_columns` text, at least 1. Above it, the search is refused before Trino is called | `8` |
| `MCP_SEARCH_MAX_WORD_LENGTH` | longest word `search_columns` accepts, not below `MCP_SEARCH_MIN_LENGTH`. A longer word is refused before Trino is called | `64` |
| `MCP_SEARCH_CONCURRENCY` | the catalogs one call of `search_columns` reads at once, from 1 to 32: as many metadata statements in flight in Trino for that call. Raising it past 8 mostly adds metadata load on Trino, with little gain. | `8` |
| `MCP_SEARCH_MAX_THREADS` | maximum number of `search_columns` reads in flight in the threads of one process, all calls together, from 1 to 256. A call that finds no free thread reads its next catalog itself | `32` |
| `MCP_SEARCH_RESERVE_SECONDS` | seconds of the call's budget that `search_columns` keeps in reserve, so that it returns what it searched before the call runs out. 0 or more, below `MCP_CALL_TIMEOUT_SECONDS` when that is set. `0` triggers a warning | `2` |

## Identity

| Variable | Meaning | Default |
|---|---|---|
| `MCP_AUTH_ENABLED` | verify the callers' bearer tokens (token verification) | `true` |
| `MCP_AUTH_REQUIRED` | refuse with `401` a request without a valid token. In `jwt` and `impersonation` modes, the server refuses to start without it. In `oauth2` mode, it adds an MCP token check and never disables the Trino login | `true` |
| `MCP_JWKS_URL` | the identity provider's JWKS endpoint | none (required when authentication is enabled) |
| `MCP_OIDC_ISSUER` | the issuer a token must carry | none (required when authentication is enabled: empty, the server refuses to start) |
| `MCP_OIDC_AUDIENCE` | the audience a token must carry: the one the identity provider gives tokens issued for this server (RFC 8707); never Trino's | none (this or `MCP_RESOURCE_URL` is required when authentication is enabled) |
| `MCP_RESOURCE_URL` | this server's canonical URL, such as `https://mcp.example.com/mcp`. It is accepted as an audience, and named in the protected resource metadata the server then publishes (RFC 9728) and in the challenge of a `401` | none |
| `MCP_JWT_LEEWAY_SECONDS` | tolerance on `exp`, `nbf` and `iat` for clock drift between the issuer and this server | `30` |
| `MCP_JWT_USER_CLAIM` | the claim that names the user, as your access rules know it. Keycloak, Entra ID, Okta and Dex put an opaque id in `sub`: name another claim, such as `preferred_username`, `upn` or `uid`. A token without the claim is refused; no other claim is tried | `sub` |
| `MCP_JWT_ALGORITHMS` | the signature algorithms a caller's token may use, comma-separated, among `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES384` and `ES512` (for example `RS256,RS512` for an identity provider that signs with either). A shared-secret algorithm (such as `HS256`) or `none` is a startup error | `RS256` |
| `MCP_JWT_CLIENT_CLAIMS`, `MCP_JWT_SCOPE_CLAIMS` | the claims that name the application that asked for a token, and those that hold its scopes, comma-separated, in order: the first one the token carries wins. A scope claim is a space-delimited string or a list of strings; any other value means no scope. Keycloak: the defaults; Entra ID: `azp` or `appid` depending on the token version, and `scp`; Okta: `cid` and `scp` (see [Modules](modules.md)) | `azp,client_id`, `scope` |
| `MCP_ALLOWED_HOSTS` | Host header filter (the SDK's DNS rebinding protection): `host` or `host:port` entries, comma-separated, `host:*` for any port (the only wildcard). Once the filter is on, a request that carries an `Origin` header is refused (403). Empty serves any Host | none (off) |

[Identity](identity.md#token-verification) says which combinations stop the server from starting.

## Serving

| Variable | Meaning | Default |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http`, `sse` or `stdio` | `streamable-http` |
| `MCP_PORT`, `MCP_HEALTH_PORT` | listening ports, HTTP transports only | `3000`, `3001` |
| `MCP_SERVER_NAME` | name announced to hosts | `trino-mcp` |
| `MCP_USER_TOKEN` | stdio only: the user's access token, verified like a bearer header | none (required when `MCP_AUTH_REQUIRED=true`) |

## Audit

| Variable | Meaning | Default |
|---|---|---|
| `MCP_AUDIT_MAX_NAMES` | the names each list of a call's audit record keeps (its tables, the tables whose description it served), from 1 to 100000. The others are counted, once each, never written | `200` |

## Plugins and extensions

| Variable | Meaning | Default |
|---|---|---|
| `MCP_TOOL_PLUGINS` | comma-separated names of registrars installed under the entry point group `akko_mcp_trino.tools` | none |
| `MCP_EXTENSIONS` | comma-separated names of extensions installed under the entry point group `akko_mcp_trino.extensions`, the [modules](modules.md) included | none |

## Modules

| Variable | Meaning | Default |
|---|---|---|
| `MCP_ALLOWED_CLIENTS` | `token-policy`: the clients a token may name, separated by commas or spaces | none: any client |
| `MCP_REQUIRED_SCOPES` | `token-policy`: the scopes a token must carry, every one, separated by commas or spaces | none |

## Profiling

| Variable | Meaning | Default |
|---|---|---|
| `MCP_PROFILE_MAX_COLUMNS`, `MCP_PROFILE_SAMPLE_ROWS` | maximum number of columns `profile_table` profiles in one call (1 to 500), and the rows its `sample` mode reads (at least 1) | `50`, `100000` |
| `MCP_PROFILE_MODES` | the modes `profile_table` allows, comma-separated, among `sample`, `bernoulli` and `full`. Empty or an unknown name is a startup error | `sample,bernoulli,full` |
| `MCP_PROFILE_VALUE_CHARS` | the characters of a text `min` or `max` it returns, or the bytes of a binary one. Trino reads one more and no further, and a longer value is cut. At least 1 | `200` |
| `MCP_PROFILE_TYPE_CHARS` | the characters of a column's type it returns, from 1 to 100000. A longer type is cut, with `"type"` in `cut`. `MCP_PROFILE_VALUE_CHARS` never cuts a type | `500` |
| `MCP_PROFILE_DESCRIBE_ROWS` | the rows of its `DESCRIBE` it reads, past the row cap, at least 1. A column past them cannot be named | `5000` |
| `MCP_PROFILE_CLIENT_TAG` | the client tag of its aggregate query (not of its `DESCRIBE`), for a Trino resource group: 1 to 100 letters, digits and `_ . : = / -` | `mcp-profile` |
| `MCP_PROFILE_TIMEOUT_SECONDS` | time budget of the aggregate query; Trino cancels the query when it runs out. The effective budget is the smallest non-zero value among this setting, `TRINO_QUERY_TIMEOUT_SECONDS` and `MCP_CALL_TIMEOUT_SECONDS`. `0` means "use the other two". A non-zero value must not exceed either of them when they are set. `--check` prints the effective budget | `0` |
| `MCP_PROFILE_MAX_CONCURRENT` | the `profile_table` calls one server process runs at once, from 1 to 64. Past them, a call immediately returns `"kind": "busy"` and sends no query | `4` |

## Context from a data catalog

| Variable | Meaning | Default |
|---|---|---|
| `MCP_CONTEXT_PROVIDERS` | comma-separated names of providers installed under the entry point group `akko_mcp_trino.context`; an unknown name is a startup error | none |
| `MCP_CONTEXT_TIMEOUT_SECONDS` | time budget of one context read, above 0 | `5` |
| `MCP_CONTEXT_RESERVE_SECONDS` | seconds of a tool call's budget kept back, so that the context ends before the call does. At least 0.3 when a provider is loaded (the server refuses less), 0 or more otherwise | `0.5` |
| `MCP_CONTEXT_MAX_CONCURRENCY` | maximum number of context reads in flight for one provider, at least 1 | `32` |
| `MCP_CONTEXT_RETRY_AFTER_SECONDS` | delay before a source that is down is called again, 0 or more | `30` |
| `MCP_CONTEXT_REFUSED_RETRY_SECONDS` | delay before a source that refused the account is called again (`SIGHUP` lifts it at once), 0 or more | `3600` |
| `MCP_CONTEXT_BATCH` | the tables one read of a source holds, at least 1 | `50` |
| `MCP_CONTEXT_TTL_SECONDS` | how long a context read is kept, 0 or more | `900` |
| `MCP_CONTEXT_CACHE_BYTES`, `MCP_CONTEXT_TERMS_CACHE_BYTES` | the limits of the caches of tables and of glossary terms, at least 1. Each limit counts the length of the JSON form of the cached entries. The process holds about three to four times that in memory, so size the pod for it | `134217728`, `16777216` |
| `MCP_VISIBILITY_TTL_SECONDS` | how long what Trino listed for a caller is kept, 0 or more | `60` |
| `MCP_VISIBILITY_CACHE_BYTES`, `MCP_VISIBILITY_MAX_NAMES` | the limit of that cache, and the maximum number of names it keeps for one caller, at least 1. Each entry counts its key as well as its value. Measured with fresh strings, the process holds 3.6 to 8 times that in memory, so size the pod for eight times that | `33554432`, `10000` |
| `MCP_CONTEXT_REQUIRE_SELECT` | `true`: `describe_table` serves context only when Trino validates a select of the columns it lists. `describe_schema` and plugins are not affected (see [Context from a data catalog](catalog-context.md)) | `false` |
| `MCP_CONTEXT_CANARY` | one table, `catalog.schema.table`, that no caller of this server may see: context is served to a caller only while Trino hides it from them. Unset triggers a warning when a provider is loaded | none |
| `MCP_CONTEXT_TEXT_CHARS`, `MCP_CONTEXT_RESPONSE_CHARS`, `MCP_CONTEXT_MAX_COLUMNS` | the longest text of one context field, the longest context in one response, and the maximum number of columns described in one table, at least 1 | `500`, `60000`, `500` |
| `MCP_VISIBILITY_ENABLED` | removed in 0.8: the filter by Trino's rights cannot be turned off, and `false` is a startup error | none |

The `file` provider's own variables are in
[Context from a data catalog](catalog-context.md#the-file-provider).
