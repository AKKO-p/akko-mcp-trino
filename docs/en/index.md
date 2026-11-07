# akko-mcp-trino documentation

**English** · [Français](../fr/index.md)

akko-mcp-trino is an MCP server for Trino. An agent reads through it with the rights of the user
it acts for, and every write is refused before Trino. Start with the [quickstart](quickstart.md),
then open the page you need.

## Learn

| Page | What it covers |
|---|---|
| [Prerequisites](prerequisites.md) | what you need, and the versions supported and tested |
| [Quickstart](quickstart.md) | the sandbox, a user's token, a host, a first question |

## Do

| Page | What it covers |
|---|---|
| [Deployment](deployment.md) | install and run, the transports, the container, Helm on Kubernetes and OpenShift, Trino Gateway, with or without a catalogue |
| [Identity](identity.md) | the four identity modes, the user claim, the door, what happens on a request |
| [Connect an MCP host](hosts.md) | Claude Desktop, Cursor, any stdio host, the Python SDK, an agent |
| [Context from a catalogue](catalog-context.md) | what a context provider serves, its bounds, the `file` provider |
| [Modules](modules.md) | `token-policy`, `fingerprint-audit` and `trino-tags`, with real examples |

## Look up

| Page | What it covers |
|---|---|
| [The tools](tools.md) | the nine tools, their arguments, their answers, their errors |
| [Configuration](configuration.md) | every variable, by theme, with its default and its bounds |
| [Operations](operations.md) | resource groups, refusals, warnings, audit, health, metrics, upgrades |

## Understand

| Page | What it covers |
|---|---|
| [Security](security.md) | what read-only covers, Trino first, the commands an agent may send |
| [Developers](developers.md) | the architecture, tool plugins, extensions, writing a context provider |
| [Design notes](design-notes.md) | why the server is built this way, and a few questions |

## Reference files

These files stay at the root of the repository: [`examples/`](../../examples/),
[`CHANGELOG.md`](../../CHANGELOG.md) and [`MIGRATING.md`](../../MIGRATING.md),
[`CONTRIBUTING.md`](../../CONTRIBUTING.md), [`SECURITY.md`](../../SECURITY.md), the Helm chart's
[README](../../charts/akko-mcp-trino/README.md) and the Atlas provider's
[README](../../providers/atlas/README.md).

The interactive version of the five steps of the README is on
[akko-ai.com/en/docs/understand](https://akko-ai.com/en/docs/understand/), and how it works, end
to end, on [akko-ai.com/en/docs/how-it-works](https://akko-ai.com/en/docs/how-it-works/).
