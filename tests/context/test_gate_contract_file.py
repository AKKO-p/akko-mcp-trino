"""The gate proven end to end on the ``file`` provider, and the faults the proof catches.

``TestFileProviderGate`` runs the published ``GateContract``: the real provider, the core's
tools, a fake Trino that answers from each caller's rights and records every statement.
The tests below break the core, one rule at a time, and the contract must catch each.
"""

import json
import logging

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


def gate_of(tmp_path, document=DOCUMENT, **over) -> GateHarness:
    path = tmp_path / "context.json"
    path.write_text(json.dumps(document), encoding="utf-8")
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
UNREADABLE = "test_a_table_listed_but_not_readable_has_no_context_when_a_read_is_required"


def _ungated(monkeypatch):
    """A core that serves the catalogue's context as it comes, without the gate."""
    monkeypatch.setattr("akko_mcp_trino.context.reply.filter_table", lambda ctx, **_: ctx)


def _trusting(monkeypatch):
    """A core that takes every table a catalogue cites as proven, and names it to Trino."""
    monkeypatch.setattr(
        "akko_mcp_trino.context.visibility.Visibility.proven", lambda self, key, ref: True
    )


def _explaining(monkeypatch):
    """A core that, when Trino refuses to describe a table, asks the catalogue about it and
    says it served it."""
    from akko_mcp_trino import tools
    from akko_mcp_trino.audit import current_call, note
    from akko_mcp_trino.context.reply import ContextService

    services = []
    built = ContextService.__init__

    def keep(self, *args, **kwargs):
        built(self, *args, **kwargs)
        services.append(self)

    original = tools.error_reply

    def explained(exc, sql="", visible=None):
        out = json.loads(original(exc, sql, visible))
        if getattr(exc, "error_name", None) == "PERMISSION_DENIED":
            asked = current_call().tables[-1]
            ref = TableRef.of(*asked.split("."))
            out["context_sources"] = services[-1].for_table(ref, [], complete=True)[
                "context_sources"
            ]
            note(metadata_served=[asked])
        return json.dumps(out)

    monkeypatch.setattr(ContextService, "__init__", keep)
    monkeypatch.setattr(tools, "error_reply", explained)


def _borrowing(monkeypatch):
    """A core whose statements carry another caller's token to Trino."""
    monkeypatch.setattr(
        "akko_mcp_trino.trino_client.current_bearer", lambda: "bearer-of-gate-sees-all"
    )


def _reading_anyway(monkeypatch):
    """A core that takes every read as validated."""
    monkeypatch.setattr(
        "akko_mcp_trino.context.visibility.Visibility.selectable", lambda self, *a, **k: True
    )


def _side_channel(write):
    """A core that filters what it serves, and writes the catalogue's columns elsewhere."""

    def breaks(monkeypatch):
        from akko_mcp_trino.context import reply

        original = reply.filter_table

        def leaky(ctx, **keywords):
            write(sorted(ctx.columns))
            return original(ctx, **keywords)

        monkeypatch.setattr(reply, "filter_table", leaky)

    return breaks


def _noted(columns):
    from akko_mcp_trino.audit import note

    note(tables=columns)


def _logged(columns):
    logging.getLogger("akko_mcp_trino.context.reply").info("read the columns %s", columns)


def _metered(monkeypatch):
    from akko_mcp_trino.metrics import Metrics

    built = Metrics.__init__
    made: list = []  # the metrics of this test only

    def keep(self, *args, **kwargs):
        built(self, *args, **kwargs)
        made.append(self)

    def counted(columns):
        made[-1].context_calls.labels(provider=",".join(columns)).inc()

    monkeypatch.setattr(Metrics, "__init__", keep)
    _side_channel(counted)(monkeypatch)


@pytest.mark.parametrize(
    ("breaks", "check", "says"),
    [
        (_ungated, LEAK, "reaches the caller .* through a reply"),
        (_side_channel(_noted), LEAK, "reaches the caller .* through the audit line"),
        (_side_channel(_logged), LEAK, "reaches the caller .* through the logs"),
        (_metered, LEAK, "reaches the caller .* through the metrics"),
        (_trusting, STATEMENTS, "names what Trino had not listed"),
        (_borrowing, STATEMENTS, "another token for Trino than the caller's \\(.*jwt"),
        (_explaining, SHAPE, "must answer the same .*a Trino that refuses"),
        (_reading_anyway, UNREADABLE, "must get its columns and no context"),
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


def test_the_fake_trino_refuses_what_exists_and_is_hidden_when_told_trino_has_it():
    invoices = TableRef("warehouse", "ledger", "invoices")
    payroll = TableRef("warehouse", "ledger", "payroll")
    fake = FakeTrino(
        {"ana": Visible({invoices: ("invoice_id",)}, unreadable=frozenset())},
        existing={invoices: ("invoice_id", "total"), payroll: ("gross_pay",)},
    )
    ana = fake.connect(user="ana").cursor()
    with pytest.raises(Exception, match="PERMISSION_DENIED.*Cannot show columns of table"):
        ana.execute("DESCRIBE warehouse.ledger.payroll")
    with pytest.raises(Exception, match="TABLE_NOT_FOUND"):
        ana.execute("DESCRIBE warehouse.ledger.ghosts")
    with pytest.raises(Exception, match="PERMISSION_DENIED.*Cannot select from columns"):
        ana.execute('EXPLAIN (TYPE VALIDATE) SELECT "total" FROM "warehouse"."ledger"."invoices"')
    with pytest.raises(Exception, match="PERMISSION_DENIED.*Cannot select from columns"):
        ana.execute(
            'EXPLAIN (TYPE VALIDATE) SELECT "gross_pay" FROM "warehouse"."ledger"."payroll"'
        )
    with pytest.raises(Exception, match="COLUMN_NOT_FOUND"):
        ana.execute('EXPLAIN (TYPE VALIDATE) SELECT "nosuch" FROM "warehouse"."ledger"."invoices"')


def test_the_fake_trino_lists_an_unreadable_table_and_refuses_its_read():
    invoices = TableRef("warehouse", "ledger", "invoices")
    fake = FakeTrino(
        {"ana": Visible({invoices: ("invoice_id",)}, unreadable=frozenset({invoices}))}
    )
    ana = fake.connect(user="ana").cursor()
    ana.execute("DESCRIBE warehouse.ledger.invoices")
    assert ana.fetchmany(5) == [["invoice_id", "varchar", "", ""]]
    with pytest.raises(Exception, match="PERMISSION_DENIED.*Cannot select from columns"):
        ana.execute(
            'EXPLAIN (TYPE VALIDATE) SELECT "invoice_id" FROM "warehouse"."ledger"."invoices"'
        )


def test_the_fake_trino_records_the_token_each_connection_carries():
    import trino.auth

    fake = FakeTrino({})
    with pytest.raises(Exception, match="does not answer"):
        fake.connect(
            user="ana", auth=trino.auth.JWTAuthentication("token-of-ana")
        ).cursor().execute("SELECT 1")
    with pytest.raises(Exception, match="does not answer"):
        fake.connect(user="ben", auth=trino.auth.BasicAuthentication("ben", "pw")).cursor().execute(
            "SELECT 1"
        )
    assert [(s.user, s.token) for s in fake.statements] == [("ana", "token-of-ana"), ("ben", "")]


# The same catalogue, without a single join: a catalogue that has no notion of one.
WITHOUT_JOINS = {
    **DOCUMENT,
    "tables": [{k: v for k, v in t.items() if k != "joins"} for t in DOCUMENT["tables"]],
}
CHECKS = [name for name in dir(GateContract) if name.startswith("test_")]


JOIN_CHECK = "test_a_hidden_join_target_is_checked_when_the_catalogue_gives_joins"


@pytest.mark.parametrize("check", [c for c in CHECKS if c != JOIN_CHECK])
def test_a_provider_without_joins_passes_every_check(tmp_path, monkeypatch, check):
    gate = gate_of(tmp_path, WITHOUT_JOINS, hidden_join_target=None)
    getattr(GateContract(), check)(gate, monkeypatch)


def test_a_provider_without_joins_says_why_the_join_is_not_checked(tmp_path, monkeypatch):
    gate = gate_of(tmp_path, WITHOUT_JOINS, hidden_join_target=None)
    with pytest.raises(pytest.skip.Exception, match="the provider gives no join"):
        getattr(GateContract(), JOIN_CHECK)(gate, monkeypatch)


@pytest.mark.parametrize("check", [c for c in CHECKS if c != JOIN_CHECK])
def test_a_provider_that_gives_joins_cannot_leave_the_join_unchecked(tmp_path, monkeypatch, check):
    gate = gate_of(tmp_path, hidden_join_target=None)
    with pytest.raises(
        pytest.fail.Exception, match="the provider gives joins: set hidden_join_target"
    ):
        getattr(GateContract(), check)(gate, monkeypatch)


def test_the_file_provider_keeps_the_join_scenario(tmp_path, monkeypatch):
    gate = gate_of(tmp_path)
    assert gate.hidden_join_target is not None
    GateContract().test_a_hidden_join_target_is_checked_when_the_catalogue_gives_joins(
        gate, monkeypatch
    )
