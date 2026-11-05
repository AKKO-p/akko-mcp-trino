"""The published contract, run on the ``file`` provider: the reference every adapter follows.

The ``file`` provider sends no HTTP request: the contract skips the checks that need one
and says so (``no HTTP``). ``tests/context/test_contract_http.py`` runs the same contract
on a provider over HTTP.
"""

import json

import httpx
import pytest

from akko_mcp_trino.context import TableRef
from akko_mcp_trino.context.file import from_env
from akko_mcp_trino.testing import ContextContract, ContractHarness, ContractNames

# A file keeps names as a catalogue may write them; the provider folds them as Trino lists them.
DOCUMENT = {
    "version": 1,
    "catalogs": ["Warehouse"],
    "tables": [
        {
            "catalog": "Warehouse",
            "schema": "Ledger",
            "table": "Invoices",
            "description": "One row per invoice",
            "owner": "billing-team",
            "tags": [
                {"name": "confidential", "set": "sensitivity"},
                {"name": "draft", "set": "internal-only"},
            ],
            "terms": ["t-invoice", "t-staff"],
            "properties": {
                "keep": {"value": "10y", "set": "retention"},
                "budget": {"value": "high", "set": "cost"},
            },
            "grain": ["Invoice_Id"],
            "joins": [
                {
                    "columns": ["Payment_Ref"],
                    "target": {"catalog": "warehouse", "schema": "ledger", "table": "payments"},
                    "target_columns": ["Payment_Ref"],
                }
            ],
            "columns": {
                "Invoice_Id": {},
                "Invoice_Total": {
                    "description": "Amount due, taxes included",
                    "terms": ["t-invoice", "t-staff"],
                    "classifications": [
                        {"name": "financial", "set": "sensitivity"},
                        {"name": "draft", "set": "internal-only"},
                    ],
                    "properties": {
                        "unit": {"value": "eur", "set": "retention"},
                        "budget": {"value": "low", "set": "cost"},
                    },
                },
                "Payment_Ref": {},
            },
        },
        {
            "catalog": "warehouse",
            "schema": "ledger",
            "table": "payments",
            "description": "One row per payment",
            "columns": {"Booking_Month": {"description": "The month booked"}, "payment_ref": {}},
        },
        {
            "catalog": "warehouse",
            "schema": "o'brien.été",
            "table": "l'année fiscale.v2",
            "description": "A name to quote",
            "columns": {"période": {}},
        },
    ],
    "terms": {
        "t-invoice": {
            "name": "Invoice",
            "glossary": "finance",
            "definition": "A bill sent to a customer",
            "categories": ["billing"],
            "relations": [
                {"key": "t-staff", "name": "Staff", "glossary": "personnel", "relation": "related"},
                {"key": "t-payment", "name": "Payment", "glossary": "finance", "relation": "part"},
            ],
        },
        "t-payment": {"name": "Payment", "glossary": "finance"},
        "t-staff": {"name": "Staff", "glossary": "personnel"},
    },
}

NAMES = ContractNames(
    catalog="warehouse",
    unmapped_catalog="lakeside",
    described=TableRef("warehouse", "ledger", "invoices"),
    partitioned=TableRef("warehouse", "ledger", "payments"),
    partition_column="booking_month",
    # A file forgets a table by leaving it out: its deleted entity is a table it no longer holds.
    dropped=TableRef("warehouse", "ledger", "refunds"),
    unknown=TableRef("warehouse", "ledger", "ghosts"),
    trap=TableRef("warehouse", "o'brien.été", "l'année fiscale.v2"),
    term_column="invoice_total",
    foreign_glossary="personnel",
)


class TestFileProviderContract(ContextContract):
    @pytest.fixture
    def harness(self, tmp_path):
        path = tmp_path / "context.json"
        path.write_text(json.dumps(DOCUMENT), encoding="utf-8")

        def replay(request: httpx.Request) -> httpx.Response:
            # Never called: the file provider reads its file, not the transport.
            return httpx.Response(200, content=path.read_bytes())

        return ContractHarness(
            build=lambda env, transport: from_env(env),
            replay=replay,
            env={
                "MCP_CONTEXT_FILE": str(path),
                "MCP_CONTEXT_FILE_GLOSSARIES": "finance",
                "MCP_CONTEXT_FILE_CLASSIFICATIONS": "sensitivity",
                "MCP_CONTEXT_FILE_PROPERTIES": "retention",
                "MCP_CONTEXT_FILE_SERVE_OWNER": "true",
            },
            # The file provider holds no secret: the contract still checks none appears.
            secret="contract-secret-5f0c8a1e",
            names=NAMES,
        )
