# akko-mcp-trino-atlas

Apache Atlas context provider for [akko-mcp-trino](https://github.com/AKKO-p/akko-mcp-trino).
It reads what Atlas holds about the tables Trino lists for the caller (descriptions,
classifications, glossary terms, business metadata) and hands it to `describe_table` and
`describe_schema`, through the server's filter by Trino's rights. It reads Atlas with an
account of its own, through the REST API v2 only, and never writes to it.

*French version: [README.fr.md](README.fr.md).*

> **Status.** Tables, columns and glossary terms are read, with the allow lists, high
> availability and Basic authentication or a Knox token; Kerberos comes in the next commit of 0.8.

## Install

```bash
pip install "akko-mcp-trino[atlas]"     # the core with this provider and Kerberos
export MCP_CONTEXT_PROVIDERS=atlas
```

The provider is registered under the entry point group `akko_mcp_trino.context` as `atlas`.
Nothing is loaded unless `MCP_CONTEXT_PROVIDERS` names it; the container image of
akko-mcp-trino carries it, inactive, without the Kerberos libraries for now.

## Settings

The server refuses to start when the provider has no address, no way to authenticate or no
`ATLAS_CLUSTERS`, or when a setting is malformed. No message names an address, a proxy or a
secret.

| Variable | Role | Default |
|---|---|---|
| `ATLAS_URL` | Atlas address, `http` or `https`; several, separated by commas, for Atlas in high availability. Never with credentials in it. | required |
| `ATLAS_AUTH` | `basic` (a user and a password, or a Knox token) or `kerberos` | `basic` |
| `ATLAS_USER`, `ATLAS_PASSWORD` or `ATLAS_PASSWORD_FILE` | Basic authentication, with a read-only account; the file comes first | empty |
| `ATLAS_KNOX_TOKEN` or `ATLAS_KNOX_TOKEN_FILE` | a Knox token, sent as `Authorization: Bearer`, for Atlas behind a Knox topology with the `ATLAS` role; not with `ATLAS_USER` | empty |
| `ATLAS_PRINCIPAL`, `ATLAS_KEYTAB` | Kerberos (SPNEGO), with `ATLAS_AUTH=kerberos` | empty |
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
| `ATLAS_TIMEOUT_SECONDS` | timeout of one request to Atlas | `5` |
| `ATLAS_MAX_CONCURRENT_REQUESTS` | glossary term reads sent at once, from 1 to 32 | `4` |

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
`ATLAS_CLUSTERS` writes it, because Atlas matches a qualified name case included.

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
| a refused connection, a timeout or a 5xx | tries the next address of `ATLAS_URL`, then is unavailable |
| a redirect to an address of `ATLAS_URL` (the passive server of Atlas HA) | follows it |
| a redirect anywhere else (a Knox login on another host) | is refused at once, no other address tried, so no loop of attempts locks the account |
| 401 or 403 | is refused at once; the session cookie is dropped |
| a 200 that is not JSON (a Knox login page), JSON that cannot be read, another status | is unavailable |
| 404 with `ATLAS-404-00-009` or `ATLAS-404-00-005` (no such instance) | knows nothing of these tables or this term |
| 400 with `ATLAS-400-00-014` (no such type) | knows nothing under that type, and remembers it until `SIGHUP` |
| any other 400 or 404 (a page, another code, a wrong path behind Knox) | is unavailable |

When unavailable, it logs `atlas unreachable`, with no address, user or object name; a refusal
is logged by the server as `atlas refused the account`. Neither the logs nor an error carry an
answer's text or the secret.

## Atlas versions

The tests replay answers recorded from a vendor build of Atlas 2.1 and from Apache Atlas 2.4, in
`tests/recordings/atlas-2.1` and `tests/recordings/atlas-2.4`. Every name and text in them was
replaced by a neutral one, structure kept, by `tests/recordings/anonymize.py`; its docstring
says how to record and anonymize a new version.

## License

Apache License 2.0, like akko-mcp-trino.
