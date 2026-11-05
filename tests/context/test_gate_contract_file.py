"""The gate proven end to end on the ``file`` provider, and the faults the proof catches.

``TestFileProviderGate`` runs the published ``GateContract``: the real provider, the core's
tools, a fake Trino that answers from each caller's rights and records every statement.
The tests below break the core, one rule at a time, and the contract must catch each.
"""

import json
import re

import pytest

from akko_mcp_trino.context import TableRef
from akko_mcp_trino.context.file import from_env
from akko_mcp_trino.testing import (
    ContractHarness,
    ContractNames,
    FakeTrino,
    GateContract,
    GateHarness,
    Visible,
)

# Neutral, fictitious: a ledger whose invoices join their payments, and a payroll beside them.
DOCUMENT = {
    "version": 1,
    "catalogs": ["warehouse"],
    "tables": [
        {
            "catalog": "warehouse",
            "schema": "ledger",
            "table": "invoices",
            "description": "One row per invoice",
            "owner": "billing-team",
            "tags": [{"name": "confidential", "set": "sensitivity"}],
            "terms": ["t-invoice"],
            "grain": ["invoice_id", "discount_code"],
            "joins": [
                {
                    "columns": ["payment_ref"],
                    "target": {"catalog": "warehouse", "schema": "ledger", "table": "payments"},
                    "target_columns": ["payment_ref"],
                }
            ],
            "columns": {
                "invoice_id": {"description": "Number printed on the invoice"},
                "discount_code": {
                    "description": "Promotion applied at checkout",
                    "classifications": [{"name": "commercial", "set": "sensitivity"}],
                    "terms": ["t-rebate"],
                },
                "payment_ref": {"description": "Reference of the settling transfer"},
            },
        },
        {
            "catalog": "warehouse",
            "schema": "ledger",
            "table": "payments",
            "description": "One row per transfer received",
            "columns": {
                "payment_ref": {},
                "booking_month": {"description": "Month the transfer was booked"},
            },
        },
        {
            "catalog": "warehouse",
            "schema": "ledger",
            "table": "payroll",
            "description": "Monthly pay slips of the staff",
            # A join to a table neither Trino nor the catalogue holds.
            "joins": [
                {
                    "columns": ["employee_ref"],
                    "target": {"catalog": "warehouse", "schema": "ledger", "table": "staff"},
                    "target_columns": ["employee_ref"],
                }
            ],
            "columns": {
                "employee_ref": {"description": "Badge number of the employee"},
                "gross_pay": {"terms": ["t-wage"]},
            },
        },
    ],
    "terms": {
        "t-invoice": {
            "name": "Invoice",
            "glossary": "finance",
            "definition": "A bill sent to a customer",
            "relations": [
                {"key": "t-rebate", "name": "Rebate", "glossary": "finance", "relation": "related"}
            ],
        },
        "t-rebate": {
            "name": "Rebate",
            "glossary": "finance",
            "definition": "A price cut granted at checkout",
        },
        "t-wage": {"name": "Wage", "glossary": "finance", "definition": "Pay before deductions"},
    },
}

NAMES = ContractNames(
    catalog="warehouse",
    unmapped_catalog="lakeside",
    described=TableRef("warehouse", "ledger", "invoices"),
    partitioned=TableRef("warehouse", "ledger", "payments"),
    partition_column="booking_month",
    dropped=TableRef("warehouse", "ledger", "refunds"),
    unknown=TableRef("warehouse", "ledger", "ghosts"),
    trap=TableRef("warehouse", "o'brien.été", "l'année fiscale.v2"),
    term_column="discount_code",
    foreign_glossary="personnel",
)


def gate_of(tmp_path, **over) -> GateHarness:
    path = tmp_path / "context.json"
    path.write_text(json.dumps(DOCUMENT), encoding="utf-8")
    contract = ContractHarness(
        build=lambda env, transport: from_env(env),
        replay=lambda request: pytest.fail("the file provider sends no request"),
        env={
            "MCP_CONTEXT_FILE": str(path),
            "MCP_CONTEXT_FILE_GLOSSARIES": "finance",
            "MCP_CONTEXT_FILE_CLASSIFICATIONS": "sensitivity",
            "MCP_CONTEXT_FILE_SERVE_OWNER": "true",
        },
        secret='gate-"secret\\é-1c9e4b7d',
        names=NAMES,
    )
    given = dict(
        contract=contract,
        hidden_table=TableRef("warehouse", "ledger", "payroll"),
        hidden_column="discount_code",
        hidden_join_target=TableRef("warehouse", "ledger", "payments"),
    )
    given.update(over)
    return GateHarness(**given)


class TestFileProviderGate(GateContract):
    @pytest.fixture
    def gate(self, tmp_path):
        return gate_of(tmp_path)


LEAK = "test_nothing_of_a_hidden_object_reaches_any_answer_status_or_audit_line"
STATEMENTS = "test_no_statement_holds_a_name_trino_had_not_listed_to_that_caller"
SHAPE = "test_a_hidden_and_a_missing_table_answer_the_same_shape"


def _ungated(monkeypatch):
    """A core that serves the catalogue's context as it comes, without the gate."""
    monkeypatch.setattr("akko_mcp_trino.context.reply.filter_table", lambda ctx, **_: ctx)


def _trusting(monkeypatch):
    """A core that takes every table a catalogue cites as proven, and names it to Trino."""
    monkeypatch.setattr(
        "akko_mcp_trino.context.visibility.Visibility.proven", lambda self, key, ref: True
    )


def _explaining(monkeypatch):
    """A core that, when Trino does not describe a table, asks the catalogue about it."""
    from akko_mcp_trino import tools
    from akko_mcp_trino.context.reply import ContextService

    services = []
    built = ContextService.__init__

    def keep(self, *args, **kwargs):
        built(self, *args, **kwargs)
        services.append(self)

    original = tools.error_reply

    def explained(exc, sql="", visible=None):
        out = json.loads(original(exc, sql, visible))
        named = re.search(r"Table '([^']+)'", str(exc))
        if named and services:
            ref = TableRef.of(*named.group(1).split("."))
            out["context_sources"] = services[-1].for_table(ref, [], complete=True)[
                "context_sources"
            ]
        return json.dumps(out)

    monkeypatch.setattr(ContextService, "__init__", keep)
    monkeypatch.setattr(tools, "error_reply", explained)


@pytest.mark.parametrize(
    ("breaks", "check", "says"),
    [
        (_ungated, LEAK, "reaches the caller"),
        (_trusting, STATEMENTS, "names what Trino had not listed"),
        (_explaining, SHAPE, "a hidden and a missing table must answer the same"),
    ],
)
def test_the_gate_contract_catches_a_core_that_breaks_a_rule(
    tmp_path, monkeypatch, breaks, check, says
):
    breaks(monkeypatch)
    with pytest.raises(pytest.fail.Exception, match=says):
        getattr(GateContract(), check)(gate_of(tmp_path), monkeypatch)


@pytest.mark.parametrize(
    ("over", "says"),
    [
        (dict(hidden_column="nosuch"), "the hidden column must be a column the catalogue"),
        (
            dict(hidden_join_target=TableRef("warehouse", "ledger", "payroll")),
            "the hidden join target must be the target of a join",
        ),
        (
            dict(hidden_table=TableRef("warehouse", "ledger", "refunds")),
            "the hidden table must be a table the catalogue describes",
        ),
        (dict(hidden_column="id"), "4 characters"),
        (
            dict(hidden_table=TableRef("warehouse", "Ledger", "payroll")),
            "as TableRef.of does",
        ),
    ],
)
def test_the_gate_contract_refuses_names_that_cannot_test_anything(
    tmp_path, monkeypatch, over, says
):
    with pytest.raises(pytest.fail.Exception, match=says):
        GateContract().test_a_hidden_and_a_missing_table_answer_the_same_shape(
            gate_of(tmp_path, **over), monkeypatch
        )


def test_the_fake_trino_lists_only_what_each_user_sees_and_records_every_statement():
    invoices = TableRef("warehouse", "ledger", "invoices")
    fake = FakeTrino(
        {
            "ana": Visible({invoices: ("invoice_id", "payment_ref")}),
            "ben": Visible({}),
        }
    )
    ana = fake.connect(user="ana", client_tags=["mcp-gate"]).cursor()
    ana.execute("SHOW CATALOGS")
    assert ana.fetchmany(10) == [["system"], ["warehouse"]]
    ana.execute('DESCRIBE "warehouse"."ledger"."invoices"')
    assert [row[0] for row in ana.fetchmany(10)] == ["invoice_id", "payment_ref"]
    assert ana.description[0][0] == "Column"
    ben = fake.connect(user="ben").cursor()
    with pytest.raises(Exception, match="does not exist"):
        ben.execute("DESCRIBE warehouse.ledger.invoices")
    with pytest.raises(Exception, match="does not answer"):
        ben.execute("SELECT 1")
    ben.cancel()
    first, second, third, fourth = fake.statements
    assert (first.user, first.client_tags, first.unlisted) == ("ana", ("mcp-gate",), ())
    # Listed by SHOW CATALOGS, never yet the schema nor the table.
    assert second.unlisted == ("ledger", "invoices")
    assert (third.user, third.client_tags, third.unlisted) == (
        "ben",
        (),
        ("warehouse", "ledger", "invoices"),
    )
    assert fake.unanswered == [fourth]


def test_a_server_that_fails_to_build_closes_the_provider(tmp_path, monkeypatch):
    gate = gate_of(tmp_path)
    built, closed = [], []
    build = gate.contract.build

    def tracked(env, transport):
        provider = build(env, transport)
        built.append(provider)
        aclose = provider.aclose

        async def close():
            closed.append(True)
            await aclose()

        provider.aclose = close
        return provider

    def refuse(config, **_):
        raise ValueError("the server refuses its settings")

    gate.contract.build = tracked
    monkeypatch.setattr("akko_mcp_trino.server.build_server", refuse)
    with pytest.raises(ValueError, match="refuses its settings"):
        GateContract().test_a_hidden_and_a_missing_table_answer_the_same_shape(gate, monkeypatch)
    # The contract's own reading of the catalogue, then the server it failed to build.
    assert len(built) == len(closed) == 2


def test_the_fake_trino_validates_only_a_read_of_what_the_user_sees():
    invoices = TableRef("warehouse", "ledger", "invoices")
    fake = FakeTrino({"ana": Visible({invoices: ("invoice_id", "payment_ref")})})
    ana = fake.connect(user="ana").cursor()
    ana.execute('EXPLAIN (TYPE VALIDATE) SELECT "invoice_id" FROM "warehouse"."ledger"."invoices"')
    assert ana.fetchmany(5) == [[True]]
    with pytest.raises(Exception, match="COLUMN_NOT_FOUND"):
        ana.execute('EXPLAIN (TYPE VALIDATE) SELECT "total" FROM "warehouse"."ledger"."invoices"')
    with pytest.raises(Exception, match="TABLE_NOT_FOUND"):
        ana.execute('EXPLAIN (TYPE VALIDATE) SELECT "total" FROM "warehouse"."ledger"."refunds"')


def test_the_fake_trino_reads_the_columns_of_a_page_from_an_offset_or_whole():
    invoices = TableRef("warehouse", "ledger", "invoices")
    fake = FakeTrino({"ana": Visible({invoices: ("invoice_id", "payment_ref", "total")})})
    ana = fake.connect(user="ana").cursor()
    select = (
        "SELECT * FROM (SELECT table_name, column_name, type_name, remarks, ordinal_position "
        "FROM system.jdbc.columns WHERE table_cat = 'warehouse' AND table_schem = 'ledger' "
        "AND table_name = 'invoices') ORDER BY table_name, ordinal_position"
    )
    ana.execute(select)
    assert [row[1] for row in ana.fetchmany(10)] == ["invoice_id", "payment_ref", "total"]
    ana.execute(select + " OFFSET 1")
    assert [row[1] for row in ana.fetchmany(10)] == ["payment_ref", "total"]
