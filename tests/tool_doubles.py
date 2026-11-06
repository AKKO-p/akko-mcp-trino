"""Doubles shared by the tests of the tools: an MCP server, Trino for the context, the setup.

It is not a test module: ``test_tools`` and ``test_tool_context`` use it, and so do the tests
that need the core's configuration.
"""

import re
import time
from dataclasses import replace

from akko_mcp_trino import trino_client
from akko_mcp_trino.audit import (
    InMemoryAudit,
    current_call,
    reset_current_request_id,
    set_current_request_id,
)
from akko_mcp_trino.auth import Principal
from akko_mcp_trino.config import Config
from akko_mcp_trino.identity import reset_current_principal, set_current_principal
from akko_mcp_trino.tools import register_query_tools
from tests.context.memory_provider import setup_for


class FakeMCP:
    """Captures the functions decorated with @mcp.tool() so tests can call them directly."""

    def __init__(self):
        self.tools = {}
        self.annotations = {}

    def tool(self, annotations=None, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            self.annotations[fn.__name__] = annotations
            return fn

        return deco


def core_config(**over) -> Config:
    """The core's configuration of the tests, without any extension, with ``over``."""
    base = dict(
        trino_host="h",
        trino_identity_mode="service",
        query_timeout_seconds=0.0,
        call_timeout_seconds=0.0,
        trino_port=8443,
        trino_user="svc",
        trino_catalog="c",
        max_rows=10,
        read_only=True,
        auth_enabled=False,
        server_name="t",
        health_port=3001,
        jwks_url="",
        oidc_issuer="",
        oidc_audience="",
        transport="streamable-http",
        mcp_port=3000,
        auth_required=False,
        trino_http_scheme="https",
    )
    base.update(over)
    return Config(**base)


def in_request(fn):
    """``fn()`` inside a request of alice_admin, request id req-1."""
    t1 = set_current_principal(Principal(subject="alice_admin", token_id="jti-9"))
    t3 = set_current_request_id("req-1")
    try:
        return fn()
    finally:
        reset_current_request_id(t3)
        reset_current_principal(t1)


class GateTrino:
    """Trino for describe_table with context: the table, the canary, the select check, the
    proofs of join targets and a sample.

    Each query is recorded with its user and the client tags it would carry. Time is
    simulated: a statement that starts with a prefix of ``slow`` takes that many seconds,
    added to ``elapsed`` without sleeping. Given a lower ``timeout``, it stops then with a
    plain ``TimeoutError``, as the Trino client does; one that would outlive the call marks
    it expired, as the client does. Only ``pause`` really sleeps."""

    def __init__(
        self,
        columns=("id", "amount"),
        truncated=False,
        describe_error=None,
        schemas=(),
        slow=None,
        pause=None,
    ):
        self.columns = list(columns)
        self.truncated = truncated
        self.describe_error = describe_error
        self.schemas = list(schemas)
        self.slow = dict(slow or {})
        self.pause = dict(pause or {})
        self.elapsed = 0.0
        self.timeouts = []
        self.calls = []

    def _take(self, seconds, timeout):
        self.timeouts.append(timeout)
        if 0 < timeout < seconds:
            self.elapsed += timeout
            raise TimeoutError("the query exceeded its time budget")
        call = current_call()
        left = call.time_left() if call is not None else None
        if left is not None and seconds >= left - self.elapsed:
            raise call.timed_out()
        self.elapsed += seconds

    def query(self, sql, user=None, params=None, **keywords):
        self.calls.append((sql, user, trino_client._query_tags.get()))
        for prefix, seconds in self.slow.items():
            if sql.startswith(prefix):
                self._take(seconds, keywords.get("timeout", 0))
        for prefix, seconds in self.pause.items():
            if sql.startswith(prefix):
                time.sleep(seconds)
        answer = self.answer(sql)
        if answer is not None:
            return answer
        if sql == "DESCRIBE demo.sales.orders":
            if self.describe_error is not None:
                raise self.describe_error
            rows = [[c, "bigint", "", ""] for c in self.columns]
            columns = ["Column", "Type", "Extra", "Comment"]
            return {"columns": columns, "rows": rows, "truncated": self.truncated}
        if sql == "SHOW CATALOGS":
            return {"columns": ["Catalog"], "rows": [["demo"]], "truncated": False}
        if sql.startswith("SELECT schema_name FROM"):
            return {"columns": ["x"], "rows": [[n] for n in self.schemas], "truncated": False}
        if sql.startswith(("SELECT table_name FROM", "EXPLAIN (TYPE VALIDATE)")):
            return {"columns": ["x"], "rows": [], "truncated": False}
        if sql.startswith("SELECT * FROM demo.sales.orders LIMIT"):
            return {"columns": ["id"], "rows": [[1]], "row_count": 1}
        raise AssertionError(f"unexpected statement: {sql}")

    def answer(self, sql):
        """The answer of a subclass to ``sql``, before the statements above; None for none."""
        return None

    def ping(self):
        return None


# What the filter asks to prove a table of demo.sales: the tables of its schema, by no name.
_SALES_TABLES = (
    "SELECT table_name FROM \"demo\".information_schema.tables WHERE table_schema = 'sales'"
)


class SchemaTrino(GateTrino):
    """Trino for describe_schema with context: the schema sales of demo, its ``tables``
    (one column ``id`` each, no comment), and the proofs of the filter, which list the
    same tables."""

    def __init__(self, tables=("customers", "orders"), **kw):
        super().__init__(schemas=["sales"], **kw)
        self.tables = list(tables)

    def answer(self, sql):
        names = sorted(re.findall(r"table_name = '([^']*)'", sql))
        if sql.startswith("SELECT table_name, table_type FROM demo.information_schema.tables"):
            start, limit = map(int, re.search(r"OFFSET (\d+) LIMIT (\d+)$", sql).groups())
            rows = [[t, "BASE TABLE"] for t in self.tables][start : start + limit]
        elif "system.jdbc.columns" in sql:
            rows = [[t, "id", "bigint", None, 1] for t in names]
        elif "system.metadata.table_comments" in sql:
            rows = [[t, None] for t in names]
        elif sql == _SALES_TABLES:
            rows = [[t] for t in self.tables]
        else:
            return None
        return {"columns": ["x"], "rows": rows, "truncated": False}


def with_context(setups, client, *providers, describers=(), call_timeout_seconds=0.0, **settings):
    """The tools over ``client`` with a context read from ``providers``, under the core's
    configuration with ``settings``; the setup is closed by the caller's ``setups``."""
    config = replace(core_config(), **settings)
    setup = setup_for(config, *providers)
    setups.append(setup)
    mcp = FakeMCP()
    audit = InMemoryAudit()
    tools = register_query_tools(
        mcp,
        client,
        audit=audit,
        call_timeout_seconds=call_timeout_seconds,
        describe_schema_tables=config.describe_schema_tables,
        context=setup,
    )
    for describer in describers:
        tools.add_describer(describer)
    return mcp, audit, tools
