"""Tables read by batch from the recorded Atlas 2.1 and 2.4, under the allow lists."""

import asyncio
import time

import pytest
from akko_mcp_trino.context import (
    CLASSIFICATION,
    CONTEXT_API,
    GLOSSARY,
    OWNER,
    PROPERTY,
    ContextProvider,
    Labelled,
    TableRef,
)
from atlas_replay import ATLAS, TRAP, VERSIONS, FakeAtlas, asked, transport

from akko_mcp_trino_atlas import AtlasProvider, from_env
from akko_mcp_trino_atlas.provider import batches
from akko_mcp_trino_atlas.settings import read_settings

BASE = {
    "ATLAS_URL": ATLAS,
    "ATLAS_USER": "reader",
    "ATLAS_PASSWORD": "reader-pass",
    "ATLAS_CLUSTERS": '{"lake": "primary"}',
}
SERVED = {
    "ATLAS_GLOSSARIES": "finance",
    "ATLAS_CLASSIFICATIONS": "confidential",
    "ATLAS_BUSINESS_METADATA": "stewardship",
    "ATLAS_OWNER_ATTRIBUTE": "stewardship.ownerTeam",
}
INVOICES = TableRef("lake", "ledger", "invoices")
PAYMENTS = TableRef("lake", "ledger", "payments")
GHOSTS = TableRef("lake", "ledger", "ghosts")
TRAP_REF = TableRef("lake", *TRAP)


def provider(atlas: FakeAtlas, **env: str) -> AtlasProvider:
    return AtlasProvider(read_settings({**BASE, **env}), transport=transport(atlas))


def describe(p: AtlasProvider, refs: list[TableRef], seconds: float = 5.0):
    async def run():
        try:
            return await p.describe(refs, deadline=time.monotonic() + seconds)
        finally:
            await p.aclose()

    return asyncio.run(run())


def test_from_env_returns_the_provider():
    built = from_env(BASE)
    try:
        assert isinstance(built, AtlasProvider) and isinstance(built, ContextProvider)
        assert (built.name, built.context_api) == ("atlas", CONTEXT_API)
        assert built.maps("lake") and not built.maps("other")
    finally:
        asyncio.run(built.aclose())


@pytest.mark.parametrize("version", VERSIONS)
def test_tables_and_columns_are_read_in_one_batch_per_type(version):
    atlas = FakeAtlas(version)
    found = describe(provider(atlas), [INVOICES, TRAP_REF, GHOSTS])
    assert set(found) == {INVOICES, TRAP_REF}
    invoices = found[INVOICES]
    assert invoices.description.endswith("of ledger.invoices")
    assert "invoice_total" in invoices.columns
    assert invoices.columns["invoice_total"].description.endswith("ledger.invoices.invoice_total")
    # One request per entity type for the whole batch, the trap name asked as written.
    assert [r.url.path.rsplit("/", 1)[1] for r in atlas.requests] == ["hive_table", "iceberg_table"]
    assert asked(atlas.requests[0]) == [
        "ledger.invoices@primary",
        f"{TRAP[0]}.{TRAP[1]}@primary",
        "ledger.ghosts@primary",
    ]
    # A table found under the first type is not asked again under the next one.
    assert asked(atlas.requests[1]) == ["ledger.ghosts@primary"]


def test_a_table_is_found_under_the_next_type():
    found = describe(provider(FakeAtlas("atlas-2.1")), [TableRef("lake", "archive", "refunds")])
    (refunds,) = found.values()
    assert refunds.description == "description of archive.refunds"
    assert len(refunds.columns) == 7


def test_an_unknown_entity_type_yields_nothing_never_an_outage(caplog):
    atlas = FakeAtlas("atlas-2.4")
    assert atlas.bulk["iceberg_table"]["response"]["status"] == 400
    found = describe(provider(atlas), [GHOSTS, INVOICES])
    assert set(found) == {INVOICES}
    assert "atlas unreachable" not in caplog.text


def test_partition_keys_are_columns_too():
    atlas = FakeAtlas("atlas-2.4")
    payments = atlas.entity("hive_table", "ledger.payments@primary")
    relations = payments["relationshipAttributes"]
    # A partition key that Atlas lists only under partitionKeys still comes with the columns.
    relations["columns"] = [h for h in relations["columns"] if h["displayText"] != "booking_month"]
    found = describe(provider(atlas), [PAYMENTS])
    assert set(found[PAYMENTS].columns) == {"payment_ref", "booking_month", "amount"}
    assert found[PAYMENTS].columns["booking_month"].description.endswith("booking_month")


def test_an_inactive_entity_or_relation_is_ignored():
    atlas = FakeAtlas("atlas-2.4")
    invoices = atlas.entity("hive_table", "ledger.invoices@primary")
    relations = invoices["relationshipAttributes"]
    by_name = {h["displayText"]: h for h in relations["columns"]}
    by_name["invoice_id"]["relationshipStatus"] = "DELETED"  # the relation is gone
    atlas.referred("hive_table")[by_name["Payment_Ref"]["guid"]]["status"] = "DELETED"
    relations["meanings"][0]["entityStatus"] = "DELETED"  # the term Invoice is gone
    invoices["classifications"].append({"typeName": "confidential", "entityStatus": "DELETED"})
    payments = atlas.entity("hive_table", "ledger.payments@primary")
    payments["status"] = "DELETED"
    env = {**SERVED, "ATLAS_GLOSSARIES": "finance,hr"}

    found = describe(provider(atlas, **env), [INVOICES, PAYMENTS])

    assert set(found) == {INVOICES}
    assert set(found[INVOICES].columns) == {"invoice_total"}
    assert found[INVOICES].terms == (relations["meanings"][1]["guid"],)
    assert found[INVOICES].tags == ()
    # A table Atlas deleted is not in its bulk answer: never returned.
    assert TableRef("lake", "ledger", "refunds") not in describe(
        provider(FakeAtlas("atlas-2.4")), [TableRef("lake", "ledger", "refunds")]
    )


def test_mixed_case_catalogue_names_attach_to_trino_lower_case_names():
    atlas = FakeAtlas("atlas-2.4", fold=True)
    invoices = atlas.entity("hive_table", "ledger.invoices@primary")
    invoices["attributes"]["qualifiedName"] = "Ledger.Invoices@primary"
    env = {"ATLAS_CLUSTERS": '{"LAKE": "primary"}'}

    p = provider(atlas, **env)
    assert p.maps("lake") and p.maps("LAKE")
    found = describe(p, [INVOICES])

    assert set(found) == {INVOICES}
    columns = found[INVOICES].columns
    assert "payment_ref" in columns and "Payment_Ref" not in columns
    assert all(name == name.lower() for name in columns)


def test_two_atlas_names_of_one_trino_name_keep_the_lower_case_one_whatever_the_order():
    def answer(reverse: bool):
        atlas = FakeAtlas("atlas-2.4", fold=True)
        invoices = atlas.entity("hive_table", "ledger.invoices@primary")
        twin = {**invoices, "attributes": {**invoices["attributes"]}}
        twin["attributes"]["qualifiedName"] = "LEDGER.INVOICES@primary"
        twin["attributes"]["userDescription"] = "the twin"
        entities = atlas.entities("hive_table")
        entities.insert(0, twin)
        # Two columns that fold to payment_ref: the one already in lower case wins.
        guid = "00000000-0000-4000-8000-0000000000ff"
        referred = atlas.referred("hive_table")
        header = next(
            h
            for h in invoices["relationshipAttributes"]["columns"]
            if h["displayText"] == "Payment_Ref"
        )
        lower = {**referred[header["guid"]], "guid": guid}
        lower["attributes"] = {
            **lower["attributes"],
            "name": "payment_ref",
            "userDescription": "lower",
        }
        referred[guid] = lower
        headers = invoices["relationshipAttributes"]["columns"]
        headers.append({**header, "guid": guid, "displayText": "payment_ref"})
        if reverse:
            entities.reverse()
            headers.reverse()
        return describe(provider(atlas), [INVOICES])[INVOICES]

    first, second = answer(False), answer(True)
    assert first == second
    assert first.description == "userDescription of ledger.invoices"
    assert first.columns["payment_ref"].description == "lower"


def test_the_first_entity_type_wins():
    atlas = FakeAtlas("atlas-2.1")
    hive = atlas.entity("hive_table", "ledger.invoices@primary")
    iceberg = {**hive, "typeName": "iceberg_table", "attributes": {**hive["attributes"]}}
    iceberg["attributes"]["userDescription"] = "as iceberg"
    atlas.entities("iceberg_table").append(iceberg)

    first = describe(provider(atlas), [INVOICES])[INVOICES]
    other = describe(provider(atlas, ATLAS_TABLE_TYPES="iceberg_table,hive_table"), [INVOICES])[
        INVOICES
    ]

    assert first.description == "userDescription of ledger.invoices"
    assert other.description == "as iceberg"


def test_two_catalogs_of_one_cluster_share_one_read():
    atlas = FakeAtlas("atlas-2.4")
    p = provider(atlas, ATLAS_CLUSTERS='{"lake": "primary", "lake_copy": "primary"}')
    copy = TableRef("lake_copy", "ledger", "invoices")
    found = describe(p, [INVOICES, copy])
    assert set(found) == {INVOICES, copy} and found[INVOICES] == found[copy]
    assert asked(atlas.requests[0]) == ["ledger.invoices@primary"]


def test_an_unmapped_catalog_sends_no_request():
    atlas = FakeAtlas("atlas-2.4")
    assert describe(provider(atlas), [TableRef("other", "ledger", "invoices")]) == {}
    assert atlas.requests == []


def test_an_empty_business_metadata_setting_serves_none():
    atlas = FakeAtlas("atlas-2.4")
    p = provider(atlas)
    assert p.served_sets() == {
        GLOSSARY: frozenset(),
        CLASSIFICATION: frozenset(),
        PROPERTY: frozenset(),
        OWNER: frozenset(),
    }
    invoices = describe(p, [INVOICES])[INVOICES]
    assert invoices.description and invoices.columns
    assert (invoices.terms, invoices.tags, dict(invoices.properties), invoices.owner) == (
        (),
        (),
        {},
        "",
    )
    for column in invoices.columns.values():
        assert (column.terms, column.classifications, dict(column.properties)) == ((), (), {})


def test_a_star_is_a_name_never_a_wildcard():
    star = {name: "*" for name in ("ATLAS_GLOSSARIES", "ATLAS_CLASSIFICATIONS")}
    p = provider(FakeAtlas("atlas-2.4"), **star, ATLAS_BUSINESS_METADATA="*")
    sets = p.served_sets()
    assert sets[GLOSSARY] == sets[CLASSIFICATION] == sets[PROPERTY] == frozenset({"*"})
    invoices = describe(p, [INVOICES])[INVOICES]
    assert (invoices.terms, invoices.tags, dict(invoices.properties)) == ((), (), {})
    assert not any(c.classifications or c.terms for c in invoices.columns.values())


def test_the_allow_lists_serve_only_the_sets_they_name():
    atlas = FakeAtlas("atlas-2.4")
    invoices_entity = atlas.entity("hive_table", "ledger.invoices@primary")
    finance, _hr = invoices_entity["relationshipAttributes"]["meanings"]
    p = provider(atlas, **SERVED)
    assert p.served_sets() == {
        GLOSSARY: frozenset({"finance"}),
        CLASSIFICATION: frozenset({"confidential"}),
        PROPERTY: frozenset({"stewardship"}),
        OWNER: frozenset({"stewardship"}),
    }
    invoices = describe(p, [INVOICES])[INVOICES]
    assert invoices.terms == (finance["guid"],)  # Staff@hr is not served
    assert invoices.tags == ()  # draft is not in the allow list
    assert dict(invoices.properties) == {
        "stewardship.approvalStatus": Labelled("APPROVED", "stewardship"),
        "stewardship.ownerTeam": Labelled("ownerTeam of ledger.invoices", "stewardship"),
    }
    assert invoices.owner == "ownerTeam of ledger.invoices"
    total = invoices.columns["invoice_total"]
    assert total.classifications == (Labelled("confidential", "confidential"),)
    assert total.terms == (finance["guid"],)
    assert invoices.columns["invoice_id"].classifications == ()  # PII is not served
    assert dict(total.properties) == {
        "stewardship.approvalStatus": Labelled("IN_REVIEW", "stewardship")
    }


def test_a_business_metadata_value_is_served_as_text():
    atlas = FakeAtlas("atlas-2.4")
    invoices = atlas.entity("hive_table", "ledger.invoices@primary")
    invoices["businessAttributes"]["stewardship"].update(
        {"reviewers": ["team-a", None, "team-b"], "empty": "", "nested": {"a": 1}, "level": 3}
    )
    found = describe(provider(atlas, ATLAS_BUSINESS_METADATA="stewardship"), [INVOICES])
    properties = found[INVOICES].properties
    assert properties["stewardship.reviewers"] == Labelled("team-a, team-b", "stewardship")
    assert properties["stewardship.level"] == Labelled("3", "stewardship")
    assert "stewardship.empty" not in properties and "stewardship.nested" not in properties


def test_a_malformed_answer_serves_what_it_can():
    atlas = FakeAtlas("atlas-2.4")
    invoices = atlas.entity("hive_table", "ledger.invoices@primary")
    invoices["attributes"]["userDescription"] = ["not", "text"]
    invoices["classifications"] = "not a list"
    invoices["relationshipAttributes"]["columns"].append("not an object")
    header = invoices["relationshipAttributes"]["columns"][0]
    column = atlas.referred("hive_table")[header["guid"]]["attributes"]
    column.update(userDescription=None, description="  ", comment=None)
    found = describe(provider(atlas), [INVOICES])
    assert found[INVOICES].description == "description of ledger.invoices"
    assert found[INVOICES].tags == ()
    assert found[INVOICES].columns[column["name"].lower()].description == ""


def test_a_bulk_read_is_split_under_the_query_length():
    atlas = FakeAtlas("atlas-2.4")
    ghosts = [TableRef("lake", "ledger", f"ghost_{i:03}") for i in range(40)]
    refs = [INVOICES, TRAP_REF, *ghosts, PAYMENTS]
    whole = describe(provider(FakeAtlas("atlas-2.4")), refs)

    split = describe(provider(atlas, ATLAS_MAX_QUERY_CHARS="300"), refs)

    assert split == whole and set(split) == {INVOICES, TRAP_REF, PAYMENTS}
    hive = [r for r in atlas.requests if r.url.path.endswith("/hive_table")]
    assert len(hive) > 3
    assert all(len(r.url.query) <= 300 for r in atlas.requests)
    names = [name for r in hive for name in asked(r)]
    assert len(names) == len(set(names)) == len(refs)
    assert all(asked(r)[0] == r.url.params["attr_0:qualifiedName"] for r in hive)


def test_a_name_longer_than_the_query_length_goes_alone():
    long = "s." + "t" * 50 + "@primary"
    groups = batches(["a.b@primary", long, "c.d@primary"], 40)
    assert groups == [["a.b@primary"], [long], ["c.d@primary"]]
    assert batches([], 40) == []


def test_terms_are_not_read_by_this_provider_yet():
    atlas = FakeAtlas("atlas-2.4")
    p = provider(atlas, **SERVED)

    async def run():
        try:
            return await p.terms(["00000000-0000-4000-8000-000000000010"], deadline=1e12)
        finally:
            await p.aclose()

    assert asyncio.run(run()) == {} and atlas.requests == []


def test_check_lines_name_the_addresses_and_sets_never_the_secret():
    p = provider(FakeAtlas("atlas-2.4"), **SERVED)
    lines = p.check_lines()
    asyncio.run(p.aclose())
    assert lines == [
        f"atlas: {ATLAS} (basic); catalogs: lake",
        "atlas: glossaries: finance; classifications: confidential; "
        "business metadata: stewardship; owner: served",
    ]
    bare = provider(FakeAtlas("atlas-2.4"))
    assert bare.check_lines()[1].endswith("business metadata: none; owner: not served")
    asyncio.run(bare.aclose())
    assert all("reader-pass" not in line for line in lines)
