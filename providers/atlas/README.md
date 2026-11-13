# akko-mcp-trino-atlas

Apache Atlas context provider for [akko-mcp-trino](https://github.com/AKKO-p/akko-mcp-trino).
It reads what Atlas holds about the tables Trino lists for the caller (descriptions,
classifications, glossary terms, business metadata) and hands it to `describe_table` and
`describe_schema`, through the server's filter by Trino's rights. It reads Atlas with an
account of its own, through the REST API v2 only, and never writes to it.

*French version: [README.fr.md](README.fr.md).*

> **Status.** Tables, columns and glossary terms are read, with the allow lists and high
> availability, under Basic authentication, a Knox token or Kerberos (SPNEGO). The provider
> passes the server's context contract on answers recorded from Apache Atlas 2.1, the version of
> reference. Kerberos (SPNEGO) has been proven against a real KDC, with short-lived tickets
> renewed from the keytab. The Knox token is tested against a fake Atlas only: it has not been
> proven behind a real Knox gateway yet.

## Install

```bash
pip install "akko-mcp-trino[atlas]"     # the core with this provider and Kerberos
export MCP_CONTEXT_PROVIDERS=atlas
```

On Linux, the `atlas` extra builds the Kerberos bindings (`gssapi`, `krb5`) from source: it needs
a C compiler and the MIT Kerberos headers (`libkrb5-dev` on Debian and Ubuntu, `krb5-devel` on
Red Hat). With Basic authentication or a Knox token, install without them:
`pip install akko-mcp-trino akko-mcp-trino-atlas`.

Alone: `pip install akko-mcp-trino-atlas` (Basic and Knox), or
`pip install "akko-mcp-trino-atlas[kerberos]"` for Kerberos, which brings `pyspnego` and its
GSSAPI bindings (MIT Kerberos libraries on the system).

The provider is registered under the entry point group `akko_mcp_trino.context` as `atlas`.
Nothing is loaded unless `MCP_CONTEXT_PROVIDERS` names it; the container image of
akko-mcp-trino carries it, inactive, with its `kerberos` extra and the MIT Kerberos libraries.

## Settings

The server refuses to start when the provider has no address, no way to authenticate or no
`ATLAS_CLUSTERS`, or when a setting is malformed. No message names an address, a proxy or a
secret.

| Variable | Role | Default |
|---|---|---|
| `ATLAS_URL` | Atlas address, `http` or `https`; several, separated by commas, for Atlas in high availability. Never with credentials in it. A password or a Knox token over `http` refuses to start (`ATLAS_PASSWORD or ATLAS_KNOX_TOKEN (or its file) is set but an address of ATLAS_URL is not https`), as the server refuses `TRINO_PASSWORD` without https. Kerberos over `http` starts, with one warning at start and in `--check`: the account is authenticated, but nothing authenticates Atlas or protects its answers and its session cookie, which could be replayed as the account until it expires. | required |
| `ATLAS_AUTH` | `basic` (a user and a password, or a Knox token) or `kerberos` | `basic` |
| `ATLAS_USER`, `ATLAS_PASSWORD` or `ATLAS_PASSWORD_FILE` | Basic authentication, with a read-only account; the file comes first | empty |
| `ATLAS_KNOX_TOKEN` or `ATLAS_KNOX_TOKEN_FILE` | a Knox token, sent as `Authorization: Bearer`, for Atlas behind a Knox topology with the `ATLAS` role; not with `ATLAS_USER` | empty |
| `ATLAS_PRINCIPAL`, `ATLAS_KEYTAB` | Kerberos (SPNEGO), with `ATLAS_AUTH=kerberos`: the account's principal and the path of its keytab. The path is never printed. | empty |
| `ATLAS_SPN` | with `ATLAS_AUTH=kerberos`, the service name Atlas runs under, `<service>@<host>`, used for every address of `ATLAS_URL`: set it when all addresses share one principal (the name of a load balancer or a service); with one principal per server in high availability, leave it unset | `HTTP@<host of each address>` |
| `ATLAS_CLUSTERS` | JSON object, Trino catalog to Atlas cluster: `{"lake": "primary"}`. Catalogs are folded to lower case, as Trino lists them; two that fold to the same name are refused. A catalog not named here is not mapped and nothing is asked about it. | required |
| `ATLAS_TABLE_TYPES` | entity types a table is looked up under, in order; the first that holds it wins | `hive_table,iceberg_table` |
| `ATLAS_MAX_QUERY_CHARS` | longest query string of one bulk read; a batch of tables is split under it | `6000` |
| `ATLAS_GLOSSARIES` | glossaries whose terms may be served, by exact name (the suffix of a term's qualified name, `<term>@<glossary>`) | empty: none |
| `ATLAS_CLASSIFICATIONS` | classifications that may be served, by exact name | empty: none |
| `ATLAS_BUSINESS_METADATA` | business metadata sets that may be served, by exact name | empty: none |
| `ATLAS_OWNER_ATTRIBUTE` | the business metadata attribute that holds a table's owner, `<set>.<attribute>` | empty: no owner |
| `ATLAS_TERM_STATUS_ATTRIBUTE` | the term attribute (in `additionalAttributes`) that holds its status | empty |
| `ATLAS_CA_BUNDLE` | CA bundle to check Atlas's certificate against | the system's |
| `ATLAS_PROXY` | `http` or `https` proxy to reach Atlas; the environment's proxy settings are never read | none |
| `ATLAS_TIMEOUT_SECONDS` | timeout of one request to Atlas; with several addresses, see below how it relates to `MCP_CONTEXT_TIMEOUT_SECONDS` | `5` |
| `ATLAS_CONNECT_TIMEOUT_SECONDS` | time to open a connection to one address, below `ATLAS_TIMEOUT_SECONDS`, so that a silent address gives way to the next one within the call | `1` |
| `ATLAS_MAX_CONCURRENT_REQUESTS` | glossary term reads sent at once, from 1 to 32; with Kerberos, also the negotiations run at once | `4` |
| `ATLAS_MAX_RESPONSE_BYTES` | longest body of one Atlas answer once decoded, at least `1048576`; the bound is per read, not per call, and a call may hold several reads at once (bulk reads, then up to `ATLAS_MAX_CONCURRENT_REQUESTS` term reads); the read stops past it, with or without `Content-Length`, and the call is `unavailable` for itself alone: no other address is tried and no retry pause opens. One warning per call says to lower `MCP_CONTEXT_BATCH` | `67108864` |

A secret given as a file (`*_FILE`) is read again when the file changes.

**Without allow lists, nothing but descriptions is served.** The three lists are closed by
default: empty serves no glossary term, no classification and no business metadata, and `*` is
a name like any other, never a wildcard. Each set must be named. The server removes again
anything outside them.

## How tables are read

A Trino table `catalog.schema.table` is the Atlas entity whose qualified name is
`schema.table@<cluster of the catalog>`, read with
`GET /api/atlas/v2/entity/bulk/uniqueAttribute/type/{type}` for the whole batch the server asks
for, under each type of `ATLAS_TABLE_TYPES`. Only the names Trino listed are asked; nothing is
searched. Schema and table are Trino's names, in lower case; the cluster is sent exactly as
`ATLAS_CLUSTERS` writes it, because Atlas matches a qualified name case included. A table whose
schema or name holds a dot is never asked for, and gets no context: schema `a` with table `b.c`
and schema `a.b` with table `c` would be the same entity.

A type Atlas says does not exist (Atlas 2.4 answers 400 with `ATLAS-400-00-014` for
`iceberg_table` without the Iceberg model) yields nothing and is not asked again. **An
administrator who adds a table type to Atlas sends `SIGHUP` to the server**, which makes the
provider ask for it again; a change of the secret's file does too.

- The description is the steward's (`userDescription`), else `description`, else the
  metastore comment. Columns come from `columns` and `partitionKeys`.
- An entity that is not `ACTIVE` is ignored, and so is a column, a partition key or a glossary
  term whose relation or target is not `ACTIVE`, and a classification propagated from an
  entity that is not.
- Every name returned is folded to lower case, as Trino lists it. When two Atlas columns fold to
  the same Trino name, the one already written in lower case wins, else the first in code point
  order, whatever the order of Atlas's answer; across types, the first type of
  `ATLAS_TABLE_TYPES` that holds the table wins.
- A business metadata value is served as text, a list joined by commas.

## How glossary terms are read

A term is read by its GUID, `GET /api/atlas/v2/glossary/term/{guid}`, once per distinct key the
server asks for. The first read goes alone, so that a refused account costs one request and the
next reads reuse its session; the others go at most `ATLAS_MAX_CONCURRENT_REQUESTS` at a time.
The call's deadline covers them all: past it, the provider is unavailable for the call and
returns no part of it. `GET /api/atlas/v2/glossary/terms/{guid}/assignedEntities` is never
called.

- The glossary is the suffix of the term's qualified name (`<term>@<glossary>`). A term outside
  `ATLAS_GLOSSARIES`, or whose qualified name names no glossary, is never served.
- The definition is `shortDescription`, else `longDescription`. The status is
  `additionalAttributes.<ATLAS_TERM_STATUS_ATTRIBUTE>` when that setting is set, never the
  status of an assignment.
- Related terms come from six relations of the term, `synonyms`, `preferredTerms`,
  `preferredToTerms`, `seeAlso`, `isA` and `replacedBy` (served as `synonym`, `preferred`,
  `preferred_to`, `see_also`, `is_a`, `replaced_by`), and categories by their names, all from the
  term's own answer, with no other request. A related term of a glossary outside the allow list,
  or without a qualified name, is left out unread. Other relations (`classifies`,
  `replacementTerms`, `antonyms`, `translationTerms`, `validValues`...) are not read. The server
  names a related term only when it is attached to an object the caller sees.
- A term Atlas does not hold (404 with `ATLAS-404-00-005`) is left out.

## Network, high availability and errors

The client reads nothing from the environment (no implicit proxy), sends `X-XSRF-HEADER` and
a `User-Agent` of its own, and keeps the `ATLASSESSIONID` session cookie.

| Atlas answers | The provider |
|---|---|
| a refused connection, or none opened within `ATLAS_CONNECT_TIMEOUT_SECONDS` | tries the next address of `ATLAS_URL`, and starts the next calls there; then is unavailable |
| no answer in time from the address the call started with: a read or write timeout, or the call's own deadline reached while it waits on it | tries the next address of `ATLAS_URL` while the call has time left, starts the next calls there, and logs it once (below); then is unavailable |
| another transport error (a connection closed under the call, a proxy error), a compressed body that cannot be decoded, stops before its end or holds anything after it (a second gzip member, other bytes), or a 5xx (its body unread) | tries the next address of `ATLAS_URL`, then is unavailable; the next calls start where they did |
| a redirect to an address of `ATLAS_URL` (the passive server of Atlas HA) | follows it, and starts the next calls at that address |
| a redirect anywhere else (a Knox login on another host) | is refused at once, no other address tried, so no loop of attempts locks the account |
| 401, or a 403 other than below | is refused at once; the session cookie is dropped |
| 403 with `ATLAS-403-00-001` (Atlas refuses the account some of the objects asked) | serves nothing of that read (the whole batch of tables, or that term), stays open, and logs `atlas refused some objects to the account; an account with no read grant serves nothing` once for the call; the tables of that batch get the status `not_found`, on purpose, the same as a table Atlas does not hold, so that an agent cannot tell a refused object from an absent one |
| a 200 that is not JSON (a Knox login page), JSON that cannot be read, another status | is unavailable |
| a body in a content coding other than none, `identity`, `gzip` or `deflate`, or in more than one (the client asks for `Accept-Encoding: gzip, deflate`) | reads none of it, and is unavailable for that call alone, as below; logs `atlas answered in a content coding other than gzip or deflate` once for the call |
| a body longer than `ATLAS_MAX_RESPONSE_BYTES` once decoded | stops reading it, and is unavailable for that call alone: no other address is tried, the next calls start where they did, no retry pause opens; logs `atlas answered more than ATLAS_MAX_RESPONSE_BYTES for one read; lower MCP_CONTEXT_BATCH` once for the call |
| 404 with `ATLAS-404-00-009` to a table read (no such instance) | knows nothing of these tables |
| 404 with `ATLAS-404-00-005` to a term read (no such GUID) | leaves that term out |
| 400 with `ATLAS-400-00-014` (no such type) | knows nothing under that type, and remembers it until `SIGHUP` |
| any other 400 or 404 (a page, another code, a code of the other read, a wrong path behind Knox) | is unavailable |

An outage Atlas reports, or no address answering, logs `atlas unreachable`, with no address,
user or object name; a read cut by the call's deadline logs only the address line below, when
the address was late. A refusal is logged by the server as `atlas refused the account`. Neither
the logs nor an error carry an answer's text or the secret.

**A server that accepts the connection and then hangs** (frozen) is left: when the address a
call started with does not answer in time, the next calls start at the next address of
`ATLAS_URL`. The provider logs it once, by its position, never its address:
`atlas address 2 of 3 in ATLAS_URL did not answer in time` (`--check` prints the addresses in
that order). The line is not said again for that address until it answers; calls that wait on it
together log one line between them, and two hung addresses log two lines. Only the call's own
deadline counts, and only against an address that held the wait for at least
`ATLAS_CONNECT_TIMEOUT_SECONDS`, or that has not answered since `ATLAS_TIMEOUT_SECONDS` before
the wait began (so a hung address is left even under a deadline shorter than the connect
timeout). A shorter wait cut by the deadline on an address that had just answered (the time went
to another address, or to an earlier read of the same tool call), or a read cancelled for
another reason (another read of the same call refused, the client gone, a shutdown), leaves no
address and logs nothing. Whether the call that meets the hung address still gets its context
depends on two settings:

- with `ATLAS_TIMEOUT_SECONDS` below `MCP_CONTEXT_TIMEOUT_SECONDS` by at least the time the next
  address needs to answer, Kerberos negotiation included, the request to the hung address times
  out first, and the next address answers within the same call; with a smaller margin, what that
  call reads after the hung address is lost, and the next calls start at the next address,
  unless the deadline also cut a wait on it of at least `ATLAS_CONNECT_TIMEOUT_SECONDS`, or a
  wait on it when it had not answered within `ATLAS_TIMEOUT_SECONDS` before;
- with equal values (the defaults, 5 s each), the call's deadline comes first: the call that
  meets the hung address is lost (`unavailable`), and the next calls start at the next address.

Behind a passive server's redirect, the hung server is the active one: the passive server keeps
sending the calls to it, so the provider has no better address to start at. Each call is lost,
and the line names the active server's position once.

With a single address, there is nowhere else to go: each call waits on it until
`ATLAS_TIMEOUT_SECONDS` or its deadline, whichever comes first. The line is logged all the same,
`atlas address 1 of 1 in ATLAS_URL did not answer in time`, once until the address answers.

## Kerberos (SPNEGO)

With `ATLAS_AUTH=kerberos`, the provider answers Atlas's `WWW-Authenticate: Negotiate` with a token
from `pyspnego`. Each negotiation gets a new ticket from the keytab, kept in a cache in memory
(`MEMORY:`), so an expired ticket is never used and no process of its own renews one; it is one
request to the KDC. Once Atlas opens a session (`ATLASSESSIONID`), no new negotiation happens until
Atlas asks for one, and a redirect of Atlas's passive server negotiates again with the active one.
Mutual authentication of Atlas is left to TLS (`https` and `ATLAS_CA_BUNDLE`).

A negotiation runs on a thread of the provider's own, never on the loop that serves the context
of every call, so a slow KDC never holds the other reads. A KDC that has not answered when the
call's deadline passes (a filtered port 88, retries across KDCs) makes that call `unavailable`
at its deadline; that alone does not set the provider aside as down, since the deadline cut the
call, so the next call negotiates again. At most `ATLAS_MAX_CONCURRENT_REQUESTS` negotiations
run at once; a negotiation still waiting for a thread when its call is cut never starts, and one
under way ends when the KDC answers, its result dropped. When every one of those threads is held
by a negotiation whose call was cut, which is taken as a KDC that stopped answering, the next
call does not wait for it: it is `unavailable` at once, within its deadline, and the provider is
retried as an outage, after `MCP_CONTEXT_RETRY_AFTER_SECONDS`. While one thread is free, a slow
KDC that still answers is waited for, up to the call's deadline. The wait on the KDC is not a wait on Atlas: the
address answered its challenge, so the address line above never names it for that wait, and the
next calls still start at the same address of `ATLAS_URL`. A negotiation under way also holds
the process when it stops: Python waits for its thread, so a stop during a KDC hang lasts until
krb5 gives up, within the pod's grace period.

A negotiation that fails is a refusal of the account, as a 401 is, with no other address tried: a
keytab the KDC refuses (a password reset in the directory, a wrong principal, a keytab that cannot
be used) is not tried again for `MCP_CONTEXT_REFUSED_RETRY_SECONDS`, so that the provider never
locks the account with failed pre-authentications. Only a KDC that cannot be found or reached makes
the provider unavailable: when the KDC says so within the call's deadline, the provider is
retried as an outage, after `MCP_CONTEXT_RETRY_AFTER_SECONDS`. `ATLAS_KEYTAB` must be a file the
server can read at start; a rotated keytab (its time or size changes) lifts the refusal, as
`SIGHUP` does.

What the container needs:

- the `kerberos` extra (`ATLAS_AUTH=kerberos` without it refuses to start);
- a `krb5.conf` (`/etc/krb5.conf`, or `KRB5_CONFIG`) that names the realm and its KDC, with
  `rdns = false`, since an address behind a load balancer or a Kubernetes service has no
  reverse name that matches Atlas's service principal (`ATLAS_SPN` names it);
- a route to the KDC (port 88, UDP and TCP, or `udp_preference_limit = 1` in `krb5.conf`) from
  the pod: Kubernetes and OpenShift network policies must let it through;
- a clock in step with the KDC (five minutes of skew at most, by default);
- the keytab mounted read-only, as a secret, never in the image.

`--check` prints the principal, never the keytab.

## The account the provider reads with

1. **Read only.** The account needs to read entities, glossaries and their terms, and nothing
   else: no write, no administration. On Atlas with its own users, a user of the
   `DATA_SCIENTIST` role is enough: the seeded reference answers (`atlas-2.1-seed`) were read
   with one. On Atlas governed by Ranger, a policy of the `atlas` service that only reads.
   **The account must read every table it serves**, under the mapped clusters, and every term
   of the allowed glossaries: Atlas refuses a whole bulk read when one of its tables is denied
   (403 `ATLAS-403-00-001`), and that batch then gets no context. An account with no read grant
   at all gets that answer on every read: the provider then serves nothing, and its log says so
   (`atlas refused some objects to the account; an account with no read grant serves nothing`,
   once a call). `--check` may not see it: it reads the version, and a name no table carries
   under each type, never an object the account could be refused.
2. **It sees what is served to everyone.** The provider serves what this account reads, within
   the allow lists, to every caller who sees the object in Trino. Give it only the glossaries,
   classifications and business metadata that anyone who sees the table may read.
3. **A refusal stops it, not a retry loop.** A refused account (401, 403, or a redirect to a
   login page on another host) is not asked again for `MCP_CONTEXT_REFUSED_RETRY_SECONDS`
   (3600 by default, a setting of the server), or until its secret file changes, or `SIGHUP`,
   so that the provider never locks it in a directory that counts failed logins.

**Rights.** The context follows Trino's rights: a caller gets it only for the tables and columns
Trino lists to them. The provider reads Atlas with this read-only technical account. Descriptions
follow what Trino lists to the caller. With `MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table`
also requires that Trino validates a select by the caller of the columns it lists, while
`describe_schema` keeps serving the descriptions of the tables Trino lists. It serves glossary
terms, classifications and business metadata only when their glossary, classification or set is
named in `ATLAS_GLOSSARIES`, `ATLAS_CLASSIFICATIONS` or `ATLAS_BUSINESS_METADATA`, closed by
default (empty means none), and the owner only when `ATLAS_OWNER_ATTRIBUTE` is set. So a glossary
or a classification restricted in Atlas is never served unless the operator names it. Reading Atlas
as the caller is not in this version.

## Checking the configuration

`akko-mcp-trino --check` prints one line per setting: the addresses (never with credentials),
the authentication (the user, or the principal and service for Kerberos, never a secret or the
keytab's path), each catalog and its cluster, the table types, the timeouts, the bound on the
query of a bulk read and on parallel term reads, the sets served, the owner and term status
attributes, and whether a CA bundle and a proxy are set (never the proxy's address). It then asks
Atlas its version (`GET /api/atlas/admin/version`) and prints `atlas: answers, version
<version>`, or a line that starts with `UNREACHABLE:` when Atlas refuses the account (a refused
Kerberos negotiation included) or does not answer; `--check --strict` then exits 2. Last, it asks
each table type for a name no table carries, and prints the types Atlas does not know: when it
knows none of them (`atlas: knows none of the table types`), the provider serves nothing, and logs
that line once when it finds out.

## Atlas versions

| Atlas | Role |
|---|---|
| Apache Atlas 2.1 | reference |
| Apache Atlas 2.4 | compatibility |

The tests replay recorded answers, in `tests/recordings/`:

- `atlas-2.1-seed`: a build of Atlas 2.1, seeded with a neutral catalogue that plays every
  role of the server's contracts, and read through an account that may only read;
- `atlas-2.1`: a real catalogue of the same version, made neutral;
- `atlas-2.4`: Apache Atlas 2.4.0, built from the official sources.

The server's context contract (`ContextContract`) and its gate contract (`GateContract`, the
server's filter by Trino's rights proven end to end over the provider) run on `atlas-2.1-seed`
and on `atlas-2.1`; a check that needs a role the real catalogue lacks is skipped and says why.
Atlas has no notion of a join: the gate's join scenario is skipped, and the contract would fail
if the provider gave one. Every name and text
of a real catalogue was replaced by a neutral one, structure kept, by
`tests/recordings/anonymize.py`; its docstring says how to record and anonymize a new version.

## License

Apache License 2.0, like akko-mcp-trino.
