# Developers

**English** · [Français](../fr/developers.md)

## Contributing to the docs

`docs/en` and `docs/fr` hold the same pages, with the same headings in the same order and the same
commands; the tests check it. Every command keeps its real output. Change both languages in the
same commit. These pages use one word per concept:

| Term | Meaning | French |
|---|---|---|
| catalog | a Trino catalog | « catalog » |
| data catalog | a metadata service such as Apache Atlas or OpenMetadata | « catalogue de données » |
| token verification | the check of the caller's bearer token (`MCP_AUTH_ENABLED`, `MCP_AUTH_REQUIRED`) | « vérification du token » |
| read-only check | the server's check of each SQL statement | « contrôle de lecture seule » |
| middleware | the ASGI layer that verifies tokens and request ids | « middleware » |
| guard | an extension hook that may refuse a request (`before_auth`, `after_auth`) | « garde » |
| visibility filter | the core's check, with Trino, of what the caller may see before context is served | « filtre de visibilité » |
| module | one of the three built-in extensions: `token-policy`, `fingerprint-audit`, `trino-tags` | « module » |
| extension | the code interface (`akko_mcp_trino.extensions.Extension`, `MCP_EXTENSIONS`, `server.extensions`) | « extension » |
| the proxy or gateway in front of the server | whatever sends `X-Request-Id` to the server | « la passerelle placée devant le serveur » |

English pages use American spelling. In prose, personas are first names (Alice, Carol, Dave); the
exact login appears only in code and output blocks. In French pages, claim, issuer, fallback, service
account, row filter, column mask and bearer token stay in English, with narrow no-break spaces before `; : ! ?` and no-break spaces inside « ».

## Architecture

Every file in `akko_mcp_trino/` has one responsibility and its tests, in `tests/`:

| File | Responsibility |
|---|---|
| `config.py` | read the environment, neutral defaults |
| `auth.py` | JWT verification against JWKS, `Principal` |
| `identity.py` | the verified caller in a ContextVar |
| `middleware.py` | the middleware: token verification, request id, the extensions' guards and documents |
| `extensions.py` | the extension points and their loading |
| `transport_security.py` | the Host header filter (`MCP_ALLOWED_HOSTS`) |
| `audit.py` | the audit line, the request id, the record and the time budget of a call |
| `sql_guard.py` | identifier validation, the read-only check on the AST |
| `tools.py`, `plugins.py` | the nine tools and the `ToolContext`, loading tool plugins |
| `trino_client.py` | the Trino connection, the identity modes (`jwt`, `impersonation`, `oauth2`, `service` in the core) and the time budgets |
| `token_exchange.py` | OAuth 2.0 Token Exchange (RFC 8693): a token for Trino in exchange for the caller's |
| `health.py`, `metrics.py` | `/health`, `/ready` and `/metrics`; the Prometheus counters |
| `server.py`, `app.py`, `__main__.py` | assembly, transport, entrypoint and `--check` |
| `context/` | the context providers: protocol, types, chain, visibility filter, the `file` provider |
| `testing/` | the contracts every context provider passes, alone and behind the visibility filter (`FakeTrino`), for the providers' own tests |

`tests/test_core_alone.py` lists what the core holds (tools, modules, settings, audit fields) and
fails the day something is added without being listed.

## Development

```bash
pip install --require-hashes -r requirements/build.txt -r requirements/dev.txt && pip install --no-deps --no-build-isolation -e .
ruff check akko_mcp_trino tests examples && ruff format akko_mcp_trino tests examples
ruff check akko_mcp_trino --select D100,D101,D102,D103,D105,D107   # every public name documented
mypy akko_mcp_trino           # the package ships py.typed and type-checks clean
pip-audit --disable-pip --require-hashes -r requirements/image.txt -r requirements/dev.txt -r requirements/release.txt   # no known vulnerability in the locks
bash scripts/lock.sh check    # the locks match their sources (CONTRIBUTING.md, "Dependencies and locks")
bash lint-vendor-neutral.sh   # fails if the package imports anything product-specific
pytest                        # line or branch coverage below 100% fails the run
integration/run.sh            # live: Keycloak, Trino and three servers in Docker, then removed
python -m build --no-isolation && twine check dist/*
akko-mcp-trino --check        # effective configuration, no secrets; exit 2 if it would not start
```

The documentation is tested too: every variable it lists is read by the code, and the defaults it
states are the defaults in the code. [CONTRIBUTING.md](../../CONTRIBUTING.md) gives the ground
rules and the locks.

## Tool plugins

A product built on this server can add its own tools without forking it. A package registers a
function, a registrar `(mcp, client) -> None` or `(mcp, client, tools) -> None`, under the entry
point group `akko_mcp_trino.tools`, and the operator names it in `MCP_TOOL_PLUGINS`:

```toml
[project.entry-points."akko_mcp_trino.tools"]
my-tools = "my_package.tools:register"
```

```python
from akko_mcp_trino.identity import current_subject


def register(mcp, client):
    @mcp.tool()
    def my_tool(text: str) -> str:
        return client.query("SELECT ...", user=current_subject())
```

The server loads nothing that is not named, and refuses to start on an unknown name. Tools added
this way go through the same client, hence the same identity mode as the built-in ones. Every tool
a registrar adds through `mcp.tool()` or `mcp.add_tool()` leaves the same audit line, with no
change to the plugin. `build_server(extra_tool_registrars=...)` does the same from Python.

A registrar with a third argument receives the `ToolContext`:

- `tools.tool(title, governed=..., open_world=...)` registers a tool with its MCP annotations;
- `tools.run(sql)` queries Trino through the identity mode;
- `tools.note(...)` records what the call did (the SQL it ran, the tables, the rows, whether they
  were cut);
- `tools.context.tables(refs)` reads the short data catalog description of the tables Trino proves
  to the caller (`tools.context` is `None` without a provider);
- `tools.add_describer(fn)` adds to what `describe_table` returns.

```python
import json


def register(mcp, client, tools):
    @tools.tool("Ask the data", governed=True, idempotent=False)
    def ask_data(question: str) -> str:
        answer = my_service.ask(question)  # runs its SQL through tools.run
        tools.note(sql=answer["sql"], tables=answer["tables"], rows=len(answer["rows"]))
        return json.dumps(answer)
```

## Extensions

A tool plugin adds tools. An extension may add more, around the core and never in its place. A
package registers a factory `(config, env) -> Extension` under the entry point group
`akko_mcp_trino.extensions`, and the operator names it in `MCP_EXTENSIONS` (comma-separated, in
order). The three [modules](modules.md) are extensions of this kind.

```toml
[project.entry-points."akko_mcp_trino.extensions"]
my-extension = "my_package.extension:create"
```

```python
from akko_mcp_trino.extensions import Extension, Refusal


class MyExtension(Extension):
    name = "my-extension"

    def after_auth(self, admission):
        if admission.principal is not None and admission.principal.subject.startswith("svc-"):
            return Refusal("service_accounts_refused", status=403)
        return None


def create(config, env):
    return MyExtension()
```

The server loads nothing that is not named. It refuses to start on an unknown name, and on a
factory that raises `ValueError` for a setting it cannot serve; `--check` reports both.
`akko_mcp_trino.extensions.Extension` is the base class: every hook does nothing, so an extension
overrides only what it adds.

| Hook | What it adds |
|---|---|
| `check_lines()` | lines that `--check` prints, never a secret |
| `identity_modes()` | identity modes by `TRINO_IDENTITY_MODE` name, each an `IdentityMode` (`describe`, `connection(user)`, `ping`); one named like a core mode (`jwt`, `impersonation`, `oauth2`, `service`) replaces it |
| `audiences()` | token audiences accepted besides `MCP_OIDC_AUDIENCE` and `MCP_RESOURCE_URL`; never Trino's |
| `before_auth(admission)` | a refusal before the token is looked at, or `None` |
| `after_auth(admission)` | a refusal once the core has verified the token (or found none), or `None` |
| `public_documents()` | JSON documents served without a token, by request path; they take precedence over the core's |
| `challenge(refusal)` | the `WWW-Authenticate` value of a refusal; it takes precedence over the core's |
| `routes()` | Starlette routes added to the served app, behind the middleware |
| `wrap_audit(audit)` | the audit sink the server writes to, given the one it would write to otherwise |
| `client_tags()` | client tags of a query run now (`X-Trino-Client-Tags`) |
| `describe_table_doc()` | text appended to the description of `describe_table` |
| `tool_registrars()` | tool registrars, called like a tool plugin's, before the plugins' |

A guard returns a `Refusal(reason, status=401, error="", headers={}, message="")`:

- the reason goes into `X-Reason` and into the body;
- `error` is the RFC 6750 code a challenge may carry;
- `headers` are added to the response (`Retry-After`, for example);
- `message`, when set, tells the client in the body what to do.

A guard reads an `Admission`: the request headers, the token the request presented, the verified
`principal` (`None` before authentication or without a token), and `stdio` (true for a stdio
admission). `admission.bind(var, value)` sets a context variable for the duration of the request,
and resets it afterwards.

Over stdio, a process is admitted once, at start, except with `TRINO_IDENTITY_MODE=oauth2`. There,
the guards run on every request that is not discovery (see
[Native browser login](identity.md#native-browser-login-oauth2-stdio-only)), never at start or for
discovery, and always after Trino named the user. `before_auth` therefore already sees that
`principal`, and a binding lasts one request. A guard that counts sessions, or that refuses at
start to keep the server from serving, must take this into account.

The rule is simple: **an extension can only refuse, never widen.** Its guards run around the
core's token verification, never instead of it. With `MCP_AUTH_REQUIRED=true`, a request without a
verified identity is still refused whatever a guard returns, and the read-only check applies to
every query.

An identity mode decides who the server is for Trino, query by query.
`akko_mcp_trino.trino_client.resolve_identity` picks the mode that `TRINO_IDENTITY_MODE` names
among the core's and the extensions', and refuses an unknown one: there is no fallback to
`service`. A mode that sends Trino a token for the caller must obtain one for Trino and never
forward the caller's: `JwtIdentity.trino_token(bearer)` is the hook. A plugin that calls its own
backend for the caller reads the verified bearer with `akko_mcp_trino.identity.current_bearer()`,
and must exchange it the same way rather than send it on. Trino decides what that identity may
read.

## Writing a context provider

A provider is a class that implements the protocol `akko_mcp_trino.context.ContextProvider`:
`name`, `context_api` (set it to `akko_mcp_trino.context.CONTEXT_API`; the server refuses another
version), `maps(catalog)`, `served_sets()`, `describe(refs, deadline=…)`,
`terms(keys, deadline=…)`, `check_lines()`, `aclose()` and `credentials_stamp()`.

It may add an optional `reset()`, which the server calls on `SIGHUP`, from its signal handler, so
that the provider forgets what it remembered of its data catalog. `reset()` is a plain function,
never `async def` (nothing awaits it). It returns `None` at once, never blocks, sends no request
and does not raise. One that raises is logged with the provider's name and the error's type, never
its text, and the contract checks the rest when `reset()` is defined.

The provider's package registers a factory `(env) -> provider` under the entry point group
`akko_mcp_trino.context`:

```toml
[project.entry-points."akko_mcp_trino.context"]
mycatalog = "my_package.provider:from_env"
```

The factory reads the provider's own variables from `env`, raises `ValueError` when it cannot serve
them, and does not reach its data catalog. A secret is read with
`akko_mcp_trino.context.secret_setting(env, name)`, so that `<NAME>_FILE` works too. A provider
follows these rules:

- It serves only the sets the operator allows by exact name, one allow list per kind (glossary,
  classification, property, owner), nothing by default, and `served_sets()` says what it serves.
- It returns names folded as Trino lists them, in lower case: the tables, the column keys, the
  grain and the joins. The core compares names exactly, so a name in another case is lost.
- It returns only the core types (`TableContext`, `ColumnContext`, `Term` and so on), never data:
  no value, no sample.
- It respects the deadline it is given. It raises `ProviderRefused` when its account is refused,
  and `ProviderUnavailable` when the data catalog is down, late, or returns what it cannot read.
  The text of its errors stays in the server. Its logs and `check_lines()` never hold its secret
  or a schema, table or column name.
- It imports neither `akko_mcp_trino.trino_client` nor `akko_mcp_trino.identity`, never receives
  the Trino client or the caller's identity, and never writes to its data catalog.

### The contract

`akko_mcp_trino.testing` publishes the contract that checks these rules. Install it in the
provider's test environment with `pip install "akko-mcp-trino[contract]"`, which brings `pytest`
with a version range, not a pin. It needs no network, no data catalog and no Trino. The provider
sends its requests to a transport that answers from the data catalog's recorded answers, or, on
purpose, late, with a 401, a 503, a body that is not JSON, or a 500 that holds the secret.

Subclass `ContextContract` in the provider's tests and give it a `harness` fixture that returns a
`ContractHarness`: the factory `build(env, transport)`, the function `replay` that plays back the
recorded answers, the settings `env`, the `secret` they hold, and the `ContractNames` of the
recorded objects.

```python
import pytest
from akko_mcp_trino.context import TableRef
from akko_mcp_trino.testing import ContextContract, ContractHarness, ContractNames

from my_package.provider import MyProvider
from tests.recordings import replay  # answers an httpx.Request from the recorded answers

# A quote, a backslash and an accent, so that a secret escaped or encoded is still found.
SECRET = 'a-"secret\\é-to-find'

NAMES = ContractNames(
    catalog="warehouse",                  # a Trino catalog the provider maps
    unmapped_catalog="lakeside",          # one it does not map
    described=TableRef("warehouse", "ledger", "invoices"),
    partitioned=TableRef("warehouse", "ledger", "payments"),
    partition_column="booking_month",
    dropped=TableRef("warehouse", "ledger", "refunds"),     # marked deleted in the data catalog
    unknown=TableRef("warehouse", "ledger", "ghosts"),      # not in the data catalog
    trap=TableRef("warehouse", "o'brien.été", "l'année fiscale.v2"),
    term_column="invoice_total",          # a column of `described` with a served term
    foreign_glossary="personnel",         # a glossary outside the allow list
)


class TestMyProviderContract(ContextContract):
    @pytest.fixture
    def harness(self):
        return ContractHarness(
            build=lambda env, transport: MyProvider(env, transport=transport),
            replay=replay,
            env={
                "MY_CATALOG_TOKEN": SECRET,
                "MY_CATALOG_GLOSSARIES": "finance",
                "MY_CATALOG_SERVE_OWNER": "true",
            },
            secret=SECRET,
            names=NAMES,
        )
```

Every HTTP request the provider sends must go through `transport`. Names are given as Trino lists
them, and should appear nowhere else in the provider's logs or `check_lines()`. The settings serve
the term's glossary, and the owner when the data catalog has one. The secret is looked for as
written, JSON-escaped, URL-encoded, as Python writes it and in base64, so choose one with a quote,
a backslash and a non-ASCII character. Logs are read from the root logger: a logger that does not
propagate escapes the check. Keep the entry point's factory in the module of the provider's class,
which the import check loads alone.

The contract checks that:

- the context comes back with its partition columns;
- an unknown table and a deleted entity are left out;
- an unmapped catalog costs no request;
- nothing outside `served_sets()` is returned, and those sets follow the settings. Without its
  allow lists, a provider still describes the table and serves no set. The owner, when the
  provider declares one, is served only when its setting asks for it; a provider whose data
  catalog has no owner declares none and skips that check;
- no type carries a data value;
- a slow data catalog returns control within the deadline plus
  `ContextContract.REPLY_MARGIN_SECONDS` (0.5 s), and leaves no request open;
- a 401 raises `ProviderRefused` and is not retried;
- a timeout, a 503 or a body that is not JSON raises `ProviderUnavailable`;
- neither the logs (`httpx` at `WARNING`, as the server sets it), nor `check_lines()` read again
  after the data catalog's errors, nor an exception hold the secret;
- the logs and `check_lines()` hold no object name;
- the provider's module loads nothing of the core's Trino side;
- a factory that refuses a malformed setting does not put the secret in its message (each setting
  that holds it is given back padded, then in quotes);
- a trap name is found under its exact name;
- every name is folded.

A provider that sends no request, such as `file`, skips the checks that need one, with
`no HTTP`. The `snapshot` check is skipped in this version.

`ContextContract.DEADLINE_SECONDS` (5 s) is the deadline the checks give a call that should
succeed, and `SLOW_DEADLINE_SECONDS` (1 s) the one they give a data catalog that does not answer.
They are class attributes: raise them in your subclass when your CI is slow, and keep the second
well under 30 s, the time the slow data catalog takes to answer.

### The visibility filter, proven over each provider

The visibility filter belongs to the core, and it is proven over each provider. `GateContract`
builds the server as it runs, with the provider and its recorded data catalog, in the `jwt` and
`impersonation` identity modes (the second with `MCP_CONTEXT_REQUIRE_SELECT=true`), and puts
`FakeTrino` in place of Trino. `FakeTrino` answers the core's own statements from what each user
sees (`Visible`: the tables, the columns Trino lists for each, and the tables it lists but refuses
a read of). It records every statement with its user, its token for Trino, its text and its client
tags (`Statement`).

Each check runs twice: once with a Trino that reports a hidden object as missing (an access
control that filters the metadata), and once with one that refuses it (`PERMISSION_DENIED`, as
rules do; `FakeTrino(..., existing=...)`). A caller sees every table of the harness but one
object: a table, then a column of the described table, then the target of one of its joins. The
contract checks that:

- nothing of that object, neither its name nor any text the data catalog gives it alone, reaches
  a reply, a `context_sources` status, an audit line, a log line or a metric, each checked on its
  own;
- every statement runs as the caller, with the caller's token in `jwt`, and names only what Trino
  has listed to them; the agent's own statements may also name what the agent wrote;
- the one statement that names a hidden table is the `DESCRIBE` the agent asked for;
- a hidden table and a missing one (`names.unknown`) get the same from the core: the keys, kinds
  and statuses apart from Trino's own error, the audit line apart from its error code, and the
  statements;
- with `MCP_CONTEXT_REQUIRE_SELECT=true`, a table Trino lists but refuses a read of gets its
  columns, no context and `not_checked`.

A caller who sees everything is served the three objects, so the checks test something. Add the
contract to the class above, with a `gate` fixture:

```python
from akko_mcp_trino.testing import GateContract, GateHarness


class TestMyProviderContract(ContextContract, GateContract):
    # the harness fixture above, then:
    @pytest.fixture
    def gate(self, harness):
        return GateHarness(
            contract=harness,
            hidden_table=TableRef("warehouse", "ledger", "payroll"),         # described
            hidden_column="discount_code",                                   # of `described`
            hidden_join_target=TableRef("warehouse", "ledger", "payments"),  # joined from it
        )
```

The three objects are in the recorded data catalog, written as Trino lists them and as an agent
names a table (letters, digits, `_` and `-`), at least four characters long. Their names are part
of no name or text the caller may see: the contract looks for them, and for each text the data
catalog gives them alone, in everything the caller receives.
`tests/context/test_gate_contract_file.py` runs it on `file`.

A data catalog with no notion of a join leaves `hidden_join_target` out (`None`). The join scenario
is then dropped from every check, and
`test_a_hidden_join_target_is_checked_when_the_catalogue_gives_joins` is skipped and says why. The
contract fails if the provider still gives a join for a table it describes ("the provider gives
joins: set hidden_join_target"), so that a provider with joins cannot leave them unchecked. With a
target, that test plays the join scenario alone.
