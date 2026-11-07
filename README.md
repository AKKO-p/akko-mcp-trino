<p align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/AKKO-p/akko-mcp-trino/ci.yml?branch=main&label=ci" alt="CI">
  <img src="https://img.shields.io/badge/tests-1280%20passed-success" alt="Tests">
  <img src="https://img.shields.io/badge/coverage-100%25-brightgreen" alt="Coverage">
  <img src="https://img.shields.io/badge/version-0.7.2-blue" alt="Version">
  <img src="https://img.shields.io/badge/python-3.12%20%7C%203.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/trino-%E2%89%A5%20351-blue" alt="Trino">
  <img src="https://img.shields.io/badge/MCP-2024--11--05%20to%202026--07--28%20%C2%B7%20streamable--http%20%7C%20sse%20%7C%20stdio-blue" alt="MCP">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License">
  <img src="https://img.shields.io/badge/code%20style-ruff-000000" alt="Ruff">
  <img src="https://img.shields.io/badge/status-beta-yellow" alt="Status">
</p>

# akko-mcp-trino

**English** · [Français](https://github.com/AKKO-p/akko-mcp-trino/blob/main/README.fr.md)

<!-- mcp-name: io.github.ab2dridi/akko-mcp-trino -->

**Give your AI agents read-only access to Trino, and nothing more.**

akko-mcp-trino is an [MCP](https://modelcontextprotocol.io) server for Trino.
It lets an agent find the tables and columns that hold what it is looking for,
describe a schema or a table, list catalogs, schemas and tables, explain a
query and run it, and it refuses every write it is sent: each statement is
parsed, and a write anywhere in it is refused before Trino. By default every
query runs as the user the agent acts for. The server verifies the user's
token, issued for this server, and obtains from the identity provider a
separate token for Trino (OAuth 2.0 Token Exchange): the user's own token never
leaves the server. Trino's access control (OPA, Ranger, or Trino's own rules)
then decides what that user may read. Results are bounded in rows and in time,
and every call leaves one audit line.

> **Upgrading from 0.7.0?** 0.7.1 and 0.7.2 are a security release. `execute_query`
> refuses `SHOW STATS`, `EXPLAIN`, `SHOW CREATE` and the other reads that can
> show facts beyond a row filter or a column mask, and the user is read from `sub`.
> With Keycloak, set `MCP_JWT_USER_CLAIM=preferred_username` before upgrading.
> See [MIGRATING.md](https://github.com/AKKO-p/akko-mcp-trino/blob/main/MIGRATING.md), and [Upgrading](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/operations.md#upgrading) for older
> versions.

## Understand in five steps

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-1-problem-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-1-problem-light.svg" alt="Carol, an analyst, asks for the list of customers. In an illustrative setup with a service account with broad rights, Trino sees mcp-svc and returns 10 customers with e-mails in clear. With akko-mcp-trino in its default jwt mode, Trino sees carol_analyst and returns 5 European customers with e-mails masked, as in the sandbox.">
</picture>

An agent connected through one service account with broad rights reaches Trino under that
account, so the rules written for Carol no longer apply. The left panel is an illustration: the
sandbox's policy would refuse such an account. akko-mcp-trino brings Carol's own identity to Trino,
and the rules Trino already has do the rest.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-2-who-sees-what-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-2-who-sees-what-light.svg" alt="The same query by three sandbox users, through the same MCP client. alice_admin gets the 10 customers in clear. carol_analyst gets the 5 European customers with masked e-mails. dave_viewer is refused by Trino: Access Denied: Cannot select from columns [country, full_name, id, email] in table or view demo.crm.customers.">
</picture>

The same query from the three users of the sandbox, through the same MCP client and the same
server. Trino answers each of them with the sandbox's OPA policy; the server copies no rule.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-3-step-by-step-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-3-step-by-step-light.svg" alt="A real trial of Mistral on 0.7.0, in six steps. It asks which customer segment holds the largest total account balance, searches with four words and gets matches word by word, joins on a column that does not exist, reads the server's hint, reads the columns of customers with describe_table, fixes the join and gets business, 17620.75.">
</picture>

A real trial, tool call by tool call: Mistral on 0.7.0 looks for the segment with the largest
balance. The search matches word by word, the error on a wrong join names the table to read, and
the agent fixes its join. Every tool answer shown is the server's, replayed in the sandbox.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-4-identity-modes-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-4-identity-modes-light.svg" alt="Three questions pick an identity mode: token exchange gives jwt, a Trino impersonation rule gives impersonation, a server on one person's machine with Trino's OAuth2 login gives oauth2, otherwise service. The four modes are drawn on the same grid of MCP host, server and Trino; service, one account for everyone, is marked in amber.">
</picture>

Four identity modes, from the safest to the simplest. `jwt` is the default; `service` gives
every caller the same account and is used only when you name it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-5-measurements-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-5-measurements-light.svg" alt="Mistral answers right 62.5% of the time on 0.6.1 and 70% on 0.7.0, Claude 93.3% on both. Mistral goes from 5.8 to 3.0 tool calls per question, Claude from 8.5 to 4.9. Mistral varies more between runs than the gap between versions, and input tokens rise by about 41%.">
</picture>

The same five business questions asked to Mistral and to Claude, on 0.6.1 and on 0.7.0. Mistral
varies more from one run to the next than the gap between the versions, and 0.7.0 sends about 41%
more input tokens per question, because its tool descriptions are longer. The harness and its
reports are in [akko-sandbox](https://github.com/AKKO-p/akko-sandbox/tree/main/agent/reports).

The interactive version of these five steps is on
[akko-ai.com/en/docs/understand](https://akko-ai.com/en/docs/understand/) (also
[en français](https://akko-ai.com/docs/comprendre/)).

## Try it in five minutes

The sandbox starts Keycloak, Trino, OPA and the server on your machine, with the three users
above:

```bash
git clone https://github.com/AKKO-p/akko-sandbox.git
cd akko-sandbox
docker compose up -d --wait
./demo.sh
```

The [quickstart](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/quickstart.md) goes on with a user's token, an MCP host and a first question.

## Why

An agent that reads a lakehouse needs a small, predictable surface: a few tools
whose descriptions a model understands, answers that never run away, and a
guarantee that a prompt injection cannot turn a question into a `DROP TABLE`
through it. This server is that surface, and nothing else. It does not decide
access: Trino decides for each user with the rules it already has, or, in
`service` mode, for the one identity you give it.

## Highlights

- **Read-only by construction, through this server.** SQL is parsed into an AST and refused if a write appears anywhere in the tree, CTEs and subqueries included. Table functions (`TABLE(...)`, such as a JDBC connector's `system.query`) and the `system` catalog are refused too, including through a name without a catalog when the session catalog is `system`. What a user may do in Trino by other means is Trino's rules: see [Security](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/security.md#read-only-through-this-server).
- **The user's own rights, safe by default.** In `jwt` mode, the default, the server verifies the caller's token (issued for this server, never for Trino), exchanges it at the identity provider for a token whose audience is Trino (RFC 8693), and Trino runs the query as that user. The caller's token is never passed on. A request without a valid token is refused with `401`.
- **A fallback without token exchange.** In `impersonation` mode the server authenticates to Trino as its own account (password or client certificate) and Trino runs the query as the verified caller, under an impersonation rule only that account has.
- **One identity, only when named.** In `service` mode every query runs as `TRINO_USER`. It is never chosen by default, and the server says so at start and to every host in its initialization instructions.
- **Bounded by default.** A row cap with a `truncated` flag (100), a time budget per query cancelled in Trino (60 s), a time budget per tool call (120 s).
- **One audit line per call.** Keyed by `X-Request-Id`, never the token, never the SQL text: who called which tool, with which outcome, for how long.
- **Nine tools,** annotated with what they can really do: `execute_query` is annotated read-only only while `TRINO_READ_ONLY` is true.
- **A guarded door.** Bearer tokens verified against any OIDC provider's JWKS (issuer, audience bound to this server, expiry, clock tolerance), a token issued for Trino refused, protected resource metadata (RFC 9728), a Host header filter.
- **Built to be extended without being forked.** Tool plugins and extensions are Python packages named at start: identity modes, guards at the door, audit fields, routes, tools. An extension can only refuse what the core allows, never widen it.
- **Any MCP host, any model.** Cursor, Claude Desktop, VS Code, the Python SDK… Mistral, or any OpenAI-compatible model through the example agent.
- **Small and proven.** 1280 tests at 100 % line and branch coverage, including in-process suites on
  the real SDK for all three transports, a live run against Keycloak and Trino in CI
  ([`integration/`](https://github.com/AKKO-p/akko-mcp-trino/tree/main/integration)), and a product-neutral guard.

## Documentation

| Page | What it covers |
|---|---|
| [Prerequisites](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/prerequisites.md) | what you need, and the versions supported and tested |
| [Quickstart](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/quickstart.md) | the sandbox, a user's token, a host, a first question |
| [Deployment](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/deployment.md) | install and run, the container, Helm on Kubernetes and OpenShift, Trino Gateway, with or without a catalogue |
| [Identity](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/identity.md) | the four identity modes, the user claim, the door |
| [Connect an MCP host](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/hosts.md) | Claude Desktop, Cursor, any stdio host, the Python SDK, an agent |
| [The tools](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/tools.md) | the nine tools, their arguments, their answers, their errors |
| [Configuration](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/configuration.md) | every variable, by theme, with its default and its bounds |
| [Security](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/security.md) | what read-only covers, Trino first, the commands an agent may send |
| [Context from a catalogue](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/catalog-context.md) | what a context provider serves, its bounds, the `file` provider |
| [Modules](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/modules.md) | `token-policy`, `fingerprint-audit` and `trino-tags`, with real examples |
| [Operations](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/operations.md) | resource groups, refusals, audit, health, metrics, upgrades |
| [Developers](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/developers.md) | the architecture, tool plugins, extensions, writing a context provider |
| [Design notes](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/design-notes.md) | why the server is built this way, and a few questions |

The documentation also exists [in French](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/index.md). The reference files stay at
the root: [`CHANGELOG.md`](https://github.com/AKKO-p/akko-mcp-trino/blob/main/CHANGELOG.md), [`MIGRATING.md`](https://github.com/AKKO-p/akko-mcp-trino/blob/main/MIGRATING.md),
[`SECURITY.md`](https://github.com/AKKO-p/akko-mcp-trino/blob/main/SECURITY.md) (also [in French](https://github.com/AKKO-p/akko-mcp-trino/blob/main/SECURITY.fr.md)) and the [examples](https://github.com/AKKO-p/akko-mcp-trino/tree/main/examples).

## Compatibility

| | Supported |
|---|---|
| Python | 3.12, 3.13 |
| Trino | 351 and later |
| Policy engines | OPA (`trino-opa`), Ranger (Trino plugin), Trino file-based access control |
| Identity providers | any OIDC provider publishing a JWKS; OAuth 2.0 Token Exchange, RFC 8693, for the default `jwt` mode |
| MCP | transports `streamable-http`, `sse` and `stdio`, through the official `mcp` SDK |
| Models | any, the server never talks to a model |

[Prerequisites](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/prerequisites.md#compatibility) says what each mode needs from Trino and what
is tested. The images the integration suite runs are pinned in
[`integration/compose.yaml`](https://github.com/AKKO-p/akko-mcp-trino/blob/main/integration/compose.yaml).

## Contributing

Contributions are welcome: see [CONTRIBUTING.md](https://github.com/AKKO-p/akko-mcp-trino/blob/main/CONTRIBUTING.md) for the
ground rules (tests first, 100 % coverage, product-neutral, fail closed) and
[SECURITY.md](https://github.com/AKKO-p/akko-mcp-trino/blob/main/SECURITY.md) for reporting a vulnerability privately.
Changes are tracked in [CHANGELOG.md](https://github.com/AKKO-p/akko-mcp-trino/blob/main/CHANGELOG.md).

## About AKKO

akko-mcp-trino is built and maintained by [AKKO](https://akko-ai.com), a
French project working on governed access for AI agents to enterprise data,
in place, on the engines and identity providers organisations already run.

**Maintained by:** AKKO ([contact@akko-ai.com](mailto:contact@akko-ai.com)) ·
**Issues:** [github.com/AKKO-p/akko-mcp-trino/issues](https://github.com/AKKO-p/akko-mcp-trino/issues)


## License

Apache 2.0. See [LICENSE](https://github.com/AKKO-p/akko-mcp-trino/blob/main/LICENSE) and [NOTICE](https://github.com/AKKO-p/akko-mcp-trino/blob/main/NOTICE).
