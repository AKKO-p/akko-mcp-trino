# akko-mcp-trino documentation

**English** · [Français](../fr/index.md)

akko-mcp-trino is an MCP server for Trino. Through it, an agent reads data with the rights of the
user it acts for, and the server refuses every write before it reaches Trino. Start with the
[quickstart](quickstart.md), then open the page you need.

These pages describe `main`. The documentation of a release is at its tag.

## Learn

| Page | What it covers |
|---|---|
| [Prerequisites](prerequisites.md) | what you need, and the versions supported and tested |
| [Quickstart](quickstart.md) | the sandbox, a user's token, a host, a first question |

## Do

| Page | What it covers |
|---|---|
| [Deployment](deployment.md) | install and run, the transports, the container, Helm on Kubernetes and OpenShift, Trino Gateway, with or without a data catalog, with or without modules |
| [Identity](identity.md) | the four identity modes, the user claim, token verification, what happens on a request |
| [Connect an MCP host](hosts.md) | Claude Desktop, Claude Code, Cursor, VS Code, any stdio host, the Python SDK, two agents (one on the Mistral Agents API) |
| [Context from a data catalog](catalog-context.md) | what a context provider serves, its limits, the `file` provider |
| [Modules](modules.md) | `token-policy`, `fingerprint-audit` and `trino-tags`, with real examples |
| [Developers](developers.md) | the architecture, tool plugins, extensions, writing a context provider |

## Look up

| Page | What it covers |
|---|---|
| [The tools](tools.md) | the nine tools, their arguments, their answers, their errors |
| [Configuration](configuration.md) | every variable, by theme, with its default and its limits |
| [Security](security.md) | what read-only covers, Trino first, the statements an agent may send |
| [Operations](operations.md) | resource groups, refusals, warnings, audit, health, metrics, upgrades |

## Understand

| Page | What it covers |
|---|---|
| [Design notes](design-notes.md) | why the server is built this way, and a few questions |

## Contribute

[`CONTRIBUTING.md`](../../CONTRIBUTING.md) gives the ground rules, the checks and the locks.
[Developers](developers.md#contributing-to-the-docs) gives the terms these pages use.

## Reference files

These files stay at the root of the repository: [`examples/`](../../examples/),
[`CHANGELOG.md`](../../CHANGELOG.md) and [`MIGRATING.md`](../../MIGRATING.md),
[`SECURITY.md`](../../SECURITY.md), the Helm chart's
[README](../../charts/akko-mcp-trino/README.md) and the Atlas provider's
[README](../../providers/atlas/README.md).

The interactive version of the five steps of the README is on
[akko-ai.com/en/docs/understand](https://akko-ai.com/en/docs/understand/). How it works, end to
end, is on [akko-ai.com/en/docs/how-it-works](https://akko-ai.com/en/docs/how-it-works/), and the
site's guide is on [akko-ai.com/en/docs/open-source-core](https://akko-ai.com/en/docs/open-source-core/).
