# Design notes

**English** · [Français](../fr/design-notes.md)

## Why

An agent that reads a lakehouse needs a small, predictable surface: a few tools
whose descriptions a model understands, answers that never run away, and a
guarantee that a prompt injection cannot turn a question into a `DROP TABLE`
through it. This server is that surface, and nothing else. It does not decide
access: Trino decides for each user with the rules it already has, or, in
`service` mode, for the one identity you give it.

## Notes

**The guard is a pure ASGI middleware, not `BaseHTTPMiddleware`.** The latter
runs the downstream in a separate task and breaks ContextVar propagation: the
verified caller would never reach the tools.

**Mounting is transport-independent.** One code path builds the transport app
and mounts the guard, whatever the transport. The guard once lived only on the
branch the default transport never took; a test now asserts it for both.

**Every tool goes through the identity mode, not only `execute_query`.**
Listing catalogs, schemas and tables is reading metadata; a test walks every
tool to check it reaches Trino the same way.

**Read-only is decided on the AST, anywhere in the tree.** A leading-keyword
check lets `WITH w AS (DELETE FROM t) SELECT 1` through. So did a root-node
check, until a live adversarial proof caught it. The guard now walks the whole
tree.

**Table functions are refused.** `TABLE(pg.system.query(query => '...'))` parses
as a plain `SELECT`, yet it sends raw SQL to the source under the connector's
own account, so the engine applies neither row filters nor column masks. The
guard refuses every table function and the `system` catalog (which shows other
users' SQL in `system.runtime.queries`). The engine policy must refuse them as
well, `system.jdbc` and `system.metadata` aside; the guard is a second line, not
the only one.

**No token passthrough.** Up to 0.5, `jwt` mode sent Trino the caller's own
token, once verified. The MCP authorization specification forbids it: a token
issued for one service must not be sent on to another, or that service may
trust a token that was never meant for it (the "confused deputy"). Since 0.6,
the server accepts only tokens whose audience is its own, refuses a token
issued for Trino, and obtains for Trino a token of its own by OAuth 2.0 Token
Exchange (RFC 8693). Where the provider cannot exchange tokens, `impersonation`
is the explicit fallback; it is never chosen in silence.

**The verified identity, never one the caller names.** The caller's name
always comes from their verified token. A caller sending a header that names a
Trino user changes nothing: the server builds its own request to Trino. A
password, a token or a client certificate is never sent over plain http, and an
identity mode the core does not know refuses to start rather than fall back.

**Safe by default.** Up to 0.5, the defaults ran every query as `TRINO_USER`
and served a request without a token. Since 0.6, the default runs every query as
the caller and refuses a request without a valid token; `service` mode and an
open door are still possible, chosen by name, and announced as warnings. The
row and time budgets have defaults too: unbounded was a choice nobody had made.

**An extension can only refuse.** Its guards run around the token
verification, never instead of it, and the read-only guard applies whatever
they answer.

**The server does not decide access.** Trino does, for each caller or for the
configured identity. Putting policy in the server would duplicate, and eventually
contradict, what the engine already enforces for every other client.

## Questions

### The catalogue already has its own MCP server

A team that already runs the MCP server of its catalogue keeps it, side by side: nothing in
this server blocks or replaces it. That is the first mode of
[Deployment](deployment.md#deployment-with-a-catalogue), and it fits when the metadata of the
catalogue is not sensitive. When metadata must follow the rights on the data, a context
provider serves it only for the tables and columns Trino lists to the caller.

### Can a user still write in Trino?

The read-only guard covers what passes through this server. It is not a guarantee about what a
user may do in Trino by other means: if the policy lets the user write, the user can still write
through any other Trino client. [Security](security.md#read-only-through-this-server) says how to
make the engine hold the line too.

### Why not one shared identity?

In `service` mode every query runs as one Trino account, whoever asks. Everyone then reads what
that account reads, Trino's row filters and masks can no longer tell users apart, and the audit
names the account rather than the person. So `service` is never the default: the server warns
at start and tells every host, and it only fits data that every caller may read anyway.

### Does the server talk to a model?

No. The server never talks to a model, so any model works. An agent's model never sees the
user's token either: the server receives it on every tool call and verifies it.
