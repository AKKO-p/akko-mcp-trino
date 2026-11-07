# Configuration

**English** · [Français](../fr/configuration.md)

Everything is read from the environment. Nothing is hardcoded. An extension
reads its own variables from the same environment. `akko-mcp-trino --check` prints the
effective configuration, never a secret.

## Trino

| Variable | Meaning | Default |
|---|---|---|
| `TRINO_HOST`, `TRINO_PORT` | the coordinator | `localhost`, `8080` |
| `TRINO_USER` | the server's own Trino account: every query runs as it in `service` mode; in `impersonation` mode it authenticates and impersonates the caller; ignored in `oauth2` mode | `trino` |
| `TRINO_PASSWORD` | its password, sent by Basic authentication; refused over `http`; `--check` says whether one is set, never which | none |
| `TRINO_CLIENT_CERT`, `TRINO_CLIENT_KEY` | a client certificate and its key (PEM paths) the account authenticates with instead (mTLS); https only | none |
| `TRINO_CATALOG` | session catalog, where a name without a catalog is read; set it to a data catalog (with `system`, such names are refused) | `system` |
| `TRINO_READ_ONLY` | refuse writes in `execute_query` | `true` |
| `MCP_EXTRA_READ_COMMANDS` | read commands the allow-list refuses, opened one by one, comma-separated: `EXPLAIN` (and the plan in `explain_query`), `SHOW STATS`, `SHOW CREATE`, `SHOW GRANTS`, `SHOW ROLES` (with `SHOW CURRENT ROLES` and `SHOW ROLE GRANTS`), `SHOW SESSION`. Trino answers them past a row filter or a column mask: open one only where no row filter or mask needs to hold; said at every start and to every host; an unknown name refuses to start | none |
| `TRINO_SOURCE` | the source Trino records for every query (`X-Trino-Source`) | the server name |
| `TRINO_HTTP_SCHEME` | `http` or `https`; a password is never sent over `http` | `http` |
| `TRINO_VERIFY` | TLS verification: `true`, `false`, or a path to the CA bundle Trino's TLS chain is checked against | `true` |
| `TRINO_REQUEST_TIMEOUT_SECONDS` | per-request timeout on the Trino client | `30` |
| `TRINO_IDENTITY_MODE` | who the server is for Trino: `jwt` (caller, by token exchange), `impersonation` (caller, impersonated by `TRINO_USER`), `oauth2` (native browser login, stdio only, checked by the first request that is not discovery), or `service` (`TRINO_USER`, announced as a warning); all except `service` require https; `jwt` and `impersonation` also require a valid MCP token; an extension may add others, and an unknown mode refuses to start | `jwt` |
| `TRINO_TOKEN_EXCHANGE_URL` | `jwt`: the provider's token endpoint (https only) | none (required in `jwt`) |
| `TRINO_TOKEN_EXCHANGE_CLIENT_ID`, `TRINO_TOKEN_EXCHANGE_CLIENT_SECRET` | `jwt`: this server's confidential client at the provider, sent by Basic authentication; `--check` never prints the secret | none (required in `jwt`) |
| `TRINO_TOKEN_EXCHANGE_AUDIENCE` | `jwt`: the audience of the token requested for Trino, the one Trino's `required-audience` names; never an audience this server accepts | none (required in `jwt`) |
| `TRINO_TOKEN_EXCHANGE_SCOPE` | `jwt`: scopes to request with it, space-separated | none |
| `TRINO_TOKEN_EXCHANGE_VERIFY` | TLS verification of the token endpoint: `true`, `false`, or a CA bundle path | `true` |

## Bounds and time budgets

| Variable | Meaning | Default |
|---|---|---|
| `TRINO_MAX_ROWS` | result cap, 1 at least; one more row is read to say whether the answer was `truncated`; also the size of one page of a listing | `100` |
| `TRINO_QUERY_TIMEOUT_SECONDS` | time budget of one query: past it, the server cancels the query in Trino; `0` leaves the limit to Trino | `60` |
| `MCP_CALL_TIMEOUT_SECONDS` | time budget of one tool call, every query of the call included: past it, no query starts, the one running is cancelled and the agent gets an error; `0` sets none | `120` |

## Listings and search

| Variable | Meaning | Default |
|---|---|---|
| `MCP_MAX_CATALOGS` | the most catalogs `list_catalogs` reads before it pages them, `system` aside, 1 at least; above it the tool answers an error, never a cut list | `10000` |
| `MCP_DESCRIBE_SCHEMA_TABLES` | the most tables in one page of `describe_schema`, from 1 to 100: a page reads each of its tables by its own branch, so its cost grows with the page | `20` |
| `MCP_DESCRIBE_SCHEMA_COLUMNS` | the most columns one page of `describe_schema` holds in all, 1 at least; the first table of a page comes whole however wide, and a page shortened for this budget says so | `1000` |
| `MCP_DESCRIBE_MAX_COLUMNS` | the most columns of a table `describe_table` reads, page by page, 1 to 100000; past them the answer says `columns_cut` | `5000` |
| `MCP_SEARCH_MIN_LENGTH` | the shortest word `search_columns` searches, 1 at least; shorter words are ignored, and a text with none left is refused | `3` |
| `MCP_SEARCH_MAX_WORDS` | the most words a text of `search_columns` may hold, 1 at least; above it the search is refused before Trino | `8` |
| `MCP_SEARCH_MAX_WORD_LENGTH` | the longest word `search_columns` takes, not below `MCP_SEARCH_MIN_LENGTH`; a longer word is refused before Trino | `64` |
| `MCP_SEARCH_CONCURRENCY` | the catalogs one call of `search_columns` reads at once, 1 to 32: as many metadata statements in flight in Trino for that call. Raising it past 8 mostly adds metadata load on Trino, with little gain. | `8` |
| `MCP_SEARCH_MAX_THREADS` | the reads of `search_columns` in flight in the threads of one process, all calls together, 1 to 256; a call that finds none free reads its next catalog itself | `32` |
| `MCP_SEARCH_RESERVE_SECONDS` | the seconds of the call's budget `search_columns` keeps in reserve, so that it answers what it searched before the call runs out; 0 or more, below `MCP_CALL_TIMEOUT_SECONDS` when that is set; 0 is warned of | `2` |

## Identity

| Variable | Meaning | Default |
|---|---|---|
| `MCP_AUTH_ENABLED` | verify the callers' bearer tokens (the door) | `true` |
| `MCP_AUTH_REQUIRED` | refuse with `401` a request without a valid token; `jwt` and `impersonation` refuse to start without it; in `oauth2`, an additional MCP token gate, never a switch to disable Trino login | `true` |
| `MCP_JWKS_URL` | the provider's JWKS endpoint | none (required when auth is enabled) |
| `MCP_OIDC_ISSUER` | the issuer a token must carry | none |
| `MCP_OIDC_AUDIENCE` | the audience a token must carry: the one the provider gives tokens issued for this server (RFC 8707); never Trino's | none (this or `MCP_RESOURCE_URL` is required when auth is enabled) |
| `MCP_RESOURCE_URL` | this server's canonical URL, such as `https://mcp.example.com/mcp`: accepted as an audience, named in the protected resource metadata the server then publishes (RFC 9728) and in the challenge of a `401` | none |
| `MCP_JWT_LEEWAY_SECONDS` | tolerance on `exp`/`nbf`/`iat` for clock drift between the issuer and this server | `30` |
| `MCP_JWT_USER_CLAIM` | the claim of a caller's token that names the user, as the access rules know them. `sub` is the OpenID Connect standard: the stable, unique identifier the issuer gives the user. An issuer whose `sub` is an opaque id the rules do not know (Keycloak gives a UUID; Entra ID, Okta and Dex an opaque id too) names another claim: `preferred_username`, `upn`, `uid`... A token without it is refused, never read from another claim | `sub` |
| `MCP_JWT_ALGORITHMS` | the signature algorithms a caller's token may use, comma-separated, among `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES384`, `ES512` (for example `RS256,RS512` for an identity provider that signs with either); a shared-secret algorithm (`HS256`...) or `none` refuses to start | `RS256` |
| `MCP_JWT_CLIENT_CLAIMS`, `MCP_JWT_SCOPE_CLAIMS` | the claims that name the application that asked for a token, and those that hold its scopes, comma-separated, in order: the first one the token carries wins. A scope claim is a space-delimited string or a list of strings; any other value is no scope. Keycloak: the defaults; Entra ID: `azp` or `appid` depending on the token version, and `scp`; Okta: `cid` and `scp` (see [Modules](modules.md)) | `azp,client_id`, `scope` |
| `MCP_ALLOWED_HOSTS` | Host header filter (the SDK's DNS rebinding protection): `host` or `host:port` entries, comma-separated, `host:*` for any port (the only wildcard); once the filter is on, a request that carries an `Origin` header is refused (403); empty serves any Host | none (off) |

Auth enabled without a JWKS URL, or without an audience of this server's own,
refuses to start: an authentication layer that cannot verify anything must not
pretend to. So does a door that requires a token it cannot verify
(`MCP_AUTH_REQUIRED=true` with `MCP_AUTH_ENABLED=false`).

## Serving

| Variable | Meaning | Default |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http`, `sse` or `stdio` | `streamable-http` |
| `MCP_PORT`, `MCP_HEALTH_PORT` | listening ports | `3000`, `3001` |
| `MCP_SERVER_NAME` | name announced to hosts | `trino-mcp` |
| `MCP_USER_TOKEN` | stdio only: the user's access token, verified like a bearer header | none (required in strict mode) |

## Plugins and extensions

| Variable | Meaning | Default |
|---|---|---|
| `MCP_TOOL_PLUGINS` | comma-separated names of registrars installed under the entry-point group `akko_mcp_trino.tools` | none |
| `MCP_EXTENSIONS` | comma-separated names of extensions installed under the entry-point group `akko_mcp_trino.extensions` | none |

## Modules

| Variable | Meaning | Default |
|---|---|---|
| `MCP_ALLOWED_CLIENTS` | `token-policy`: the clients a token may name, separated by commas or spaces | none: any client |
| `MCP_REQUIRED_SCOPES` | `token-policy`: the scopes a token must carry, every one, separated by commas or spaces | none |
| `MCP_AUDIT_MAX_NAMES` | the names each list of a call's audit record keeps (its tables, the tables whose description it served), 1 to 100000; the others are counted, once each, never written | `200` |

## profile_table

| Variable | Meaning | Default |
|---|---|---|
| `MCP_PROFILE_MAX_COLUMNS`, `MCP_PROFILE_SAMPLE_ROWS` | the most columns `profile_table` profiles in one call (1 to 500), and the rows its `sample` mode reads, 1 at least | `50`, `100000` |
| `MCP_PROFILE_MODES` | the modes `profile_table` allows, comma-separated, among `sample`, `bernoulli` and `full`; empty or an unknown name refuses to start | `sample,bernoulli,full` |
| `MCP_PROFILE_VALUE_CHARS` | the characters of a text `min` or `max` it serves, or the bytes of a binary one; Trino reads one more and no further, and past it the value is cut; 1 at least | `200` |
| `MCP_PROFILE_TYPE_CHARS` | the characters of a column's type it serves, 1 to 100000; a longer type is cut, with `"type"` in `cut`; `MCP_PROFILE_VALUE_CHARS` never cuts a type | `500` |
| `MCP_PROFILE_DESCRIBE_ROWS` | the rows of its `DESCRIBE` it reads, past the row cap, 1 at least; a column past them cannot be named | `5000` |
| `MCP_PROFILE_CLIENT_TAG` | the client tag of its aggregate query (not of its `DESCRIBE`), for a Trino resource group: 1 to 100 letters, digits and `_ . : = / -` | `mcp-profile` |
| `MCP_PROFILE_TIMEOUT_SECONDS` | the time budget of its aggregate query, past which it is cancelled in Trino; the budget that applies is the smallest of this one, `TRINO_QUERY_TIMEOUT_SECONDS` and the call's budget, each counted only above 0, and `--check` prints it; 0 leaves the smaller of the other two; above 0, at most `TRINO_QUERY_TIMEOUT_SECONDS` and `MCP_CALL_TIMEOUT_SECONDS` when they are above 0 | `0` |
| `MCP_PROFILE_MAX_CONCURRENT` | the calls of `profile_table` one server process runs at once, 1 to 64; past them a call answers `"kind": "busy"` at once and sends no query | `4` |

## Context from a catalogue

| Variable | Meaning | Default |
|---|---|---|
| `MCP_CONTEXT_PROVIDERS` | comma-separated names of providers installed under the entry-point group `akko_mcp_trino.context`; an unknown name refuses to start | none |
| `MCP_CONTEXT_TIMEOUT_SECONDS` | time budget of one context read, more than 0 | `5` |
| `MCP_CONTEXT_RESERVE_SECONDS` | the seconds of a tool call's budget kept back so that the context ends before the call does; 0.3 or more when a provider is loaded (the server refuses less), 0 or more otherwise | `0.5` |
| `MCP_CONTEXT_MAX_CONCURRENCY` | the most context reads in flight for one provider, 1 at least | `32` |
| `MCP_CONTEXT_RETRY_AFTER_SECONDS` | the delay before a source that is down is called again, 0 or more | `30` |
| `MCP_CONTEXT_REFUSED_RETRY_SECONDS` | the delay before a source that refused the account is called again (`SIGHUP` lifts it at once), 0 or more | `3600` |
| `MCP_CONTEXT_BATCH` | the tables one read of a source holds, 1 at least | `50` |
| `MCP_CONTEXT_TTL_SECONDS` | how long a context read is kept, 0 or more | `900` |
| `MCP_CONTEXT_CACHE_BYTES`, `MCP_CONTEXT_TERMS_CACHE_BYTES` | the bound of the caches of tables and of glossary terms, 1 at least; each bound counts the length of the JSON form of the cached entries, and the process holds about three to four times that in memory, so size the pod for that | `134217728`, `16777216` |
| `MCP_VISIBILITY_TTL_SECONDS` | how long what Trino listed for a caller is kept, 0 or more | `60` |
| `MCP_VISIBILITY_CACHE_BYTES`, `MCP_VISIBILITY_MAX_NAMES` | the bound of that cache (each entry counts its key as well as its value; measured with fresh strings, the process holds 3.6 to 8 times that in memory, so size the pod for eight times that), and the most names it keeps for one caller, 1 at least | `33554432`, `10000` |
| `MCP_CONTEXT_REQUIRE_SELECT` | `true`: context is served only for tables the caller may read | `false` |
| `MCP_CONTEXT_CANARY` | one table, `catalog.schema.table`, that no caller of this server may see: context is served to a caller only while Trino hides it from them; unset is warned of when a provider is loaded | none |
| `MCP_CONTEXT_TEXT_CHARS`, `MCP_CONTEXT_RESPONSE_CHARS`, `MCP_CONTEXT_MAX_COLUMNS` | the longest text of one context field, the longest context in one response, and the most columns described in one table, 1 at least | `500`, `60000`, `500` |
| `MCP_VISIBILITY_ENABLED` | removed in 0.8: the filter by Trino's rights cannot be turned off, and `false` refuses to start | none |

The `file` provider's own variables are in
[Context from a catalogue](catalog-context.md#the-file-provider).
