# Design notes

**English** · [Français](../fr/design-notes.md)

## Why

The [README](https://github.com/AKKO-p/akko-mcp-trino#why) says why this server exists: an agent
needs a small, predictable surface over Trino, and Trino keeps deciding access.

## Notes

**The middleware is pure ASGI, not `BaseHTTPMiddleware`.** `BaseHTTPMiddleware` runs the rest of
the app in a separate task and breaks ContextVar propagation: the verified caller would never
reach the tools.

**Mounting does not depend on the transport.** One code path builds the transport app and mounts
the middleware, whatever the transport. A test checks this for every transport, because the
middleware once sat only on a code path the default transport never took.

**Every tool goes through the identity mode, not only `execute_query`.** Listing catalogs,
schemas and tables reads metadata, and metadata is data. A test walks every tool to check that it
reaches Trino the same way.

**The read-only check runs on the AST, anywhere in the tree.** A check of the leading keyword lets
`WITH w AS (DELETE FROM t) SELECT 1` through. So did a check of the root node, until an
adversarial test against a live Trino caught it. The check now walks the whole tree.

**Table functions are refused.** `TABLE(pg.system.query(query => '...'))` parses as a plain
`SELECT`, yet it sends raw SQL to the source under the connector's own account, so the engine
applies neither row filters nor column masks. The read-only check refuses every table function
and the `system` catalog, which shows other users' SQL in `system.runtime.queries`. The engine's
policy must refuse them as well, `system.jdbc` and `system.metadata` aside: the server's check is
a second line of defense, not the only one.

**No token passthrough.** The MCP authorization specification forbids sending a token issued for
one service on to another: that service might trust a token that was never meant for it (the
"confused deputy"). The server accepts only tokens whose audience is its own, refuses a token
issued for Trino, and obtains a token of its own for Trino by OAuth 2.0 Token Exchange (RFC 8693).
Where the identity provider cannot exchange tokens, `impersonation` is the explicit fallback; it
is never chosen silently. [MIGRATING.md](../../MIGRATING.md) describes the versions that passed the
caller's token on.

**The verified identity, never one the caller names.** The caller's name always comes from their
verified token. A caller who sends a header that names a Trino user changes nothing: the server
builds its own request to Trino. A password, a token or a client certificate is never sent over
plain http, and an identity mode the core does not know stops the server from starting rather than
falling back to another.

**Safe by default.** By default, every query runs as the caller, and a request without a valid
token is refused. `service` mode and token verification turned off remain possible, chosen by
name, and announced as warnings. The row and time limits have defaults too: unlimited was a
choice nobody had made.

**An extension can only refuse.** Its guards run around token verification, never instead of
it, and the read-only check applies whatever they return.

**The server does not decide access.** See [Trino first](security.md#trino-first).

**One MCP server per job.** This server queries Trino as the verified user and lets Trino decide
access; a data catalog such as Atlas, OpenMetadata or DataHub has its own job, the meaning of the
data, its lineage and its glossaries, and when it has an MCP server of its own, the agent plugs
in both side by side and combines them. Five rules follow.

- The server never calls another MCP server: a catalog adapter talks to the catalog's API, so
  every call keeps one known caller and two sets of rights never contradict each other.
- An adapter is thin and optional: it reads, by key, the context of objects Trino has already
  listed to the user, and it neither searches, nor copies the catalog's lineage or search, nor
  writes to the catalog.
- The core depends on no catalog: without an adapter it answers the same, and a catalog that is
  down never blocks a query, because the context is missing and the answer says so.
- Trino comes first: no name from a catalog is served before Trino has listed it to the user, so
  context enriches what Trino shows and never proposes an object.
- Names are shared: every answer gives the full Trino name (`catalog.schema.table`), so an agent
  can match our answers with those of the catalog's server without the two servers knowing each
  other.

The catalog's own server keeps rich business search, lineage, glossary editing and stewards' work.
This server keeps the context that helps to write a query, served under Trino's rights.

## Questions

### What if the data catalog already has its own MCP server?

The agent plugs in both servers. This server never calls the catalog's server: its adapter talks
to the catalog's API, and the full name `catalog.schema.table` that every answer carries lets the
agent match the answers of the two. The [first mode of deployment](deployment.md#with-a-data-catalog)
runs this server next to the catalog's: nothing here blocks or replaces it. See
[One MCP server per job](#notes) above.

### Can a user still write in Trino?

The read-only check covers what passes through this server. It is not a guarantee about what a
user may do in Trino by other means: if the policy lets the user write, the user can still write
through any other Trino client. [Security](security.md#read-only-through-this-server) says how to
make the engine enforce read-only too.

### Why not one shared identity?

In `service` mode, every query runs as one Trino account, whoever asks. Everyone then reads what
that account reads, Trino's row filters and column masks can no longer tell users apart, and the
audit names the account rather than the person. That is why `service` is never the default: the
server warns at start and tells every host, and the mode fits only data that every caller may
read anyway.

### Does the server talk to a model?

No. The server never talks to a model, so any model works. In the [example agent](hosts.md#from-an-agent),
the model never sees the user's token either: the agent sends it to the server with each tool
call.
