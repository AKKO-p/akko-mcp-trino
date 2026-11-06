# akko-mcp-trino-atlas

Apache Atlas context provider for [akko-mcp-trino](https://github.com/AKKO-p/akko-mcp-trino).
It reads what Atlas holds about the tables Trino lists for the caller (descriptions,
classifications, glossary terms, business metadata) and hands it to `describe_table` and
`describe_schema`, through the server's filter by Trino's rights. It reads Atlas with an
account of its own, through the REST API v2 only, and never writes to it.

*French version: [README.fr.md](README.fr.md).*

> **Status.** The package, its entry point and its settings check are in place; the reads of
> tables and glossary terms come in the next commits of 0.8.

## Install

```bash
pip install "akko-mcp-trino[atlas]"     # the core with this provider and Kerberos
export MCP_CONTEXT_PROVIDERS=atlas
```

The provider is registered under the entry point group `akko_mcp_trino.context` as `atlas`.
Nothing is loaded unless `MCP_CONTEXT_PROVIDERS` names it; the container image of
akko-mcp-trino carries it, inactive, without the Kerberos libraries for now.

## Settings

The server refuses to start when the provider has no address or no way to authenticate. No
message names an address or a secret.

| Variable | Role | Default |
|---|---|---|
| `ATLAS_URL` | Atlas address, `http` or `https`; several, separated by commas, for Atlas in high availability. Never with credentials in it. | required |
| `ATLAS_AUTH` | `basic` (a user and a password, or a Knox token) or `kerberos` | `basic` |
| `ATLAS_USER`, `ATLAS_PASSWORD` or `ATLAS_PASSWORD_FILE` | Basic authentication, with a read-only account | empty |
| `ATLAS_KNOX_TOKEN` or `ATLAS_KNOX_TOKEN_FILE` | a Knox token, for Atlas behind a Knox topology with the `ATLAS` role; not with `ATLAS_USER` | empty |
| `ATLAS_PRINCIPAL`, `ATLAS_KEYTAB` | Kerberos (SPNEGO), with `ATLAS_AUTH=kerberos` | empty |

A secret given as a file (`*_FILE`) is read again when the file changes.

## Atlas versions

The tests replay answers recorded from a vendor build of Atlas 2.1 and from Apache Atlas 2.4, in
`tests/recordings/atlas-2.1` and `tests/recordings/atlas-2.4`. Every name and text in them was
replaced by a neutral one, structure kept, by `tests/recordings/anonymize.py`; its docstring
says how to record and anonymize a new version.

## License

Apache License 2.0, like akko-mcp-trino.
