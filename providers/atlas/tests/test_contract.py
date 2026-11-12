"""The published contract of a context provider, run on the Atlas provider.

Two recorded catalogues play it. ``atlas-2.1-seed`` is the reference: an Atlas 2.1
seeded with a neutral catalogue that covers every role, read through an account that may
only read. ``atlas-2.1`` is a real catalogue made neutral: it plays the
roles it covers, and each check that needs a role it lacks is skipped, with the reason.
"""

from collections.abc import Mapping

import httpx
import pytest
from akko_mcp_trino.context import TableRef
from akko_mcp_trino.testing import ContextContract, ContractHarness, ContractNames
from atlas_replay import ATLAS, FakeAtlas

from akko_mcp_trino_atlas import AtlasProvider
from akko_mcp_trino_atlas.settings import read_settings

# A quote, a backslash and an accent: escaped or encoded, it must still be found.
SECRET = 'reader-"pass\\é-contract'
CATALOG = "lake"


def ref(schema: str, table: str) -> TableRef:
    return TableRef(CATALOG, schema, table)


ENV = {
    "ATLAS_URL": ATLAS,
    "ATLAS_USER": "reader",
    "ATLAS_PASSWORD": SECRET,
    "ATLAS_CLUSTERS": '{"lake": "primary"}',
    "ATLAS_GLOSSARIES": "finance",
    "ATLAS_CLASSIFICATIONS": "confidential",
    "ATLAS_BUSINESS_METADATA": "stewardship",
    "ATLAS_OWNER_ATTRIBUTE": "stewardship.ownerTeam",
    "ATLAS_TERM_STATUS_ATTRIBUTE": "approvalStatus",
}

SEED = ContractNames(
    catalog=CATALOG,
    unmapped_catalog="other",
    described=ref("ledger", "invoices"),
    partitioned=ref("ledger", "payments"),
    partition_column="booking_month",
    dropped=ref("ledger", "legacy_invoices"),
    unknown=ref("ledger", "ghosts"),
    trap=ref("o'brien", "l'année fiscale"),
    term_column="invoice_total",
    foreign_glossary="hr",
)
# The real catalogue lacks three roles; their names below are never looked up.
REAL = ContractNames(
    catalog=CATALOG,
    unmapped_catalog="other",
    described=ref("ledger", "invoices"),
    partitioned=ref("ledger", "invoices"),
    partition_column="invoice_total",
    dropped=ref("ledger", "no_deleted_table"),
    unknown=ref("ledger", "ghosts"),
    trap=ref("o'brien", "été.l'année fiscale.v2"),
    term_column="invoice_total",
    foreign_glossary="no_other_glossary",
)
NAMES = {"atlas-2.1-seed": SEED, "atlas-2.1": REAL}
# The checks a catalogue cannot play, and why; a skip, never a pass.
MISSING_ROLES: Mapping[str, Mapping[str, str]] = {
    "atlas-2.1-seed": {},
    "atlas-2.1": {
        "test_describe_returns_the_context_partition_columns_included": (
            "the recorded catalogue has no table with a partition key"
        ),
        "test_a_deleted_entity_is_never_returned": (
            "the recorded catalogue has no deleted table that is not active under another type"
        ),
        "test_nothing_outside_served_sets_is_returned": (
            "the recorded catalogue holds one glossary only: none outside the allow list"
        ),
        "test_trap_names_are_looked_up_as_trino_lists_them": (
            "the provider never looks up a dotted name, which Atlas could not tell from another "
            "table; the recorded trap holds dots, and the seeded catalogue plays the role"
        ),
    },
}


def build(env: Mapping[str, str], transport: httpx.AsyncBaseTransport) -> AtlasProvider:
    return AtlasProvider(read_settings(env), transport=transport)


def harness_for(version: str) -> ContractHarness:
    return ContractHarness(
        build=build, replay=FakeAtlas(version), env=ENV, secret=SECRET, names=NAMES[version]
    )


class TestAtlasContract(ContextContract):
    """The contract on the reference catalogue and on the real one, role by role."""

    @pytest.fixture(params=sorted(NAMES))
    def harness(self, request: pytest.FixtureRequest) -> ContractHarness:
        why = MISSING_ROLES[request.param].get(request.node.originalname)
        if why:
            pytest.skip(f"{request.param}: {why}")
        return harness_for(request.param)


def test_every_skipped_check_names_a_check_of_the_contract():
    checks = {name for name in dir(ContextContract) if name.startswith("test_")}
    for skipped in MISSING_ROLES.values():
        assert set(skipped) <= checks


def test_the_reference_catalogue_plays_every_role():
    atlas = FakeAtlas("atlas-2.1-seed")
    names = [e["attributes"]["qualifiedName"] for t in atlas.bulk for e in atlas.entities(t)]
    assert "ledger.payments@primary" in names and "archive.snapshots@primary" in names
    assert "ledger.legacy_invoices@primary" not in names  # deleted: the bulk read omits it
    payments = atlas.entity("hive_table", "ledger.payments@primary")
    assert [h["displayText"] for h in payments["relationshipAttributes"]["partitionKeys"]] == [
        "booking_month"
    ]
    glossaries = {t["qualifiedName"].rpartition("@")[2] for t in atlas.terms.values()}
    assert glossaries == {"finance", "hr"}
